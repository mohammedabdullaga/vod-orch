import re
from typing import Any

from sqlalchemy import delete, select

from app.models import (
    ProviderProfileCatalogCategory,
    ProviderProfileCatalogItem,
    ProviderProfileMonitoredCategory,
    SessionLocal,
    utc_now,
)
from app.services.content_paths import safe_extension


def _item_id(row: dict[str, Any], kind: str) -> str | None:
    keys = ("series_id", "id") if kind == "series" else ("stream_id", "id")
    for key in keys:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _integer_or_none(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _year(row: dict[str, Any]) -> str | None:
    value = row.get("year") or row.get("releaseDate") or row.get("releasedate")
    if value is None:
        return None
    match = re.search(r"\b(19|20)\d{2}\b", str(value))
    return match.group(0) if match else None


def store_categories(
    provider_url: str,
    kind: str,
    rows: list[dict[str, Any]],
) -> None:
    now = utc_now()
    with SessionLocal() as db:
        existing = db.scalars(
            select(ProviderProfileCatalogCategory).where(
                ProviderProfileCatalogCategory.provider_url == provider_url,
                ProviderProfileCatalogCategory.kind == kind,
            )
        ).all()
        by_id = {category.category_id: category for category in existing}
        monitored_by_id = {
            category.category_id: category
            for category in db.scalars(
                select(ProviderProfileMonitoredCategory).where(
                    ProviderProfileMonitoredCategory.provider_url == provider_url,
                    ProviderProfileMonitoredCategory.kind == kind,
                )
            ).all()
        }
        seen: set[str] = set()
        for row in rows:
            category_id = row.get("category_id", row.get("id"))
            if category_id is None or not str(category_id).strip():
                continue
            category_id = str(category_id)
            if category_id in seen:
                continue
            seen.add(category_id)
            category = by_id.get(category_id)
            name = str(row.get("category_name", row.get("name", "Unknown")))[:512]
            if category is None:
                db.add(
                    ProviderProfileCatalogCategory(
                        provider_url=provider_url,
                        kind=kind,
                        category_id=category_id,
                        category_name=name,
                        updated_at=now,
                    )
                )
            else:
                category.category_name = name
                category.updated_at = now
            monitored = monitored_by_id.get(category_id)
            if monitored is not None:
                monitored.name = name
                monitored.updated_at = now
        removed = set(by_id) - seen
        if removed:
            db.execute(
                delete(ProviderProfileCatalogItem).where(
                    ProviderProfileCatalogItem.provider_url == provider_url,
                    ProviderProfileCatalogItem.kind == kind,
                    ProviderProfileCatalogItem.category_id.in_(removed),
                )
            )
            db.execute(
                delete(ProviderProfileCatalogCategory).where(
                    ProviderProfileCatalogCategory.provider_url == provider_url,
                    ProviderProfileCatalogCategory.kind == kind,
                    ProviderProfileCatalogCategory.category_id.in_(removed),
                )
            )
        db.commit()


def store_category_items(
    provider_url: str,
    kind: str,
    category_id: str,
    category_name: str,
    rows: list[dict[str, Any]],
) -> None:
    now = utc_now()
    indexed: list[ProviderProfileCatalogItem] = []
    seen: set[str] = set()
    for row in rows:
        item_id = _item_id(row, kind)
        if item_id is None or item_id in seen:
            continue
        seen.add(item_id)
        seasons = row.get("seasons")
        season_count = (
            len(seasons)
            if isinstance(seasons, (dict, list))
            else _integer_or_none(row.get("season_count"))
        )
        episode_count = _integer_or_none(
            row.get("episode_count", row.get("episodes_count"))
        )
        if episode_count is None:
            episode_rows = row.get("episodes")
            if isinstance(episode_rows, list):
                episode_count = len(episode_rows)
            elif isinstance(episode_rows, dict):
                episode_count = sum(
                    len(episodes)
                    for episodes in episode_rows.values()
                    if isinstance(episodes, list)
                )
        indexed.append(
            ProviderProfileCatalogItem(
                provider_url=provider_url,
                kind=kind,
                category_id=category_id,
                category_name=category_name[:512],
                stream_id=item_id,
                name=str(row.get("name") or row.get("title") or f"Media {item_id}")[:1024],
                cover=row.get("cover") or row.get("stream_icon"),
                total_seasons=season_count if kind == "series" else None,
                total_episodes=episode_count if kind == "series" else None,
                year=_year(row),
                container_ext=safe_extension(
                    str(row.get("container_extension") or "mkv")
                ),
                download_status="unknown",
                updated_at=now,
            )
        )

    with SessionLocal() as db:
        category = db.scalar(
            select(ProviderProfileCatalogCategory).where(
                ProviderProfileCatalogCategory.provider_url == provider_url,
                ProviderProfileCatalogCategory.kind == kind,
                ProviderProfileCatalogCategory.category_id == category_id,
            )
        )
        if category is None:
            category = ProviderProfileCatalogCategory(
                provider_url=provider_url,
                kind=kind,
                category_id=category_id,
                category_name=category_name[:512],
            )
            db.add(category)
        category.category_name = category_name[:512]
        category.item_count = len(indexed)
        category.items_synced_at = now
        category.updated_at = now
        db.execute(
            delete(ProviderProfileCatalogItem).where(
                ProviderProfileCatalogItem.provider_url == provider_url,
                ProviderProfileCatalogItem.kind == kind,
                ProviderProfileCatalogItem.category_id == category_id,
            )
        )
        db.add_all(indexed)
        db.commit()


def store_inventory_status(
    provider_url: str,
    kind: str,
    items: list[dict[str, Any]],
) -> None:
    item_keys = {
        (str(item["category_id"]), str(item["id"])): item
        for item in items
        if item.get("category_id") is not None and item.get("id") is not None
    }
    if not item_keys:
        return
    category_ids = {category_id for category_id, _ in item_keys}
    stream_ids = {stream_id for _, stream_id in item_keys}
    now = utc_now()
    with SessionLocal() as db:
        records = db.scalars(
            select(ProviderProfileCatalogItem).where(
                ProviderProfileCatalogItem.provider_url == provider_url,
                ProviderProfileCatalogItem.kind == kind,
                ProviderProfileCatalogItem.category_id.in_(category_ids),
                ProviderProfileCatalogItem.stream_id.in_(stream_ids),
            )
        ).all()
        for record in records:
            item = item_keys.get((record.category_id, record.stream_id))
            if item is not None:
                record.download_status = item.get("download_status", "unknown")
                record.storage_match = item.get("storage_match")
                record.inventory_checked_at = now
        db.commit()
