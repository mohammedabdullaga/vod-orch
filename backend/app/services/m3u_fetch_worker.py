import asyncio
import logging

import httpx
from sqlalchemy import select, update

from app.models import M3UFetchJob, M3UPlaylist, SessionLocal, utc_now
from app.security import decrypt_secret, encrypt_secret
from app.services.m3u import fetch_playlist, replace_playlist_entries
from app.services.provider_client import resolve_provider_proxy

logger = logging.getLogger(__name__)


class M3UFetchWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._wake_event = asyncio.Event()

    async def start(self) -> None:
        with SessionLocal() as db:
            db.execute(
                update(M3UFetchJob)
                .where(M3UFetchJob.status == "RUNNING")
                .values(status="PENDING", error=None, updated_at=utc_now())
            )
            db.commit()
        self._task = asyncio.create_task(self._run(), name="m3u-fetch-worker")

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
                    logger.exception("M3U fetch job %s worker crashed", job_id)
                    with SessionLocal() as db:
                        job = db.get(M3UFetchJob, job_id)
                        if job and job.status == "RUNNING":
                            job.status = "FAILED"
                            job.error = (
                                f"M3U worker failed ({type(exc).__name__}). "
                                "Check the backend logs."
                            )
                            job.updated_at = utc_now()
                            db.commit()
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
                select(M3UFetchJob.id)
                .where(M3UFetchJob.status == "PENDING")
                .order_by(M3UFetchJob.created_at, M3UFetchJob.id)
                .limit(1)
            )
            if job_id is None:
                return None
            claimed = db.execute(
                update(M3UFetchJob)
                .where(
                    M3UFetchJob.id == job_id,
                    M3UFetchJob.status == "PENDING",
                )
                .values(status="RUNNING", updated_at=utc_now())
            )
            if claimed.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return job_id

    async def _process(self, job_id: int) -> None:
        new_playlist_id: int | None = None
        operation: str | None = None
        try:
            with SessionLocal() as db:
                job = db.get(M3UFetchJob, job_id)
                if not job or job.status != "RUNNING":
                    return
                operation = job.operation
                name = job.name
                playlist_id = job.playlist_id
                source_url = decrypt_secret(job.source_url_encrypted)
                proxy_id = job.proxy_id
                if operation == "refresh" and (
                    playlist_id is None or db.get(M3UPlaylist, playlist_id) is None
                ):
                    raise ValueError("The playlist was removed before it could be refreshed.")

            proxy_url = resolve_provider_proxy(proxy_id)
            entries = await self._fetch_with_retries(source_url, proxy_url, job_id)
            if not entries:
                raise ValueError("The M3U playlist contains no usable HTTP streams.")

            if operation == "import":
                if playlist_id is None:
                    with SessionLocal() as db:
                        playlist = M3UPlaylist(
                            name=name,
                            source_url_encrypted=encrypt_secret(source_url),
                            proxy_id=proxy_id,
                            entry_count=0,
                            fetched_at=utc_now(),
                        )
                        db.add(playlist)
                        db.flush()
                        new_playlist_id = playlist.id
                        playlist_id = playlist.id
                        job = db.get(M3UFetchJob, job_id)
                        if not job:
                            raise RuntimeError("M3U fetch job was removed before playlist creation.")
                        job.playlist_id = playlist.id
                        job.updated_at = utc_now()
                        db.commit()
            if playlist_id is None:
                raise RuntimeError("M3U fetch job has no target playlist.")

            entry_count = await asyncio.to_thread(
                replace_playlist_entries, playlist_id, entries
            )
            with SessionLocal() as db:
                job = db.get(M3UFetchJob, job_id)
                if job:
                    job.status = "COMPLETED"
                    job.entry_count = entry_count
                    job.error = None
                    job.updated_at = utc_now()
                    db.commit()
        except Exception as exc:
            cleanup_playlist_id = new_playlist_id
            if cleanup_playlist_id is None and operation == "import":
                with SessionLocal() as db:
                    job = db.get(M3UFetchJob, job_id)
                    cleanup_playlist_id = job.playlist_id if job else None
            if cleanup_playlist_id is not None:
                with SessionLocal() as db:
                    playlist = db.get(M3UPlaylist, cleanup_playlist_id)
                    if playlist:
                        db.delete(playlist)
                        db.commit()
            error = self._safe_error(exc)
            logger.warning(
                "M3U fetch job %s failed (%s)",
                job_id,
                type(exc).__name__,
            )
            with SessionLocal() as db:
                job = db.get(M3UFetchJob, job_id)
                if job:
                    job.status = "FAILED"
                    job.error = error
                    job.updated_at = utc_now()
                    db.commit()

    @staticmethod
    async def _fetch_with_retries(
        source_url: str,
        proxy_url: str | None,
        job_id: int,
    ) -> list[dict]:
        for attempt in range(3):
            try:
                return await fetch_playlist(source_url, proxy_url)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if 400 <= status_code < 500 and status_code != 429:
                    raise
                if attempt == 2:
                    raise
                logger.warning(
                    "M3U source or proxy returned HTTP %s for fetch job %s; retrying",
                    status_code,
                    job_id,
                )
            except httpx.RequestError as exc:
                if attempt == 2:
                    raise
                logger.warning(
                    "M3U network request failed for fetch job %s (%s); retrying",
                    job_id,
                    type(exc).__name__,
                )
            await asyncio.sleep(2 ** (attempt + 1))
        raise RuntimeError("M3U retry loop exited unexpectedly.")

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, ValueError):
            return str(exc)
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code < 100 or status_code > 599:
                return (
                    f"M3U source or proxy returned non-standard HTTP status {status_code} "
                    "after retries. Check provider and proxy availability."
                )
            return (
                f"M3U source returned HTTP {status_code}. "
                "Check the playlist URL and assigned proxy."
            )
        if isinstance(exc, httpx.TimeoutException):
            return "M3U source request timed out. Check the source and assigned proxy."
        if isinstance(exc, httpx.RequestError):
            return (
                f"M3U network request failed ({type(exc).__name__}). "
                "Check the source URL and assigned proxy."
            )
        logger.exception("Unexpected M3U fetch worker error")
        return f"M3U fetch failed ({type(exc).__name__}). Check the backend logs."
