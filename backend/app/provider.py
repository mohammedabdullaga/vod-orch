"""Compatibility imports for provider client functions."""

from app.services.provider_client import (
    cached_provider_action,
    parse_series_episodes,
    provider_action,
    resolve_provider_proxy,
    resolve_provider_proxy_selection,
    stream_url,
)

__all__ = [
    "cached_provider_action",
    "parse_series_episodes",
    "provider_action",
    "resolve_provider_proxy",
    "resolve_provider_proxy_selection",
    "stream_url",
]
