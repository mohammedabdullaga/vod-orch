import asyncio
import json
import logging
import re
import threading
import time
from datetime import timedelta
from pathlib import PurePosixPath

from sqlalchemy import delete, or_, select, update

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
from app.security import decrypt_secret, encrypt_secret
from app.services.content_paths import (
    clean_content_name,
    legacy_identity_segment,
    legacy_identity_suffix,
    safe_extension,
    series_identity_key,
)
from app.services.dispatcher import DispatchCancelled, RemoteDispatcher

logger = logging.getLogger(__name__)
_TASK_LOG_LIMIT = 262144
_ERROR_LINE = re.compile(
    r"(?:\b(?:401|403)\b|unauthori[sz]ed|forbidden|connection refused|"
    r"no space left|disk quota|quota exceeded)",
    re.IGNORECASE,
)


class WorkerPool:
    def __init__(self, dispatcher: RemoteDispatcher) -> None:
        self.dispatcher = dispatcher
        self._manager: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._workers: dict[int, asyncio.Task] = {}
        self._active_jobs: dict[int, int] = {}
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
        next_reconciliation = time.monotonic() + 15
        next_pending_recovery = time.monotonic() + 15
        while not self._stopping.is_set():
            try:
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
                                logger.error(
                                    "Account worker %s stopped unexpectedly: %s",
                                    account_id,
                                    error,
                                )
                for account_id in account_ids - self._workers.keys():
                    wakeup = asyncio.Event()
                    self._wakeups[account_id] = wakeup
                    self._workers[account_id] = asyncio.create_task(
                        self._account_worker(account_id, wakeup),
                        name=f"account-worker-{account_id}",
                    )
                    wakeup.set()
                for account_id in self._workers.keys() - account_ids:
                    if account_id in self._busy_accounts:
                        continue
                    self._workers.pop(account_id).cancel()
                    self._wakeups.pop(account_id, None)
                with SessionLocal() as db:
                    released_retries = self._release_due_retries(db, utc_now())
                if released_retries:
                    logger.info(
                        "Released %s failed queue item(s) for automatic retry.",
                        released_retries,
                    )
                    self.wake()
                if time.monotonic() >= next_reconciliation:
                    self._reconcile_worker_state()
                    next_reconciliation = time.monotonic() + 15
                if time.monotonic() >= next_pending_recovery:
                    self._recover_stalled_pending()
                    next_pending_recovery = time.monotonic() + 15
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Worker pool loop crashed: %s", str(exc))
            try:
                await asyncio.wait_for(
                    self._manager_wakeup.wait(),
                    timeout=settings.worker_poll_seconds,
                )
            except asyncio.TimeoutError:
                pass

    async def _account_worker(self, account_id: int, wakeup: asyncio.Event) -> None:
        while not self._stopping.is_set():
            try:
                wakeup.clear()
                if account_id in self._busy_accounts:
                    logger.error(
                        "Account %s worker attempted to claim concurrent work.",
                        account_id,
                    )
                    await self._wait_for_wakeup(wakeup)
                    continue
                item_id = self._claim_next(account_id)
                if item_id is None:
                    await self._wait_for_wakeup(wakeup)
                    continue
                self.active_count += 1
                self._busy_accounts.add(account_id)
                self._active_jobs[account_id] = item_id
                try:
                    await self._process(account_id, item_id)
                except Exception as exc:
                    logger.exception(
                        "Account worker %s could not process queue item %s",
                        account_id,
                        item_id,
                    )
                    try:
                        self._fail(item_id, str(exc))
                    except Exception:
                        logger.exception(
                            "Could not record failure for queue item %s; "
                            "releasing it for another worker",
                            item_id,
                        )
                        self._release_claim(item_id, account_id)
                finally:
                    self._busy_accounts.discard(account_id)
                    self._active_jobs.pop(account_id, None)
                    self.active_count = len(self._busy_accounts)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Worker pool loop crashed: %s", str(exc))
                await asyncio.sleep(max(1, settings.worker_poll_seconds))

    async def _wait_for_wakeup(self, wakeup: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(
                wakeup.wait(), timeout=max(1, settings.worker_poll_seconds)
            )
        except asyncio.TimeoutError:
            pass

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
        recovered_statuses = ("DOWNLOADING", "VERIFYING")
        with SessionLocal() as db:
            interrupted = db.scalars(
                select(QueueItem).where(QueueItem.status.in_(recovered_statuses))
            ).all()
            message = (
                "The service restarted while this task was active; "
                "it was returned to the pending queue."
            )
            for item in interrupted:
                item.status = "PENDING"
                item.account_id = None
                item.progress = 0
                item.speed_bytes_per_second = None
                item.attempted_account_ids = "[]"
                item.error = message
                item.last_error = message
                item.task_log = (item.task_log + f"\n{message}\n")[-_TASK_LOG_LIMIT:]
                item.task_log_offset += len(f"\n{message}\n")
                item.retry_after = None
                item.updated_at = utc_now()
            db.commit()
        if interrupted:
            logger.warning(
                "Reset %s interrupted queue jobs to PENDING.",
                len(interrupted),
            )

    def _reconcile_orphaned_jobs(self) -> None:
        self._reconcile_worker_state()

    def _recover_stalled_pending(self) -> int:
        cutoff = utc_now() - timedelta(seconds=60)
        idle_account_ids = {
            account_id
            for account_id, worker in self._workers.items()
            if not worker.done() and account_id not in self._busy_accounts
        }

        with SessionLocal() as db:
            all_accounts = {
                account.id: account
                for account in db.scalars(
                    select(Account).where(Account.enabled.is_(True))
                ).all()
            }
            idle_accounts = {
                account_id: account
                for account_id, account in all_accounts.items()
                if account_id in idle_account_ids
            }
            routed_kinds = set(db.scalars(select(StorageRoute.kind)).all())
            pending = db.scalars(
                select(QueueItem)
                .where(
                    QueueItem.status == "PENDING",
                    QueueItem.updated_at <= cutoff,
                )
                .order_by(QueueItem.created_at, QueueItem.id)
            ).all()
            requeued = 0
            diagnosed = 0
            for item in pending:
                has_destination = (
                    item.kind in routed_kinds or item.destination_node_id is not None
                )
                if not has_destination:
                    reason = (
                        f"No storage route is configured for {item.kind} tasks; "
                        "the task remains pending.\n"
                    )
                    eligible_ids: set[int] = set()
                elif item.source_provider_url and item.source_provider_url.startswith("m3u:"):
                    eligible_ids = set(all_accounts)
                    reason = ""
                elif item.provider_stream_id:
                    eligible_ids = set(all_accounts)
                    reason = ""
                else:
                    source_url = item.source_provider_url
                    if source_url is None:
                        try:
                            stored_url = decrypt_secret(item.stream_url_encrypted)
                        except Exception as exc:
                            logger.error(
                                "Could not inspect stalled queue item %s provider URL (%s).",
                                item.id,
                                type(exc).__name__,
                            )
                            stored_url = ""
                        source_url = next(
                            (
                                account.provider_url
                                for account in all_accounts.values()
                                if stored_url.startswith(account.provider_url.rstrip("/") + "/")
                            ),
                            None,
                        )
                    eligible_ids = {
                        account_id
                        for account_id, account in all_accounts.items()
                        if source_url
                        and account.provider_url.rstrip("/") == source_url.rstrip("/")
                    }
                    reason = ""

                if has_destination and not eligible_ids:
                    reason = (
                        "No enabled provider account matches this pending task; "
                        "the task remains pending.\n"
                    )
                elif has_destination:
                    idle_eligible_ids = eligible_ids & set(idle_accounts)
                    if not idle_eligible_ids:
                        reason = (
                            "All compatible worker accounts are busy or unavailable; "
                            "the task remains pending until one is idle.\n"
                        )
                    else:
                        previous_account_id = item.account_id
                        item.account_id = None
                        reason = (
                            "Pending task had no dispatch for 60 seconds; "
                            "scheduler released its account assignment for eligible "
                            f"idle worker(s) {', '.join(str(value) for value in sorted(idle_eligible_ids))}.\n"
                        )
                        requeued += 1
                        logger.warning(
                            "Requeued stalled pending task %s (previous account %s; "
                            "eligible idle accounts %s).",
                            item.id,
                            previous_account_id,
                            sorted(idle_eligible_ids),
                        )

                if not reason:
                    continue
                item.task_log = (item.task_log + reason)[-_TASK_LOG_LIMIT:]
                item.task_log_offset += len(reason)
                item.updated_at = utc_now()
                diagnosed += 1
            if diagnosed:
                db.commit()

        if requeued:
            self.wake()
        return requeued

    def _reconcile_worker_state(self) -> int:
        with self._cancel_lock:
            cancel_event_ids = set(self._cancel_events)
        with SessionLocal() as db:
            active_items = db.scalars(
                select(QueueItem).where(
                    QueueItem.status.in_(("DOWNLOADING", "VERIFYING", "CANCELLING"))
                )
            ).all()
            live_active_by_id = {
                item.id: item.account_id for item in active_items
            }
            recovered = 0
            for item in active_items:
                if (
                    self._active_jobs.get(item.account_id) == item.id
                    and item.account_id in self._busy_accounts
                    and item.id in cancel_event_ids
                ):
                    continue
                message = (
                    "Worker lease was not associated with a live task; "
                    "the task was reconciled by the scheduler."
                )
                if item.status == "CANCELLING":
                    item.status = "CANCELLED"
                    item.error = "Cancelled after the worker lease was lost."
                else:
                    item.status = "PENDING"
                    item.error = message
                    item.attempted_account_ids = "[]"
                item.account_id = None
                item.progress = 0
                item.speed_bytes_per_second = None
                item.last_error = message
                item.task_log = (item.task_log + f"\n{message}\n")[-_TASK_LOG_LIMIT:]
                item.task_log_offset += len(f"\n{message}\n")
                item.updated_at = utc_now()
                recovered += 1
            if recovered:
                db.commit()
        stale_accounts = [
            account_id
            for account_id in self._busy_accounts
            if account_id not in self._active_jobs
            or live_active_by_id.get(self._active_jobs[account_id]) != account_id
            or self._active_jobs[account_id] not in cancel_event_ids
        ]
        for account_id in stale_accounts:
            item_id = self._active_jobs.pop(account_id, None)
            self._busy_accounts.discard(account_id)
            if item_id is not None:
                with self._cancel_lock:
                    cancel_event = self._cancel_events.get(item_id)
                if cancel_event:
                    cancel_event.set()
        self.active_count = len(self._busy_accounts)
        if recovered or stale_accounts:
            logger.warning(
                "Reconciled %s orphaned tasks and %s stale account worker flags.",
                recovered,
                len(stale_accounts),
            )
            self.wake()
        return recovered + len(stale_accounts)

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
                    or_(
                        QueueItem.kind.in_(select(StorageRoute.kind)),
                        QueueItem.destination_node_id.is_not(None),
                    ),
                )
                .order_by(QueueItem.created_at, QueueItem.id)
            ).all()
            candidate = None
            for item in candidates:
                if item.account_id not in (None, account_id):
                    assigned_account = db.get(Account, item.account_id)
                    assigned_worker = self._workers.get(item.account_id)
                    assigned_worker_available = (
                        assigned_account is not None
                        and assigned_account.enabled
                        and assigned_worker is not None
                        and not assigned_worker.done()
                        and item.account_id not in self._busy_accounts
                    )
                    if assigned_worker_available:
                        continue
                    logger.warning(
                        "Queue item %s was assigned to unavailable or busy account %s; "
                        "releasing it for an eligible worker.",
                        item.id,
                        item.account_id,
                    )
                    item.account_id = None
                    db.flush()
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
                elif (
                    item.provider_stream_id is None
                    and not item.source_provider_url.startswith("m3u:")
                    and item.source_provider_url.rstrip("/")
                    != account.provider_url.rstrip("/")
                ):
                    continue
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
                    eligible_account_ids = set(db.scalars(
                        select(Account.id).where(Account.enabled.is_(True))
                    ).all())
                    if (
                        item.source_provider_url
                        and not item.source_provider_url.startswith("m3u:")
                        and item.provider_stream_id is None
                    ):
                        eligible_account_ids = set(db.scalars(
                            select(Account.id).where(
                                Account.enabled.is_(True),
                                Account.provider_url == item.source_provider_url,
                            )
                        ).all())
                    if eligible_account_ids - attempted_account_ids:
                        continue
                    attempted_account_ids.clear()
                candidate = item
                break
            if candidate is None:
                db.commit()
                return None
            claim_message = (
                f"Worker account {account_id} claimed the task; "
                "preparing remote dispatch.\n"
            )
            claimed = db.execute(
                update(QueueItem)
                .where(QueueItem.id == candidate.id, QueueItem.status == "PENDING")
                .values(
                    status="DOWNLOADING",
                    progress=0,
                    file_size_bytes=None,
                    speed_bytes_per_second=None,
                    dispatched_command=None,
                    task_log=claim_message,
                    task_log_offset=len(claim_message),
                    last_error=None,
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

    async def purge_history(
        self,
        scope: str,
        force_reset_account_busy_states: bool = False,
        timeout_seconds: float = 120,
    ) -> dict[str, int | str | bool]:
        if scope not in (
            "all",
            "failed_and_completed",
            "stuck_pending",
            "none",
        ):
            raise ValueError("Unsupported task history purge scope.")
        if self._clearing_queue:
            raise RuntimeError("Another queue maintenance operation is already in progress.")
        self._clearing_queue = True
        try:
            reset_count = (
                self._reconcile_worker_state()
                if force_reset_account_busy_states
                else 0
            )
            with SessionLocal() as db:
                if scope == "all":
                    items = db.scalars(select(QueueItem)).all()
                elif scope == "failed_and_completed":
                    items = db.scalars(
                        select(QueueItem).where(
                            QueueItem.status.in_(("FAILED", "COMPLETED"))
                        )
                    ).all()
                elif scope == "stuck_pending":
                    items = db.scalars(
                        select(QueueItem).where(QueueItem.status == "PENDING")
                    ).all()
                else:
                    items = []

                if any(item.status == "CLEANING" for item in items):
                    raise RuntimeError(
                        "A failed-item storage cleanup is in progress; wait for it to finish."
                    )
                selected_ids = {item.id for item in items}
                original_statuses = {item.id: item.status for item in items}
                active_ids = {
                    item.id
                    for item in items
                    if item.status in ("DOWNLOADING", "VERIFYING", "CANCELLING")
                }
                for item in items:
                    if item.status in ("DOWNLOADING", "VERIFYING"):
                        item.status = "CANCELLING"
                        item.error = "Cancellation requested by queue history maintenance."
                        item.updated_at = utc_now()
                    elif item.status == "PENDING":
                        item.status = "CANCELLED"
                        item.account_id = None
                        item.error = "Cancelled by queue history maintenance."
                        item.updated_at = utc_now()
                db.commit()

            with self._cancel_lock:
                cancel_events = [
                    self._cancel_events[item_id]
                    for item_id in active_ids
                    if item_id in self._cancel_events
                ]
            for cancel_event in cancel_events:
                cancel_event.set()
            self.wake()

            deadline = asyncio.get_running_loop().time() + timeout_seconds
            while True:
                with self._cancel_lock:
                    active_selected = active_ids.intersection(self._cancel_events)
                if not active_selected:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        "Timed out waiting for selected downloads to stop; "
                        "their task history was not deleted."
                    )
                await asyncio.sleep(min(0.1, remaining))

            cleanup_targets: list[tuple[int, str, int, bool, str]] = []
            with SessionLocal() as db:
                for item in db.scalars(
                    select(QueueItem).where(QueueItem.id.in_(selected_ids))
                ).all() if selected_ids else []:
                    if original_statuses[item.id] == "COMPLETED":
                        continue
                    if not item.remote_node_id and not item.dispatched_command:
                        continue
                    node = (
                        db.get(Node, item.remote_node_id)
                        if item.remote_node_id is not None
                        else None
                    )
                    if node is None:
                        route = db.get(StorageRoute, item.kind)
                        node = (
                            db.get(Node, item.destination_node_id)
                            if item.destination_node_id is not None
                            else db.get(Node, route.node_id) if route else None
                        )
                    if node is None:
                        raise RuntimeError(
                            f"Cannot clean remote artifacts for task {item.id}: "
                            "its storage server is not configured."
                        )
                    cleanup_targets.append(
                        (
                            item.id,
                            item.file_name,
                            node.id,
                            bool(item.remote_match_existing_series),
                            item.destination_path or node.watch_path,
                        )
                    )
                nodes_by_id = {
                    node_id: db.get(Node, node_id)
                    for _, _, node_id, _, _ in cleanup_targets
                }

            for item_id, file_name, node_id, match_existing, destination_root in cleanup_targets:
                node = nodes_by_id[node_id]
                await asyncio.to_thread(
                    self.dispatcher.cleanup_failed_job,
                    node,
                    item_id,
                    file_name,
                    destination_root,
                    False,
                    match_existing,
                )

            with SessionLocal() as db:
                deleted = 0
                if selected_ids:
                    result = db.execute(
                        delete(QueueItem).where(QueueItem.id.in_(selected_ids))
                    )
                    deleted = result.rowcount or 0
                db.commit()
            return {
                "success": True,
                "deleted_tasks_count": deleted,
                "reset_account_states_count": reset_count,
                "message": "Queue and task history successfully cleaned.",
            }
        finally:
            self._clearing_queue = False
            self.wake()

    @staticmethod
    def _release_due_retries(db, now) -> int:
        due = db.scalars(
            select(QueueItem).where(
                QueueItem.status == "FAILED",
                QueueItem.retry_after.is_not(None),
                QueueItem.retry_after <= now,
                QueueItem.retries < settings.max_retries,
            )
        ).all()
        released = 0
        for item in due:
            try:
                attempted_account_ids = {
                    int(value)
                    for value in json.loads(item.attempted_account_ids or "[]")
                }
            except (json.JSONDecodeError, TypeError, ValueError):
                item.retry_after = None
                item.error = (
                    "Invalid account retry history; inspect the queue record before retrying."
                )
                item.updated_at = utc_now()
                continue
            accounts_query = select(Account.id).where(Account.enabled.is_(True))
            if (
                item.source_provider_url
                and not item.source_provider_url.startswith("m3u:")
                and item.provider_stream_id is None
            ):
                accounts_query = select(Account.id).where(
                    Account.enabled.is_(True),
                    Account.provider_url == item.source_provider_url,
                )
            eligible_accounts = list(db.scalars(accounts_query).all())
            available_account_ids = set(eligible_accounts)
            if not available_account_ids - attempted_account_ids:
                attempted_account_ids = set()
            retry_message = (
                f"Automatic retry {item.retries + 1}/{settings.max_retries} "
                "released to the queue.\n"
            )
            item.status = "PENDING"
            item.account_id = None
            item.attempted_account_ids = json.dumps(sorted(attempted_account_ids))
            item.retry_after = None
            item.error = None
            item.task_log = (item.task_log + retry_message)[-_TASK_LOG_LIMIT:]
            item.task_log_offset += len(retry_message)
            item.updated_at = utc_now()
            released += 1
            logger.info(
                "Queue item %s scheduled as automatic retry %s/%s.",
                item.id,
                item.retries + 1,
                settings.max_retries,
            )
        if due:
            db.commit()
        return released

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
                node = (
                    db.get(Node, item.destination_node_id)
                    if item.destination_node_id is not None
                    else db.get(Node, route.node_id) if route else None
                )
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
                match_existing_series = bool(
                    category
                    and not item.destination_path
                    and item.kind == "series"
                    and category.match_existing_series
                )
                item.remote_node_id = node.id
                item.remote_match_existing_series = match_existing_series
                db.commit()
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
                series_name = item.series_title if item.kind == "series" else None
                source_url = item.source_provider_url or account.provider_url
                series_id = item.series_id or item.provider_stream_id or item.stream_id
                legacy_series_folder = (
                    legacy_identity_segment(
                        clean_content_name(
                            series_name,
                            category.strip_prefixes if category else None,
                        ),
                        series_id,
                        source_url,
                    )
                    if series_name
                    else None
                )
                legacy_series_suffix = (
                    legacy_identity_suffix(series_id, source_url)
                    if series_name
                    else None
                )
                series_key = (
                    series_identity_key(series_id, source_url)
                    if series_name
                    else None
                )
                destination_root = (
                    item.destination_path
                    or (category.destination_path if category else None)
                    or node.watch_path
                )
                series_match_name = item.series_title if match_existing_series else None
                series_match_prefixes = (
                    category.strip_prefixes
                    if category and item.kind == "series"
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
                series_name=series_match_name or series_name,
                series_match_prefixes=series_match_prefixes,
                series_identity=series_key,
                legacy_series_folder=legacy_series_folder,
                legacy_series_suffix=legacy_series_suffix,
                on_log=lambda text: self._append_task_log(
                    item_id, self._redact_task_text(text, stream_url_value, proxy_url)
                ),
                on_command=lambda command: self._set_dispatched_command(
                    item_id, command
                ),
            )
            with SessionLocal() as db:
                item = db.get(QueueItem, item_id)
                if item and item.status != "CANCELLED":
                    item.status = "COMPLETED"
                    item.progress = 100
                    item.final_path = result.final_path
                    if result.relative_path:
                        item.file_name = result.relative_path
                    item.error = None
                    item.last_error = None
                    item.retry_after = None
                    db.commit()
                    should_compact_legacy_folder = bool(
                        result.relative_path
                        and series_name
                        and series_key
                        and legacy_series_folder
                        and legacy_series_suffix
                        and not match_existing_series
                        and item.series_id
                        and not db.scalar(
                            select(QueueItem.id)
                            .where(
                                QueueItem.series_id == item.series_id,
                                QueueItem.source_provider_url
                                == item.source_provider_url,
                                QueueItem.status.in_(
                                    ("PENDING", "DOWNLOADING", "VERIFYING", "CANCELLING")
                                ),
                            )
                            .limit(1)
                        )
                    )
                    series_prefixes = category.strip_prefixes if category else None
                else:
                    should_compact_legacy_folder = False
                    series_prefixes = None
            if should_compact_legacy_folder:
                try:
                    compacted_path = await asyncio.to_thread(
                        self.dispatcher.compact_series_folder,
                        node,
                        destination_root,
                        result.relative_path,
                        series_name,
                        series_key,
                        legacy_series_suffix,
                        series_prefixes,
                    )
                    if compacted_path:
                        with SessionLocal() as db:
                            item = db.get(QueueItem, item_id)
                            if item and item.status == "COMPLETED":
                                old_relative_prefix = PurePosixPath(
                                    result.relative_path
                                ).parts[:-2]
                                new_relative_prefix = PurePosixPath(
                                    compacted_path
                                ).parts[:-2]
                                old_final_prefix = PurePosixPath(
                                    destination_root
                                ).joinpath(*old_relative_prefix).parts
                                new_final_prefix = PurePosixPath(
                                    destination_root
                                ).joinpath(*new_relative_prefix).parts
                                completed_items = db.scalars(
                                    select(QueueItem).where(
                                        QueueItem.kind == "series",
                                        QueueItem.series_id == item.series_id,
                                        QueueItem.source_provider_url
                                        == item.source_provider_url,
                                        QueueItem.remote_node_id == node.id,
                                        QueueItem.status == "COMPLETED",
                                    )
                                ).all()
                                for completed_item in completed_items:
                                    stored_relative = PurePosixPath(
                                        completed_item.file_name
                                    )
                                    if (
                                        stored_relative.parts[
                                            : len(old_relative_prefix)
                                        ]
                                        == old_relative_prefix
                                    ):
                                        completed_item.file_name = str(
                                            PurePosixPath(
                                                *new_relative_prefix,
                                                *stored_relative.parts[
                                                    len(old_relative_prefix) :
                                                ],
                                            )
                                        )
                                    if completed_item.final_path:
                                        stored_final = PurePosixPath(
                                            completed_item.final_path
                                        )
                                        if (
                                            stored_final.parts[
                                                : len(old_final_prefix)
                                            ]
                                            == old_final_prefix
                                        ):
                                            completed_item.final_path = str(
                                                PurePosixPath(
                                                    *new_final_prefix,
                                                    *stored_final.parts[
                                                        len(old_final_prefix) :
                                                    ],
                                                )
                                            )
                                item = db.get(QueueItem, item_id)
                                if item and item.status == "COMPLETED":
                                    item.task_log = (
                                        item.task_log
                                        + "Renamed legacy series folder to a readable title after downloads completed.\n"
                                    )[-_TASK_LOG_LIMIT:]
                                    item.task_log_offset += len(
                                        "Renamed legacy series folder to a readable title after downloads completed.\n"
                                    )
                                db.commit()
                        logger.info(
                            "Renamed series folder for completed task %s to %s.",
                            item_id,
                            compacted_path,
                        )
                except Exception:
                    logger.exception(
                        "Could not rename legacy series folder after task %s completed.",
                        item_id,
                    )
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
    def _redact_task_text(
        text: str, stream_url_value: str, proxy_url: str | None
    ) -> str:
        if stream_url_value:
            text = text.replace(stream_url_value, "[redacted stream URL]")
        if proxy_url:
            text = text.replace(proxy_url, "[redacted proxy URL]")
        return text

    @staticmethod
    def _set_dispatched_command(item_id: int, command: str) -> None:
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if item and item.status in ("DOWNLOADING", "VERIFYING"):
                item.dispatched_command = encrypt_secret(command)
                db.commit()

    @staticmethod
    def _append_task_log(item_id: int, text: str) -> None:
        if not text:
            return
        with SessionLocal() as db:
            item = db.get(QueueItem, item_id)
            if not item:
                return
            item.task_log = (item.task_log + text)[-_TASK_LOG_LIMIT:]
            item.task_log_offset += len(text)
            for line in re.split(r"[\r\n]+", item.task_log[-8192:]):
                if _ERROR_LINE.search(line):
                    item.last_error = line[-4000:]
            item.updated_at = utc_now()
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
                item.last_error = message[-4000:]
                item.retry_after = (
                    None
                    if item.retries >= settings.max_retries
                    else utc_now()
                    + timedelta(seconds=min(3600, 30 * (2 ** max(0, item.retries - 1))))
                )
                db.commit()
