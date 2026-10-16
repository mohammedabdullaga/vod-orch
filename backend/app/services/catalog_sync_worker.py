import asyncio
import json
import logging
from typing import Any

import httpx
from sqlalchemy import select, update

from app.models import (
    Account,
    ProviderCatalogSyncJob,
    SessionLocal,
    utc_now,
)
from app.services.provider_client import (
    cached_provider_action,
    parse_series_episodes,
    resolve_provider_proxy,
)

logger = logging.getLogger(__name__)

CATALOG_ACTIONS = (
    ("series", "get_series_categories", "get_series"),
    ("vod", "get_vod_categories", "get_vod_streams"),
)


class ProviderCatalogSyncWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake_event = asyncio.Event()

    async def start(self) -> None:
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
                    error=None,
                    updated_at=utc_now(),
                )
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

    async def _run(self) -> None:
        while not self._stopping.is_set():
            job_id = self._claim_next()
            if job_id is not None:
                try:
                    await self._process(job_id)
                except Exception as exc:
                    logger.exception("Provider catalog sync job %s crashed", job_id)
                    self._finish_failed(job_id, self._safe_error(exc))
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
        for kind, category_action, list_action in CATALOG_ACTIONS:
            categories, stale = await self._fetch_action(
                account,
                category_action,
                proxy_url,
            )
            if not isinstance(categories, list) or any(
                not isinstance(row, dict) for row in categories
            ):
                raise ValueError(f"Provider returned invalid {category_action} data.")
            self._progress(
                job_id,
                phase=f"{kind}_categories",
                categories_delta=len(categories),
                stale_delta=int(stale),
            )
            for category in categories:
                category_id = category.get("category_id", category.get("id"))
                if category_id is not None:
                    category_records.append((kind, str(category_id), list_action))

        series_ids: set[str] = set()
        for kind, category_id, list_action in category_records:
            rows, stale = await self._fetch_action(
                account,
                list_action,
                proxy_url,
                category_id=category_id,
            )
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError(
                    f"Provider returned invalid {list_action} data for category {category_id}."
                )
            new_series_ids: set[str] = set()
            for row in rows:
                stream_id = row.get(
                    "series_id" if kind == "series" else "stream_id",
                    row.get("id"),
                )
                if stream_id is None:
                    continue
                if kind == "series":
                    new_series_ids.add(str(stream_id))
            series_ids.update(new_series_ids)
            self._progress(
                job_id,
                phase=f"{kind}_lists",
                lists_delta=1,
                cached_items_delta=len(rows),
                stale_delta=int(stale),
            )

        self._progress(job_id, phase="series_details", total_series=len(series_ids))
        for series_id in sorted(series_ids):
            details, stale = await self._fetch_action(
                account,
                "get_series_info",
                proxy_url,
                series_id=series_id,
            )
            parse_series_episodes(details)
            self._progress(
                job_id,
                phase="series_details",
                series_details_delta=1,
                stale_delta=int(stale),
            )

        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if job:
                job.status = "COMPLETED"
                job.phase = "completed"
                job.error = None
                job.updated_at = utc_now()
                db.commit()
        logger.info("Provider catalog sync job %s completed for account %s", job_id, account_id)

    @staticmethod
    async def _fetch_action(
        account: Account,
        action: str,
        proxy_url: str | None,
        **params: str,
    ) -> tuple[Any, bool]:
        payload, cache_state = await cached_provider_action(
            account,
            action,
            proxy_url=proxy_url,
            force_refresh=True,
            **params,
        )
        return payload, cache_state == "stale"

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
