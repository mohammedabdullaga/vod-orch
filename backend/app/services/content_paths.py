import hashlib
import re
import unicodedata
from difflib import SequenceMatcher


def parse_strip_prefixes(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    prefixes = {
        prefix.strip()
        for prefix in re.split(r"[,\n\r]+", value)
        if prefix.strip()
    }
    return tuple(sorted(prefixes, key=len, reverse=True))


def clean_content_name(value: str, prefixes: str | None = None) -> str:
    cleaned = value.strip()
    strip_prefixes = tuple(
        sorted(set(parse_strip_prefixes(prefixes)) | {"4k"}, key=len, reverse=True)
    )
    while strip_prefixes:
        for prefix in strip_prefixes:
            pattern = rf"^{re.escape(prefix)}(?=$|[\s._\-\[\]()])[\s._\-]*"
            cleaned, count = re.subn(pattern, "", cleaned, count=1, flags=re.IGNORECASE)
            if count:
                break
        else:
            break
    return cleaned.strip(" -_.") or value


def normalize_match_name(value: str, prefixes: str | None = None) -> str:
    cleaned = unicodedata.normalize("NFKD", clean_content_name(value, prefixes)).casefold()
    return "".join(
        char for char in cleaned
        if not unicodedata.combining(char) and char.isalnum()
    )


def match_series_directory(
    series_name: str,
    directory_names: list[str],
    prefixes: str | None = None,
) -> str:
    expected = normalize_match_name(series_name, prefixes)
    if len(expected) < 3:
        raise ValueError("Series title is too short to safely match an existing folder.")
    expected_tokens = set(re.findall(r"[^\W_]+", clean_content_name(series_name, prefixes).casefold()))
    scores = []
    for name in directory_names:
        candidate = normalize_match_name(name, prefixes)
        if not candidate:
            continue
        candidate_tokens = set(
            re.findall(r"[^\W_]+", clean_content_name(name, prefixes).casefold())
        )
        shared_tokens = expected_tokens & candidate_tokens
        score = SequenceMatcher(None, expected, candidate).ratio()
        shorter_token_count = min(len(expected_tokens), len(candidate_tokens))
        extra_token_count = max(len(expected_tokens), len(candidate_tokens)) - shorter_token_count
        if (
            shorter_token_count >= 2
            and len(shared_tokens) == shorter_token_count
            and extra_token_count <= 2
        ):
            score = max(score, 0.86)
        scores.append((score, name))
    scores.sort(reverse=True)
    if not scores or scores[0][0] < 0.84:
        raise ValueError(
            f"No existing series folder closely matches '{series_name}' in the configured path."
        )
    if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.08:
        raise ValueError(
            f"More than one existing series folder closely matches '{series_name}'; "
            "rename the folders or use a dedicated destination path."
        )
    return scores[0][1]


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
    category_name: str | None = None,
    provider_stream_id: str,
    provider_url: str,
    extension: str,
    series_name: str | None = None,
    series_id: str | None = None,
    season: object = None,
    episode_num: object = None,
    filename: str | None = None,
    strip_prefixes: str | None = None,
) -> tuple[str, int | None, int | None]:
    ext = safe_extension(extension)
    season_value = season_number(season)
    episode_value = season_number(episode_num)
    basename = safe_filename(filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]) if filename else ""
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    if not stem:
        stem = title
    stem = safe_filename(clean_content_name(stem, strip_prefixes), max_length=160)
    category_segment = (
        safe_filename(clean_content_name(category_name, strip_prefixes))
        if category_name
        else None
    )
    category_path = f"{category_segment}/" if category_segment else ""

    if kind == "series":
        actual_series_name = clean_content_name(series_name or title, strip_prefixes)
        actual_series_id = series_id or provider_stream_id
        season_name = f"Season {season_value or 0:02d}"
        if season_value is not None and episode_value is not None:
            stem = f"S{season_value:02d}E{episode_value:02d}"
        path = (
            f"{category_path}{identity_segment(actual_series_name, actual_series_id, provider_url)}/"
            f"{season_name}/{stem}.{ext}"
        )
    else:
        path = f"{category_path}{stem}.{ext}"
    return path, season_value, episode_value
