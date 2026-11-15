import asyncio
import json
import logging
from typing import Any

import httpx
from sqlalchemy import delete, select, update

from app.models import (
    Account,
    ProviderCatalogCache,
    ProviderProfileCatalogCache,
    ProviderCatalogSyncJob,
    SessionLocal,
    utc_now,
)
from app.services.catalog_index import store_categories, store_category_items
from app.services.provider_client import (
    begin_catalog_cache_clear,
    cached_provider_action,
    end_catalog_cache_clear,
    invalidate_provider_action_cache,
    parse_series_episodes,
    resolve_provider_proxy,
)

logger = logging.getLogger(__name__)

CATALOG_ACTIONS = (
    ("series", "get_series_categories", "get_series"),
    ("vod", "get_vod_categories", "get_vod_streams"),
)
PROGRESS_RANGES = {
    "categories": (0, 15),
    "movie_ids": (15, 35),
    "series_ids": (35, 55),
    "series_details": (55, 100),
}


class ProviderCatalogSyncWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_job_id: int | None = None
        self._active_job_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._clearing_data = False
        self._last_worker_error: str | None = None

    @property
    def clearing_data(self) -> bool:
        return self._clearing_data

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        with SessionLocal() as db:
            db.execute(
                update(ProviderCatalogSyncJob)
                .where(ProviderCatalogSyncJob.status == "RUNNING")
                .values(
                    status="PENDING",
                    phase="queued",
                    categories_processed=0,
                    lists_processed=0,
                    cached_items=0,
                    series_details_processed=0,
                    total_series_details=0,
                    progress_current=0,
                    progress_total=0,
                    progress_percent=0,
                    seasons_cached=0,
                    episodes_cached=0,
                    episode_ids_cached=0,
                    stale_responses=0,
                    cache_hits=0,
                    invalid_series_details=0,
                    error=None,
                    updated_at=utc_now(),
                )
            )
            db.execute(
                update(ProviderCatalogSyncJob)
                .where(ProviderCatalogSyncJob.status == "CANCELLING")
                .values(status="CANCELLED", phase="cancelled", updated_at=utc_now())
            )
            db.commit()
        self._task = asyncio.create_task(self._run(), name="provider-catalog-sync-worker")

    async def stop(self) -> None:
        self._stopping.set()
        self._wake_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def wake(self) -> None:
        self.ensure_running()
        self._wake_event.set()

    def ensure_running(self) -> bool:
        if self._stopping.is_set():
            return False
        if self._task is None or self._task.done():
            if self._task and not self._task.cancelled():
                error = self._task.exception()
                if error:
                    logger.error("Catalog sync worker stopped unexpectedly: %s", error)
            self._task = asyncio.create_task(
                self._run(), name="provider-catalog-sync-worker"
            )
            logger.warning("Started or restarted the provider catalog sync worker.")
        return True

    def health(self) -> dict[str, Any]:
        return {
            "worker_running": self._task is not None and not self._task.done(),
            "active_job_id": self._active_job_id,
            "last_worker_error": self._last_worker_error,
        }

    async def stop_job(self, job_id: int, *, force: bool = False) -> str:
        task = None
        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if not job:
                raise ValueError("Catalog sync job not found.")
            if job.status == "PENDING":
                job.status = "CANCELLED"
                job.phase = "cancelled"
                job.updated_at = utc_now()
                db.commit()
                self._signal_wakeup()
                return job.status
            if job.status in ("RUNNING", "CANCELLING"):
                job.status = "CANCELLING"
                job.phase = "cancelling"
                job.updated_at = utc_now()
                db.commit()
                task = (
                    self._active_job_task
                    if self._active_job_id == job_id
                    else None
                )
                if task and not task.done():
                    task.cancel()
                else:
                    job.status = "CANCELLED"
                    job.phase = "cancelled"
                    job.updated_at = utc_now()
                    db.commit()
                    return job.status
            else:
                return job.status

        if task and not task.done():
            if force:
                done, _ = await asyncio.wait({task}, timeout=3)
                if not done:
                    task.cancel()
                    done, _ = await asyncio.wait({task}, timeout=2)
                    if not done:
                        raise RuntimeError(
                            "Catalog sync task did not stop after repeated cancellation. "
                            "The API service was left running; inspect backend logs."
                        )
            else:
                try:
                    await asyncio.wait_for(task, timeout=15)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(
                        "Catalog sync did not stop within 15 seconds; "
                        "the API service was left running."
                    ) from exc
        self._finish_cancelled(job_id)
        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if not job:
                raise ValueError("Catalog sync job not found.")
            return job.status

    def _signal_wakeup(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._wake_event.set)

    async def stop_profile(self, provider_url: str) -> None:
        with SessionLocal() as db:
            job_ids = db.scalars(
                select(ProviderCatalogSyncJob.id)
                .join(Account, Account.id == ProviderCatalogSyncJob.account_id)
                .where(
                    Account.provider_url == provider_url,
                    ProviderCatalogSyncJob.status.in_(("PENDING", "RUNNING", "CANCELLING")),
                )
            ).all()
        for job_id in job_ids:
            await self.stop_job(job_id)

    async def clear_all_data(self) -> dict[str, int]:
        if self._clearing_data:
            raise RuntimeError("Catalog sync data is already being cleared.")
        self._clearing_data = True
        begin_catalog_cache_clear()
        try:
            with SessionLocal() as db:
                job_ids = db.scalars(
                    select(ProviderCatalogSyncJob.id).where(
                        ProviderCatalogSyncJob.status.in_(
                            ("PENDING", "RUNNING", "CANCELLING")
                        )
                    )
                ).all()
            for job_id in job_ids:
                await self.stop_job(job_id)

            with SessionLocal() as db:
                deleted_jobs = db.execute(delete(ProviderCatalogSyncJob))
                deleted_profile_cache = db.execute(delete(ProviderProfileCatalogCache))
                deleted_legacy_cache = db.execute(delete(ProviderCatalogCache))
                counts = {
                    "sync_jobs": deleted_jobs.rowcount or 0,
                    "catalog_cache_entries": deleted_profile_cache.rowcount or 0,
                    "legacy_cache_entries": deleted_legacy_cache.rowcount or 0,
                }
                db.commit()
            return counts
        finally:
            end_catalog_cache_clear()
            self._clearing_data = False
            self.wake()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                if self._clearing_data:
                    self._wake_event.clear()
                    if self._clearing_data:
                        await self._wake_event.wait()
                    continue
                job_id = self._claim_next()
                if job_id is not None:
                    process_task = asyncio.create_task(
                        self._process(job_id),
                        name=f"provider-catalog-sync-{job_id}",
                    )
                    self._active_job_id = job_id
                    self._active_job_task = process_task
                    try:
                        await process_task
                    except asyncio.CancelledError:
                        if asyncio.current_task() and asyncio.current_task().cancelling():
                            raise
                        self._finish_cancelled(job_id)
                    except Exception as exc:
                        logger.exception("Provider catalog sync job %s crashed", job_id)
                        self._finish_failed(job_id, self._safe_error(exc))
                    finally:
                        self._active_job_id = None
                        self._active_job_task = None
                    continue
                self._wake_event.clear()
                if self._stopping.is_set():
                    break
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_worker_error = type(exc).__name__
                logger.exception(
                    "Provider catalog sync worker loop failed; retrying shortly."
                )
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def _claim_next() -> int | None:
        with SessionLocal() as db:
            job_id = db.scalar(
                select(ProviderCatalogSyncJob.id)
                .where(ProviderCatalogSyncJob.status == "PENDING")
                .order_by(ProviderCatalogSyncJob.created_at, ProviderCatalogSyncJob.id)
                .limit(1)
            )
            if job_id is None:
                return None
            result = db.execute(
                update(ProviderCatalogSyncJob)
                .where(
                    ProviderCatalogSyncJob.id == job_id,
                    ProviderCatalogSyncJob.status == "PENDING",
                )
                .values(status="RUNNING", phase="connecting", updated_at=utc_now())
            )
            if result.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return job_id

    async def _process(self, job_id: int) -> None:
        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if not job or job.status != "RUNNING":
                return
            account = db.get(Account, job.account_id)
            if not account or not account.enabled:
                raise ValueError("The selected Xtream account is no longer enabled.")
            proxy_id = job.proxy_id
            provider_url = account.provider_url

        proxy_url = resolve_provider_proxy(proxy_id)
        category_records: dict[str, list[tuple[str, str, str]]] = {
            "series": [],
            "vod": [],
        }
        series_ids: list[str] = []
        seen_series_ids: set[str] = set()
        self._progress(
            job_id,
            phase="categories",
            progress_current=0,
            progress_total=len(CATALOG_ACTIONS),
        )
        for index, (kind, category_action, list_action) in enumerate(CATALOG_ACTIONS, 1):
            categories, cache_state = await self._fetch_action(
                account, category_action, proxy_url
            )
            if not isinstance(categories, list) or any(
                not isinstance(row, dict) for row in categories
            ):
                invalidate_provider_action_cache(account, category_action)
                raise ValueError(f"Provider returned invalid {category_action} data.")
            await asyncio.to_thread(store_categories, provider_url, kind, categories)
            for category in categories:
                category_id = category.get("category_id", category.get("id"))
                if category_id is not None:
                    category_records[kind].append(
                        (
                            str(category_id),
                            list_action,
                            str(category.get("category_name", category.get("name", "Unknown"))),
                        )
                    )
            self._progress(
                job_id,
                phase="categories",
                categories_delta=len(categories),
                progress_current=index,
                progress_total=len(CATALOG_ACTIONS),
                stale_delta=int(cache_state == "stale"),
                cache_hits_delta=int(cache_state == "hit"),
            )

        for kind, phase in (("vod", "movie_ids"), ("series", "series_ids")):
            records = category_records[kind]
            self._progress(
                job_id,
                phase=phase,
                progress_current=0,
                progress_total=len(records),
            )
            for index, (category_id, list_action, category_name) in enumerate(records, 1):
                rows, cache_state = await self._fetch_action(
                    account,
                    list_action,
                    proxy_url,
                    category_id=category_id,
                )
                if not isinstance(rows, list) or any(
                    not isinstance(row, dict) for row in rows
                ):
                    invalidate_provider_action_cache(
                        account, list_action, category_id=category_id
                    )
                    raise ValueError(
                        f"Provider returned invalid {list_action} data for category {category_id}."
                    )
                await asyncio.to_thread(
                    store_category_items,
                    provider_url,
                    kind,
                    category_id,
                    category_name,
                    rows,
                )
                if kind == "series":
                    for row in rows:
                        series_id = row.get("series_id", row.get("id"))
                        if series_id is not None and str(series_id).strip():
                            normalized_id = str(series_id)
                            if normalized_id not in seen_series_ids:
                                seen_series_ids.add(normalized_id)
                                series_ids.append(normalized_id)
                self._progress(
                    job_id,
                    phase=phase,
                    lists_delta=1,
                    cached_items_delta=len(rows),
                    progress_current=index,
                    progress_total=len(records),
                    stale_delta=int(cache_state == "stale"),
                    cache_hits_delta=int(cache_state == "hit"),
                )

        self._progress(
            job_id,
            phase="series_details",
            total_series=len(series_ids),
            progress_current=0,
            progress_total=len(series_ids),
        )
        for index, series_id in enumerate(series_ids, 1):
            invalid_details = False
            cache_state = "refreshed"
            seasons_count = 0
            episodes_count = 0
            episode_ids_count = 0
            try:
                details, cache_state = await self._fetch_action(
                    account,
                    "get_series_info",
                    proxy_url,
                    series_id=series_id,
                )
                parsed_episodes = parse_series_episodes(details)
                seasons = details.get("seasons")
                if isinstance(seasons, (dict, list)):
                    seasons_count = len(seasons)
                else:
                    episodes_by_season = details.get("episodes")
                    seasons_count = (
                        len(episodes_by_season)
                        if isinstance(episodes_by_season, dict)
                        else 0
                    )
                episodes_count = len(parsed_episodes)
                episode_ids_count = len(
                    {episode["stream_id"] for episode in parsed_episodes}
                )
            except asyncio.CancelledError:
                raise
            except ValueError:
                invalid_details = True
                invalidate_provider_action_cache(
                    account,
                    "get_series_info",
                    series_id=series_id,
                )
                logger.warning(
                    "Provider returned invalid episode details for series %s "
                    "in sync job %s.",
                    series_id,
                    job_id,
                )
            except Exception:
                invalid_details = True
                logger.warning(
                    "Could not cache episode details for series %s in sync job %s.",
                    series_id,
                    job_id,
                    exc_info=True,
                )
            self._progress(
                job_id,
                phase="series_details",
                series_details_delta=1,
                invalid_series_details_delta=int(invalid_details),
                progress_current=index,
                progress_total=len(series_ids),
                seasons_delta=seasons_count,
                episodes_delta=episodes_count,
                episode_ids_delta=episode_ids_count,
                stale_delta=int(cache_state == "stale"),
                cache_hits_delta=int(cache_state == "hit"),
            )

        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if job and job.status == "RUNNING":
                job.status = "COMPLETED"
                job.phase = "completed"
                job.progress_percent = 100
                job.progress_current = job.progress_total
                job.error = None
                job.updated_at = utc_now()
                db.commit()
        logger.info(
            "Provider catalog sync job %s completed catalog lists for profile %s "
            "(%s categories, %s series details)",
            job_id,
            provider_url,
            sum(len(records) for records in category_records.values()),
            len(series_ids),
        )

    @staticmethod
    async def _fetch_action(
        account: Account,
        action: str,
        proxy_url: str | None,
        **params: str,
    ) -> tuple[Any, str]:
        payload, cache_state = await cached_provider_action(
            account,
            action,
            proxy_url=proxy_url,
            force_refresh=True,
            **params,
        )
        return payload, cache_state

    @staticmethod
    def _progress(
        job_id: int,
        *,
        phase: str,
        categories_delta: int = 0,
        lists_delta: int = 0,
        cached_items_delta: int = 0,
        total_series: int | None = None,
        series_details_delta: int = 0,
        progress_current: int | None = None,
        progress_total: int | None = None,
        seasons_delta: int = 0,
        episodes_delta: int = 0,
        episode_ids_delta: int = 0,
        stale_delta: int = 0,
        cache_hits_delta: int = 0,
        invalid_series_details_delta: int = 0,
    ) -> None:
        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if not job or job.status != "RUNNING":
                return
            job.phase = phase
            job.categories_processed += categories_delta
            job.lists_processed += lists_delta
            job.cached_items += cached_items_delta
            job.series_details_processed += series_details_delta
            job.seasons_cached += seasons_delta
            job.episodes_cached += episodes_delta
            job.episode_ids_cached += episode_ids_delta
            job.stale_responses += stale_delta
            job.cache_hits += cache_hits_delta
            job.invalid_series_details += invalid_series_details_delta
            if total_series is not None:
                job.total_series_details = total_series
            if progress_current is not None:
                job.progress_current = progress_current
            if progress_total is not None:
                job.progress_total = progress_total
            if phase in PROGRESS_RANGES:
                start, end = PROGRESS_RANGES[phase]
                fraction = (
                    min(job.progress_current / job.progress_total, 1)
                    if job.progress_total
                    else 1
                )
                job.progress_percent = round(start + (end - start) * fraction)
            job.updated_at = utc_now()
            db.commit()

    @staticmethod
    def _finish_failed(job_id: int, message: str) -> None:
        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if job:
                job.status = "FAILED"
                job.phase = "failed"
                job.error = message
                job.updated_at = utc_now()
                db.commit()

    @staticmethod
    def _finish_cancelled(job_id: int) -> None:
        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if job and job.status in ("RUNNING", "CANCELLING"):
                job.status = "CANCELLED"
                job.phase = "cancelled"
                job.error = None
                job.updated_at = utc_now()
                db.commit()

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, ValueError):
            return str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            return f"Provider returned HTTP {exc.response.status_code}. Check provider and proxy."
        if isinstance(exc, httpx.TimeoutException):
            return "Provider request timed out. Check provider and proxy."
        if isinstance(exc, httpx.RequestError):
            return f"Provider network request failed ({type(exc).__name__}). Check provider and proxy."
        if isinstance(exc, json.JSONDecodeError):
            return "Provider returned invalid JSON. Check the account and provider response."
        logger.exception("Unexpected provider catalog sync error")
        return f"Provider catalog sync failed ({type(exc).__name__}). Check backend logs."
