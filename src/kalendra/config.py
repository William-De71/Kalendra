"""Configuration, read exclusively from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(slots=True)
class Config:
    """Server runtime settings.

    Every variable is prefixed with ``KALENDRA_``.
    """

    db_path: str = field(default_factory=lambda: os.environ.get("KALENDRA_DB", "/data/kalendra.db"))
    host: str = field(default_factory=lambda: os.environ.get("KALENDRA_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("KALENDRA_PORT", 5232))

    #: URL prefix when the server is mounted under a sub-path ("" or "/cal").
    base_path: str = field(
        default_factory=lambda: os.environ.get("KALENDRA_BASE_PATH", "").rstrip("/")
    )

    #: Public URL, used only to display URLs in the admin UI.
    public_url: str = field(
        default_factory=lambda: os.environ.get("KALENDRA_PUBLIC_URL", "").rstrip("/")
    )

    #: Maximum size of a calendar object accepted on PUT (bytes).
    max_resource_size: int = field(
        default_factory=lambda: _int("KALENDRA_MAX_RESOURCE_SIZE", 1_048_576)
    )

    #: Maximum size of an XML request body (bytes).
    max_request_body: int = field(
        default_factory=lambda: _int("KALENDRA_MAX_REQUEST_BODY", 8_388_608)
    )

    #: Enables the web admin interface.
    admin_ui: bool = field(default_factory=lambda: _bool("KALENDRA_ADMIN_UI", True))

    #: Enables the public read-only ICS feeds (Google / Proton).
    feeds_enabled: bool = field(default_factory=lambda: _bool("KALENDRA_FEEDS", True))

    #: Freshness hint advertised to ICS aggregators, in minutes.
    feed_refresh_minutes: int = field(default_factory=lambda: _int("KALENDRA_FEED_REFRESH", 60))

    #: How long (s) verified credentials stay cached; 0 disables it.
    auth_cache_ttl: int = field(default_factory=lambda: _int("KALENDRA_AUTH_CACHE_TTL", 60))

    log_level: str = field(default_factory=lambda: os.environ.get("KALENDRA_LOG_LEVEL", "info"))

    #: Administrator account created on first start when the database is empty.
    bootstrap_admin: str = field(default_factory=lambda: os.environ.get("KALENDRA_ADMIN_USER", ""))
    bootstrap_password: str = field(
        default_factory=lambda: os.environ.get("KALENDRA_ADMIN_PASSWORD", "")
    )

    @classmethod
    def from_env(cls) -> Config:
        return cls()
