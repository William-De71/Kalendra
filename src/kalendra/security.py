"""Hachage de mots de passe (PBKDF2-HMAC-SHA256, stdlib) et jetons."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16


def hash_password(password: str, *, iterations: int | None = None) -> str:
    """Renvoie une empreinte au format ``pbkdf2_sha256$<iters>$<salt_b64>$<dk_b64>``.

    Le nombre d'itérations est relu à chaque appel : la suite de tests peut
    l'abaisser, et les empreintes existantes restent vérifiables puisqu'elles
    embarquent leur propre paramètre.
    """
    if not password:
        raise ValueError("le mot de passe ne peut pas être vide")
    iterations = PBKDF2_ITERATIONS if iterations is None else iterations
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    """Vérifie un mot de passe en temps constant. Ne lève jamais."""
    try:
        algo, iters, salt_b64, dk_b64 = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iters), dklen=len(expected)
        )
    except Exception:
        return False
    return hmac.compare_digest(candidate, expected)


def new_token(nbytes: int = 24) -> str:
    """Jeton URL-safe pour les flux ICS publics."""
    return secrets.token_urlsafe(nbytes)


def csrf_token(secret: str, username: str) -> str:
    return hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_valid(secret: str, username: str, candidate: str) -> bool:
    return hmac.compare_digest(csrf_token(secret, username), candidate or "")


def etag_for(data: str | bytes) -> str:
    """ETag fort (guillemets inclus) calculé sur le contenu."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
