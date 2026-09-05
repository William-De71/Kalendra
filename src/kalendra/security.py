"""Password hashing (PBKDF2-HMAC-SHA256, stdlib) and tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16


def hash_password(password: str, *, iterations: int | None = None) -> str:
    """Return a digest shaped ``pbkdf2_sha256$<iters>$<salt_b64>$<dk_b64>``.

    The iteration count is re-read on every call: the test suite may lower it,
    and existing digests stay verifiable since they carry their own parameter.
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
    """Verify a password in constant time. Never raises."""
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
    """URL-safe token for public ICS feeds."""
    return secrets.token_urlsafe(nbytes)


def csrf_token(secret: str, username: str) -> str:
    return hmac.new(secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_valid(secret: str, username: str, candidate: str) -> bool:
    return hmac.compare_digest(csrf_token(secret, username), candidate or "")


def etag_for(data: str | bytes) -> str:
    """Strong ETag (quotes included) computed over the content."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
