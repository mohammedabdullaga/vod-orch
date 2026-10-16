import asyncio
import logging
import os
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import httpx
from sqlalchemy import select, update

from app.config import settings
from app.models import (
    Account,
    M3UPlaylist,
    ProviderCatalogSyncJob,
    SessionLocal,
    utc_now,
)
from app.security import decrypt_secret, encrypt_secret
from app.services.m3u import fetch_playlist_file, replace_playlist_entries_from_file
from app.services.provider_client import resolve_provider_proxy

logger = logging.getLogger(__name__)


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
                .values(status="PENDING", phase="queued", error=None, updated_at=utc_now())
            )
            db.commit()
        self._task = asyncio.create_task(self._run(), name="provider-catalog-sync-worker")

    async def stop(self) -> None:
        self._stopping.set()
        self._wake_event.set()
        if self._task:
            await self._task

    def wake(self) -> None:
        self._wake_event.set()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            job_id = self._claim_next()
            if job_id is not None:
                try:
                    await self._process(job_id)
                except Exception as exc:
                    logger.exception("Provider catalog sync job %s worker crashed", job_id)
                    with SessionLocal() as db:
                        job = db.get(ProviderCatalogSyncJob, job_id)
                        if job and job.status == "RUNNING":
                            job.status = "FAILED"
                            job.phase = "failed"
                            job.error = (
                                f"Catalog sync failed ({type(exc).__name__}). "
                                "Check backend logs."
                            )
                            job.updated_at = utc_now()
                            db.commit()
                continue
            self._wake_event.clear()
            if self._stopping.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=max(1, settings.worker_poll_seconds),
                )
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
            claimed = db.execute(
                update(ProviderCatalogSyncJob)
                .where(
                    ProviderCatalogSyncJob.id == job_id,
                    ProviderCatalogSyncJob.status == "PENDING",
                )
                .values(
                    status="RUNNING",
                    phase="fetching_playlist",
                    error=None,
                    updated_at=utc_now(),
                )
            )
            if claimed.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return job_id

    @staticmethod
    def _playlist_url(account: Account) -> str:
        endpoint = urlsplit(urljoin(account.provider_url.rstrip("/") + "/", "get.php"))
        query = urlencode({
            "username": account.username,
            "password": decrypt_secret(account.password_encrypted),
            "type": "m3u_plus",
            "output": "ts",
        })
        return urlunsplit((endpoint.scheme, endpoint.netloc, endpoint.path, query, ""))

    async def _process(self, job_id: int) -> None:
        temp_path: str | None = None
        created_playlist_id: int | None = None
        try:
            with SessionLocal() as db:
                job = db.get(ProviderCatalogSyncJob, job_id)
                if not job or job.status != "RUNNING":
                    return
                account = db.get(Account, job.account_id)
                if not account or not account.enabled:
                    raise ValueError("The provider account was removed or disabled.")
                source_url = self._playlist_url(account)
                account_id = account.id
                account_username = account.username
                user_agent = account.user_agent or "Mozilla"
                proxy_id = job.proxy_id
                playlist = db.scalar(
                    select(M3UPlaylist).where(
                        M3UPlaylist.provider_account_id == account_id
                    )
                )
                if playlist is None:
                    playlist = M3UPlaylist(
                        provider_account_id=account_id,
                        name=f"Provider catalog · {account_username}",
                        source_url_encrypted=encrypt_secret(source_url),
                        proxy_id=proxy_id,
                        enabled=True,
                    )
                    db.add(playlist)
                    db.flush()
                    created_playlist_id = playlist.id
                job.playlist_id = playlist.id
                job.entries_processed = 0
                job.categories_processed = 0
                job.lists_processed = 0
                job.cached_items = 0
                job.series_details_processed = 0
                job.total_series_details = 0
                job.stale_responses = 0
                job.phase = "fetching_playlist"
                job.updated_at = utc_now()
                db.commit()
                playlist_id = playlist.id

            proxy_url = resolve_provider_proxy(proxy_id)
            temp_path = await self._fetch_with_retries(
                source_url,
                proxy_url,
                user_agent,
                job_id,
            )
            self._set_phase(job_id, "indexing_playlist")
            entry_count, category_count = await asyncio.to_thread(
                replace_playlist_entries_from_file,
                playlist_id,
                temp_path,
                encrypt_secret(source_url),
            )
            with SessionLocal() as db:
                playlist = db.get(M3UPlaylist, playlist_id)
                if playlist is None:
                    raise LookupError("The synced playlist was removed during indexing.")
                playlist.provider_account_id = account_id
                playlist.name = f"Provider catalog · {account_username}"
                playlist.proxy_id = proxy_id
                job = db.get(ProviderCatalogSyncJob, job_id)
                if job is None:
                    raise LookupError("Catalog sync job was removed during indexing.")
                job.status = "COMPLETED"
                job.phase = "completed"
                job.entries_processed = entry_count
                job.categories_processed = category_count
                job.cached_items = entry_count
                job.error = None
                job.updated_at = utc_now()
                db.commit()
        except Exception as exc:
            error = self._safe_error(exc)
            logger.warning(
                "Provider catalog sync job %s failed (%s)",
                job_id,
                type(exc).__name__,
            )
            if created_playlist_id is not None:
                with SessionLocal() as db:
                    playlist = db.get(M3UPlaylist, created_playlist_id)
                    if playlist:
                        db.delete(playlist)
                        db.commit()
            with SessionLocal() as db:
                job = db.get(ProviderCatalogSyncJob, job_id)
                if job:
                    job.status = "FAILED"
                    job.phase = "failed"
                    job.error = error
                    job.updated_at = utc_now()
                    db.commit()
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.exception(
                        "Could not remove temporary playlist file for catalog sync job %s",
                        job_id,
                    )

    @staticmethod
    def _set_phase(job_id: int, phase: str) -> None:
        with SessionLocal() as db:
            job = db.get(ProviderCatalogSyncJob, job_id)
            if job:
                job.phase = phase
                job.updated_at = utc_now()
                db.commit()

    @staticmethod
    async def _fetch_with_retries(
        source_url: str,
        proxy_url: str | None,
        user_agent: str,
        job_id: int,
    ) -> str:
        for attempt in range(3):
            try:
                return await fetch_playlist_file(source_url, proxy_url, user_agent)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if 400 <= status_code < 500 and status_code != 429:
                    raise
                if attempt == 2:
                    raise
                logger.warning(
                    "M3U source or proxy returned HTTP %s for catalog sync job %s; retrying",
                    status_code,
                    job_id,
                )
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise
                logger.warning(
                    "M3U request failed for catalog sync job %s (%s); retrying",
                    job_id,
                    type(exc).__name__,
                )
            await asyncio.sleep(2 ** (attempt + 1))
        raise RuntimeError("Catalog playlist retry loop exited unexpectedly.")

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, UnicodeDecodeError):
            return "The M3U playlist is not valid UTF-8 text."
        if isinstance(exc, ValueError):
            return str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code < 100 or status_code > 599:
                return (
                    f"M3U source or proxy returned non-standard HTTP status {status_code}. "
                    "Check provider and proxy availability."
                )
            return (
                f"M3U source returned HTTP {status_code}. "
                "Check provider account and assigned proxy."
            )
        if isinstance(exc, httpx.TimeoutException):
            return "M3U source request timed out. Check provider and proxy availability."
        if isinstance(exc, httpx.RequestError):
            return (
                f"M3U network request failed ({type(exc).__name__}). "
                "Check provider and proxy availability."
            )
        logger.exception("Unexpected provider catalog sync error")
        return f"Catalog sync failed ({type(exc).__name__}). Check backend logs."
