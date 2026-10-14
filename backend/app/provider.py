from typing import Any
from urllib.parse import quote, urljoin

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
        response = await client.get(api_url, params=query, headers={"User-Agent": account.user_agent})
        response.raise_for_status()
        return response.json()


def stream_url(account: Account, kind: str, stream_id: str, extension: str = "mkv") -> str:
    base_url = account.provider_url.rstrip("/")
    password = decrypt_secret(account.password_encrypted)
    route = "series" if kind == "series" else "movie"
    return (
        f"{base_url}/{route}/{quote(account.username, safe='')}/{quote(password, safe='')}/"
        f"{quote(stream_id, safe='')}.{quote(extension.lstrip('.'), safe='')}"
    )
