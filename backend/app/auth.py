import hmac
import hashlib
import secrets
import time
from collections import defaultdict, deque
from typing import Any

from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.config import settings

COOKIE_NAME = "media_ingestion_session"
_login_failures: dict[str, deque[float]] = defaultdict(deque)
_MAX_FAILURES = 5
_FAILURE_WINDOW_SECONDS = 900


def validate_auth_settings() -> None:
    if not settings.admin_username or len(settings.admin_password) < 12:
        raise RuntimeError(
            "Set ADMIN_USERNAME and an ADMIN_PASSWORD of at least 12 characters before starting."
        )
    if len(settings.session_secret_key) < 32:
        raise RuntimeError("Set SESSION_SECRET_KEY to a random value of at least 32 characters.")


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret_key, salt="media-ingestion-session-v1")


def _auth_revision() -> str:
    return hmac.new(
        settings.session_secret_key.encode("utf-8"),
        f"{settings.admin_username}\0{settings.admin_password}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_session() -> tuple[str, str]:
    csrf_token = secrets.token_urlsafe(32)
    token = _serializer().dumps({
        "username": settings.admin_username,
        "csrf": csrf_token,
        "auth_revision": _auth_revision(),
    })
    return token, csrf_token


def read_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        session = _serializer().loads(token, max_age=settings.session_ttl_seconds)
    except (BadSignature, TypeError, ValueError):
        return None
    if not isinstance(session, dict) or session.get("username") != settings.admin_username:
        return None
    if not hmac.compare_digest(str(session.get("auth_revision", "")), _auth_revision()):
        return None
    if not isinstance(session.get("csrf"), str):
        return None
    return session


def authenticate(username: str, password: str) -> bool:
    username_matches = hmac.compare_digest(username, settings.admin_username)
    password_matches = hmac.compare_digest(password, settings.admin_password)
    return username_matches & password_matches


def check_login_limit(client_key: str) -> int:
    now = time.monotonic()
    attempts = _login_failures[client_key]
    while attempts and now - attempts[0] >= _FAILURE_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= _MAX_FAILURES:
        return max(1, int(_FAILURE_WINDOW_SECONDS - (now - attempts[0])))
    return 0


def record_login_failure(client_key: str) -> None:
    _login_failures[client_key].append(time.monotonic())


def clear_login_failures(client_key: str) -> None:
    _login_failures.pop(client_key, None)


def safe_csrf_match(expected: str, actual: str | None) -> bool:
    return bool(actual) and hmac.compare_digest(expected, actual)
