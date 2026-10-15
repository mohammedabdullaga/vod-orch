import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from starlette.responses import JSONResponse

from app.config import settings
from app.auth import (
    COOKIE_NAME,
    authenticate,
    check_login_limit,
    clear_login_failures,
    create_session,
    read_session,
    record_login_failure,
    safe_csrf_match,
    validate_auth_settings,
)
from app.models import (
    Account,
    AppSettings,
    M3UPlaylist,
    MonitoredCategory,
    Node,
    Proxy,
    QueueItem,
    SessionLocal,
    StorageRoute,
    init_db,
    utc_now,
)
from app.services.provider_client import (
    cached_provider_action,
    resolve_provider_proxy,
    resolve_provider_proxy_selection,
)
from app.security import decrypt_secret, encrypt_secret, mask_secret
from app.services.dispatcher import RemoteDispatcher
from app.services.poller import CategoryPoller
from app.services.worker_pool import WorkerPool
from app.routers.content import (
    playlist_router,
    queue_router,
    router as content_router,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
dispatcher = RemoteDispatcher(
    settings.known_hosts_path,
    settings.remote_job_timeout_seconds,
)
poller = CategoryPoller()
worker_pool = WorkerPool(dispatcher)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not settings.encryption_key:
        raise RuntimeError("Set APP_ENCRYPTION_KEY before starting the API.")
    validate_auth_settings()
    init_db()
    await poller.start()
    await worker_pool.start()
    yield
    await worker_pool.stop()
    await poller.stop()


app = FastAPI(title="Media Ingestion Engine", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
    expose_headers=["X-Provider-Catalog-Cache"],
    allow_credentials=True,
)
app.state.worker_pool = worker_pool
app.state.dispatcher = dispatcher
app.include_router(content_router)
app.include_router(queue_router)
app.include_router(playlist_router)


@app.middleware("http")
async def require_admin_session(request: Request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/") or request.url.path in (
        "/api/auth/login",
        "/api/auth/session",
    ):
        return await call_next(request)

    session = read_session(request.cookies.get(COOKIE_NAME))
    if not session:
        return JSONResponse(status_code=401, content={"detail": "Admin login required."})
    if request.method not in ("GET", "HEAD", "OPTIONS") and not safe_csrf_match(
        session["csrf"], request.headers.get("X-CSRF-Token")
    ):
        return JSONResponse(status_code=403, content={"detail": "CSRF token is missing or invalid."})
    return await call_next(request)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=1024)


@app.get("/api/auth/session")
def get_auth_session(request: Request):
    session = read_session(request.cookies.get(COOKIE_NAME))
    if not session:
        return {"authenticated": False}
    return {
        "authenticated": True,
        "username": session["username"],
        "csrf_token": session["csrf"],
    }


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    client_key = request.client.host if request.client else "unknown"
    retry_after = check_login_limit(client_key)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )
    if not authenticate(payload.username, payload.password):
        record_login_failure(client_key)
        raise HTTPException(status_code=401, detail="Invalid admin username or password.")
    clear_login_failures(client_key)
    token, csrf_token = create_session()
    response = JSONResponse({
        "authenticated": True,
        "username": settings.admin_username,
        "csrf_token": csrf_token,
    })
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


class AccountCreate(BaseModel):
    provider_url: HttpUrl
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)
    user_agent: str = Field(default="Mozilla", max_length=1024)


class ProxyCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ProxyEnabled(BaseModel):
    enabled: bool


class ProviderProxyUpdate(BaseModel):
    proxy_id: int | None = Field(default=None, ge=1)


class CategoryToggle(BaseModel):
    category_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=512)
    kind: Literal["series", "vod"]
    monitored: bool
    destination_path: str | None = Field(default=None, max_length=4096)
    match_existing_series: bool | None = None
    strip_prefixes: str | None = Field(default=None, max_length=1024)


class SSHSettings(BaseModel):
    name: str = Field(default="Storage server", min_length=1, max_length=255)
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    private_key_path: str
    staging_path: str
    watch_path: str
    proxy_id: int | None = Field(default=None, ge=1)
    axel_connections: int = Field(
        default=settings.default_axel_connections, ge=1, le=12
    )
    test: bool = False


class StorageRouting(BaseModel):
    series_node_id: int | None = Field(default=None, ge=1)
    movie_node_id: int | None = Field(default=None, ge=1)


def _validated_remote_path(
    path: str | None, field_name: str, allow_none: bool = False
) -> str | None:
    if path is None or not path.strip():
        if allow_none:
            return None
        raise HTTPException(status_code=422, detail=f"{field_name} must be an absolute remote path.")
    if "\x00" in path:
        raise HTTPException(status_code=422, detail=f"{field_name} contains an invalid character.")
    normalized = PurePosixPath(path.strip())
    if not normalized.is_absolute() or ".." in normalized.parts or normalized == PurePosixPath("/"):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} must be an absolute path other than / and cannot contain '..'.",
        )
    return str(normalized)


def _account_json(account: Account) -> dict:
    return {
        "id": account.id, "provider_url": account.provider_url, "username": account.username,
        "password": mask_secret(decrypt_secret(account.password_encrypted)),
        "user_agent": account.user_agent, "enabled": account.enabled,
    }


def _node_json(node: Node) -> dict:
    return {
        "id": node.id, "name": node.name, "host": node.host, "port": node.port, "username": node.username,
        "private_key_path": node.private_key_path, "staging_path": node.staging_path,
        "watch_path": node.watch_path, "proxy_id": node.proxy_id,
        "axel_connections": node.axel_connections,
        "last_connected_at": node.last_connected_at.isoformat() if node.last_connected_at else None,
    }


@app.get("/api/status")
def get_status():
    with SessionLocal() as db:
        counts = dict(
            db.query(QueueItem.status, func.count(QueueItem.id))
            .where(QueueItem.hidden_from_dashboard.is_(False))
            .group_by(QueueItem.status)
            .all()
        )
        routed_nodes = db.scalars(
            select(Node)
            .join(StorageRoute, StorageRoute.node_id == Node.id)
            .distinct()
        ).all()
        accounts = db.scalar(select(func.count(Account.id)).where(Account.enabled.is_(True))) or 0
    ssh_connected = bool(routed_nodes) and all(
        node.last_connected_at
        and (utc_now() - node.last_connected_at.replace(tzinfo=timezone.utc)).total_seconds() < 300
        for node in routed_nodes
    )
    return {
        "daemon_running": True, "ssh_connected": ssh_connected, "active_workers": worker_pool.active_count,
        "available_accounts": accounts,
        "default_axel_connections": settings.default_axel_connections,
        "queue": {
            "pending": counts.get("PENDING", 0), "downloading": counts.get("DOWNLOADING", 0),
            "verifying": counts.get("VERIFYING", 0), "completed": counts.get("COMPLETED", 0),
            "failed": counts.get("FAILED", 0),
            "cancelled": counts.get("CANCELLED", 0),
            "cancelling": counts.get("CANCELLING", 0),
        },
    }


@app.get("/api/categories")
async def get_categories(
    response: Response,
    proxy_id: int | None = Query(default=None, ge=0),
    refresh: bool = Query(default=False),
):
    with SessionLocal() as db:
        account = db.scalar(select(Account).where(Account.enabled.is_(True)).order_by(Account.id).limit(1))
        states = {
            (item.kind, item.category_id): item
            for item in db.scalars(select(MonitoredCategory)).all()
        }
    if not account:
        raise HTTPException(status_code=409, detail="Add an enabled provider account first.")
    try:
        proxy_url = resolve_provider_proxy_selection(proxy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        result = []
        for kind, action in (("series", "get_series_categories"), ("vod", "get_vod_categories")):
            rows, cache_state = await cached_provider_action(
                account,
                action,
                proxy_url=proxy_url,
                force_refresh=refresh,
            )
            if (
                response.headers.get("X-Provider-Catalog-Cache") != "stale"
                or cache_state == "stale"
            ):
                response.headers["X-Provider-Catalog-Cache"] = cache_state
            for row in rows:
                category_id = str(row.get("category_id", row.get("id", "")))
                state = states.get((kind, category_id))
                result.append({
                    "category_id": category_id, "name": row.get("category_name", row.get("name", "Unknown")),
                    "kind": kind, "monitored": bool(state.monitored) if state else False,
                    "destination_path": state.destination_path if state else None,
                    "match_existing_series": bool(state.match_existing_series) if state else False,
                    "strip_prefixes": state.strip_prefixes if state else "",
                })
        return result
    except Exception as exc:
        logger.error("Could not fetch provider categories (%s)", type(exc).__name__)
        raise HTTPException(status_code=502, detail="Provider category request failed.") from exc


@app.post("/api/categories/toggle")
def toggle_category(payload: CategoryToggle):
    with SessionLocal() as db:
        item = db.scalar(select(MonitoredCategory).where(
            MonitoredCategory.category_id == payload.category_id,
            MonitoredCategory.kind == payload.kind,
        ))
        if not item:
            item = MonitoredCategory(category_id=payload.category_id, name=payload.name, kind=payload.kind)
            db.add(item)
        item.name = payload.name
        item.kind = payload.kind
        item.monitored = payload.monitored
        if "destination_path" in payload.model_fields_set:
            item.destination_path = _validated_remote_path(
                payload.destination_path, "Category destination", allow_none=True
            )
        if payload.match_existing_series is not None:
            if payload.match_existing_series and payload.kind != "series":
                raise HTTPException(
                    status_code=422,
                    detail="Existing-folder matching is only available for series categories.",
                )
            item.match_existing_series = payload.match_existing_series
        if payload.strip_prefixes is not None:
            item.strip_prefixes = payload.strip_prefixes.strip()
        resolved_destination = (
            item.destination_path
            if "destination_path" not in payload.model_fields_set
            else payload.destination_path
        )
        if item.match_existing_series and not resolved_destination:
            raise HTTPException(
                status_code=422,
                detail="Set a custom category destination path before enabling series-folder matching.",
            )
        db.commit()
    return {
        "ok": True,
        "category_id": payload.category_id,
        "monitored": payload.monitored,
        "destination_path": item.destination_path,
        "match_existing_series": item.match_existing_series,
        "strip_prefixes": item.strip_prefixes,
    }


@app.get("/api/queue")
def get_queue():
    with SessionLocal() as db:
        rows = db.execute(
            select(QueueItem, Account.username)
            .outerjoin(Account, QueueItem.account_id == Account.id)
            .where(QueueItem.hidden_from_dashboard.is_(False))
            .order_by(QueueItem.created_at.desc())
            .limit(500)
        ).all()
    return [{
        "id": item.id, "stream_id": item.provider_stream_id or item.stream_id,
        "title": item.title, "kind": item.kind,
        "category_id": item.category_id, "category_name": item.category_name,
        "series_id": item.series_id,
        "series_title": item.series_title, "season": item.season, "episode": item.episode,
        "status": item.status, "progress": item.progress, "account": username,
        "file_size_bytes": item.file_size_bytes,
        "speed_bytes_per_second": item.speed_bytes_per_second,
        "final_path": item.final_path,
        "error": item.error, "retries": item.retries,
    } for item, username in rows]


@app.post("/api/queue/clear-completed")
def clear_completed_queue():
    with SessionLocal() as db:
        result = db.execute(
            update(QueueItem)
            .where(
                QueueItem.status == "COMPLETED",
                QueueItem.hidden_from_dashboard.is_(False),
            )
            .values(hidden_from_dashboard=True)
        )
        cleared_count = result.rowcount or 0
        db.commit()
    return {"ok": True, "cleared_count": cleared_count}


@app.post("/api/queue/stop-all-and-clear")
async def stop_all_and_clear_queue():
    try:
        return await worker_pool.stop_all_and_clear()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc


class RetryRequest(BaseModel):
    item_id: int


@app.post("/api/queue/retry")
def retry_item(payload: RetryRequest):
    with SessionLocal() as db:
        item = db.get(QueueItem, payload.item_id)
        if not item:
            raise HTTPException(status_code=404, detail="Queue item not found.")
        if item.status != "FAILED":
            raise HTTPException(status_code=409, detail="Only failed queue items can be retried.")
        item.status = "PENDING"
        item.account_id = None
        item.file_size_bytes = None
        item.speed_bytes_per_second = None
        item.progress = 0
        item.retry_after = None
        item.error = None
        item.retries = 0
        db.commit()
    worker_pool.wake()
    return {"ok": True}


@app.post("/api/queue/{item_id}/cancel")
def cancel_queue_item(item_id: int):
    try:
        status = worker_pool.cancel(item_id)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Queue item not found." else 409
        raise HTTPException(status_code=status_code, detail=detail) from exc
    return {"ok": True, "status": status}


@app.get("/api/accounts")
def list_accounts():
    with SessionLocal() as db:
        return [_account_json(item) for item in db.scalars(select(Account).order_by(Account.id)).all()]


@app.get("/api/accounts/availability")
def get_account_availability(
    content_type: Literal["SERIES", "MOVIE"],
    source_account_id: int | None = None,
):
    kind = "series" if content_type == "SERIES" else "vod"
    with SessionLocal() as db:
        source_account = (
            db.get(Account, source_account_id)
            if source_account_id is not None
            else db.scalar(
                select(Account)
                .where(Account.enabled.is_(True))
                .order_by(Account.id)
                .limit(1)
            )
        )
        if not source_account or not source_account.enabled:
            raise HTTPException(status_code=409, detail="Add an enabled provider account first.")
        accounts = db.scalars(
            select(Account)
            .where(Account.enabled.is_(True))
            .order_by(Account.id)
        ).all()
        account_ids = [account.id for account in accounts]
        busy_ids = set()
        if account_ids:
            busy_ids = set(db.scalars(
                select(QueueItem.account_id).where(
                    QueueItem.account_id.in_(account_ids),
                    QueueItem.status.in_(
                        ("PENDING", "DOWNLOADING", "VERIFYING", "CANCELLING")
                    ),
                ).distinct()
            ).all())
        route = db.get(StorageRoute, kind)
        node = db.get(Node, route.node_id) if route else None
        assigned_proxy = db.get(Proxy, node.proxy_id) if node and node.proxy_id else None
        enabled_proxies = db.scalars(
            select(Proxy).where(Proxy.enabled.is_(True)).order_by(Proxy.id)
        ).all()
    return {
        "provider_url": source_account.provider_url,
        "proxy_available": bool(enabled_proxies),
        "assigned_proxy_id": (
            assigned_proxy.id if assigned_proxy and assigned_proxy.enabled else None
        ),
        "proxies": [
            {
                "id": proxy.id,
                "url": mask_secret(decrypt_secret(proxy.url_encrypted)),
            }
            for proxy in enabled_proxies
        ],
        "accounts": [
            {
                "id": account.id,
                "username": account.username,
                "provider_url": account.provider_url,
                "free": account.id not in busy_ids,
            }
            for account in accounts
        ],
    }


@app.post("/api/accounts")
def add_account(payload: AccountCreate):
    with SessionLocal() as db:
        provider_url = str(payload.provider_url).rstrip("/")
        exists = db.scalar(select(Account.id).where(
            Account.provider_url == provider_url,
            Account.username == payload.username,
        ))
        if exists:
            raise HTTPException(status_code=409, detail="This provider account is already configured.")
        account = Account(
            provider_url=provider_url, username=payload.username,
            password_encrypted=encrypt_secret(payload.password), user_agent=payload.user_agent,
        )
        db.add(account)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail="This provider account is already configured.") from exc
        db.refresh(account)
        result = _account_json(account)
    worker_pool.wake()
    return result


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int):
    with SessionLocal() as db:
        account = db.get(Account, account_id)
        if not account:
            raise HTTPException(status_code=404, detail="Account not found.")
        active = db.scalar(select(func.count(QueueItem.id)).where(
            QueueItem.account_id == account_id, QueueItem.status.in_(("DOWNLOADING", "VERIFYING"))
        ))
        if active:
            raise HTTPException(status_code=409, detail="Account has an active download.")
        db.delete(account)
        db.commit()
    return {"ok": True}


@app.get("/api/proxies")
def list_proxies():
    with SessionLocal() as db:
        rows = db.scalars(select(Proxy).order_by(Proxy.id)).all()
    return [{"id": item.id, "url": mask_secret(decrypt_secret(item.url_encrypted)), "enabled": item.enabled} for item in rows]


@app.get("/api/settings/provider-proxy")
def get_provider_proxy_setting():
    with SessionLocal() as db:
        setting = db.get(AppSettings, 1)
        return {"proxy_id": setting.provider_proxy_id if setting else None}


@app.put("/api/settings/provider-proxy")
def update_provider_proxy_setting(payload: ProviderProxyUpdate):
    with SessionLocal() as db:
        if payload.proxy_id is not None:
            proxy = db.get(Proxy, payload.proxy_id)
            if not proxy or not proxy.enabled:
                raise HTTPException(
                    status_code=404,
                    detail="The selected provider API proxy is unavailable or disabled.",
                )
        setting = db.get(AppSettings, 1)
        if setting is None:
            setting = AppSettings(id=1)
            db.add(setting)
        setting.provider_proxy_id = payload.proxy_id
        db.commit()
    return {"proxy_id": payload.proxy_id}


@app.post("/api/proxies")
def add_proxy(payload: ProxyCreate):
    with SessionLocal() as db:
        proxy = Proxy(url_encrypted=encrypt_secret(payload.url))
        db.add(proxy)
        db.commit()
        db.refresh(proxy)
        return {"id": proxy.id, "url": mask_secret(payload.url), "enabled": proxy.enabled}


@app.put("/api/proxies/{proxy_id}/enabled")
def set_proxy_enabled(proxy_id: int, payload: ProxyEnabled):
    with SessionLocal() as db:
        proxy = db.get(Proxy, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found.")
        proxy.enabled = payload.enabled
        if not payload.enabled:
            setting = db.get(AppSettings, 1)
            if setting and setting.provider_proxy_id == proxy_id:
                setting.provider_proxy_id = None
        db.commit()
    return {"ok": True, "id": proxy_id, "enabled": payload.enabled}


@app.delete("/api/proxies/{proxy_id}")
def delete_proxy(proxy_id: int):
    with SessionLocal() as db:
        proxy = db.get(Proxy, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found.")
        assigned_playlists = db.scalar(
            select(func.count(M3UPlaylist.id)).where(M3UPlaylist.proxy_id == proxy_id)
        )
        if assigned_playlists:
            raise HTTPException(
                status_code=409,
                detail="This proxy is assigned to an M3U playlist. Reassign that playlist before deleting the proxy.",
            )
        db.delete(proxy)
        db.commit()
    return {"ok": True}


@app.post("/api/proxies/{proxy_id}/test")
async def test_proxy(proxy_id: int):
    with SessionLocal() as db:
        proxy = db.get(Proxy, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found.")
        proxy_url = decrypt_secret(proxy.url_encrypted)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(proxy=proxy_url, timeout=10) as client:
            response = await client.get("https://api.ipify.org?format=json")
            response.raise_for_status()
            address = response.json().get("ip")
    except Exception as exc:
        logger.exception("Proxy test failed for proxy %s", proxy_id)
        raise HTTPException(status_code=502, detail="Proxy test failed; verify proxy URL and connectivity.") from exc
    return {"ip": address, "latency_ms": round((time.perf_counter() - started) * 1000)}


@app.get("/api/settings/ssh")
def get_ssh_settings():
    with SessionLocal() as db:
        node = db.scalar(select(Node).order_by(Node.id).limit(1))
    return _node_json(node) if node else None


@app.post("/api/settings/ssh")
def save_ssh_settings(payload: SSHSettings):
    staging_path = _validated_remote_path(payload.staging_path, "Staging directory")
    watch_path = _validated_remote_path(payload.watch_path, "Storage root")
    with SessionLocal() as db:
        if payload.proxy_id is not None and not db.get(Proxy, payload.proxy_id):
            raise HTTPException(status_code=404, detail="Selected proxy not found.")
        node = db.scalar(select(Node).order_by(Node.id).limit(1))
        if not node:
            node = Node(
                name=payload.name,
                host=payload.host, port=payload.port, username=payload.username,
                private_key_path=payload.private_key_path, staging_path=staging_path,
                watch_path=watch_path, proxy_id=payload.proxy_id,
                axel_connections=payload.axel_connections,
            )
            db.add(node)
        else:
            node.name = payload.name
            node.host, node.port, node.username = payload.host, payload.port, payload.username
            node.private_key_path = payload.private_key_path
            node.staging_path, node.watch_path = staging_path, watch_path
            node.proxy_id = payload.proxy_id
            node.axel_connections = payload.axel_connections
        db.flush()
        for kind in ("series", "vod"):
            route = db.get(StorageRoute, kind)
            if route is None:
                db.add(StorageRoute(kind=kind, node_id=node.id))
        if payload.test:
            try:
                dispatcher.test_connection(node)
            except Exception as exc:
                logger.exception("SSH connection test failed")
                raise HTTPException(status_code=502, detail=f"SSH connection failed: {exc}") from exc
            node.last_connected_at = datetime.now(timezone.utc)
        db.commit()
        return {"ok": True, **_node_json(node)}


@app.get("/api/nodes")
def list_storage_nodes():
    with SessionLocal() as db:
        nodes = db.scalars(select(Node).order_by(Node.id)).all()
    return [_node_json(node) for node in nodes]


@app.post("/api/nodes")
def add_storage_node(payload: SSHSettings):
    staging_path = _validated_remote_path(payload.staging_path, "Staging directory")
    watch_path = _validated_remote_path(payload.watch_path, "Storage root")
    with SessionLocal() as db:
        if payload.proxy_id is not None and not db.get(Proxy, payload.proxy_id):
            raise HTTPException(status_code=404, detail="Selected proxy not found.")
        node = Node(
            name=payload.name,
            host=payload.host,
            port=payload.port,
            username=payload.username,
            private_key_path=payload.private_key_path,
            staging_path=staging_path,
            watch_path=watch_path,
            proxy_id=payload.proxy_id,
            axel_connections=payload.axel_connections,
        )
        if payload.test:
            try:
                dispatcher.test_connection(node)
            except Exception as exc:
                logger.exception("SSH connection test failed for new node")
                raise HTTPException(status_code=502, detail=f"SSH connection failed: {exc}") from exc
            node.last_connected_at = datetime.now(timezone.utc)
        db.add(node)
        db.commit()
        db.refresh(node)
        return _node_json(node)


@app.put("/api/nodes/{node_id}")
def update_storage_node(node_id: int, payload: SSHSettings):
    staging_path = _validated_remote_path(payload.staging_path, "Staging directory")
    watch_path = _validated_remote_path(payload.watch_path, "Storage root")
    with SessionLocal() as db:
        node = db.get(Node, node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Storage node not found.")
        if payload.proxy_id is not None and not db.get(Proxy, payload.proxy_id):
            raise HTTPException(status_code=404, detail="Selected proxy not found.")
        node.name = payload.name
        node.host, node.port, node.username = payload.host, payload.port, payload.username
        node.private_key_path = payload.private_key_path
        node.staging_path, node.watch_path = staging_path, watch_path
        node.proxy_id = payload.proxy_id
        node.axel_connections = payload.axel_connections
        if payload.test:
            try:
                dispatcher.test_connection(node)
            except Exception as exc:
                logger.exception("SSH connection test failed for node %s", node_id)
                raise HTTPException(status_code=502, detail=f"SSH connection failed: {exc}") from exc
            node.last_connected_at = datetime.now(timezone.utc)
        db.commit()
        return _node_json(node)


@app.delete("/api/nodes/{node_id}")
def delete_storage_node(node_id: int):
    with SessionLocal() as db:
        node = db.get(Node, node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Storage node not found.")
        assignments = db.scalars(
            select(StorageRoute).where(StorageRoute.node_id == node_id)
        ).all()
        if assignments:
            kinds = ", ".join("movies" if route.kind == "vod" else "series" for route in assignments)
            raise HTTPException(
                status_code=409,
                detail=f"Reassign {kinds} storage before removing this node.",
            )
        db.delete(node)
        db.commit()
    return {"ok": True}


@app.get("/api/settings/storage-routing")
def get_storage_routing():
    with SessionLocal() as db:
        routes = db.scalars(select(StorageRoute)).all()
    assignments = {route.kind: route.node_id for route in routes}
    return {"series_node_id": assignments.get("series"), "movie_node_id": assignments.get("vod")}


@app.put("/api/settings/storage-routing")
def save_storage_routing(payload: StorageRouting):
    assignments = {"series": payload.series_node_id, "vod": payload.movie_node_id}
    with SessionLocal() as db:
        for kind, node_id in assignments.items():
            route = db.get(StorageRoute, kind)
            if node_id is not None and not db.get(Node, node_id):
                raise HTTPException(status_code=404, detail=f"Storage node {node_id} not found.")
            if node_id is None:
                if route:
                    db.delete(route)
            elif route is None:
                db.add(StorageRoute(kind=kind, node_id=node_id))
            else:
                route.node_id = node_id
                route.updated_at = utc_now()
        db.commit()
    return {
        "series_node_id": payload.series_node_id,
        "movie_node_id": payload.movie_node_id,
    }


@app.get("/health")
def health():
    return {"ok": True}
