import asyncio
import hashlib
import logging
from sqlalchemy import or_, select

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
                    await self._enqueue_series(account, category, series)
            else:
                streams = await provider_action(account, "get_vod_streams", category_id=category.category_id)
                for stream in streams:
                    await self._enqueue_vod(account, category, stream)

    async def _enqueue_series(
        self, account: Account, category: MonitoredCategory, series: dict
    ) -> None:
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
                series_title = str(series.get("name") or "Series")
                season = _int_or_none(episode.get("season")) or season_key_number
                episode_number = _int_or_none(episode.get("episode_num"))
                episode_title = _safe_filename(
                    f"{series_title} - S{season or 0:02d}E{episode_number or 0:02d}",
                    max_length=140,
                )
                title = episode.get("title") or episode_title
                file_name = (
                    f"Series/{_identity_segment(series_title, str(series_id), account.provider_url)}"
                    f"/Season {season or 0:02d}/"
                    f"{episode_title} [{_safe_provider_id(str(stream_id), account.provider_url)}]."
                    f"{_safe_extension(extension)}"
                )
                self._insert_item(
                    account, str(stream_id), title, "series",
                    stream_url(account, "series", str(stream_id), extension),
                    file_name, str(series_title), season, episode_number,
                    category.category_id, category.name, str(series_id),
                )

    async def _enqueue_vod(
        self, account: Account, category: MonitoredCategory, stream: dict
    ) -> None:
        stream_id = stream.get("stream_id", stream.get("id"))
        if stream_id is None:
            return
        extension = stream.get("container_extension", "mkv")
        title = str(stream.get("name") or f"Movie {stream_id}")
        file_name = (
            f"Movies/{_identity_segment(title, str(stream_id), account.provider_url)}/"
            f"{_safe_filename(title, max_length=160)}.{_safe_extension(extension)}"
        )
        self._insert_item(
            account, str(stream_id), title, "vod",
            stream_url(account, "vod", str(stream_id), extension),
            file_name, None, None, None, category.category_id, category.name, None,
        )

    @staticmethod
    def _insert_item(
        account: Account, provider_stream_id: str, title: str, kind: str, url: str, file_name: str,
        series_title: str | None, season: int | None, episode: int | None,
        category_id: str, category_name: str, series_id: str | None,
    ) -> None:
        identity = hashlib.sha256(
            f"{account.provider_url.rstrip('/')}\0{kind}\0{provider_stream_id}".encode("utf-8")
        ).hexdigest()
        with SessionLocal() as db:
            if db.scalar(select(QueueItem.id).where(or_(
                QueueItem.stream_id == identity,
                (QueueItem.provider_stream_id.is_(None))
                & (QueueItem.stream_id == provider_stream_id),
            ))):
                return
            db.add(QueueItem(
                stream_id=identity, provider_stream_id=provider_stream_id,
                title=title, kind=kind, stream_url_encrypted=encrypt_secret(url),
                file_name=file_name, series_title=series_title, season=season,
                episode=episode, category_id=category_id, category_name=category_name,
                series_id=series_id, status="PENDING", created_at=utc_now(),
            ))
            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Could not enqueue provider stream %s", provider_stream_id)


def _safe_filename(value: str, max_length: int = 240) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in " ._-()[]")
    return cleaned.strip(" .")[:max_length].rstrip(" .") or "media.mkv"


def _identity_segment(title: str, provider_id: str, provider_url: str) -> str:
    return (
        f"{_safe_filename(title, max_length=130)} "
        f"[{_safe_provider_id(provider_id, provider_url)}]"
    )


def _safe_provider_id(provider_id: str, provider_url: str) -> str:
    safe_id = "".join(char for char in provider_id if char.isalnum() or char in "_-")[:48]
    digest = hashlib.sha256(
        f"{provider_url.rstrip('/')}\0{provider_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{safe_id}-{digest}" if safe_id else digest


def _safe_extension(value: str) -> str:
    extension = "".join(char for char in value.lstrip(".") if char.isalnum())[:12]
    return extension or "mkv"


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
