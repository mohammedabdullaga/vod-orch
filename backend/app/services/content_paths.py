import hashlib


def stream_identity(provider_url: str, content_type: str, provider_stream_id: str) -> str:
    return hashlib.sha256(
        f"{provider_url.rstrip('/')}\0{content_type}\0{provider_stream_id}".encode("utf-8")
    ).hexdigest()


def safe_filename(value: str, max_length: int = 240) -> str:
    cleaned = "".join(char for char in value if char.isalnum() or char in " ._-()[]")
    return cleaned.strip(" .")[:max_length].rstrip(" .") or "media"


def safe_provider_id(provider_id: str, provider_url: str) -> str:
    safe_id = "".join(char for char in provider_id if char.isalnum() or char in "_-")[:48]
    digest = hashlib.sha256(
        f"{provider_url.rstrip('/')}\0{provider_id}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{safe_id}-{digest}" if safe_id else digest


def identity_segment(title: str, provider_id: str, provider_url: str) -> str:
    return (
        f"{safe_filename(title, max_length=130)} "
        f"[{safe_provider_id(provider_id, provider_url)}]"
    )


def safe_extension(value: str) -> str:
    extension = "".join(char for char in value.lstrip(".") if char.isalnum())[:12]
    return extension or "mkv"


def season_number(value: object) -> int | None:
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        digits = "".join(char for char in value if char.isdigit())
        if digits:
            return int(digits)
    return None


def normalized_episode_path(
    *,
    kind: str,
    title: str,
    provider_stream_id: str,
    provider_url: str,
    extension: str,
    series_name: str | None = None,
    series_id: str | None = None,
    season: object = None,
    episode_num: object = None,
    filename: str | None = None,
) -> tuple[str, int | None, int | None]:
    ext = safe_extension(extension)
    season_value = season_number(season)
    episode_value = season_number(episode_num)
    basename = safe_filename(filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) if filename else ""
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    if not stem:
        stem = safe_filename(title, max_length=160)

    if kind == "series":
        actual_series_name = series_name or title
        actual_series_id = series_id or provider_stream_id
        season_name = f"Season {season_value or 0:02d}"
        if season_value is not None and episode_value is not None:
            stem = f"S{season_value:02d}E{episode_value:02d}"
        path = (
            f"Series/{identity_segment(actual_series_name, actual_series_id, provider_url)}/"
            f"{season_name}/{stem}.{ext}"
        )
    else:
        actual_title = title or f"Movie {provider_stream_id}"
        path = (
            f"Movies/{identity_segment(actual_title, provider_stream_id, provider_url)}/"
            f"{stem}.{ext}"
        )
    return path, season_value, episode_value
