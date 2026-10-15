from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
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
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, default=0)
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
            },
            "nodes": {
                "name": "VARCHAR(255) NOT NULL DEFAULT 'Storage server'",
                "axel_connections": (
                    f"INTEGER NOT NULL DEFAULT {settings.default_axel_connections}"
                ),
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
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
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
