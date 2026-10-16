import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import (
    Account,
    AppSettings,
    ProviderCatalogCache,
    Proxy,
    SessionLocal,
    utc_now,
)
from app.security import decrypt_secret

logger = logging.getLogger(__name__)
_series_details_lock = asyncio.Lock()
_last_series_details_request_at = 0.0


def _provider_cache_params_key(account: Account, params: dict[str, Any]) -> str:
    cache_params = {
        key: str(value) if key.endswith("_id") and value is not None else value
        for key, value in params.items()
    }
    return json.dumps(
        {
            "provider_url": account.provider_url.rstrip("/"),
            "username": account.username,
            "params": cache_params,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def invalidate_provider_action_cache(
    account: Account,
    action: str,
    **params: Any,
) -> None:
    params_key = _provider_cache_params_key(account, params)
    with SessionLocal() as db:
        db.execute(
            delete(ProviderCatalogCache).where(
                ProviderCatalogCache.account_id == account.id,
                ProviderCatalogCache.action == action,
                ProviderCatalogCache.params_key == params_key,
            )
        )
        db.commit()


def parse_series_episodes(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("Provider returned invalid series details.")
    episodes_by_season = data.get("episodes", {})
    if not isinstance(episodes_by_season, dict):
        raise ValueError("Provider returned an invalid episode list.")

    parsed: list[dict[str, Any]] = []
    for season_key, season_episodes in episodes_by_season.items():
        if not isinstance(season_episodes, list):
            raise ValueError(f"Provider returned an invalid episode list for season {season_key}.")
        for episode in season_episodes:
            if not isinstance(episode, dict) or episode.get("id") is None:
                raise ValueError(f"Provider returned an invalid episode in season {season_key}.")
            try:
                episode_num = int(episode["episode_num"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Provider returned an invalid episode number in season {season_key}."
                ) from exc
            parsed.append({
                "season": str(season_key),
                "stream_id": str(episode["id"]),
                "episode_num": episode_num,
                "container_extension": str(episode.get("container_extension") or "mkv"),
                "title": str(episode.get("title") or f"Episode {episode_num}"),
            })
    return parsed


def resolve_provider_proxy(proxy_id: int | None) -> str | None:
    if proxy_id is None:
        return None
    with SessionLocal() as db:
        proxy = db.get(Proxy, proxy_id)
        if not proxy or not proxy.enabled:
            raise ValueError("The selected provider proxy is unavailable or disabled.")
        return decrypt_secret(proxy.url_encrypted)


def resolve_provider_proxy_selection(proxy_id: int | None) -> str | None:
    if proxy_id == 0:
        return None
    if proxy_id is None:
        with SessionLocal() as db:
            setting = db.get(AppSettings, 1)
            proxy_id = setting.provider_proxy_id if setting else None
    return resolve_provider_proxy(proxy_id)


async def provider_action(
    account: Account,
    action: str,
    *,
    proxy_url: str | None = None,
    **params: Any,
) -> Any:
    base_url = account.provider_url.rstrip("/") + "/"
    api_url = urljoin(base_url, "player_api.php")
    query = {
        "username": account.username,
        "password": decrypt_secret(account.password_encrypted),
        "action": action,
        **params,
    }
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        proxy=proxy_url,
    ) as client:
        response = await client.get(
            api_url,
            params=query,
            headers={"User-Agent": account.user_agent or "Mozilla"},
        )
        response.raise_for_status()
        return response.json()


async def cached_provider_action(
    account: Account,
    action: str,
    *,
    proxy_url: str | None = None,
    force_refresh: bool = False,
    **params: Any,
) -> tuple[Any, str]:
    params_key = _provider_cache_params_key(account, params)
    with SessionLocal() as db:
        cached = db.scalar(
            select(ProviderCatalogCache).where(
                ProviderCatalogCache.account_id == account.id,
                ProviderCatalogCache.action == action,
                ProviderCatalogCache.params_key == params_key,
            )
        )
        cached_payload = None
        cached_at = None
        if cached:
            cached_at = cached.fetched_at
            try:
                cached_payload = json.loads(cached.payload)
            except json.JSONDecodeError:
                logger.warning(
                    "Ignoring invalid cached provider payload for account %s action %s",
                    account.id,
                    action,
                )

    ttl_seconds = (
        settings.provider_series_details_ttl_seconds
        if action == "get_series_info"
        else settings.provider_catalog_ttl_seconds
    )
    if cached_payload is not None and cached_at is not None and not force_refresh:
        cache_time = cached_at
        if cache_time.tzinfo is None:
            cache_time = cache_time.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - cache_time).total_seconds()
        if age_seconds < ttl_seconds:
            return cached_payload, "hit"

    try:
        if action == "get_series_info":
            global _last_series_details_request_at
            async with _series_details_lock:
                loop = asyncio.get_running_loop()
                wait_seconds = (
                    _last_series_details_request_at
                    + settings.provider_series_details_interval_seconds
                    - loop.time()
                )
                if wait_seconds > 0:
                    await asyncio.sleep(wait_seconds)
                _last_series_details_request_at = loop.time()
                payload = await provider_action(
                    account,
                    action,
                    proxy_url=proxy_url,
                    **params,
                )
        else:
            payload = await provider_action(
                account,
                action,
                proxy_url=proxy_url,
                **params,
            )
        serialized_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except Exception as exc:
        if cached_payload is None:
            raise
        logger.warning(
            "Provider refresh failed for account %s action %s (%s); serving stale catalog",
            account.id,
            action,
            type(exc).__name__,
        )
        return cached_payload, "stale"

    with SessionLocal() as db:
        entry = db.scalar(
            select(ProviderCatalogCache).where(
                ProviderCatalogCache.account_id == account.id,
                ProviderCatalogCache.action == action,
                ProviderCatalogCache.params_key == params_key,
            )
        )
        if entry is None:
            entry = ProviderCatalogCache(
                account_id=account.id,
                action=action,
                params_key=params_key,
                payload=serialized_payload,
                fetched_at=utc_now(),
            )
            db.add(entry)
        else:
            entry.payload = serialized_payload
            entry.fetched_at = utc_now()
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            entry = db.scalar(
                select(ProviderCatalogCache).where(
                    ProviderCatalogCache.account_id == account.id,
                    ProviderCatalogCache.action == action,
                    ProviderCatalogCache.params_key == params_key,
                )
            )
            if entry is None:
                raise
            entry.payload = serialized_payload
            entry.fetched_at = utc_now()
            db.commit()
    return payload, "refreshed"


def stream_url(account: Account, kind: str, stream_id: str, extension: str = "mkv") -> str:
    parsed_base = urlsplit(account.provider_url.rstrip("/"))
    if parsed_base.scheme.lower() in ("http", "https") and parsed_base.port is None:
        hostname = parsed_base.hostname
        if not hostname:
            raise ValueError("Provider URL must include a valid host.")
        quoted_host = f"[{hostname}]" if ":" in hostname else hostname
        userinfo, separator, _host = parsed_base.netloc.rpartition("@")
        authority = f"{userinfo}{separator}" if separator else ""
        default_port = 80 if parsed_base.scheme.lower() == "http" else 443
        parsed_base = parsed_base._replace(
            netloc=f"{authority}{quoted_host}:{default_port}"
        )
    base_url = urlunsplit(parsed_base).rstrip("/")
    password = decrypt_secret(account.password_encrypted)
    route = "series" if kind == "series" else "movie"
    return (
        f"{base_url}/{route}/{quote(account.username, safe='')}/{quote(password, safe='')}/"
        f"{quote(stream_id, safe='')}.{quote(extension.lstrip('.'), safe='')}"
    )
