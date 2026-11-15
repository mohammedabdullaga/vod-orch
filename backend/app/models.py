import json
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine, "connect")
def set_sqlite_pragmas(connection, _record):
    if settings.database_url.startswith("sqlite"):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("provider_url", "username"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_url: Mapped[str] = mapped_column(String(2048))
    username: Mapped[str] = mapped_column(String(255))
    password_encrypted: Mapped[str] = mapped_column(Text)
    user_agent: Mapped[str] = mapped_column(String(1024), default="Mozilla")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProviderCatalogCache(Base):
    __tablename__ = "provider_catalog_cache"
    __table_args__ = (
        UniqueConstraint("account_id", "action", "params_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(64))
    params_key: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProviderProfileCatalogCache(Base):
    __tablename__ = "provider_profile_catalog_cache"
    __table_args__ = (
        UniqueConstraint("provider_url", "action", "params_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_url: Mapped[str] = mapped_column(String(2048), index=True)
    action: Mapped[str] = mapped_column(String(64))
    params_key: Mapped[str] = mapped_column(Text)
    payload: Mapped[str] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ProviderCatalogCategory(Base):
    __tablename__ = "provider_catalog_categories"
    __table_args__ = (
        UniqueConstraint("account_id", "kind", "category_id"),
        Index("ix_provider_catalog_categories_account_kind", "account_id", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    category_id: Mapped[str] = mapped_column(String(255), index=True)
    category_name: Mapped[str] = mapped_column(String(512))
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    items_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProviderCatalogItem(Base):
    __tablename__ = "provider_catalog_items"
    __table_args__ = (
        UniqueConstraint("account_id", "kind", "category_id", "stream_id"),
        Index(
            "ix_provider_catalog_items_lookup",
            "account_id",
            "kind",
            "category_id",
            "name",
        ),
        Index(
            "ix_provider_catalog_items_account_stream",
            "account_id",
            "kind",
            "stream_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    category_id: Mapped[str] = mapped_column(String(255), index=True)
    category_name: Mapped[str] = mapped_column(String(512))
    stream_id: Mapped[str] = mapped_column(String(512), index=True)
    name: Mapped[str] = mapped_column(String(1024), index=True)
    cover: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_seasons: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[str | None] = mapped_column(String(8), nullable=True)
    container_ext: Mapped[str] = mapped_column(String(16), default="mkv")
    download_status: Mapped[str] = mapped_column(String(16), default="unknown")
    storage_match: Mapped[str | None] = mapped_column(Text, nullable=True)
    inventory_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProviderProfileCatalogCategory(Base):
    __tablename__ = "provider_profile_catalog_categories"
    __table_args__ = (
        UniqueConstraint("provider_url", "kind", "category_id"),
        Index("ix_profile_catalog_categories_profile_kind", "provider_url", "kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_url: Mapped[str] = mapped_column(String(2048), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    category_id: Mapped[str] = mapped_column(String(255), index=True)
    category_name: Mapped[str] = mapped_column(String(512))
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    items_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProviderProfileCatalogItem(Base):
    __tablename__ = "provider_profile_catalog_items"
    __table_args__ = (
        UniqueConstraint("provider_url", "kind", "category_id", "stream_id"),
        Index(
            "ix_profile_catalog_items_lookup",
            "provider_url",
            "kind",
            "category_id",
            "name",
        ),
        Index(
            "ix_profile_catalog_items_stream",
            "provider_url",
            "kind",
            "stream_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_url: Mapped[str] = mapped_column(String(2048), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    category_id: Mapped[str] = mapped_column(String(255), index=True)
    category_name: Mapped[str] = mapped_column(String(512))
    stream_id: Mapped[str] = mapped_column(String(512), index=True)
    name: Mapped[str] = mapped_column(String(1024), index=True)
    cover: Mapped[str | None] = mapped_column(Text, nullable=True)
    total_seasons: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    year: Mapped[str | None] = mapped_column(String(8), nullable=True)
    container_ext: Mapped[str] = mapped_column(String(16), default="mkv")
    download_status: Mapped[str] = mapped_column(String(16), default="unknown")
    storage_match: Mapped[str | None] = mapped_column(Text, nullable=True)
    inventory_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class ProviderCatalogSyncJob(Base):
    __tablename__ = "provider_catalog_sync_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    proxy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempted_account_ids: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    phase: Mapped[str] = mapped_column(String(32), default="queued")
    categories_processed: Mapped[int] = mapped_column(Integer, default=0)
    lists_processed: Mapped[int] = mapped_column(Integer, default=0)
    cached_items: Mapped[int] = mapped_column(Integer, default=0)
    series_details_processed: Mapped[int] = mapped_column(Integer, default=0)
    total_series_details: Mapped[int] = mapped_column(Integer, default=0)
    progress_current: Mapped[int] = mapped_column(Integer, default=0)
    progress_total: Mapped[int] = mapped_column(Integer, default=0)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    seasons_cached: Mapped[int] = mapped_column(Integer, default=0)
    episodes_cached: Mapped[int] = mapped_column(Integer, default=0)
    episode_ids_cached: Mapped[int] = mapped_column(Integer, default=0)
    stale_responses: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    invalid_series_details: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class M3UPlaylist(Base):
    __tablename__ = "m3u_playlists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    source_url_encrypted: Mapped[str] = mapped_column(Text)
    proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class M3UFetchJob(Base):
    __tablename__ = "m3u_fetch_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(255))
    playlist_id: Mapped[int | None] = mapped_column(
        ForeignKey("m3u_playlists.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_url_encrypted: Mapped[str] = mapped_column(Text)
    proxy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class M3UEntry(Base):
    __tablename__ = "m3u_entries"
    __table_args__ = (
        UniqueConstraint("playlist_id", "stable_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[int] = mapped_column(
        ForeignKey("m3u_playlists.id", ondelete="CASCADE"), index=True
    )
    stable_key: Mapped[str] = mapped_column(String(64))
    position: Mapped[int] = mapped_column(Integer)
    group_title: Mapped[str] = mapped_column(String(512), default="Uncategorized")
    kind: Mapped[str] = mapped_column(String(16), default="vod", index=True)
    title: Mapped[str] = mapped_column(String(1024))
    tvg_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    tvg_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    tvg_logo: Mapped[str | None] = mapped_column(Text, nullable=True)
    stream_url_encrypted: Mapped[str] = mapped_column(Text)
    extension: Mapped[str] = mapped_column(String(32), default="mkv")
    series_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attributes_json: Mapped[str] = mapped_column(Text, default="{}")
    directives_json: Mapped[str] = mapped_column(Text, default="[]")


class Proxy(Base):
    __tablename__ = "proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url_encrypted: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AppSettings(Base):
    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    provider_proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True
    )


class MonitoredCategory(Base):
    __tablename__ = "monitored_categories"
    __table_args__ = (UniqueConstraint("category_id", "kind"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    destination_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_existing_series: Mapped[bool] = mapped_column(Boolean, default=False)
    strip_prefixes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ProviderProfileMonitoredCategory(Base):
    __tablename__ = "provider_profile_monitored_categories"
    __table_args__ = (
        UniqueConstraint("provider_url", "category_id", "kind"),
        Index(
            "ix_profile_monitored_categories_lookup",
            "provider_url",
            "kind",
            "category_id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_url: Mapped[str] = mapped_column(String(2048), index=True)
    category_id: Mapped[str] = mapped_column(String(255), index=True)
    name: Mapped[str] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(16))
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)
    destination_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    match_existing_series: Mapped[bool] = mapped_column(Boolean, default=False)
    strip_prefixes: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class QueueItem(Base):
    __tablename__ = "queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    provider_stream_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    title: Mapped[str] = mapped_column(String(1024))
    kind: Mapped[str] = mapped_column(String(16))
    category_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    category_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_provider_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    proxy_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True
    )
    downloader: Mapped[str] = mapped_column(String(16), default="axel")
    series_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    stream_url_encrypted: Mapped[str] = mapped_column(Text)
    file_name: Mapped[str] = mapped_column(String(1024))
    final_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    series_title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    season: Mapped[int | None] = mapped_column(Integer, nullable=True)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    hidden_from_dashboard: Mapped[bool] = mapped_column(Boolean, default=False)
    progress: Mapped[float] = mapped_column(Float, default=0)
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    speed_bytes_per_second: Mapped[float | None] = mapped_column(Float, nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    source_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    destination_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    destination_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    remote_node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    remote_match_existing_series: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dispatched_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_log: Mapped[str] = mapped_column(Text, default="")
    task_log_offset: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    attempted_account_ids: Mapped[str] = mapped_column(Text, default="[]")
    eligible_account_ids: Mapped[str] = mapped_column(Text, default="[]")
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="Storage server")
    host: Mapped[str] = mapped_column(String(512))
    port: Mapped[int] = mapped_column(Integer, default=22)
    username: Mapped[str] = mapped_column(String(255))
    private_key_path: Mapped[str] = mapped_column(Text)
    staging_path: Mapped[str] = mapped_column(Text)
    watch_path: Mapped[str] = mapped_column(Text)
    axel_connections: Mapped[int] = mapped_column(
        Integer, default=settings.default_axel_connections
    )
    proxy_id: Mapped[int | None] = mapped_column(
        ForeignKey("proxies.id", ondelete="SET NULL"), nullable=True
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class StorageRoute(Base):
    __tablename__ = "storage_routes"

    kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    node_id: Mapped[int] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        cache_migration = "shared-provider-profile-catalog-cache-v1"
        cache_migration_applied = connection.exec_driver_sql(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (cache_migration,),
        ).first()
        if not cache_migration_applied:
            legacy_cache_rows = connection.exec_driver_sql(
                "SELECT accounts.provider_url, provider_catalog_cache.action, "
                "provider_catalog_cache.params_key, provider_catalog_cache.payload, "
                "provider_catalog_cache.fetched_at "
                "FROM provider_catalog_cache "
                "JOIN accounts ON accounts.id = provider_catalog_cache.account_id "
                "ORDER BY provider_catalog_cache.fetched_at DESC"
            ).mappings()
            for row in legacy_cache_rows:
                try:
                    cached_params = json.loads(row["params_key"]).get("params", {})
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                params_key = json.dumps(
                    cached_params,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO provider_profile_catalog_cache "
                    "(provider_url, action, params_key, payload, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["provider_url"].rstrip("/"),
                        row["action"],
                        params_key,
                        row["payload"],
                        row["fetched_at"],
                    ),
                )
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (cache_migration,),
            )
        account_cache_migration = "provider-catalog-cache-account-scope-v2"
        account_cache_migration_applied = connection.exec_driver_sql(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (account_cache_migration,),
        ).first()
        if not account_cache_migration_applied:
            connection.exec_driver_sql("DELETE FROM provider_profile_catalog_cache")
            legacy_cache_rows = connection.exec_driver_sql(
                "SELECT accounts.provider_url, accounts.username, "
                "provider_catalog_cache.action, provider_catalog_cache.params_key, "
                "provider_catalog_cache.payload, provider_catalog_cache.fetched_at "
                "FROM provider_catalog_cache "
                "JOIN accounts ON accounts.id = provider_catalog_cache.account_id "
                "ORDER BY provider_catalog_cache.fetched_at DESC"
            ).mappings()
            for row in legacy_cache_rows:
                try:
                    stored_key = json.loads(row["params_key"])
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(stored_key, dict):
                    continue
                cached_params = stored_key.get("params", stored_key)
                if not isinstance(cached_params, dict):
                    continue
                params_key = json.dumps(
                    {"username": row["username"], "params": cached_params},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO provider_profile_catalog_cache "
                    "(provider_url, action, params_key, payload, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["provider_url"].rstrip("/"),
                        row["action"],
                        params_key,
                        row["payload"],
                        row["fetched_at"],
                    ),
                )
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (account_cache_migration,),
            )

        migrations = {
            "monitored_categories": {
                "destination_path": "TEXT",
                "match_existing_series": "BOOLEAN NOT NULL DEFAULT 0",
                "strip_prefixes": "TEXT NOT NULL DEFAULT ''",
            },
            "queue": {
                "provider_stream_id": "VARCHAR(512)",
                "category_id": "VARCHAR(255)",
                "category_name": "VARCHAR(512)",
                "source_provider_url": "VARCHAR(2048)",
                "proxy_mode": "VARCHAR(16)",
                "proxy_id": "INTEGER",
                "downloader": "VARCHAR(16) NOT NULL DEFAULT 'axel'",
                "series_id": "VARCHAR(512)",
                "hidden_from_dashboard": "BOOLEAN NOT NULL DEFAULT 0",
                "final_path": "TEXT",
                "file_size_bytes": "INTEGER",
                "speed_bytes_per_second": "FLOAT",
                "attempted_account_ids": "TEXT NOT NULL DEFAULT '[]'",
                "eligible_account_ids": "TEXT NOT NULL DEFAULT '[]'",
                "source_account_id": "INTEGER",
                "remote_node_id": "INTEGER",
                "destination_node_id": "INTEGER",
                "destination_path": "TEXT",
                "remote_match_existing_series": "BOOLEAN NOT NULL DEFAULT 0",
                "dispatched_command": "TEXT",
                "task_log": "TEXT NOT NULL DEFAULT ''",
                "task_log_offset": "INTEGER NOT NULL DEFAULT 0",
                "last_error": "TEXT",
            },
            "nodes": {
                "name": "VARCHAR(255) NOT NULL DEFAULT 'Storage server'",
                "axel_connections": (
                    f"INTEGER NOT NULL DEFAULT {settings.default_axel_connections}"
                ),
            },
            "m3u_playlists": {
                "proxy_id": "INTEGER",
            },
            "provider_catalog_sync_jobs": {
                "cache_hits": "INTEGER NOT NULL DEFAULT 0",
                "invalid_series_details": "INTEGER NOT NULL DEFAULT 0",
                "stale_responses": "INTEGER NOT NULL DEFAULT 0",
                "attempted_account_ids": "TEXT NOT NULL DEFAULT '[]'",
                "progress_current": "INTEGER NOT NULL DEFAULT 0",
                "progress_total": "INTEGER NOT NULL DEFAULT 0",
                "progress_percent": "INTEGER NOT NULL DEFAULT 0",
                "seasons_cached": "INTEGER NOT NULL DEFAULT 0",
                "episodes_cached": "INTEGER NOT NULL DEFAULT 0",
                "episode_ids_cached": "INTEGER NOT NULL DEFAULT 0",
                "warning": "TEXT",
            },
        }
        inspector = inspect(connection)
        for table_name, columns in migrations.items():
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_type in columns.items():
                if column_name not in existing:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
                    )
                    if table_name == "m3u_playlists" and column_name == "proxy_id":
                        connection.exec_driver_sql(
                            "UPDATE m3u_playlists SET proxy_id = "
                            "(SELECT provider_proxy_id FROM app_settings WHERE id = 1) "
                            "WHERE proxy_id IS NULL"
                        )
        axel_migration = "default-axel-connections-12"
        already_applied = connection.exec_driver_sql(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (axel_migration,),
        ).first()
        if not already_applied:
            connection.exec_driver_sql(
                "UPDATE nodes SET axel_connections = 12 WHERE axel_connections = 4"
            )
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (axel_migration,),
            )
        legacy_node = connection.exec_driver_sql(
            "SELECT id FROM nodes ORDER BY id LIMIT 1"
        ).first()
        if legacy_node:
            for kind in ("series", "vod"):
                connection.exec_driver_sql(
                    "INSERT OR IGNORE INTO storage_routes (kind, node_id, updated_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (kind, legacy_node[0]),
                )
        profile_catalog_migration = "provider-profile-catalog-and-watchlist-v1"
        profile_migration_applied = connection.exec_driver_sql(
            "SELECT 1 FROM schema_migrations WHERE version = ?",
            (profile_catalog_migration,),
        ).first()
        if not profile_migration_applied:
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO provider_profile_catalog_categories "
                "(provider_url, kind, category_id, category_name, item_count, "
                "items_synced_at, updated_at) "
                "SELECT RTRIM(accounts.provider_url, '/'), categories.kind, "
                "categories.category_id, categories.category_name, categories.item_count, "
                "categories.items_synced_at, categories.updated_at "
                "FROM provider_catalog_categories AS categories "
                "JOIN accounts ON accounts.id = categories.account_id "
                "ORDER BY accounts.id"
            )
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO provider_profile_catalog_items "
                "(provider_url, kind, category_id, category_name, stream_id, name, cover, "
                "total_seasons, total_episodes, year, container_ext, download_status, "
                "storage_match, inventory_checked_at, updated_at) "
                "SELECT RTRIM(accounts.provider_url, '/'), items.kind, items.category_id, "
                "items.category_name, items.stream_id, items.name, items.cover, "
                "items.total_seasons, items.total_episodes, items.year, items.container_ext, "
                "items.download_status, items.storage_match, items.inventory_checked_at, "
                "items.updated_at FROM provider_catalog_items AS items "
                "JOIN accounts ON accounts.id = items.account_id "
                "ORDER BY accounts.id"
            )
            connection.exec_driver_sql(
                "INSERT OR IGNORE INTO provider_profile_monitored_categories "
                "(provider_url, category_id, name, kind, monitored, destination_path, "
                "match_existing_series, strip_prefixes, updated_at) "
                "SELECT RTRIM((SELECT provider_url FROM accounts ORDER BY id LIMIT 1), '/'), "
                "categories.category_id, categories.name, categories.kind, "
                "categories.monitored, categories.destination_path, "
                "categories.match_existing_series, categories.strip_prefixes, "
                "categories.updated_at FROM monitored_categories AS categories "
                "WHERE EXISTS (SELECT 1 FROM accounts)"
            )
            connection.exec_driver_sql(
                "INSERT INTO schema_migrations (version) VALUES (?)",
                (profile_catalog_migration,),
            )
