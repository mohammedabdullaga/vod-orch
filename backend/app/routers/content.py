import asyncio
import hashlib
import logging
import re
import shlex
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import (
    Account,
    AppSettings,
    M3UEntry,
    M3UFetchJob,
    M3UPlaylist,
    MonitoredCategory,
    Node,
    Proxy,
    ProviderCatalogSyncJob,
    QueueItem,
    SessionLocal,
    StorageRoute,
    utc_now,
)
from app.services.provider_client import (
    cached_provider_action,
    parse_series_episodes,
    resolve_provider_proxy,
    resolve_provider_proxy_selection,
    stream_url,
)
from app.security import decrypt_secret, encrypt_secret
from app.services.content_paths import (
    category_directory_name,
    clean_content_name,
    identity_segment,
    library_name_matches,
    normalized_episode_path,
    safe_extension,
    season_number as parse_season_number,
    stream_identity,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/content", tags=["content"])
queue_router = APIRouter(prefix="/api/queue", tags=["queue"])
playlist_router = APIRouter(prefix="/api/playlists", tags=["playlists"])


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


def _resolve_api_proxy(proxy_id: int | None) -> str | None:
    try:
        return resolve_provider_proxy_selection(proxy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _request_provider(
    account: Account,
    action: str,
    *,
    proxy_url: str | None = None,
    force_refresh: bool = False,
    response: Response | None = None,
    **params: Any,
) -> Any:
    try:
        payload, cache_state = await cached_provider_action(
            account,
            action,
            proxy_url=proxy_url,
            force_refresh=force_refresh,
            **params,
        )
        if response is not None and (
            response.headers.get("X-Provider-Catalog-Cache") != "stale"
            or cache_state == "stale"
        ):
            response.headers["X-Provider-Catalog-Cache"] = cache_state
        return payload
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        upstream_status = exc.response.status_code
        logger.warning(
            "Provider request %s returned HTTP %s",
            action,
            upstream_status,
        )
        raise HTTPException(
            status_code=502,
            detail=f"Provider request {action} returned HTTP {upstream_status}. Check the provider and selected proxy.",
        ) from exc
    except httpx.TimeoutException as exc:
        logger.warning("Provider request %s timed out", action)
        raise HTTPException(
            status_code=504,
            detail=f"Provider request {action} timed out. Check the provider and selected proxy.",
        ) from exc
    except httpx.RequestError as exc:
        error_type = type(exc).__name__
        logger.warning("Provider request %s failed (%s)", action, error_type)
        raise HTTPException(
            status_code=502,
            detail=f"Provider request {action} failed ({error_type}). Check the provider and selected proxy.",
        ) from exc
    except Exception as exc:
        logger.error("Provider request %s failed (%s)", action, type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"Provider request {action} failed.") from exc


def _provider_kind(content_type: str) -> tuple[str, str, str]:
    if content_type == "series":
        return "series", "get_series_categories", "get_series"
    return "vod", "get_vod_categories", "get_vod_streams"


class M3UPlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=8, max_length=4096)
    proxy_id: int | None = Field(default=None, ge=0)


class M3UPlaylistProxyUpdate(BaseModel):
    proxy_id: int | None = Field(default=None, ge=0)


class CatalogSyncRequest(BaseModel):
    account_id: int = Field(ge=1)
    proxy_id: int | None = Field(default=None, ge=0)


def _playlist_category_id(playlist_id: int, group_title: str) -> str:
    group_hash = hashlib.sha256(group_title.encode("utf-8")).hexdigest()[:16]
    return f"m3u:{playlist_id}:{group_hash}"


@router.post("/catalog-sync")
def start_catalog_sync(payload: CatalogSyncRequest, request: Request) -> dict[str, Any]:
    with SessionLocal() as db:
        account = db.get(Account, payload.account_id)
        if not account or not account.enabled:
            raise HTTPException(
                status_code=409,
                detail="Select an enabled Xtream account to sync.",
            )
        proxy_id = payload.proxy_id
        if proxy_id is None:
            settings_row = db.get(AppSettings, 1)
            proxy_id = settings_row.provider_proxy_id if settings_row else None
        if proxy_id == 0:
            proxy_id = None
        active_job = db.scalar(
            select(ProviderCatalogSyncJob.id).where(
                ProviderCatalogSyncJob.account_id == account.id,
                ProviderCatalogSyncJob.status.in_(("PENDING", "RUNNING")),
            )
        )
        if active_job is not None:
            raise HTTPException(
                status_code=409,
                detail="A catalog sync is already queued or running for this account.",
            )
    try:
        resolve_provider_proxy(proxy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with SessionLocal() as db:
        job = ProviderCatalogSyncJob(
            account_id=payload.account_id,
            proxy_id=proxy_id,
            status="PENDING",
            phase="queued",
        )
        db.add(job)
        db.commit()
        job_id = job.id
    request.app.state.catalog_sync_worker.wake()
    return {"job_id": job_id, "status": "PENDING"}


def _catalog_sync_job_json(job: ProviderCatalogSyncJob) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "account_id": job.account_id,
        "playlist_id": job.playlist_id,
        "status": job.status,
        "phase": job.phase,
        "entries_processed": job.entries_processed,
        "categories_processed": job.categories_processed,
        "lists_processed": job.lists_processed,
        "cached_items": job.cached_items,
        "series_details_processed": job.series_details_processed,
        "total_series_details": job.total_series_details,
        "stale_responses": job.stale_responses,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@router.get("/catalog-sync")
def get_latest_catalog_sync_status() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        latest_job_ids = (
            select(func.max(ProviderCatalogSyncJob.id).label("job_id"))
            .group_by(ProviderCatalogSyncJob.account_id)
            .subquery()
        )
        jobs = db.scalars(
            select(ProviderCatalogSyncJob)
            .where(ProviderCatalogSyncJob.id.in_(select(latest_job_ids.c.job_id)))
            .order_by(ProviderCatalogSyncJob.account_id)
        ).all()
        return [_catalog_sync_job_json(job) for job in jobs]


@router.get("/catalog-sync/{job_id}")
def get_catalog_sync_status(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(ProviderCatalogSyncJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Catalog sync job not found.")
        return _catalog_sync_job_json(job)


def _playlist_entry_json(entry: M3UEntry, category_id: str) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "source_entry_id": entry.id,
        "name": entry.title,
        "category_id": category_id,
        "category_name": entry.group_title,
        "kind": entry.kind,
        "container_ext": safe_extension(entry.extension),
        "tvg_id": entry.tvg_id,
        "tvg_name": entry.tvg_name,
        "tvg_logo": decrypt_secret(entry.tvg_logo) if entry.tvg_logo else None,
        "season": entry.season,
        "episode_num": entry.episode,
        "series_name": entry.series_name,
    }


def _find_series_folder(
    files: list[str],
    series_name: str,
    series_id: str,
    provider_url: str,
    prefixes: str | None,
) -> tuple[str | None, bool]:
    directories = {entry.split("/", 1)[0] for entry in files if "/" in entry}
    expected = identity_segment(
        clean_content_name(series_name, prefixes), series_id, provider_url
    )
    if expected in directories:
        return expected, False
    matches = [
        directory
        for directory in directories
        if library_name_matches(series_name, directory, prefixes)
    ]
    if len(matches) == 1:
        return matches[0], False
    return None, len(matches) > 1


def _stored_episode_numbers(filename: str) -> set[tuple[int, int]]:
    result = set()
    pattern = re.compile(
        r"(?:^|[^A-Za-z0-9])(?:S(\d{1,3})E(\d{1,3})|(\d{1,3})x(\d{1,3}))"
        r"(?=[^A-Za-z0-9]|$)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(filename):
        season, episode = match.group(1, 2)
        if season is None:
            season, episode = match.group(3, 4)
        result.add((int(season), int(episode)))
    return result


def _mark_series_storage(
    details: dict[str, Any],
    files: list[str],
    provider_url: str,
    prefixes: str | None = None,
) -> None:
    series_folder, ambiguous = _find_series_folder(
        files,
        details["name"],
        str(details["series_id"]),
        provider_url,
        prefixes,
    )
    details["storage_inventory_available"] = True
    details["storage_match"] = series_folder
    details["storage_inventory_ambiguous"] = ambiguous
    stored_episodes: set[tuple[int, int]] = set()
    has_unclassified_video = False
    if series_folder:
        for entry in files:
            if not entry.startswith(f"{series_folder}/"):
                continue
            filename = entry.rsplit("/", 1)[-1]
            episode_numbers = _stored_episode_numbers(filename)
            if episode_numbers:
                stored_episodes.update(episode_numbers)
            else:
                has_unclassified_video = True
    details["storage_inventory_unclassified"] = has_unclassified_video
    missing_count = 0
    for season_key, episodes in details["seasons"].items():
        for episode in episodes:
            season = episode.get("season")
            if season is None:
                season = parse_season_number(season_key) or 0
            present = (
                None
                if ambiguous or (
                    has_unclassified_video
                    and (int(season), int(episode["episode_num"]))
                    not in stored_episodes
                )
                else (int(season), int(episode["episode_num"])) in stored_episodes
            )
            episode["on_storage"] = present
            if present is False:
                missing_count += 1
    details["missing_episode_count"] = (
        missing_count if not ambiguous and not has_unclassified_video else None
    )


@playlist_router.get("")
def list_m3u_playlists() -> list[dict[str, Any]]:
    with SessionLocal() as db:
        playlists = db.scalars(select(M3UPlaylist).order_by(M3UPlaylist.id)).all()
        return [
            {
                "id": playlist.id,
                "provider_account_id": playlist.provider_account_id,
                "name": playlist.name,
                "proxy_id": playlist.proxy_id,
                "enabled": playlist.enabled,
                "entry_count": playlist.entry_count,
                "fetched_at": playlist.fetched_at.isoformat(),
            }
            for playlist in playlists
        ]


@playlist_router.post("/import")
def import_m3u_playlist(
    payload: M3UPlaylistCreate,
    request: Request,
) -> dict[str, Any]:
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Playlist name cannot be blank.")
    proxy_id = payload.proxy_id or None
    try:
        resolve_provider_proxy(proxy_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    source = urlsplit(payload.url)
    if source.scheme.lower() not in ("http", "https") or not source.netloc:
        raise HTTPException(
            status_code=422,
            detail="Playlist URL must be an absolute HTTP or HTTPS URL.",
        )
    with SessionLocal() as db:
        job = M3UFetchJob(
            operation="import",
            name=payload.name.strip(),
            source_url_encrypted=encrypt_secret(payload.url),
            proxy_id=proxy_id,
            status="PENDING",
        )
        db.add(job)
        db.commit()
        job_id = job.id
    request.app.state.m3u_fetch_worker.wake()
    return {"job_id": job_id, "status": "PENDING"}


@playlist_router.post("/{playlist_id}/refresh")
def refresh_m3u_playlist(playlist_id: int, request: Request) -> dict[str, Any]:
    with SessionLocal() as db:
        playlist = db.get(M3UPlaylist, playlist_id)
        if not playlist:
            raise HTTPException(status_code=404, detail="M3U playlist not found.")
        source_url_encrypted = playlist.source_url_encrypted
        proxy_id = playlist.proxy_id
    try:
        resolve_provider_proxy(proxy_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"The assigned M3U proxy is unavailable. Replace it before refreshing: {exc}",
        ) from exc
    with SessionLocal() as db:
        job = M3UFetchJob(
            operation="refresh",
            name="",
            playlist_id=playlist_id,
            source_url_encrypted=source_url_encrypted,
            proxy_id=proxy_id,
            status="PENDING",
        )
        db.add(job)
        db.commit()
        job_id = job.id
    request.app.state.m3u_fetch_worker.wake()
    return {"job_id": job_id, "playlist_id": playlist_id, "status": "PENDING"}


@playlist_router.get("/jobs/{job_id}")
def get_m3u_fetch_job(job_id: int) -> dict[str, Any]:
    with SessionLocal() as db:
        job = db.get(M3UFetchJob, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="M3U fetch job not found.")
        return {
            "job_id": job.id,
            "operation": job.operation,
            "playlist_id": job.playlist_id,
            "status": job.status,
            "entry_count": job.entry_count,
            "error": job.error,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
        }


@playlist_router.put("/{playlist_id}/proxy")
def update_m3u_playlist_proxy(
    playlist_id: int,
    payload: M3UPlaylistProxyUpdate,
) -> dict[str, int | None]:
    proxy_id = payload.proxy_id or None
    if proxy_id is not None:
        try:
            resolve_provider_proxy(proxy_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    with SessionLocal() as db:
        playlist = db.get(M3UPlaylist, playlist_id)
        if not playlist:
            raise HTTPException(status_code=404, detail="M3U playlist not found.")
        playlist.proxy_id = proxy_id
        db.commit()
    return {"id": playlist_id, "proxy_id": proxy_id}


@playlist_router.delete("/{playlist_id}")
def delete_m3u_playlist(playlist_id: int) -> dict[str, bool]:
    with SessionLocal() as db:
        playlist = db.get(M3UPlaylist, playlist_id)
        if not playlist:
            raise HTTPException(status_code=404, detail="M3U playlist not found.")
        db.delete(playlist)
        db.commit()
    return {"ok": True}


@playlist_router.get("/{playlist_id}/categories")
def get_m3u_categories(
    playlist_id: int,
    type: Literal["series", "movie"] = Query(...),
) -> list[dict[str, Any]]:
    expected_kind = "series" if type == "series" else "vod"
    with SessionLocal() as db:
        playlist = db.get(M3UPlaylist, playlist_id)
        if not playlist or not playlist.enabled:
            raise HTTPException(status_code=404, detail="Enabled M3U playlist not found.")
        groups = db.execute(
            select(M3UEntry.group_title, func.count(M3UEntry.id))
            .where(M3UEntry.playlist_id == playlist_id, M3UEntry.kind == expected_kind)
            .group_by(M3UEntry.group_title)
            .order_by(M3UEntry.group_title)
        ).all()
    return [
        {
            "category_id": _playlist_category_id(playlist_id, group_title),
            "category_name": group_title,
            "name": group_title,
            "kind": expected_kind,
            "entry_count": count,
            "monitored": False,
            "destination_path": None,
            "match_existing_series": False,
            "strip_prefixes": "",
        }
        for group_title, count in groups
    ]


@playlist_router.get("/{playlist_id}/items")
def get_m3u_items(
    playlist_id: int,
    type: Literal["series", "movie"] = Query(...),
    category_ids: str = Query(..., min_length=1, max_length=5000),
) -> list[dict[str, Any]]:
    expected_kind = "series" if type == "series" else "vod"
    requested_ids = set(part.strip() for part in category_ids.split(",") if part.strip())
    if not requested_ids or len(requested_ids) > 50:
        raise HTTPException(status_code=422, detail="Select between 1 and 50 category IDs.")
    with SessionLocal() as db:
        playlist = db.get(M3UPlaylist, playlist_id)
        if not playlist or not playlist.enabled:
            raise HTTPException(status_code=404, detail="Enabled M3U playlist not found.")
        available_groups = db.scalars(
            select(M3UEntry.group_title)
            .where(M3UEntry.playlist_id == playlist_id, M3UEntry.kind == expected_kind)
            .distinct()
        ).all()
        selected_groups = [
            group_title for group_title in available_groups
            if _playlist_category_id(playlist_id, group_title) in requested_ids
        ]
        selected = db.scalars(
            select(M3UEntry)
            .where(
                M3UEntry.playlist_id == playlist_id,
                M3UEntry.kind == expected_kind,
                M3UEntry.group_title.in_(selected_groups),
            )
            .order_by(M3UEntry.position)
        ).all() if selected_groups else []
    if not selected:
        raise HTTPException(status_code=404, detail="No entries found for the selected M3U categories.")
    if expected_kind == "vod":
        return [
            _playlist_entry_json(
                row, _playlist_category_id(playlist_id, row.group_title)
            )
            for row in selected
        ]
    grouped: dict[tuple[str, str], list[M3UEntry]] = defaultdict(list)
    for row in selected:
        grouped[(row.group_title, row.series_name or row.title)].append(row)
    result = []
    for (group_title, series_name), episodes in grouped.items():
        category_id = _playlist_category_id(playlist_id, group_title)
        series_key = hashlib.sha256(
            f"{playlist_id}\0{group_title}\0{series_name}".encode("utf-8")
        ).hexdigest()[:24]
        result.append({
            "id": f"m3u-series:{series_key}",
            "name": series_name,
            "category_id": category_id,
            "category_name": group_title,
            "kind": "series",
            "total_episodes": len(episodes),
            "container_ext": "mkv",
            "download_status": "unknown",
            "storage_match": None,
            "source_series_key": series_key,
        })
    return result


@playlist_router.get("/{playlist_id}/series/{series_key}/details")
async def get_m3u_series_details(
    request: Request,
    playlist_id: int,
    series_key: str,
    check_storage: bool = Query(default=True),
) -> dict[str, Any]:
    with SessionLocal() as db:
        playlist = db.get(M3UPlaylist, playlist_id)
        if not playlist or not playlist.enabled:
            raise HTTPException(status_code=404, detail="Enabled M3U playlist not found.")
        series_names = db.execute(
            select(M3UEntry.group_title, M3UEntry.series_name)
            .where(M3UEntry.playlist_id == playlist_id, M3UEntry.kind == "series")
            .group_by(M3UEntry.group_title, M3UEntry.series_name)
        ).all()
        matching_series = next(
            (
                (group_title, series_name)
                for group_title, series_name in series_names
                if hashlib.sha256(
                    f"{playlist_id}\0{group_title}\0{series_name or ''}".encode("utf-8")
                ).hexdigest()[:24] == series_key
            ),
            None,
        )
        if matching_series is None:
            raise HTTPException(status_code=404, detail="M3U series not found.")
        group_title, series_name = matching_series
        matching = db.scalars(
            select(M3UEntry)
            .where(
                M3UEntry.playlist_id == playlist_id,
                M3UEntry.kind == "series",
                M3UEntry.group_title == group_title,
                M3UEntry.series_name == series_name,
            )
            .order_by(M3UEntry.position)
        ).all()
    seasons: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in matching:
        season = row.season or 0
        seasons[str(season)].append({
            "episode_num": row.episode or row.position + 1,
            "id": str(row.id),
            "source_entry_id": row.id,
            "ext": safe_extension(row.extension),
            "title": row.title,
            "season": season,
        })
    details = {
        "series_id": series_key,
        "name": matching[0].series_name or matching[0].title,
        "category_id": _playlist_category_id(playlist_id, matching[0].group_title),
        "category_name": matching[0].group_title,
        "seasons": dict(seasons),
    }
    with SessionLocal() as db:
        route = db.get(StorageRoute, "series")
        node = db.get(Node, route.node_id) if route else None
    if node and check_storage:
        root, prefixes = _series_storage_location(
            node,
            details["category_id"],
            details["category_name"],
        )
        if root is None:
            _set_unknown_series_inventory(details)
            return details
        await _populate_series_inventory(
            request,
            details,
            f"m3u:{playlist_id}",
            node,
            root,
            prefixes,
        )
    else:
        _set_unknown_series_inventory(details)
    return details


@router.get("/categories")
async def get_content_categories(
    response: Response,
    type: Literal["series", "movie"] = Query(...),
    account_id: int | None = Query(default=None, ge=1),
    proxy_id: int | None = Query(default=None, ge=0),
    refresh: bool = Query(default=False),
) -> list[dict[str, Any]]:
    kind, action, _ = _provider_kind(type)
    account = _provider_account(account_id)
    proxy_url = _resolve_api_proxy(proxy_id)
    rows = _provider_rows(
        await _request_provider(
            account, action, proxy_url=proxy_url, force_refresh=refresh, response=response
        ),
        action,
    )
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
    request: Request,
    response: Response,
    type: Literal["series", "movie"] = Query(...),
    category_ids: str = Query(..., min_length=1, max_length=5000),
    account_id: int | None = Query(default=None, ge=1),
    proxy_id: int | None = Query(default=None, ge=0),
    refresh: bool = Query(default=False),
) -> list[dict[str, Any]]:
    kind, _, items_action = _provider_kind(type)
    ids = list(dict.fromkeys(part.strip() for part in category_ids.split(",") if part.strip()))
    if not ids or len(ids) > 50:
        raise HTTPException(status_code=422, detail="Select between 1 and 50 category IDs.")
    account = _provider_account(account_id)
    proxy_url = _resolve_api_proxy(proxy_id)
    categories_response = await _request_provider(
        account,
        "get_series_categories" if kind == "series" else "get_vod_categories",
        proxy_url=proxy_url,
        response=response,
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
            await _request_provider(
                account,
                items_action,
                proxy_url=proxy_url,
                force_refresh=refresh,
                response=response,
                category_id=category_id,
            ),
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

    with SessionLocal() as db:
        states = {
            item.category_id: item
            for item in db.scalars(
                select(MonitoredCategory).where(
                    MonitoredCategory.kind == kind,
                    MonitoredCategory.category_id.in_(ids),
                )
            ).all()
        }
        route = db.get(StorageRoute, kind)
        node = db.get(Node, route.node_id) if route else None

    roots: dict[str, str] = {}
    category_prefixes: dict[str, str] = {}
    if node:
        for category_id in ids:
            state = states.get(category_id)
            prefixes = state.strip_prefixes if state else ""
            category_prefixes[category_id] = prefixes
            category_name = category_names[category_id]
            configured_path = state.destination_path if state else None
            roots[category_id] = (
                configured_path
                if configured_path
                else str(PurePosixPath(node.watch_path) / category_directory_name(category_name, prefixes))
            )

    inventory: dict[str, list[str]] = {}
    inventory_available = False
    if roots:
        try:
            inventory = await asyncio.to_thread(
                request.app.state.dispatcher.scan_library_files,
                node,
                roots,
            )
            inventory_available = True
        except Exception as exc:
            logger.warning(
                "Storage library scan failed for %s categories (%s)",
                len(roots),
                type(exc).__name__,
            )

    for item in result:
        category_id = item["category_id"]
        files = inventory.get(category_id)
        item["download_status"] = "unknown"
        item["storage_match"] = None
        if not inventory_available or files is None:
            continue

        if kind == "series":
            series_directory, ambiguous = _find_series_folder(
                files,
                item["name"],
                item["id"],
                account.provider_url,
                category_prefixes.get(category_id, ""),
            )
            if ambiguous:
                item["download_status"] = "unknown"
                item["inventory_warning"] = "Multiple matching series folders were found."
                continue
            if not series_directory:
                item["download_status"] = "missing"
                continue

            episode_ids = set()
            for entry in files:
                if not entry.startswith(f"{series_directory}/"):
                    continue
                filename = entry.rsplit("/", 1)[-1]
                episode_ids.update(_stored_episode_numbers(filename))
            total_episodes = item["total_episodes"]
            is_complete = (
                total_episodes is not None
                and total_episodes > 0
                and len(episode_ids) >= total_episodes
            )
            item["download_status"] = "downloaded" if is_complete else "partial"
            item["storage_match"] = series_directory
            continue

        movie_candidates = []
        for entry in files:
            filename = entry.rsplit("/", 1)[-1]
            if "." not in filename:
                continue
            movie_candidates.append((filename.rsplit(".", 1)[0], entry))
        matches = [
            stored_path
            for candidate, stored_path in movie_candidates
            if library_name_matches(
                item["name"],
                candidate,
                category_prefixes.get(category_id, ""),
            )
        ]
        match = matches[0] if len(matches) == 1 else None
        item["download_status"] = (
            "downloaded" if match else "unknown" if len(matches) > 1 else "missing"
        )
        if len(matches) > 1:
            item["inventory_warning"] = "Multiple matching movie files were found."
        item["storage_match"] = match
    return result


@router.get("/series/{series_id}/details")
async def get_series_details(
    request: Request,
    response: Response,
    series_id: str,
    account_id: int | None = Query(default=None, ge=1),
    category_id: str | None = Query(default=None, max_length=255),
    category_name: str | None = Query(default=None, max_length=512),
    proxy_id: int | None = Query(default=None, ge=0),
    refresh: bool = Query(default=False),
    check_storage: bool = Query(default=True),
) -> dict[str, Any]:
    if not series_id or len(series_id) > 512:
        raise HTTPException(status_code=422, detail="Invalid series ID.")
    account = _provider_account(account_id)
    proxy_url = _resolve_api_proxy(proxy_id)
    info = await _request_provider(
        account,
        "get_series_info",
        proxy_url=proxy_url,
        force_refresh=refresh,
        response=response,
        series_id=series_id,
    )
    details = _normalize_series_details(info, series_id)
    with SessionLocal() as db:
        route = db.get(StorageRoute, "series")
        node = db.get(Node, route.node_id) if route else None
    root, prefixes = (
        _series_storage_location(node, category_id, category_name)
        if node
        else (None, "")
    )
    if node and root and check_storage:
        await _populate_series_inventory(
            request,
            details,
            account.provider_url,
            node,
            root,
            prefixes,
        )
    else:
        _set_unknown_series_inventory(details)
    return details


def _set_unknown_series_inventory(details: dict[str, Any]) -> None:
    details["storage_inventory_available"] = False
    details["storage_inventory_ambiguous"] = False
    details["storage_inventory_unclassified"] = False
    details["storage_match"] = None
    details["missing_episode_count"] = None
    for episodes in details["seasons"].values():
        for episode in episodes:
            episode["on_storage"] = None


async def _populate_series_inventory(
    request: Request,
    details: dict[str, Any],
    provider_url: str,
    node: Node,
    root: str,
    prefixes: str | None = None,
) -> None:
    try:
        inventory = await asyncio.to_thread(
            request.app.state.dispatcher.scan_library_files,
            node,
            {"series": root},
        )
        _mark_series_storage(
            details,
            inventory["series"],
            provider_url,
            prefixes,
        )
    except Exception as exc:
        logger.warning(
            "Could not match stored episodes for series %s (%s)",
            details.get("series_id"),
            type(exc).__name__,
        )
        _set_unknown_series_inventory(details)


def _series_storage_location(
    node: Node,
    category_id: str | None,
    category_name: str | None,
) -> tuple[str | None, str]:
    category = None
    if category_id:
        with SessionLocal() as db:
            category = db.scalar(
                select(MonitoredCategory).where(
                    MonitoredCategory.kind == "series",
                    MonitoredCategory.category_id == category_id,
                )
            )
    prefixes = category.strip_prefixes if category else ""
    if category and category.destination_path:
        return category.destination_path, prefixes
    name = category.name if category else category_name
    if not name:
        return None, prefixes
    return (
        str(PurePosixPath(node.watch_path) / category_directory_name(name, prefixes)),
        prefixes,
    )


def _normalize_series_details(info: Any, series_id: str) -> dict[str, Any]:
    if not isinstance(info, dict):
        raise HTTPException(status_code=502, detail="Provider returned invalid series details.")
    try:
        parsed_episodes = parse_series_episodes(info)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    series_metadata = info.get("info")
    if not isinstance(series_metadata, dict):
        series_metadata = {}
    seasons: dict[str, list[dict[str, Any]]] = {}
    for episode in parsed_episodes:
        season_key = episode["season"]
        episode_season = parse_season_number(episode["season"])
        seasons.setdefault(str(season_key), []).append({
            "episode_num": episode["episode_num"],
            "id": episode["stream_id"],
            "ext": safe_extension(episode["container_extension"]),
            "title": episode["title"],
            "season": episode_season,
        })
    for season_episodes in seasons.values():
        season_episodes.sort(key=lambda item: item["episode_num"])
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
    source_entry_id: int | None = Field(default=None, ge=1)
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
    source_playlist_id: int | None = Field(default=None, ge=1)
    target_node_id: str | None = Field(default=None, max_length=64)
    account_ids: list[int] | None = Field(default=None, min_length=1, max_length=100)
    proxy_mode: Literal["node", "selected", "direct"] | None = None
    proxy_id: int | None = Field(default=None, ge=1)
    downloader: Literal["axel", "wget"] = "axel"
    account_offset: int = Field(default=0, ge=0)
    items: list[ManualQueueItem] = Field(min_length=1, max_length=5000)


class SeriesQueueSelection(BaseModel):
    series_id: str = Field(min_length=1, max_length=512)
    title: str = Field(min_length=1, max_length=1024)
    category_id: str | None = Field(default=None, max_length=255)
    category_name: str | None = Field(default=None, max_length=512)


class SeriesQueueBatchRequest(BaseModel):
    source_account_id: int | None = Field(default=None, ge=1)
    source_playlist_id: int | None = Field(default=None, ge=1)
    provider_proxy_id: int | None = Field(default=None, ge=0)
    account_ids: list[int] = Field(min_length=1, max_length=100)
    proxy_mode: Literal["selected", "direct"] = "direct"
    proxy_id: int | None = Field(default=None, ge=1)
    downloader: Literal["axel", "wget"] = "axel"
    missing_only: bool = False
    series: list[SeriesQueueSelection] = Field(min_length=1, max_length=500)


@queue_router.post("/manual-batch")
def queue_manual_batch(payload: ManualBatchRequest, request: Request) -> dict[str, Any]:
    return _queue_manual_batch(payload, request)


def _queue_manual_batch(
    payload: ManualBatchRequest,
    request: Request,
    *,
    allow_existing_account_queue: bool = False,
    series_storage_matches: dict[str, str] | None = None,
) -> dict[str, Any]:
    kind = "series" if payload.content_type == "SERIES" else "vod"
    with SessionLocal() as db:
        playlist = (
            db.get(M3UPlaylist, payload.source_playlist_id)
            if payload.source_playlist_id is not None
            else None
        )
        if payload.source_playlist_id is not None and (
            not playlist or not playlist.enabled
        ):
            raise HTTPException(status_code=409, detail="The selected M3U playlist is unavailable.")
        account = None
        if playlist is None:
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
        elif payload.source_account_id is not None:
            raise HTTPException(
                status_code=422,
                detail="Select either an M3U playlist or an Xtream account, not both.",
            )
        kind = "series" if payload.content_type == "SERIES" else "vod"
        source_provider_url = (
            f"m3u:{playlist.id}" if playlist is not None else account.provider_url
        )
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
            if account and any(
                selected.provider_url.rstrip("/") != account.provider_url.rstrip("/")
                for selected in selected_accounts
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Xtream download accounts must use the same provider endpoint as the source account.",
                )
            if not allow_existing_account_queue:
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
            provider_stream_id = entry.stream_id
            title = entry.title
            category_id = entry.category_id
            category_name_input = entry.category_name
            series_id = entry.series_id
            series_name_input = entry.series_name
            season_input = entry.season
            episode_input = entry.episode_num
            filename_input = entry.filename
            extension_input = entry.container_ext
            resolved_stream_url = None
            if playlist is not None:
                if entry.source_entry_id is None:
                    raise HTTPException(
                        status_code=422,
                        detail="M3U queue items must reference a stored playlist entry.",
                    )
                playlist_entry = db.get(M3UEntry, entry.source_entry_id)
                if (
                    not playlist_entry
                    or playlist_entry.playlist_id != playlist.id
                    or playlist_entry.kind != kind
                ):
                    raise HTTPException(
                        status_code=404,
                        detail="An M3U playlist entry is missing or does not match the selected content type.",
                    )
                provider_stream_id = playlist_entry.stable_key
                title = playlist_entry.title
                category_id = _playlist_category_id(
                    playlist.id, playlist_entry.group_title
                )
                category_name_input = playlist_entry.group_title
                series_name_input = playlist_entry.series_name
                series_id = (
                    hashlib.sha256(
                        f"{playlist.id}\0{playlist_entry.group_title}\0"
                        f"{playlist_entry.series_name or playlist_entry.title}".encode("utf-8")
                    ).hexdigest()[:24]
                    if kind == "series"
                    else None
                )
                season_input = playlist_entry.season
                episode_input = playlist_entry.episode
                extension_input = playlist_entry.extension
                filename_input = (
                    f"S{playlist_entry.season or 0:02d}"
                    f"E{playlist_entry.episode or playlist_entry.position + 1:02d}."
                    f"{safe_extension(playlist_entry.extension)}"
                    if kind == "series"
                    else f"{playlist_entry.title}.{safe_extension(playlist_entry.extension)}"
                )
                resolved_stream_url = decrypt_secret(
                    playlist_entry.stream_url_encrypted
                )
            identity = stream_identity(source_provider_url, kind, provider_stream_id)
            if identity in seen:
                skipped.append({"stream_id": provider_stream_id, "reason": "duplicate_in_request"})
                continue
            seen.add(identity)

            existing = db.scalar(
                select(QueueItem).where(
                    (QueueItem.stream_id == identity)
                    | (
                        QueueItem.provider_stream_id.is_(None)
                        & (QueueItem.stream_id == provider_stream_id)
                    )
                )
            )
            if existing and existing.status not in ("FAILED", "CANCELLED"):
                skipped.append({"stream_id": provider_stream_id, "reason": existing.status.lower()})
                continue

            category = None
            if category_id:
                category = db.scalar(
                    select(MonitoredCategory).where(
                        MonitoredCategory.kind == kind,
                        MonitoredCategory.category_id == category_id,
                    )
                )
            category_name = (
                category.name if category else category_name_input
            )
            extension = safe_extension(extension_input)
            series_name = series_name_input or (title if kind == "series" else None)
            file_name, season, episode = normalized_episode_path(
                kind=kind,
                title=title,
                category_name=(
                    (category_name or "Uncategorized")
                    if not (category and category.destination_path)
                    else None
                ),
                provider_stream_id=provider_stream_id,
                provider_url=source_provider_url,
                extension=extension,
                series_name=series_name,
                series_id=series_id,
                season=season_input,
                episode_num=episode_input,
                filename=filename_input,
                strip_prefixes=category.strip_prefixes if category else None,
            )
            matched_series_folder = (
                series_storage_matches.get(str(series_id))
                if kind == "series" and series_storage_matches
                else None
            )
            if matched_series_folder:
                folder_parts = PurePosixPath(matched_series_folder).parts
                if (
                    len(folder_parts) != 1
                    or folder_parts[0] in ("", ".", "..")
                    or "\\" in matched_series_folder
                    or "\x00" in matched_series_folder
                ):
                    raise HTTPException(
                        status_code=409,
                        detail="The matched series folder has an unsafe path and cannot be used for queueing.",
                    )
                expected_identity = identity_segment(
                    clean_content_name(
                        series_name or title,
                        category.strip_prefixes if category else None,
                    ),
                    series_id or provider_stream_id,
                    source_provider_url,
                )
                path_parts = list(PurePosixPath(file_name).parts)
                identity_index = next(
                    (
                        index
                        for index, part in enumerate(path_parts[:-2])
                        if part == expected_identity
                    ),
                    None,
                )
                if identity_index is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Could not map the matched series folder to its queue destination.",
                    )
                path_parts[identity_index] = folder_parts[0]
                file_name = str(PurePosixPath(*path_parts))
            if kind == "series":
                series_name = clean_content_name(
                    series_name or title,
                    category.strip_prefixes if category else None,
                )
            item_values = {
                "stream_id": identity,
                "provider_stream_id": provider_stream_id,
                "title": title,
                "kind": kind,
                "source_provider_url": source_provider_url,
                "category_id": category_id,
                "category_name": category_name,
                "series_id": series_id,
                "proxy_mode": payload.proxy_mode,
                "proxy_id": selected_proxy_id,
                "downloader": payload.downloader,
                "stream_url_encrypted": encrypt_secret(
                    resolved_stream_url
                    if resolved_stream_url is not None
                    else stream_url(account, kind, provider_stream_id, extension)
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
                "attempted_account_ids": "[]",
                "updated_at": utc_now(),
            }
            if existing:
                for field, value in item_values.items():
                    setattr(existing, field, value)
                existing.retries = 0
                existing.created_at = utc_now()
            else:
                db.add(QueueItem(**item_values, created_at=utc_now()))
            queued.append({"stream_id": provider_stream_id, "title": title})
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


@queue_router.post("/prepare-series-batch")
async def prepare_series_batch(
    payload: SeriesQueueBatchRequest,
    request: Request,
) -> dict[str, Any]:
    if len(set(payload.account_ids)) != len(payload.account_ids):
        raise HTTPException(status_code=422, detail="Selected account IDs must be unique.")
    series_ids = [series.series_id for series in payload.series]
    if len(set(series_ids)) != len(series_ids):
        raise HTTPException(status_code=422, detail="Selected series IDs must be unique.")
    if (payload.source_account_id is None) == (payload.source_playlist_id is None):
        raise HTTPException(
            status_code=422,
            detail="Select exactly one source account or M3U playlist.",
        )
    if payload.proxy_mode == "selected" and payload.proxy_id is None:
        raise HTTPException(status_code=422, detail="Select a download proxy or choose direct connection.")
    if payload.proxy_mode == "direct" and payload.proxy_id is not None:
        raise HTTPException(status_code=422, detail="Direct connection cannot include a proxy ID.")

    with SessionLocal() as db:
        selected_accounts = db.scalars(
            select(Account).where(
                Account.id.in_(payload.account_ids),
                Account.enabled.is_(True),
            )
        ).all()
        if len(selected_accounts) != len(payload.account_ids):
            raise HTTPException(status_code=409, detail="Selected download accounts must be enabled.")
        source_account = (
            db.get(Account, payload.source_account_id)
            if payload.source_account_id is not None
            else None
        )
        if payload.source_account_id is not None and (
            not source_account or not source_account.enabled
        ):
            raise HTTPException(status_code=409, detail="The source Xtream account is unavailable.")
        if source_account and any(
            selected.provider_url.rstrip("/") != source_account.provider_url.rstrip("/")
            for selected in selected_accounts
        ):
            raise HTTPException(
                status_code=409,
                detail="Xtream download accounts must use the same provider endpoint as the source account.",
            )
        busy_ids = set(db.scalars(
            select(QueueItem.account_id).where(
                QueueItem.account_id.in_(payload.account_ids),
                QueueItem.status.in_(("PENDING", "DOWNLOADING", "VERIFYING", "CANCELLING")),
            ).distinct()
        ).all())
        if busy_ids:
            raise HTTPException(
                status_code=409,
                detail="One or more selected accounts already have queued or active work. Refresh account availability.",
            )
        route = db.get(StorageRoute, "series")
        series_node = db.get(Node, route.node_id) if route else None
        if not series_node:
            raise HTTPException(
                status_code=409,
                detail="Assign a storage node for series content before queueing.",
            )
        if payload.proxy_mode == "selected":
            proxy = db.get(Proxy, payload.proxy_id)
            if not proxy or not proxy.enabled:
                raise HTTPException(
                    status_code=409,
                    detail="The selected download proxy is no longer enabled. Choose another proxy or direct connection.",
                )
            if (
                payload.downloader == "wget"
                and urlsplit(decrypt_secret(proxy.url_encrypted)).scheme.lower()
                in ("socks5", "socks5h")
            ):
                raise HTTPException(
                    status_code=422,
                    detail="Wget does not support SOCKS5 proxies. Choose Axel/aria2c or an HTTP(S) proxy.",
                )

    account = None
    playlist = None
    if payload.source_playlist_id is not None:
        with SessionLocal() as db:
            playlist = db.get(M3UPlaylist, payload.source_playlist_id)
            if not playlist or not playlist.enabled:
                raise HTTPException(status_code=409, detail="The selected M3U playlist is unavailable.")
    else:
        account = _provider_account(payload.source_account_id)

    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    enqueued_first_series = False
    inventory_by_root: dict[str, list[str] | None] = {}

    for selection in payload.series:
        try:
            if playlist is not None:
                details = await get_m3u_series_details(
                    request,
                    playlist.id,
                    selection.series_id,
                    check_storage=False,
                )
            else:
                if account is None:
                    raise HTTPException(
                        status_code=409,
                        detail="The selected Xtream account is unavailable.",
                    )
                details = await get_series_details(
                    request,
                    Response(),
                    selection.series_id,
                    account_id=account.id,
                    category_id=selection.category_id,
                    category_name=selection.category_name,
                    proxy_id=payload.provider_proxy_id,
                    check_storage=False,
                )
            if payload.missing_only:
                if series_node is None:
                    raise HTTPException(
                        status_code=409,
                        detail="A series storage node is not configured.",
                    )
                root, prefixes = _series_storage_location(
                    series_node,
                    selection.category_id,
                    selection.category_name,
                )
                if root is None:
                    raise HTTPException(
                        status_code=409,
                        detail="The series category has no known storage destination.",
                    )
                if root not in inventory_by_root:
                    try:
                        inventory = await asyncio.to_thread(
                            request.app.state.dispatcher.scan_library_files,
                            series_node,
                            {"series": root},
                        )
                        inventory_by_root[root] = inventory["series"]
                    except Exception as exc:
                        inventory_by_root[root] = None
                        logger.warning(
                            "Could not inspect storage for category %s (%s)",
                            selection.category_id,
                            type(exc).__name__,
                        )
                        raise HTTPException(
                            status_code=409,
                            detail="Storage inventory is unavailable; check the storage connection and try again.",
                        ) from exc
                files = inventory_by_root[root]
                if files is None:
                    raise HTTPException(
                        status_code=409,
                        detail="Storage inventory is unavailable; check the storage connection and try again.",
                    )
                provider_url = (
                    f"m3u:{playlist.id}"
                    if playlist is not None
                    else account.provider_url
                    if account is not None
                    else ""
                )
                _mark_series_storage(details, files, provider_url, prefixes)
                if not details.get("storage_inventory_available"):
                    raise HTTPException(
                        status_code=409,
                        detail="Storage inventory is unavailable; check the storage connection and try again.",
                    )
                if details.get("storage_inventory_ambiguous"):
                    raise HTTPException(
                        status_code=409,
                        detail="Multiple series folders match this title. Resolve the folder ambiguity before queueing missing episodes.",
                    )
                if details.get("storage_inventory_unclassified"):
                    raise HTTPException(
                        status_code=409,
                        detail="Some files in the series folder have no recognizable episode number. Review the folder before queueing missing episodes.",
                    )

            entries: list[ManualQueueItem] = []
            series_storage_matches: dict[str, str] = {}
            for season_key, episodes in details["seasons"].items():
                for episode in episodes:
                    if payload.missing_only and episode.get("on_storage") is not False:
                        continue
                    season = episode.get("season")
                    if season is None:
                        season = parse_season_number(season_key) or 0
                    episode_num = int(episode["episode_num"])
                    extension = safe_extension(episode["ext"])
                    entries.append(ManualQueueItem(
                        stream_id=episode["id"],
                        source_entry_id=episode.get("source_entry_id"),
                        title=(
                            f"{details['name']} S{season:02d}E{episode_num:02d}"
                        ),
                        series_name=details["name"],
                        series_id=details["series_id"],
                        season=str(season),
                        episode_num=episode_num,
                        filename=f"S{season:02d}E{episode_num:02d}.{extension}",
                        container_ext=extension,
                        category_id=selection.category_id,
                        category_name=selection.category_name,
                    ))
            if details.get("storage_match"):
                series_storage_matches[str(details["series_id"])] = details[
                    "storage_match"
                ]
            if payload.missing_only and not entries:
                skipped.append({
                    "series_id": selection.series_id,
                    "title": selection.title,
                    "reason": "no_missing_episodes",
                })
                continue
            if not entries:
                failed.append({
                    "series_id": selection.series_id,
                    "title": selection.title,
                    "error": "No episodes were returned for this series.",
                })
                continue

            result = _queue_manual_batch(
                ManualBatchRequest(
                    content_type="SERIES",
                    source_account_id=payload.source_account_id,
                    source_playlist_id=payload.source_playlist_id,
                    account_ids=payload.account_ids,
                    proxy_mode=payload.proxy_mode,
                    proxy_id=payload.proxy_id,
                    downloader=payload.downloader,
                    items=entries,
                ),
                request,
                allow_existing_account_queue=enqueued_first_series,
                series_storage_matches=series_storage_matches,
            )
            enqueued_first_series = True
            queued.extend(result["queued"])
            skipped.extend(result["skipped"])
        except HTTPException as exc:
            failed.append({
                "series_id": selection.series_id,
                "title": selection.title,
                "error": str(exc.detail),
            })
            logger.warning(
                "Series queue preparation failed for series %s (HTTP %s)",
                selection.series_id,
                exc.status_code,
            )

    return {
        "queued": queued,
        "queued_count": len(queued),
        "skipped": skipped,
        "failed": failed,
        "processed_series": len(payload.series) - len(failed),
    }


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
