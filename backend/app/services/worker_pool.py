import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select, update

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
from app.security import decrypt_secret
from app.services.dispatcher import RemoteDispatcher

logger = logging.getLogger(__name__)


class WorkerPool:
    def __init__(self, dispatcher: RemoteDispatcher) -> None:
        self.dispatcher = dispatcher
        self._manager: asyncio.Task | None = None
        self._workers: dict[int, asyncio.Task] = {}
        self._wakeups: dict[int, asyncio.Event] = {}
        self._stopping = asyncio.Event()
        self.active_count = 0

    async def start(self) -> None:
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
            for account_id in self._workers.keys() - account_ids:
                self._workers.pop(account_id).cancel()
                self._wakeups.pop(account_id, None)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=settings.worker_poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _account_worker(self, account_id: int, wakeup: asyncio.Event) -> None:
        while not self._stopping.is_set():
            wakeup.clear()
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
            try:
                await self._process(account_id, item_id)
            except Exception as exc:
                logger.exception("Account worker %s could not process queue item %s", account_id, item_id)
                self._fail(item_id, str(exc))
            finally:
                self.active_count -= 1

    def wake(self) -> None:
        for wakeup in self._wakeups.values():
            wakeup.set()

    def _claim_next(self, account_id: int) -> int | None:
        with SessionLocal() as db:
            now = utc_now()
            self._release_due_retries(db, now)
            candidate_id = db.scalar(
                select(QueueItem)
                .where(
                    QueueItem.status == "PENDING",
                    (QueueItem.account_id.is_(None) | (QueueItem.account_id == account_id)),
                    QueueItem.kind.in_(select(StorageRoute.kind)),
                )
                .order_by(QueueItem.created_at, QueueItem.id)
                .limit(1)
                .with_only_columns(QueueItem.id)
            )
            if candidate_id is None:
                db.commit()
                return None
            claimed = db.execute(
                update(QueueItem)
                .where(QueueItem.id == candidate_id, QueueItem.status == "PENDING")
                .values(
                    status="DOWNLOADING", progress=0, account_id=account_id, error=None,
                )
            )
            if claimed.rowcount != 1:
                db.rollback()
                return None
            db.commit()
            return candidate_id

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
            item.retry_after = None
        if due:
            db.commit()

    async def _process(self, account_id: int, item_id: int) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            account = db.get(Account, account_id)
            route = db.get(StorageRoute, item.kind) if item else None
            node = db.get(Node, route.node_id) if route else None
            category = db.scalar(
                select(MonitoredCategory).where(
                    MonitoredCategory.kind == item.kind,
                    MonitoredCategory.category_id == item.category_id,
                )
            ) if item and item.category_id else None
            proxy_records = db.scalars(
                select(Proxy).where(Proxy.enabled.is_(True)).order_by(Proxy.id)
            ).all()
            if not item or not account or not node:
                content_type = "series" if item and item.kind == "series" else "movie"
                self._fail(
                    item_id,
                    f"Account or {content_type} storage node is not configured.",
                )
                return
            stream_url, file_name = decrypt_secret(item.stream_url_encrypted), item.file_name
            user_agent = account.user_agent
            destination_root = category.destination_path if category and category.destination_path else node.watch_path
            proxy_record = proxy_records[item_id % len(proxy_records)] if proxy_records else None
            proxy_url = decrypt_secret(proxy_record.url_encrypted) if proxy_record else None

        loop = asyncio.get_running_loop()
        try:
            await asyncio.to_thread(
                self.dispatcher.dispatch,
                node,
                stream_url,
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
            )
        except Exception as exc:
            logger.error("Download job %s failed (%s)", item_id, type(exc).__name__)
            detail = str(exc).replace(stream_url, "[redacted stream URL]")
            if proxy_url:
                detail = detail.replace(proxy_url, "[redacted proxy URL]")
            self._fail(item_id, detail)
            return
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item:
                item.status = "COMPLETED"
                item.progress = 100
                item.error = None
                item.retry_after = None
                db.commit()

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
            if item and item.status not in ("COMPLETED", "FAILED"):
                item.status = status
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
