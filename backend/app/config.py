import math
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./ingestion.db")
    encryption_key: str = os.getenv("APP_ENCRYPTION_KEY", "")
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
    provider_catalog_ttl_seconds: int = int(
        os.getenv("PROVIDER_CATALOG_TTL_SECONDS", "1800")
    )
    provider_series_details_ttl_seconds: int = int(
        os.getenv("PROVIDER_SERIES_DETAILS_TTL_SECONDS", "21600")
    )
    provider_series_details_interval_seconds: float = float(
        os.getenv("PROVIDER_SERIES_DETAILS_INTERVAL_SECONDS", "3")
    )
    provider_sync_max_attempts: int = int(
        os.getenv("PROVIDER_SYNC_MAX_ATTEMPTS", "3")
    )
    provider_sync_retry_base_seconds: float = float(
        os.getenv("PROVIDER_SYNC_RETRY_BASE_SECONDS", "1")
    )
    provider_sync_retry_max_seconds: float = float(
        os.getenv("PROVIDER_SYNC_RETRY_MAX_SECONDS", "15")
    )
    worker_poll_seconds: int = int(os.getenv("WORKER_POLL_SECONDS", "2"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "20"))
    request_timeout_seconds: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30"))
    remote_job_timeout_seconds: int = int(os.getenv("REMOTE_JOB_TIMEOUT_SECONDS", "86400"))
    remote_download_idle_timeout_seconds: int = int(
        os.getenv("REMOTE_DOWNLOAD_IDLE_TIMEOUT_SECONDS", "300")
    )
    default_axel_connections: int = int(os.getenv("DEFAULT_AXEL_CONNECTIONS", "12"))
    master_user_agent: str = os.getenv(
        "MASTER_USER_AGENT", "VLC/3.0.18 LibVLC/3.0.18"
    )
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

    def __post_init__(self) -> None:
        if self.max_retries < 1:
            raise ValueError("MAX_RETRIES must be at least 1.")
        if not 1 <= self.default_axel_connections <= 12:
            raise ValueError("DEFAULT_AXEL_CONNECTIONS must be between 1 and 12.")
        if not self.master_user_agent.strip():
            raise ValueError("MASTER_USER_AGENT cannot be empty.")
        if self.provider_series_details_interval_seconds < 0:
            raise ValueError("PROVIDER_SERIES_DETAILS_INTERVAL_SECONDS cannot be negative.")
        if not 1 <= self.provider_sync_max_attempts <= 10:
            raise ValueError("PROVIDER_SYNC_MAX_ATTEMPTS must be between 1 and 10.")
        if self.provider_sync_retry_base_seconds <= 0:
            raise ValueError("PROVIDER_SYNC_RETRY_BASE_SECONDS must be positive.")
        if not math.isfinite(self.provider_sync_retry_base_seconds):
            raise ValueError("PROVIDER_SYNC_RETRY_BASE_SECONDS must be finite.")
        if self.provider_sync_retry_max_seconds < self.provider_sync_retry_base_seconds:
            raise ValueError(
                "PROVIDER_SYNC_RETRY_MAX_SECONDS must be at least "
                "PROVIDER_SYNC_RETRY_BASE_SECONDS."
            )
        if not math.isfinite(self.provider_sync_retry_max_seconds):
            raise ValueError("PROVIDER_SYNC_RETRY_MAX_SECONDS must be finite.")
        if self.remote_download_idle_timeout_seconds < 1:
            raise ValueError("REMOTE_DOWNLOAD_IDLE_TIMEOUT_SECONDS must be positive.")


settings = Settings()
