from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone

from app.core.config import settings

_SENSITIVE_PATTERNS = ("prompt", "source_code", "llm_response", "access_token", "api_key", "secret")


class RedactingLogFilter(logging.Filter):
    """Фильтр логгера: запрещает логировать промпты, исходники, ответы LLM и ключи."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = str(record.getMessage()).lower()
        if any(pat in msg for pat in _SENSITIVE_PATTERNS):
            record.msg = "[REDACTED SENSITIVE PAYLOAD]"
            record.args = ()
        return True


def _derive_key() -> bytes:
    return hashlib.sha256(settings.token_encryption_key.encode("utf-8")).digest()


def encrypt_token(plain_token: str) -> str:
    """Симметричное XOR+HMAC шифрование токена ключом конфигурации (отдельно от БД)."""
    key = _derive_key()
    nonce = secrets.token_bytes(16)
    stream = hashlib.pbkdf2_hmac("sha256", key, nonce, 1000, dklen=len(plain_token.encode("utf-8")))
    cipher = bytes(a ^ b for a, b in zip(plain_token.encode("utf-8"), stream))
    mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + mac + cipher).decode("ascii")


def decrypt_token(encrypted_token: str) -> str:
    raw = base64.urlsafe_b64decode(encrypted_token.encode("ascii"))
    nonce, mac, cipher = raw[:16], raw[16:48], raw[48:]
    key = _derive_key()
    expected_mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        raise ValueError("Token integrity check failed")
    stream = hashlib.pbkdf2_hmac("sha256", key, nonce, 1000, dklen=len(cipher))
    return bytes(a ^ b for a, b in zip(cipher, stream)).decode("utf-8")


def hash_session_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_oauth_state() -> str:
    return secrets.token_urlsafe(32)


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def verify_github_webhook_signature(raw_body: bytes, signature_header: str | None) -> tuple[bool, str]:
    """
    Проверяет HMAC-SHA256 исходных bytes тела до парсинга JSON (constant-time).
    Возвращает (is_valid, payload_sha256_hex).
    """
    payload_hash = hashlib.sha256(raw_body).hexdigest()
    if not signature_header or not signature_header.startswith("sha256="):
        return False, payload_hash
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header), payload_hash


def default_session_expiry(hours: int = 24) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=hours)
