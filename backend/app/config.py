import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./ingestion.db")
    encryption_key: str = os.getenv("APP_ENCRYPTION_KEY", "")
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    worker_poll_seconds: int = int(os.getenv("WORKER_POLL_SECONDS", "2"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "5"))
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    remote_job_timeout_seconds: int = int(os.getenv("REMOTE_JOB_TIMEOUT_SECONDS", "86400"))
    known_hosts_path: str = os.getenv("SSH_KNOWN_HOSTS", "")
    admin_username: str = os.getenv("ADMIN_USERNAME", "")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    session_secret_key: str = os.getenv("SESSION_SECRET_KEY", "")
    session_ttl_seconds: int = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
    session_cookie_secure: bool = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
        if origin.strip()
    )


settings = Settings()
