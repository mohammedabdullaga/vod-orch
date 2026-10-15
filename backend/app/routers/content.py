import logging
import re
import shlex
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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
from app.provider import provider_action, stream_url
from app.security import decrypt_secret, encrypt_secret
from app.services.content_paths import (
    clean_content_name,
    normalized_episode_path,
    safe_extension,
    season_number as parse_season_number,
    stream_identity,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/content", tags=["content"])
queue_router = APIRouter(prefix="/api/queue", tags=["queue"])


def _provider_account(account_id: int | None) -> Account:
    with SessionLocal() as db:
        account = (
            db.get(Account, account_id)
            if account_id is not None
            else db.scalar(
                select(Account)
                .where(Account.enabled.is_(True))
                .order_by(Account.id)
                .limit(1)
            )
        )
        if not account or not account.enabled:
            raise HTTPException(
                status_code=404 if account_id is not None else 409,
                detail=(
                    "The selected Xtream account is not available."
                    if account_id is not None
                    else "Add an enabled provider account first."
                ),
            )
        return account


def _provider_rows(value: Any, action: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        logger.error("Provider returned an invalid response for action %s", action)
        raise HTTPException(status_code=502, detail=f"Provider returned invalid {action} data.")
    return value


async def _request_provider(account: Account, action: str, **params: Any) -> Any:
    try:
        return await provider_action(account, action, **params)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Provider request %s failed (%s)", action, type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"Provider request {action} failed.") from exc


def _provider_kind(content_type: str) -> tuple[str, str, str]:
    if content_type == "series":
        return "series", "get_series_categories", "get_series"
    return "vod", "get_vod_categories", "get_vod_streams"


@router.get("/categories")
async def get_content_categories(
    type: Literal["series", "movie"] = Query(...),
    account_id: int | None = Query(default=None, ge=1),
) -> list[dict[str, Any]]:
    kind, action, _ = _provider_kind(type)
    account = _provider_account(account_id)
    rows = _provider_rows(await _request_provider(account, action), action)
    with SessionLocal() as db:
        states = {
            item.category_id: item
            for item in db.scalars(
                select(MonitoredCategory).where(MonitoredCategory.kind == kind)
            ).all()
        }
    result = []
    for row in rows:
        category_id = row.get("category_id", row.get("id"))
        if category_id is None:
            continue
        category_id = str(category_id)
        state = states.get(category_id)
        name = str(row.get("category_name", row.get("name", "Unknown")))
        result.append({
            "category_id": category_id,
            "category_name": name,
            "name": name,
            "kind": kind,
            "monitored": bool(state.monitored) if state else False,
            "destination_path": state.destination_path if state else None,
            "match_existing_series": bool(state.match_existing_series) if state else False,
            "strip_prefixes": state.strip_prefixes if state else "",
        })
    return result


@router.get("/items")
async def get_content_items(
    type: Literal["series", "movie"] = Query(...),
    category_ids: str = Query(..., min_length=1, max_length=5000),
    account_id: int | None = Query(default=None, ge=1),
) -> list[dict[str, Any]]:
    kind, _, items_action = _provider_kind(type)
    ids = list(dict.fromkeys(part.strip() for part in category_ids.split(",") if part.strip()))
    if not ids or len(ids) > 50:
        raise HTTPException(status_code=422, detail="Select between 1 and 50 category IDs.")
    account = _provider_account(account_id)
    categories_response = await _request_provider(
        account, "get_series_categories" if kind == "series" else "get_vod_categories"
    )
    category_rows = _provider_rows(
        categories_response,
        "get_series_categories" if kind == "series" else "get_vod_categories",
    )
    category_names = {
        str(row.get("category_id", row.get("id"))): str(
            row.get("category_name", row.get("name", "Unknown"))
        )
        for row in category_rows
        if row.get("category_id", row.get("id")) is not None
    }
    unknown_ids = [category_id for category_id in ids if category_id not in category_names]
    if unknown_ids:
        raise HTTPException(status_code=404, detail=f"Unknown provider category IDs: {', '.join(unknown_ids)}")

    result: list[dict[str, Any]] = []
    seen_item_ids: set[str] = set()
    for category_id in ids:
        rows = _provider_rows(
            await _request_provider(account, items_action, category_id=category_id),
            items_action,
        )
        for row in rows:
            item_id = row.get("series_id", row.get("stream_id", row.get("id")))
            if item_id is None:
                continue
            item_id = str(item_id)
            if item_id in seen_item_ids:
                continue
            seen_item_ids.add(item_id)
            seasons = row.get("seasons")
            if isinstance(seasons, dict):
                season_count = len(seasons)
            elif isinstance(seasons, list):
                season_count = len(seasons)
            else:
                season_count = _integer_or_none(row.get("season_count"))
            episode_count = _integer_or_none(
                row.get("episode_count", row.get("episodes_count"))
            )
            if episode_count is None:
                episode_rows = row.get("episodes")
                if isinstance(episode_rows, list):
                    episode_count = len(episode_rows)
                elif isinstance(episode_rows, dict):
                    episode_count = sum(
                        len(episodes) for episodes in episode_rows.values()
                        if isinstance(episodes, list)
                    )
            result.append({
                "id": item_id,
                "name": str(row.get("name") or row.get("title") or f"Media {item_id}"),
                "category_id": category_id,
                "category_name": category_names[category_id],
                "kind": kind,
                "cover": row.get("cover") or row.get("stream_icon"),
                "total_seasons": season_count if kind == "series" else None,
                "total_episodes": episode_count if kind == "series" else None,
                "year": _media_year(row),
                "container_ext": safe_extension(str(row.get("container_extension") or "mkv")),
            })
    return result


@router.get("/series/{series_id}/details")
async def get_series_details(
    series_id: str,
    account_id: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    if not series_id or len(series_id) > 512:
        raise HTTPException(status_code=422, detail="Invalid series ID.")
    account = _provider_account(account_id)
    info = await _request_provider(account, "get_series_info", series_id=series_id)
    if not isinstance(info, dict):
        raise HTTPException(status_code=502, detail="Provider returned invalid series details.")

    raw_episodes = info.get("episodes", {})
    if not isinstance(raw_episodes, dict):
        raise HTTPException(status_code=502, detail="Provider returned an invalid episode list.")
    series_metadata = info.get("info")
    if not isinstance(series_metadata, dict):
        series_metadata = {}
    seasons: dict[str, list[dict[str, Any]]] = {}
    for season_key, entries in raw_episodes.items():
        if not isinstance(entries, list):
            logger.warning("Ignoring malformed episode list for series %s season %s", series_id, season_key)
            continue
        season_value = parse_season_number(season_key)
        normalized = []
        for position, episode in enumerate(entries, start=1):
            if not isinstance(episode, dict):
                continue
            episode_id = episode.get("id", episode.get("stream_id"))
            if episode_id is None:
                continue
            episode_num = _integer_or_none(episode.get("episode_num")) or position
            episode_season = parse_season_number(episode.get("season"))
            if episode_season is None:
                episode_season = season_value
            extension = safe_extension(str(episode.get("container_extension") or "mkv"))
            normalized.append({
                "episode_num": episode_num,
                "id": str(episode_id),
                "ext": extension,
                "title": str(episode.get("title") or f"S{episode_season or 0:02d}E{episode_num:02d}"),
                "season": episode_season,
            })
        normalized.sort(key=lambda item: item["episode_num"])
        seasons[str(season_key)] = normalized
    return {
        "series_id": str(info.get("series_id", series_id)),
        "name": str(
            info.get("name")
            or info.get("title")
            or series_metadata.get("name")
            or series_metadata.get("title")
            or f"Series {series_id}"
        ),
        "seasons": seasons,
    }


class ManualQueueItem(BaseModel):
    stream_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1024)
    series_name: str | None = Field(default=None, max_length=1024)
    series_id: str | None = Field(default=None, max_length=512)
    season: str | int | None = None
    episode_num: int | None = Field(default=None, ge=0)
    filename: str = Field(min_length=1, max_length=512)
    container_ext: str = Field(default="mkv", min_length=1, max_length=32)
    category_id: str | None = Field(default=None, max_length=255)
    category_name: str | None = Field(default=None, max_length=512)


class ManualBatchRequest(BaseModel):
    content_type: Literal["SERIES", "MOVIE"]
    source_account_id: int | None = Field(default=None, ge=1)
    target_node_id: str | None = Field(default=None, max_length=64)
    account_ids: list[int] | None = Field(default=None, min_length=1, max_length=100)
    proxy_mode: Literal["node", "selected", "direct"] | None = None
    proxy_id: int | None = Field(default=None, ge=1)
    downloader: Literal["axel", "wget"] = "axel"
    account_offset: int = Field(default=0, ge=0)
    items: list[ManualQueueItem] = Field(min_length=1, max_length=5000)


@queue_router.post("/manual-batch")
def queue_manual_batch(payload: ManualBatchRequest, request: Request) -> dict[str, Any]:
    kind = "series" if payload.content_type == "SERIES" else "vod"
    with SessionLocal() as db:
        account = (
            db.get(Account, payload.source_account_id)
            if payload.source_account_id is not None
            else db.scalar(
                select(Account)
                .where(Account.enabled.is_(True))
                .order_by(Account.id)
                .limit(1)
            )
        )
        if not account or not account.enabled:
            raise HTTPException(
                status_code=409,
                detail="The selected browsing Xtream account is not enabled.",
            )
        kind = "series" if payload.content_type == "SERIES" else "vod"
        route = db.get(StorageRoute, kind)
        node = db.get(Node, route.node_id) if route else None
        if not node:
            label = "series" if kind == "series" else "movie"
            raise HTTPException(
                status_code=409,
                detail=f"Assign a storage node for {label} content before queueing.",
            )
        legacy_target = "series_node" if kind == "series" else "movie_node"
        if payload.target_node_id not in (None, str(node.id), legacy_target):
            raise HTTPException(
                status_code=409,
                detail="The requested node does not match the configured content-type route.",
            )
        selected_accounts: list[Account] = []
        if payload.account_ids is not None:
            if len(set(payload.account_ids)) != len(payload.account_ids):
                raise HTTPException(status_code=422, detail="Selected account IDs must be unique.")
            selected_accounts = db.scalars(
                select(Account).where(
                    Account.id.in_(payload.account_ids),
                    Account.enabled.is_(True),
                ).order_by(Account.id)
            ).all()
            if len(selected_accounts) != len(payload.account_ids):
                raise HTTPException(
                    status_code=409,
                    detail="Selected download accounts must be enabled.",
                )
            busy_ids = set(db.scalars(
                select(QueueItem.account_id).where(
                    QueueItem.account_id.in_(payload.account_ids),
                    QueueItem.status.in_(
                        ("PENDING", "DOWNLOADING", "VERIFYING", "CANCELLING")
                    ),
                ).distinct()
            ).all())
            if busy_ids:
                raise HTTPException(
                    status_code=409,
                    detail="One or more selected accounts already have queued or active work. Refresh account availability.",
                )
        selected_proxy_id = None
        if payload.proxy_mode == "node":
            if payload.proxy_id is not None:
                raise HTTPException(
                    status_code=422,
                    detail="Use selected proxy mode when specifying a proxy ID.",
                )
            selected_proxy_id = node.proxy_id
            proxy = db.get(Proxy, selected_proxy_id) if selected_proxy_id else None
            if not proxy or not proxy.enabled:
                raise HTTPException(
                    status_code=409,
                    detail="The routed storage server has no enabled proxy. Refresh proxy availability or choose direct connection.",
                )
        elif payload.proxy_mode == "selected":
            if payload.proxy_id is None:
                raise HTTPException(
                    status_code=422,
                    detail="Select an enabled proxy or choose direct connection.",
                )
            selected_proxy_id = payload.proxy_id
            proxy = db.get(Proxy, selected_proxy_id)
            if not proxy or not proxy.enabled:
                raise HTTPException(
                    status_code=409,
                    detail="The selected proxy is no longer enabled. Refresh proxy availability or choose another proxy.",
                )
        elif payload.proxy_id is not None:
            raise HTTPException(
                status_code=422,
                detail="A proxy can only be selected when proxy mode is enabled.",
            )
        if payload.downloader == "wget" and selected_proxy_id:
            selected_proxy = db.get(Proxy, selected_proxy_id)
            proxy_scheme = (
                urlsplit(decrypt_secret(selected_proxy.url_encrypted)).scheme.lower()
                if selected_proxy
                else ""
            )
            if proxy_scheme in ("socks5", "socks5h"):
                raise HTTPException(
                    status_code=422,
                    detail="Wget does not support SOCKS5 proxies. Choose Axel/aria2c or an HTTP(S) proxy.",
                )

        queued: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        seen: set[str] = set()
        assigned_count = 0
        for entry in payload.items:
            identity = stream_identity(account.provider_url, kind, entry.stream_id)
            if identity in seen:
                skipped.append({"stream_id": entry.stream_id, "reason": "duplicate_in_request"})
                continue
            seen.add(identity)

            existing = db.scalar(
                select(QueueItem).where(
                    (QueueItem.stream_id == identity)
                    | (
                        QueueItem.provider_stream_id.is_(None)
                        & (QueueItem.stream_id == entry.stream_id)
                    )
                )
            )
            if existing and existing.status not in ("FAILED", "CANCELLED"):
                skipped.append({"stream_id": entry.stream_id, "reason": existing.status.lower()})
                continue

            category = None
            if entry.category_id:
                category = db.scalar(
                    select(MonitoredCategory).where(
                        MonitoredCategory.kind == kind,
                        MonitoredCategory.category_id == entry.category_id,
                    )
                )
            category_name = (
                category.name if category else entry.category_name
            )
            extension = safe_extension(entry.container_ext)
            series_name = entry.series_name or (entry.title if kind == "series" else None)
            file_name, season, episode = normalized_episode_path(
                kind=kind,
                title=entry.title,
                category_name=(
                    (category_name or "Uncategorized")
                    if not (category and category.destination_path)
                    else None
                ),
                provider_stream_id=entry.stream_id,
                provider_url=account.provider_url,
                extension=extension,
                series_name=series_name,
                series_id=entry.series_id,
                season=entry.season,
                episode_num=entry.episode_num,
                filename=entry.filename,
                strip_prefixes=category.strip_prefixes if category else None,
            )
            if kind == "series":
                series_name = clean_content_name(
                    series_name or entry.title,
                    category.strip_prefixes if category else None,
                )
            item_values = {
                "stream_id": identity,
                "provider_stream_id": entry.stream_id,
                "title": entry.title,
                "kind": kind,
                "source_provider_url": account.provider_url,
                "category_id": entry.category_id,
                "category_name": category_name,
                "series_id": entry.series_id,
                "proxy_mode": payload.proxy_mode,
                "proxy_id": selected_proxy_id,
                "downloader": payload.downloader,
                "stream_url_encrypted": encrypt_secret(
                    stream_url(account, kind, entry.stream_id, extension)
                ),
                "file_size_bytes": None,
                "speed_bytes_per_second": None,
                "file_name": file_name,
                "final_path": None,
                "series_title": series_name,
                "season": season,
                "episode": episode,
                "account_id": (
                    selected_accounts[(payload.account_offset + assigned_count) % len(selected_accounts)].id
                    if selected_accounts else None
                ),
                "status": "PENDING",
                "hidden_from_dashboard": False,
                "progress": 0,
                "error": None,
                "retry_after": None,
                "updated_at": utc_now(),
            }
            if existing:
                for field, value in item_values.items():
                    setattr(existing, field, value)
                existing.retries = 0
                existing.created_at = utc_now()
            else:
                db.add(QueueItem(**item_values, created_at=utc_now()))
            queued.append({"stream_id": entry.stream_id, "title": entry.title})
            assigned_count += 1

        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            logger.warning("Manual batch collided with a concurrently queued provider stream.")
            raise HTTPException(
                status_code=409,
                detail="Queue changed while this batch was being saved. Refresh and retry the skipped items.",
            ) from exc

    if queued:
        request.app.state.worker_pool.wake()
    return {"queued": queued, "skipped": skipped, "queued_count": len(queued)}
def _integer_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _media_year(row: dict[str, Any]) -> str | None:
    value = row.get("year") or row.get("releaseDate") or row.get("releasedate")
    if value is None:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", str(value))
    return match.group(0) if match else None


class GenerateScriptRequest(BaseModel):
    content_type: Literal["SERIES", "MOVIE"]
    source_account_id: int | None = Field(default=None, ge=1)
    items: list[ManualQueueItem] = Field(min_length=1, max_length=5000)


@router.post("/generate-script")
def generate_bash_script(payload: GenerateScriptRequest) -> Response:
    kind = "series" if payload.content_type == "SERIES" else "vod"
    account = _provider_account(payload.source_account_id)
    with SessionLocal() as db:
        route = db.get(StorageRoute, kind)
        node = db.get(Node, route.node_id) if route else None
        if not node:
            label = "series" if kind == "series" else "movie"
            raise HTTPException(
                status_code=409,
                detail=f"Assign a storage node for {label} content before exporting a script.",
            )
        proxy = db.get(Proxy, node.proxy_id) if node.proxy_id else None
        if proxy and not proxy.enabled:
            proxy = None
        script_proxy_url = decrypt_secret(proxy.url_encrypted) if proxy else None
        category_overrides = {
            category.category_id: category
            for category in db.scalars(
                select(MonitoredCategory).where(
                    MonitoredCategory.kind == kind,
                    MonitoredCategory.category_id.in_(
                        {entry.category_id for entry in payload.items if entry.category_id}
                    ),
                )
            ).all()
        }

    proxy_scheme = script_proxy_url and script_proxy_url.split(":", 1)[0].lower()
    if proxy_scheme and proxy_scheme not in ("http", "https", "socks5"):
        raise HTTPException(status_code=409, detail="Configured worker proxy scheme is unsupported.")
    timestamp = utc_now()
    categories = sorted({
        (entry.category_name or entry.category_id or "Uncategorized").replace("\n", " ").replace("\r", " ")
        for entry in payload.items
    })
    host_comment = str(node.host).replace("\n", " ").replace("\r", " ")
    axel_connections = node.axel_connections or settings.default_axel_connections
    user_agent = account.user_agent or "Mozilla"
    required_downloader = "aria2c" if proxy_scheme == "socks5" else "axel"
    lines = [
        "#!/usr/bin/env bash",
        "# Standalone media download script generated by Media Ingestion.",
        "# WARNING: Stream URLs contain provider credentials. Protect this file and delete it when finished.",
        f"# Storage host: {host_comment}",
        f"# Categories: {', '.join(categories)}",
        f"# Generated at: {timestamp.isoformat()}",
        "set -euo pipefail",
        "unset http_proxy https_proxy ftp_proxy all_proxy "
        "HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY no_proxy NO_PROXY",
        "",
        f"command -v {required_downloader} >/dev/null 2>&1 || "
        f"{{ echo '{required_downloader} is required' >&2; exit 127; }}",
        "",
    ]
    added: set[str] = set()
    for entry in payload.items:
        if kind == "series" and (entry.season is None or entry.episode_num is None):
            raise HTTPException(
                status_code=422,
                detail=f"Season and episode numbers are required for series stream {entry.stream_id}.",
            )
        extension = safe_extension(entry.container_ext)
        category = category_overrides.get(entry.category_id or "")
        relative_path, _season, _episode = normalized_episode_path(
            kind=kind,
            title=entry.title,
            category_name=(
                (
                    entry.category_name
                    or (category.name if category else None)
                    or "Uncategorized"
                )
                if not (category and category.destination_path)
                else None
            ),
            provider_stream_id=entry.stream_id,
            provider_url=account.provider_url,
            extension=extension,
            series_name=entry.series_name,
            series_id=entry.series_id,
            season=entry.season,
            episode_num=entry.episode_num,
            filename=entry.filename,
            strip_prefixes=(
                category_overrides[entry.category_id].strip_prefixes
                if entry.category_id in category_overrides
                else None
            ),
        )
        if relative_path in added:
            continue
        added.add(relative_path)
        root = (category.destination_path if category else None) or node.watch_path
        destination = f"{root.rstrip('/')}/{relative_path}"
        parent = destination.rsplit("/", 1)[0]
        url = stream_url(account, kind, entry.stream_id, extension)
        quoted_proxy = shlex.quote(script_proxy_url) if script_proxy_url else None
        q_parent = shlex.quote(parent)
        q_destination = shlex.quote(destination)
        q_url = shlex.quote(url)
        q_user_agent = (
            '"'
            + user_agent.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("$", "\\$")
            .replace("`", "\\`")
            + '"'
        )
        lines.append(f"mkdir -p {q_parent} && chmod 0755 {q_parent}")
        if proxy_scheme == "socks5" and quoted_proxy:
            lines.append(
                f"aria2c --split={axel_connections} "
                f"--max-connection-per-server={axel_connections} "
                f"--allow-overwrite=true --auto-file-renaming=false "
                f"--file-allocation=none --summary-interval=1 "
                f"--all-proxy={quoted_proxy} --user-agent={q_user_agent} "
                f"--dir={shlex.quote(parent)} --out={shlex.quote(destination.rsplit('/', 1)[-1])} {q_url}"
            )
        elif quoted_proxy:
            lines.append(
                f"http_proxy={quoted_proxy} https_proxy={quoted_proxy} "
                f"HTTP_PROXY={quoted_proxy} HTTPS_PROXY={quoted_proxy} "
                f"axel -n {axel_connections} -o {q_destination} "
                f"--user-agent={q_user_agent} {q_url}"
            )
        else:
            lines.append(
                f"axel -n {axel_connections} -o {q_destination} "
                f"--user-agent={q_user_agent} {q_url}"
            )
        lines.append("")

    filename = f"media-ingest-{timestamp.strftime('%Y%m%d-%H%M%S')}.sh"
    return Response(
        content="\n".join(lines),
        media_type="text/x-shellscript; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
    )
