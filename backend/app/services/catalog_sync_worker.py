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
from app.services.provider_client import (
    begin_catalog_cache_clear,
    cached_provider_action,
    end_catalog_cache_clear,
    invalidate_provider_action_cache,
    resolve_provider_proxy,
)

logger = logging.getLogger(__name__)

CATALOG_ACTIONS = (
    ("series", "get_series_categories", "get_series"),
    ("vod", "get_vod_categories", "get_vod_streams"),
)
CATALOG_REQUEST_CONCURRENCY = 4


class ProviderCatalogSyncWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_job_id: int | None = None
        self._active_job_task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._clearing_data = False

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
        self._wake_event.set()

    async def stop_job(self, job_id: int) -> str:
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
            try:
                await asyncio.wait_for(task, timeout=15)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    "Catalog sync did not stop within 15 seconds; clear was not performed."
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
            account_id = account.id

        proxy_url = resolve_provider_proxy(proxy_id)
        category_records: list[tuple[str, str, str]] = []
        category_results = await asyncio.gather(
            *(
                self._fetch_action(account, category_action, proxy_url)
                for _, category_action, _ in CATALOG_ACTIONS
            ),
            return_exceptions=True,
        )
        category_error = next(
            (result for result in category_results if isinstance(result, BaseException)),
            None,
        )
        if category_error:
            raise category_error
        for (kind, category_action, list_action), (categories, cache_state) in zip(
            CATALOG_ACTIONS,
            category_results,
        ):
            if not isinstance(categories, list) or any(
                not isinstance(row, dict) for row in categories
            ):
                invalidate_provider_action_cache(account, category_action)
                raise ValueError(f"Provider returned invalid {category_action} data.")
            self._progress(
                job_id,
                phase=f"{kind}_categories",
                categories_delta=len(categories),
                stale_delta=int(cache_state == "stale"),
                cache_hits_delta=int(cache_state == "hit"),
            )
            for category in categories:
                category_id = category.get("category_id", category.get("id"))
                if category_id is not None:
                    category_records.append((kind, str(category_id), list_action))

        semaphore = asyncio.Semaphore(CATALOG_REQUEST_CONCURRENCY)

        async def fetch_category_list(
            kind: str,
            category_id: str,
            list_action: str,
        ) -> None:
            async with semaphore:
                rows, cache_state = await self._fetch_action(
                    account,
                    list_action,
                    proxy_url,
                    category_id=category_id,
                )
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                invalidate_provider_action_cache(
                    account, list_action, category_id=category_id
                )
                raise ValueError(
                    f"Provider returned invalid {list_action} data for category {category_id}."
                )
            self._progress(
                job_id,
                phase=f"{kind}_lists",
                lists_delta=1,
                cached_items_delta=len(rows),
                stale_delta=int(cache_state == "stale"),
                cache_hits_delta=int(cache_state == "hit"),
            )

        list_results = await asyncio.gather(
            *(
                fetch_category_list(kind, category_id, list_action)
                for kind, category_id, list_action in category_records
            ),
            return_exceptions=True,
        )
        list_error = next(
            (result for result in list_results if isinstance(result, BaseException)),
            None,
        )
        if list_error:
            raise list_error

        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if job and job.status == "RUNNING":
                job.status = "COMPLETED"
                job.phase = "completed"
                job.error = None
                job.updated_at = utc_now()
                db.commit()
        logger.info(
            "Provider catalog sync job %s completed catalog lists for account %s "
            "(%s categories, up to %s concurrent requests); series details remain lazy-loaded",
            job_id,
            account_id,
            len(category_records),
            CATALOG_REQUEST_CONCURRENCY,
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
            force_refresh=False,
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
            job.stale_responses += stale_delta
            job.cache_hits += cache_hits_delta
            job.invalid_series_details += invalid_series_details_delta
            if total_series is not None:
                job.total_series_details = total_series
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
