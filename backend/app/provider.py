from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx

from app.config import settings
from app.models import Account
from app.security import decrypt_secret


async def provider_action(account: Account, action: str, **params: Any) -> Any:
    base_url = account.provider_url.rstrip("/") + "/"
    api_url = urljoin(base_url, "player_api.php")
    query = {
        "username": account.username,
        "password": decrypt_secret(account.password_encrypted),
        "action": action,
        **params,
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.get(
            api_url,
            params=query,
            headers={"User-Agent": account.user_agent or "Mozilla"},
        )
        response.raise_for_status()
        return response.json()


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
