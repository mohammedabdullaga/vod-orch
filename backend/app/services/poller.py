import asyncio
import logging
from sqlalchemy import or_, select

from app.config import settings
from app.models import Account, AppSettings, MonitoredCategory, QueueItem, SessionLocal, utc_now
from app.provider import provider_action, resolve_provider_proxy, stream_url
from app.security import encrypt_secret
from app.services.content_paths import (
    clean_content_name,
    normalized_episode_path,
    safe_filename,
    stream_identity,
)

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
            setting = db.get(AppSettings, 1)
            provider_proxy_id = setting.provider_proxy_id if setting else None
        if not accounts:
            return
        proxy_url = resolve_provider_proxy(provider_proxy_id)
        for account in accounts:
            try:
                await self._poll_account(account, monitored, proxy_url)
            except Exception as exc:
                logger.error(
                    "Provider poll failed for account %s (%s)",
                    account.id,
                    type(exc).__name__,
                )

    async def _poll_account(
        self,
        account: Account,
        monitored: list[MonitoredCategory],
        proxy_url: str | None,
    ) -> None:
        source_categories = {
            "series": await provider_action(
                account,
                "get_series_categories",
                proxy_url=proxy_url,
            ),
            "vod": await provider_action(
                account,
                "get_vod_categories",
                proxy_url=proxy_url,
            ),
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
                series_list = await provider_action(
                    account,
                    "get_series",
                    proxy_url=proxy_url,
                    category_id=category.category_id,
                )
                for series in series_list:
                    await self._enqueue_series(account, category, series, proxy_url)
            else:
                streams = await provider_action(
                    account,
                    "get_vod_streams",
                    proxy_url=proxy_url,
                    category_id=category.category_id,
                )
                for stream in streams:
                    await self._enqueue_vod(account, category, stream)

    async def _enqueue_series(
        self,
        account: Account,
        category: MonitoredCategory,
        series: dict,
        proxy_url: str | None,
    ) -> None:
        series_id = series.get("series_id", series.get("id"))
        if series_id is None:
            return
        info = await provider_action(
            account,
            "get_series_info",
            proxy_url=proxy_url,
            series_id=series_id,
        )
        episodes = info.get("episodes", {}) if isinstance(info, dict) else {}
        for season_key, season_rows in episodes.items():
            season_key_number = _int_or_none(season_key)
            for episode in season_rows:
                stream_id = episode.get("id", episode.get("stream_id"))
                if stream_id is None:
                    continue
                extension = episode.get("container_extension", "mkv")
                series_title = clean_content_name(
                    str(series.get("name") or "Series"), category.strip_prefixes
                )
                season = _int_or_none(episode.get("season")) or season_key_number
                episode_number = _int_or_none(episode.get("episode_num"))
                episode_title = safe_filename(
                    f"{series_title} - S{season or 0:02d}E{episode_number or 0:02d}",
                    max_length=140,
                )
                title = episode.get("title") or episode_title
                file_name, season, episode_number = normalized_episode_path(
                    kind="series",
                    title=title,
                    category_name=category.name if not category.destination_path else None,
                    provider_stream_id=str(stream_id),
                    provider_url=account.provider_url,
                    extension=extension,
                    series_name=series_title,
                    series_id=str(series_id),
                    season=season,
                    episode_num=episode_number,
                    strip_prefixes=category.strip_prefixes,
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
        file_name, _, _ = normalized_episode_path(
            kind="vod",
            title=title,
            category_name=category.name if not category.destination_path else None,
            provider_stream_id=str(stream_id),
            provider_url=account.provider_url,
            extension=extension,
            filename=title,
            strip_prefixes=category.strip_prefixes,
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
        identity = stream_identity(account.provider_url, kind, provider_stream_id)
        with SessionLocal() as db:
            existing = db.scalar(select(QueueItem).where(or_(
                QueueItem.stream_id == identity,
                (QueueItem.provider_stream_id.is_(None))
                & (QueueItem.stream_id == provider_stream_id),
            )))
            if existing:
                if existing.status == "PENDING" and existing.file_name != file_name:
                    existing.file_name = file_name
                    existing.updated_at = utc_now()
                    db.commit()
                return
            db.add(QueueItem(
                stream_id=identity, provider_stream_id=provider_stream_id,
                title=title, kind=kind, stream_url_encrypted=encrypt_secret(url),
                file_name=file_name, series_title=series_title, season=season,
                episode=episode, category_id=category_id, category_name=category_name,
                source_provider_url=account.provider_url,
                series_id=series_id, status="PENDING", created_at=utc_now(),
            ))
            try:
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Could not enqueue provider stream %s", provider_stream_id)


def _int_or_none(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
