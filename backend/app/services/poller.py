import asyncio
import logging
from sqlalchemy import select

from app.config import settings
from app.models import Account, MonitoredCategory, QueueItem, SessionLocal, utc_now
from app.provider import provider_action, stream_url
from app.security import encrypt_secret

logger = logging.getLogger(__name__)


class CategoryPoller:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="category-poller")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.poll_once()
            except Exception:
                logger.exception("Provider polling cycle failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=settings.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

    async def poll_once(self) -> None:
        with SessionLocal() as db:
            accounts = db.scalars(select(Account).where(Account.enabled.is_(True))).all()
            monitored = db.scalars(
                select(MonitoredCategory).where(MonitoredCategory.monitored.is_(True))
            ).all()
        if not accounts:
            return
        for account in accounts:
            try:
                await self._poll_account(account, monitored)
            except Exception as exc:
                logger.error(
                    "Provider poll failed for account %s (%s)",
                    account.id,
                    type(exc).__name__,
                )

    async def _poll_account(self, account: Account, monitored: list[MonitoredCategory]) -> None:
        source_categories = {
            "series": await provider_action(account, "get_series_categories"),
            "vod": await provider_action(account, "get_vod_categories"),
        }
        available = {
            (kind, str(item.get("category_id", item.get("id", ""))))
            for kind, rows in source_categories.items()
            for item in rows
        }
        active_categories = [
            item for item in monitored if (item.kind, item.category_id) in available
        ]
        for category in active_categories:
            if category.kind == "series":
                series_list = await provider_action(account, "get_series", category_id=category.category_id)
                for series in series_list:
                    await self._enqueue_series(account, series)
            else:
                streams = await provider_action(account, "get_vod_streams", category_id=category.category_id)
                for stream in streams:
                    await self._enqueue_vod(account, stream)

    async def _enqueue_series(self, account: Account, series: dict) -> None:
        series_id = series.get("series_id", series.get("id"))
        if series_id is None:
            return
        info = await provider_action(account, "get_series_info", series_id=series_id)
        episodes = info.get("episodes", {}) if isinstance(info, dict) else {}
        for season_key, season_rows in episodes.items():
            season_key_number = _int_or_none(season_key)
            for episode in season_rows:
                stream_id = episode.get("id", episode.get("stream_id"))
                if stream_id is None:
                    continue
                extension = episode.get("container_extension", "mkv")
                series_title = series.get("name", "Series")
                season = _int_or_none(episode.get("season")) or season_key_number
                episode_number = _int_or_none(episode.get("episode_num"))
                title = episode.get("title") or f"{series_title} S{season or 0:02d}E{episode_number or 0:02d}"
                file_name = _safe_filename(f"{series_title} - S{season or 0:02d}E{episode_number or 0:02d}.{extension}")
                self._insert_item(
                    str(stream_id), title, "series", stream_url(account, "series", str(stream_id), extension),
                    file_name, str(series_title), season, episode_number,
                )

    async def _enqueue_vod(self, account: Account, stream: dict) -> None:
        stream_id = stream.get("stream_id", stream.get("id"))
        if stream_id is None:
            return
        extension = stream.get("container_extension", "mkv")
        title = stream.get("name") or f"Movie {stream_id}"
        file_name = _safe_filename(f"{title}.{extension}")
        self._insert_item(
            str(stream_id), title, "vod", stream_url(account, "vod", str(stream_id), extension),
            file_name, None, None, None,
        )

    @staticmethod
    def _insert_item(
        stream_id: str, title: str, kind: str, url: str, file_name: str,
        series_title: str | None, season: int | None, episode: int | None,
    ) -> None:
        with SessionLocal() as db:
            if db.scalar(select(QueueItem.id).where(QueueItem.stream_id == stream_id)):
                return
            db.add(QueueItem(
                stream_id=stream_id, title=title, kind=kind, stream_url_encrypted=encrypt_secret(url),
                file_name=file_name, series_title=series_title, season=season,
                episode=episode, status="PENDING", created_at=utc_now(),
            ))
            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Could not enqueue provider stream %s", stream_id)


def _safe_filename(value: str) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in " ._-()[]")
    return cleaned.strip(" .")[:240] or "media.mkv"


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
