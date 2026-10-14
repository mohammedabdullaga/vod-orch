from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


def _fernet() -> Fernet:
    if not settings.encryption_key:
        raise RuntimeError("APP_ENCRYPTION_KEY must be configured to store credentials.")
    try:
        return Fernet(settings.encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise RuntimeError("APP_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError) as exc:
        raise RuntimeError("Stored credential could not be decrypted; verify APP_ENCRYPTION_KEY.") from exc


def mask_secret(value: str) -> str:
    return f"••••{value[-4:]}" if len(value) > 4 else "••••"
