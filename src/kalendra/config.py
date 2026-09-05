"""Configuration, lue exclusivement depuis l'environnement."""

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
    """Paramètres d'exécution du serveur.

    Toutes les variables sont préfixées ``KALENDRA_``.
    """

    db_path: str = field(default_factory=lambda: os.environ.get("KALENDRA_DB", "/data/kalendra.db"))
    host: str = field(default_factory=lambda: os.environ.get("KALENDRA_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("KALENDRA_PORT", 5232))

    #: Préfixe d'URL si le serveur est monté dans un sous-chemin ("" ou "/cal").
    base_path: str = field(
        default_factory=lambda: os.environ.get("KALENDRA_BASE_PATH", "").rstrip("/")
    )

    #: URL publique, utilisée uniquement pour afficher les URLs dans l'UI admin.
    public_url: str = field(
        default_factory=lambda: os.environ.get("KALENDRA_PUBLIC_URL", "").rstrip("/")
    )

    #: Taille maximale d'un objet calendrier accepté en PUT (octets).
    max_resource_size: int = field(
        default_factory=lambda: _int("KALENDRA_MAX_RESOURCE_SIZE", 1_048_576)
    )

    #: Taille maximale d'un corps de requête XML (octets).
    max_request_body: int = field(
        default_factory=lambda: _int("KALENDRA_MAX_REQUEST_BODY", 8_388_608)
    )

    #: Active l'interface web d'administration.
    admin_ui: bool = field(default_factory=lambda: _bool("KALENDRA_ADMIN_UI", True))

    #: Active les flux ICS publics en lecture seule (Google / Proton).
    feeds_enabled: bool = field(default_factory=lambda: _bool("KALENDRA_FEEDS", True))

    #: Durée de fraîcheur annoncée aux agrégateurs ICS, en minutes.
    feed_refresh_minutes: int = field(default_factory=lambda: _int("KALENDRA_FEED_REFRESH", 60))

    #: Durée (s) de mise en cache des identifiants validés ; 0 désactive.
    auth_cache_ttl: int = field(default_factory=lambda: _int("KALENDRA_AUTH_CACHE_TTL", 60))

    log_level: str = field(default_factory=lambda: os.environ.get("KALENDRA_LOG_LEVEL", "info"))

    #: Compte administrateur créé au premier démarrage si la base est vide.
    bootstrap_admin: str = field(default_factory=lambda: os.environ.get("KALENDRA_ADMIN_USER", ""))
    bootstrap_password: str = field(
        default_factory=lambda: os.environ.get("KALENDRA_ADMIN_PASSWORD", "")
    )

    @classmethod
    def from_env(cls) -> Config:
        return cls()
