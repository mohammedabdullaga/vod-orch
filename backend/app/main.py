import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import httpx
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import Account, MonitoredCategory, Node, Proxy, QueueItem, SessionLocal, init_db, utc_now
from app.provider import provider_action
from app.security import decrypt_secret, encrypt_secret, mask_secret
from app.services.dispatcher import RemoteDispatcher
from app.services.poller import CategoryPoller
from app.services.worker_pool import WorkerPool

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
    allow_headers=["*"],
)


class AccountCreate(BaseModel):
    provider_url: HttpUrl
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)
    user_agent: str = Field(default="Mozilla/5.0", max_length=1024)


class ProxyCreate(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class CategoryToggle(BaseModel):
    category_id: str
    name: str
    kind: Literal["series", "vod"]
    monitored: bool


class SSHSettings(BaseModel):
    host: str
    port: int = Field(default=22, ge=1, le=65535)
    username: str
    private_key_path: str
    staging_path: str
    watch_path: str
    proxy_id: int | None = None
    test: bool = False


def _account_json(account: Account) -> dict:
    return {
        "id": account.id, "provider_url": account.provider_url, "username": account.username,
        "password": mask_secret(decrypt_secret(account.password_encrypted)),
        "user_agent": account.user_agent, "enabled": account.enabled,
    }


def _node_json(node: Node) -> dict:
    return {
        "host": node.host, "port": node.port, "username": node.username,
        "private_key_path": node.private_key_path, "staging_path": node.staging_path,
        "watch_path": node.watch_path, "proxy_id": node.proxy_id,
        "last_connected_at": node.last_connected_at.isoformat() if node.last_connected_at else None,
    }


@app.get("/api/status")
def get_status():
    with SessionLocal() as db:
        counts = dict(db.query(QueueItem.status, func.count(QueueItem.id)).group_by(QueueItem.status).all())
        node = db.scalar(select(Node).order_by(Node.id).limit(1))
        accounts = db.scalar(select(func.count(Account.id)).where(Account.enabled.is_(True))) or 0
    ssh_connected = bool(node and node.last_connected_at and
                         (utc_now() - node.last_connected_at.replace(tzinfo=timezone.utc)).total_seconds() < 300)
    return {
        "daemon_running": True, "ssh_connected": ssh_connected, "active_workers": worker_pool.active_count,
        "available_accounts": accounts, "queue": {
            "pending": counts.get("PENDING", 0), "downloading": counts.get("DOWNLOADING", 0),
            "verifying": counts.get("VERIFYING", 0), "completed": counts.get("COMPLETED", 0),
            "failed": counts.get("FAILED", 0),
        },
    }


@app.get("/api/categories")
async def get_categories():
    with SessionLocal() as db:
        account = db.scalar(select(Account).where(Account.enabled.is_(True)).order_by(Account.id).limit(1))
        states = {
            (item.kind, item.category_id): item
            for item in db.scalars(select(MonitoredCategory)).all()
        }
    if not account:
        raise HTTPException(status_code=409, detail="Add an enabled provider account first.")
    try:
        result = []
        for kind, action in (("series", "get_series_categories"), ("vod", "get_vod_categories")):
            for row in await provider_action(account, action):
                category_id = str(row.get("category_id", row.get("id", "")))
                state = states.get((kind, category_id))
                result.append({
                    "category_id": category_id, "name": row.get("category_name", row.get("name", "Unknown")),
                    "kind": kind, "monitored": bool(state.monitored) if state else False,
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
        db.commit()
    return {"ok": True, "category_id": payload.category_id, "monitored": payload.monitored}


@app.get("/api/queue")
def get_queue():
    with SessionLocal() as db:
        rows = db.execute(
            select(QueueItem, Account.username)
            .outerjoin(Account, QueueItem.account_id == Account.id)
            .order_by(QueueItem.created_at.desc())
            .limit(500)
        ).all()
    return [{
        "id": item.id, "stream_id": item.stream_id, "title": item.title, "kind": item.kind,
        "series_title": item.series_title, "season": item.season, "episode": item.episode,
        "status": item.status, "progress": item.progress, "account": username,
        "error": item.error, "retries": item.retries,
    } for item, username in rows]


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
        item.retry_after = None
        item.error = None
        item.retries = 0
        db.commit()
    return {"ok": True}


@app.get("/api/accounts")
def list_accounts():
    with SessionLocal() as db:
        return [_account_json(item) for item in db.scalars(select(Account).order_by(Account.id)).all()]


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
        return _account_json(account)


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


@app.post("/api/proxies")
def add_proxy(payload: ProxyCreate):
    with SessionLocal() as db:
        proxy = Proxy(url_encrypted=encrypt_secret(payload.url))
        db.add(proxy)
        db.commit()
        db.refresh(proxy)
        return {"id": proxy.id, "url": mask_secret(payload.url), "enabled": proxy.enabled}


@app.delete("/api/proxies/{proxy_id}")
def delete_proxy(proxy_id: int):
    with SessionLocal() as db:
        proxy = db.get(Proxy, proxy_id)
        if not proxy:
            raise HTTPException(status_code=404, detail="Proxy not found.")
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
    with SessionLocal() as db:
        node = db.scalar(select(Node).order_by(Node.id).limit(1))
        if not node:
            node = Node(
                host=payload.host, port=payload.port, username=payload.username,
                private_key_path=payload.private_key_path, staging_path=payload.staging_path,
                watch_path=payload.watch_path, proxy_id=payload.proxy_id,
            )
            db.add(node)
        else:
            node.host, node.port, node.username = payload.host, payload.port, payload.username
            node.private_key_path = payload.private_key_path
            node.staging_path, node.watch_path = payload.staging_path, payload.watch_path
            node.proxy_id = payload.proxy_id
        if payload.test:
            try:
                dispatcher.test_connection(node)
            except Exception as exc:
                logger.exception("SSH connection test failed")
                raise HTTPException(status_code=502, detail=f"SSH connection failed: {exc}") from exc
            node.last_connected_at = datetime.now(timezone.utc)
        db.commit()
        return {"ok": True, **_node_json(node)}


@app.get("/health")
def health():
    return {"ok": True}
