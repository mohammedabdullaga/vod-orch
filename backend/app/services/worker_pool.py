import asyncio
import json
import logging
import threading
from datetime import timedelta

from sqlalchemy import or_, select, update

from app.config import settings
from app.models import (
    Account,
    MonitoredCategory,
    Node,
    Proxy,
    QueueItem,
    SessionLocal,
    StorageRoute,
    utc_now,
)
from app.services.provider_client import stream_url
from app.security import decrypt_secret
from app.services.content_paths import safe_extension
from app.services.dispatcher import DispatchCancelled, RemoteDispatcher

logger = logging.getLogger(__name__)


class WorkerPool:
    def __init__(self, dispatcher: RemoteDispatcher) -> None:
        self.dispatcher = dispatcher
        self._manager: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._workers: dict[int, asyncio.Task] = {}
        self._wakeups: dict[int, asyncio.Event] = {}
        self._busy_accounts: set[int] = set()
        self._cancel_events: dict[int, threading.Event] = {}
        self._cancel_lock = threading.Lock()
        self._clearing_queue = False
        self._manager_wakeup = asyncio.Event()
        self._stopping = asyncio.Event()
        self.active_count = 0

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._recover_interrupted_jobs()
        self._manager = asyncio.create_task(self._manage(), name="worker-pool-manager")

    async def stop(self) -> None:
        self._stopping.set()
        self.wake()
        if self._manager:
            await self._manager
        for task in self._workers.values():
            task.cancel()
        await asyncio.gather(*self._workers.values(), return_exceptions=True)

    async def _manage(self) -> None:
        while not self._stopping.is_set():
            self._manager_wakeup.clear()
            with SessionLocal() as db:
                account_ids = set(db.scalars(
                    select(Account.id).where(Account.enabled.is_(True))
                ).all())
            for account_id, task in list(self._workers.items()):
                if task.done():
                    self._workers.pop(account_id)
                    self._wakeups.pop(account_id, None)
                    if not task.cancelled():
                        error = task.exception()
                        if error:
                            logger.error("Account worker %s stopped unexpectedly: %s", account_id, error)
            for account_id in account_ids - self._workers.keys():
                wakeup = asyncio.Event()
                self._wakeups[account_id] = wakeup
                self._workers[account_id] = asyncio.create_task(
                    self._account_worker(account_id, wakeup), name=f"account-worker-{account_id}"
                )
                wakeup.set()
            for account_id in self._workers.keys() - account_ids:
                self._workers.pop(account_id).cancel()
                self._wakeups.pop(account_id, None)
            try:
                await asyncio.wait_for(
                    self._manager_wakeup.wait(),
                    timeout=settings.worker_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _account_worker(self, account_id: int, wakeup: asyncio.Event) -> None:
        while not self._stopping.is_set():
            wakeup.clear()
            if account_id in self._busy_accounts:
                logger.error("Account %s worker attempted to claim concurrent work.", account_id)
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=settings.worker_poll_seconds)
                except asyncio.TimeoutError:
                    pass
                continue
            item_id = self._claim_next(account_id)
            if item_id is None:
                try:
                    await asyncio.wait_for(
                        wakeup.wait(), timeout=settings.worker_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            self.active_count += 1
            self._busy_accounts.add(account_id)
            try:
                await self._process(account_id, item_id)
            except Exception as exc:
                logger.exception("Account worker %s could not process queue item %s", account_id, item_id)
                try:
                    self._fail(item_id, str(exc))
                except Exception:
                    logger.exception(
                        "Could not record failure for queue item %s; releasing it for another worker",
                        item_id,
                    )
                    self._release_claim(item_id, account_id)
            finally:
                self._busy_accounts.discard(account_id)
                self.active_count -= 1

    def wake(self) -> None:
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._signal_wakeups)
            return
        self._signal_wakeups()

    def _signal_wakeups(self) -> None:
        self._manager_wakeup.set()
        for wakeup in self._wakeups.values():
            wakeup.set()

    def cancel(self, item_id: int) -> str:
        with self._cancel_lock:
            cancel_event = self._cancel_events.get(item_id)
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if not item:
                raise ValueError("Queue item not found.")
            if item.status not in ("PENDING", "DOWNLOADING", "VERIFYING", "CANCELLING"):
                raise ValueError("Only pending or active downloads can be cancelled.")
            if item.status == "PENDING":
                item.status = "CANCELLED"
                item.account_id = None
                item.progress = 0
                item.error = "Cancelled by administrator."
            else:
                item.status = "CANCELLING"
                item.error = "Cancellation requested; stopping remote download."
            item.retry_after = None
            item.updated_at = utc_now()
            db.commit()
            status = item.status
        if cancel_event:
            cancel_event.set()
        self.wake()
        return status

    @staticmethod
    def _recover_interrupted_jobs() -> None:
        with SessionLocal() as db:
            result = db.execute(
                update(QueueItem)
                .where(QueueItem.status.in_(("DOWNLOADING", "VERIFYING")))
                .values(
                    status="PENDING",
                    account_id=None,
                    progress=0,
                    speed_bytes_per_second=None,
                    error=None,
                    retry_after=None,
                    updated_at=utc_now(),
                )
            )
            db.commit()
        if result.rowcount:
            logger.warning("Reset %s interrupted queue jobs to PENDING.", result.rowcount)

    def _claim_next(self, account_id: int) -> int | None:
        if self._clearing_queue:
            return None
        with SessionLocal() as db:
            now = utc_now()
            self._release_due_retries(db, now)
            account = db.get(Account, account_id)
            if not account or not account.enabled:
                db.commit()
                return None
            candidates = db.scalars(
                select(QueueItem)
                .where(
                    QueueItem.status == "PENDING",
                    QueueItem.kind.in_(select(StorageRoute.kind)),
                )
                .order_by(QueueItem.created_at, QueueItem.id)
            ).all()
            candidate = None
            for item in candidates:
                if item.account_id not in (None, account_id):
                    continue
                if item.source_provider_url is None:
                    if item.provider_stream_id:
                        item.source_provider_url = account.provider_url
                        db.flush()
                    else:
                        try:
                            stored_url = decrypt_secret(item.stream_url_encrypted)
                        except Exception as exc:
                            logger.error(
                                "Could not inspect legacy queue item %s provider URL (%s).",
                                item.id,
                                type(exc).__name__,
                            )
                            item.status = "FAILED"
                            item.error = (
                                "Could not match this legacy queue item to a provider account. "
                                "Verify APP_ENCRYPTION_KEY before retrying."
                            )
                            item.retry_after = None
                            item.updated_at = utc_now()
                            db.flush()
                            continue
                        if not stored_url.startswith(account.provider_url.rstrip("/") + "/"):
                            continue
                    item.source_provider_url = account.provider_url
                    db.flush()
                try:
                    attempted_account_ids = {
                        int(value) for value in json.loads(item.attempted_account_ids or "[]")
                    }
                except (json.JSONDecodeError, TypeError, ValueError):
                    logger.error(
                        "Queue item %s has invalid attempted-account history; refusing to claim it.",
                        item.id,
                    )
                    item.status = "FAILED"
                    item.error = "Invalid account retry history; inspect the queue record before retrying."
                    item.retry_after = None
                    item.updated_at = utc_now()
                    db.flush()
                    continue
                if account_id in attempted_account_ids:
                    continue
                candidate = item
                break
            if candidate is None:
                db.commit()
                return None
            claimed = db.execute(
                update(QueueItem)
                .where(QueueItem.id == candidate.id, QueueItem.status == "PENDING")
                .values(
                    status="DOWNLOADING",
                    progress=0,
                    file_size_bytes=None,
                    speed_bytes_per_second=None,
                    account_id=account_id,
                    attempted_account_ids=json.dumps(
                        sorted(attempted_account_ids | {account_id})
                    ),
                    error=None,
                    updated_at=utc_now(),
                )
            )
            if claimed.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return candidate.id

    async def stop_all_and_clear(
        self, timeout_seconds: float = 120
    ) -> dict[str, int | bool]:
        if self._clearing_queue:
            raise RuntimeError("A stop-all-and-clear operation is already in progress.")
        self._clearing_queue = True
        try:
            with SessionLocal() as db:
                statuses = db.scalars(select(QueueItem.status)).all()
                if any(status == "CLEANING" for status in statuses):
                    raise RuntimeError(
                        "A failed-item storage cleanup is in progress; wait for it to finish."
                    )
                pending_count = sum(status == "PENDING" for status in statuses)
                active_count = sum(
                    status in ("DOWNLOADING", "VERIFYING", "CANCELLING")
                    for status in statuses
                )
                db.execute(
                    update(QueueItem)
                    .where(QueueItem.status == "PENDING")
                    .values(
                        status="CANCELLED",
                        account_id=None,
                        progress=0,
                        error="Cancelled by administrator during stop-all-and-clear.",
                        retry_after=None,
                        updated_at=utc_now(),
                    )
                )
                db.execute(
                    update(QueueItem)
                    .where(QueueItem.status.in_(("DOWNLOADING", "VERIFYING")))
                    .values(
                        status="CANCELLING",
                        error="Cancellation requested by stop-all-and-clear.",
                        retry_after=None,
                        updated_at=utc_now(),
                    )
                )
                db.commit()

            with self._cancel_lock:
                cancel_events = list(self._cancel_events.values())
            for cancel_event in cancel_events:
                cancel_event.set()
            self.wake()

            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while True:
                with self._cancel_lock:
                    active_jobs = bool(self._cancel_events)
                if not active_jobs:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        "Timed out waiting for remote downloads to stop; queue records were not cleared."
                    )
                await asyncio.sleep(min(0.1, remaining))

            with SessionLocal() as db:
                result = db.execute(
                    update(QueueItem)
                    .where(QueueItem.hidden_from_dashboard.is_(False))
                    .values(hidden_from_dashboard=True, updated_at=utc_now())
                )
                cleared_count = result.rowcount or 0
                db.commit()
            return {
                "ok": True,
                "cleared_count": cleared_count,
                "pending_cancelled_count": pending_count,
                "active_cancelled_count": active_count,
            }
        finally:
            self._clearing_queue = False
            self.wake()

    @staticmethod
    def _release_due_retries(db, now) -> None:
        due = db.execute(
            select(
                QueueItem.id,
                QueueItem.attempted_account_ids,
                QueueItem.source_provider_url,
            ).where(
                QueueItem.status == "FAILED",
                QueueItem.retry_after.is_not(None),
                QueueItem.retry_after <= now,
                QueueItem.retries < settings.max_retries,
            )
        ).all()
        for item_id, history, source_provider_url in due:
            try:
                attempted_account_ids = {
                    int(value) for value in json.loads(history or "[]")
                }
            except (json.JSONDecodeError, TypeError, ValueError):
                db.execute(
                    update(QueueItem)
                    .where(
                        QueueItem.id == item_id,
                        QueueItem.status == "FAILED",
                        QueueItem.retry_after <= now,
                    )
                    .values(
                        retry_after=None,
                        error="Invalid account retry history; inspect the queue record before retrying.",
                        updated_at=utc_now(),
                    )
                )
                continue
            accounts_query = select(Account.id).where(Account.enabled.is_(True))
            if source_provider_url and not source_provider_url.startswith("m3u:"):
                item = db.get(QueueItem, item_id)
                if item and item.provider_stream_id is None:
                    accounts_query = select(Account.id).where(
                        Account.enabled.is_(True),
                        Account.provider_url == source_provider_url,
                    )
            eligible_accounts = list(db.scalars(accounts_query).all())
            available_account_ids = set(eligible_accounts)
            if not available_account_ids - attempted_account_ids:
                attempted_account_ids = set()
            db.execute(
                update(QueueItem)
                .where(
                    QueueItem.id == item_id,
                    QueueItem.status == "FAILED",
                    QueueItem.retry_after <= now,
                )
                .values(
                    status="PENDING",
                    account_id=None,
                    attempted_account_ids=json.dumps(sorted(attempted_account_ids)),
                    retry_after=None,
                    error=None,
                    updated_at=utc_now(),
                )
            )
        if due:
            db.commit()

    @staticmethod
    def _release_claim(item_id: int, account_id: int) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item and item.status in ("DOWNLOADING", "VERIFYING") and item.account_id == account_id:
                item.status = "PENDING"
                item.account_id = None
                item.progress = 0
                item.updated_at = utc_now()
                db.commit()

    async def _process(self, account_id: int, item_id: int) -> None:
        cancel_event = threading.Event()
        stream_url_value = ""
        proxy_url: str | None = None
        with self._cancel_lock:
            self._cancel_events[item_id] = cancel_event
        try:
            with SessionLocal() as db:
                item = db.get(QueueItem, item_id)
                if not item or item.status in ("CANCELLED", "CANCELLING"):
                    self._mark_cancelled(item_id)
                    return
                account = db.get(Account, account_id)
                route = db.get(StorageRoute, item.kind)
                node = db.get(Node, route.node_id) if route else None
                category = db.scalar(
                    select(MonitoredCategory).where(
                        MonitoredCategory.kind == item.kind,
                        MonitoredCategory.category_id == item.category_id,
                    )
                ) if item.category_id else None
                if not account or not node:
                    content_type = "series" if item.kind == "series" else "movie"
                    self._fail(
                        item_id,
                        f"Account or {content_type} storage node is not configured.",
                    )
                    return
                file_name = item.file_name
                if item.provider_stream_id and not (
                    item.source_provider_url or ""
                ).startswith("m3u:"):
                    extension = safe_extension(file_name.rsplit(".", 1)[-1])
                    stream_url_value = stream_url(
                        account, item.kind, item.provider_stream_id, extension
                    )
                else:
                    stream_url_value = decrypt_secret(item.stream_url_encrypted)
                user_agent = account.user_agent or "Mozilla"
                destination_root = (
                    category.destination_path
                    if category and category.destination_path
                    else node.watch_path
                )
                match_existing_series = bool(
                    category
                    and item.kind == "series"
                    and category.match_existing_series
                )
                series_match_name = item.series_title if match_existing_series else None
                series_match_prefixes = (
                    category.strip_prefixes
                    if match_existing_series and category
                    else None
                )
                if item.proxy_mode == "selected":
                    proxy_record = db.get(Proxy, item.proxy_id) if item.proxy_id else None
                else:
                    proxy_record = db.get(Proxy, node.proxy_id) if node.proxy_id else None
                if item.proxy_mode in ("node", "selected") and (
                    not proxy_record or not proxy_record.enabled
                ):
                    self._fail(
                        item_id,
                        "The selected proxy is no longer available. "
                        "Retry and choose an enabled proxy or direct connection.",
                    )
                    return
                if item.proxy_mode == "direct" or (proxy_record and not proxy_record.enabled):
                    proxy_record = None
                proxy_url = decrypt_secret(proxy_record.url_encrypted) if proxy_record else None
                axel_connections = (
                    node.axel_connections or settings.default_axel_connections
                )
                downloader = item.downloader or "axel"
                if cancel_event.is_set():
                    self._mark_cancelled(item_id)
                    return

            loop = asyncio.get_running_loop()
            result = await asyncio.to_thread(
                self.dispatcher.dispatch,
                node,
                stream_url_value,
                file_name,
                user_agent,
                proxy_url,
                item_id,
                destination_root,
                lambda value: loop.call_soon_threadsafe(
                    asyncio.create_task, self._set_progress(item_id, value)
                ),
                lambda: loop.call_soon_threadsafe(
                    asyncio.create_task, self._set_status(item_id, "VERIFYING")
                ),
                axel_connections,
                cancel_event,
                lambda progress, speed, file_size: self._set_transfer_metrics(
                    item_id, progress, speed, file_size
                ),
                downloader=downloader,
                match_existing_series=match_existing_series,
                series_name=series_match_name,
                series_match_prefixes=series_match_prefixes,
            )
            with SessionLocal() as db:
                item = db.get(QueueItem, item_id)
                if item and item.status != "CANCELLED":
                    item.status = "COMPLETED"
                    item.progress = 100
                    item.final_path = result.final_path
                    item.error = None
                    item.retry_after = None
                    db.commit()
        except DispatchCancelled:
            logger.info("Download job %s was cancelled.", item_id)
            self._mark_cancelled(item_id)
        except Exception as exc:
            logger.error("Download job %s failed (%s)", item_id, type(exc).__name__)
            detail = str(exc).replace(stream_url_value, "[redacted stream URL]")
            if proxy_url:
                detail = detail.replace(proxy_url, "[redacted proxy URL]")
            with SessionLocal() as db:
                item = db.get(QueueItem, item_id)
                if item and item.status != "CANCELLED":
                    self._fail(item_id, detail)
        finally:
            with self._cancel_lock:
                self._cancel_events.pop(item_id, None)

    @staticmethod
    async def _set_progress(item_id: int, progress: float) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item and item.status == "DOWNLOADING":
                item.progress = progress
                db.commit()

    @staticmethod
    async def _set_status(item_id: int, status: str) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item and item.status not in ("COMPLETED", "FAILED", "CANCELLED", "CANCELLING"):
                item.status = status
                db.commit()

    @staticmethod
    def _set_transfer_metrics(
        item_id: int,
        progress: float | None,
        speed: float | None,
        file_size: int | None,
    ) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item and item.status == "DOWNLOADING":
                if progress is not None:
                    item.progress = progress
                if speed is not None:
                    item.speed_bytes_per_second = speed
                if file_size is not None:
                    item.file_size_bytes = file_size
                db.commit()

    @staticmethod
    def _mark_cancelled(item_id: int) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item and item.status in ("CANCELLING", "CANCELLED"):
                item.status = "CANCELLED"
                item.account_id = None
                item.progress = 0
                item.error = "Cancelled by administrator."
                item.updated_at = utc_now()
                db.commit()

    @staticmethod
    def _fail(item_id: int, message: str) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item:
                item.status = "FAILED"
                item.progress = 0
                item.retries += 1
                if item.retries >= settings.max_retries:
                    message = (
                        f"{message} Automatic retry limit reached; use Retry to start a new cycle."
                    )
                item.error = message[-4000:]
                item.retry_after = (
                    None
                    if item.retries >= settings.max_retries
                    else utc_now()
                    + timedelta(seconds=min(3600, 30 * (2 ** max(0, item.retries - 1))))
                )
                db.commit()
