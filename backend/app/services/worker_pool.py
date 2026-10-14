import asyncio
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
from app.provider import stream_url
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
                    error=None,
                    retry_after=None,
                    updated_at=utc_now(),
                )
            )
            db.commit()
        if result.rowcount:
            logger.warning("Reset %s interrupted queue jobs to PENDING.", result.rowcount)

    def _claim_next(self, account_id: int) -> int | None:
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
                    or_(
                        QueueItem.account_id.is_(None),
                        QueueItem.account_id == account_id,
                    ),
                    or_(
                        QueueItem.source_provider_url == account.provider_url,
                        QueueItem.source_provider_url.is_(None),
                    ),
                )
                .order_by(QueueItem.created_at, QueueItem.id)
            ).all()
            candidate = None
            for item in candidates:
                if item.source_provider_url is None:
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
                candidate = item
                break
            if candidate is None:
                db.commit()
                return None
            claimed = db.execute(
                update(QueueItem)
                .where(QueueItem.id == candidate.id, QueueItem.status == "PENDING")
                .values(
                    status="DOWNLOADING", progress=0, account_id=account_id, error=None,
                )
            )
            if claimed.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return candidate.id

    @staticmethod
    def _release_due_retries(db, now) -> None:
        due = db.scalars(
            select(QueueItem).where(
                QueueItem.status == "FAILED",
                QueueItem.retry_after.is_not(None),
                QueueItem.retry_after <= now,
                QueueItem.retries < settings.max_retries,
            )
        ).all()
        for item in due:
            item.status = "PENDING"
            item.account_id = None
            item.retry_after = None
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
                if item.provider_stream_id:
                    extension = safe_extension(file_name.rsplit(".", 1)[-1])
                    stream_url_value = stream_url(
                        account, item.kind, item.provider_stream_id, extension
                    )
                else:
                    stream_url_value = decrypt_secret(item.stream_url_encrypted)
                user_agent = account.user_agent
                destination_root = (
                    category.destination_path
                    if category and category.destination_path
                    else node.watch_path
                )
                proxy_record = db.get(Proxy, node.proxy_id) if node.proxy_id else None
                if item.proxy_mode == "node" and (
                    not proxy_record or not proxy_record.enabled
                ):
                    self._fail(
                        item_id,
                        "The selected storage-server proxy is no longer enabled. "
                        "Retry and choose direct connection or enable the proxy.",
                    )
                    return
                if item.proxy_mode == "direct" or (proxy_record and not proxy_record.enabled):
                    proxy_record = None
                proxy_url = decrypt_secret(proxy_record.url_encrypted) if proxy_record else None
                axel_connections = node.axel_connections
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
                item.error = message[-4000:]
                item.retry_after = utc_now() + timedelta(seconds=min(3600, 30 * (2 ** max(0, item.retries - 1))))
                db.commit()
