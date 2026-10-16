import asyncio
import hashlib
import json
import logging
import re
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from app.config import settings
from app.models import M3UEntry, M3UPlaylist, SessionLocal, utc_now
from app.security import encrypt_secret

logger = logging.getLogger(__name__)
MAX_PLAYLIST_BYTES = 32 * 1024 * 1024
_ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')
_EPISODE = re.compile(r"(?i)\bS(\d{1,3})\s*E(\d{1,3})\b")
_ALT_EPISODE = re.compile(r"(?i)\b(\d{1,2})x(\d{1,3})\b")


def _episode_metadata(title: str) -> tuple[str | None, int | None, int | None]:
    match = _EPISODE.search(title) or _ALT_EPISODE.search(title)
    if not match:
        return None, None, None
    season, episode = (int(value) for value in match.groups())
    series_name = title[:match.start()].strip(" ._-") or title
    return series_name, season, episode


def _parse_extinf(line: str) -> tuple[dict[str, str], str]:
    payload = line[len("#EXTINF:"):]
    in_quotes = False
    comma_index = None
    for index, character in enumerate(payload):
        if character == '"':
            in_quotes = not in_quotes
        elif character == "," and not in_quotes:
            comma_index = index
            break
    if comma_index is None:
        raise ValueError("Playlist contains an EXTINF line without a display name.")
    attributes = {key: value for key, value in _ATTRIBUTE.findall(payload[:comma_index])}
    return attributes, payload[comma_index + 1:].strip()


def parse_m3u(content: str) -> list[dict]:
    entries: list[dict] = []
    duplicate_counts: dict[str, int] = {}
    pending_attributes: dict[str, str] | None = None
    pending_title = ""
    pending_directives: list[str] = []
    for raw_line in content.lstrip("\ufeff").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            pending_attributes, pending_title = _parse_extinf(line)
            pending_directives = []
            continue
        if line.startswith("#"):
            if pending_attributes is not None and line != "#EXTM3U":
                pending_directives.append(line)
            continue
        parsed = urlsplit(line)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            logger.warning("Skipping non-HTTP M3U stream at entry %s", len(entries) + 1)
            pending_attributes = None
            pending_directives = []
            continue
        if pending_attributes is None:
            logger.warning("Skipping M3U stream without an EXTINF record at entry %s", len(entries) + 1)
            continue

        title = pending_title or pending_attributes.get("tvg-name") or "Untitled"
        series_name, season, episode = _episode_metadata(title)
        is_series_path = any(
            segment.lower() == "series" for segment in PurePosixPath(parsed.path).parts
        )
        kind = "series" if series_name and season is not None and episode is not None else "vod"
        if is_series_path and kind == "vod":
            logger.info("M3U entry %s uses a series URL without a recognized episode number", len(entries) + 1)
        stable_source = json.dumps(
            [line, pending_attributes.get("tvg-id", ""), title],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        duplicate_index = duplicate_counts.get(stable_source, 0)
        duplicate_counts[stable_source] = duplicate_index + 1
        entries.append({
            "stable_key": hashlib.sha256(
                f"{stable_source}\0{duplicate_index}".encode("utf-8")
            ).hexdigest(),
            "position": len(entries),
            "group_title": pending_attributes.get("group-title") or "Uncategorized",
            "kind": kind,
            "title": title,
            "tvg_id": pending_attributes.get("tvg-id"),
            "tvg_name": pending_attributes.get("tvg-name"),
            "tvg_logo": pending_attributes.get("tvg-logo"),
            "stream_url": line,
            "extension": PurePosixPath(parsed.path).suffix.lstrip(".") or "mkv",
            "series_name": series_name,
            "season": season,
            "episode": episode,
            "attributes_json": json.dumps(pending_attributes, ensure_ascii=False),
            "directives_json": json.dumps(pending_directives, ensure_ascii=False),
        })
        pending_attributes = None
        pending_title = ""
        pending_directives = []
    if pending_attributes is not None:
        logger.warning("Ignoring final M3U EXTINF record without a stream URL.")
    return entries


async def fetch_playlist(source_url: str, proxy_url: str | None) -> list[dict]:
    parsed = urlsplit(source_url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError("Playlist URL must be an absolute HTTP or HTTPS URL.")
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        proxy=proxy_url,
        follow_redirects=True,
    ) as client:
        async with client.stream("GET", source_url, headers={"User-Agent": "Mozilla"}) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > MAX_PLAYLIST_BYTES:
                raise ValueError("Playlist exceeds the 32 MiB import limit.")
            payload = bytearray()
            async for chunk in response.aiter_bytes():
                payload.extend(chunk)
                if len(payload) > MAX_PLAYLIST_BYTES:
                    raise ValueError("Playlist exceeds the 32 MiB import limit.")
    try:
        decoded = bytes(payload).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("The M3U playlist is not valid UTF-8 text.") from exc
    if not decoded.lstrip().startswith("#EXTM3U"):
        raise ValueError("The URL did not return an extended M3U playlist.")
    return await asyncio.to_thread(parse_m3u, decoded)


def replace_playlist_entries(playlist_id: int, entries: list[dict]) -> int:
    with SessionLocal() as db:
        playlist = db.get(M3UPlaylist, playlist_id)
        if playlist is None:
            raise LookupError("Playlist not found.")
        existing = {
            entry.stable_key: entry
            for entry in db.scalars(
                select(M3UEntry).where(M3UEntry.playlist_id == playlist_id)
            ).all()
        }
        incoming_keys = {entry["stable_key"] for entry in entries}
        for old_key in existing.keys() - incoming_keys:
            db.delete(existing[old_key])
        for values in entries:
            entry = existing.get(values["stable_key"])
            persisted_values = {
                **{key: value for key, value in values.items() if key != "stream_url"},
                "stream_url_encrypted": encrypt_secret(values["stream_url"]),
                "attributes_json": encrypt_secret(values["attributes_json"]),
                "directives_json": encrypt_secret(values["directives_json"]),
                "tvg_logo": (
                    encrypt_secret(values["tvg_logo"])
                    if values["tvg_logo"]
                    else None
                ),
            }
            if entry is None:
                db.add(M3UEntry(
                    playlist_id=playlist_id,
                    **persisted_values,
                ))
            else:
                for key, value in persisted_values.items():
                    setattr(entry, key, value)
        playlist.entry_count = len(entries)
        playlist.fetched_at = utc_now()
        db.commit()
        return playlist.entry_count
