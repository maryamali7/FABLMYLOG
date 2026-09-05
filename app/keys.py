"""Exchange API credentials.

Keys are entered in the dashboard, encrypted at rest and **never sent back to
the browser** — the API only ever returns a masked fingerprint like
``a1b2••••7f9c``. The secret leaves this process in exactly one place: the
signature of an order you asked for.

Encryption uses Fernet when ``cryptography`` is installed, and falls back to a
keyed XOR stream otherwise. The fallback is obfuscation, not real crypto, and
says so loudly in the API response — on a shared host, install ``cryptography``
and set ``FABL_SECRET_KEY``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("keys")

# venue -> which fields that venue's signed API needs
VENUE_FIELDS: dict[str, list[str]] = {
    "binance": ["key", "secret"],
    "bybit": ["key", "secret"],
    "okx": ["key", "secret", "passphrase"],
    "mexc": ["key", "secret"],
    "kucoin": ["key", "secret", "passphrase"],
    "gate": ["key", "secret"],
    "bitget": ["key", "secret", "passphrase"],
    "htx": ["key", "secret"],
}

# venues this build can actually place orders on (the rest store keys and can
# be connection-tested, but order routing is not wired yet)
TRADABLE = ("binance", "bybit")

ENV_FALLBACK = {
    "binance": ("BINANCE_API_KEY", "BINANCE_API_SECRET", ""),
    "bybit": ("BYBIT_API_KEY", "BYBIT_API_SECRET", ""),
    "okx": ("OKX_API_KEY", "OKX_API_SECRET", "OKX_PASSPHRASE"),
}


def _machine_secret() -> bytes:
    raw = os.getenv("FABL_SECRET_KEY") or ""
    if not raw:
        # stable per-install fallback so restarts can still read the file
        raw = f"fablmylog::{Path.home()}::{os.getuid() if hasattr(os, 'getuid') else 0}"
    return hashlib.sha256(raw.encode()).digest()


try:  # pragma: no cover - depends on the host
    from cryptography.fernet import Fernet, InvalidToken

    _FERNET = Fernet(base64.urlsafe_b64encode(_machine_secret()))
    CRYPTO = "fernet"
except Exception:  # pragma: no cover
    _FERNET = None
    InvalidToken = Exception
    CRYPTO = "obfuscated"


def _encrypt(plain: str) -> str:
    if not plain:
        return ""
    if _FERNET:
        return _FERNET.encrypt(plain.encode()).decode()
    key = _machine_secret()
    data = plain.encode()
    out = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return "xor:" + base64.urlsafe_b64encode(out).decode()


def _decrypt(blob: str) -> str:
    if not blob:
        return ""
    if blob.startswith("xor:"):
        key = _machine_secret()
        data = base64.urlsafe_b64decode(blob[4:].encode())
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data)).decode(errors="ignore")
    if _FERNET:
        try:
            return _FERNET.decrypt(blob.encode()).decode()
        except InvalidToken:
            log.warning("stored credential could not be decrypted (secret key changed?)")
            return ""
    return ""


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


class KeyStore:
    """Encrypted per-venue credential store."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict[str, dict[str, Any]] = {}
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except Exception:
            raw = {}
        self.data = raw if isinstance(raw, dict) else {}
        for venue, (k_env, s_env, p_env) in ENV_FALLBACK.items():
            if venue in self.data:
                continue
            key, secret = os.getenv(k_env, ""), os.getenv(s_env, "")
            if key and secret:
                self.data[venue] = {
                    "key": _encrypt(key),
                    "secret": _encrypt(secret),
                    "passphrase": _encrypt(os.getenv(p_env, "")) if p_env else "",
                    "testnet": False,
                    "trade_enabled": False,
                    "source": "env",
                    "added": time.time(),
                }

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), "utf-8")
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o600)
        except Exception as exc:  # pragma: no cover - disk only
            log.warning("could not persist credentials: %s", exc)

    # --------------------------------------------------------------- crud
    def set(self, venue: str, key: str, secret: str, passphrase: str = "", testnet: bool = False) -> dict[str, Any]:
        venue = venue.lower().strip()
        if venue not in VENUE_FIELDS:
            raise ValueError(f"unknown venue {venue}")
        if not key or not secret:
            raise ValueError("both an API key and secret are required")
        if "passphrase" in VENUE_FIELDS[venue] and not passphrase:
            raise ValueError(f"{venue} also needs the API passphrase")
        prev = self.data.get(venue) or {}
        self.data[venue] = {
            "key": _encrypt(key.strip()),
            "secret": _encrypt(secret.strip()),
            "passphrase": _encrypt(passphrase.strip()) if passphrase else "",
            "testnet": bool(testnet),
            "trade_enabled": bool(prev.get("trade_enabled", False)),
            "source": "dashboard",
            "added": time.time(),
            "last_test": None,
        }
        self.save()
        return self.describe(venue)

    def delete(self, venue: str) -> bool:
        if venue in self.data:
            self.data.pop(venue)
            self.save()
            return True
        return False

    def creds(self, venue: str) -> dict[str, str]:
        """Decrypted credentials — for signing only, never for responses."""
        row = self.data.get(venue.lower()) or {}
        return {
            "key": _decrypt(row.get("key", "")),
            "secret": _decrypt(row.get("secret", "")),
            "passphrase": _decrypt(row.get("passphrase", "")),
            "testnet": bool(row.get("testnet")),
        }

    def ready(self, venue: str) -> bool:
        c = self.creds(venue)
        return bool(c["key"] and c["secret"])

    def set_trade_enabled(self, venue: str, on: bool) -> dict[str, Any]:
        row = self.data.get(venue.lower())
        if not row:
            raise ValueError(f"no credentials stored for {venue}")
        row["trade_enabled"] = bool(on)
        self.save()
        return self.describe(venue)

    def note_test(self, venue: str, ok: bool, detail: str) -> None:
        row = self.data.get(venue.lower())
        if row is not None:
            row["last_test"] = {"ok": bool(ok), "detail": detail[:200], "ts": time.time()}
            self.save()

    # -------------------------------------------------------------- views
    def describe(self, venue: str) -> dict[str, Any]:
        row = self.data.get(venue) or {}
        creds = self.creds(venue) if row else {"key": "", "secret": "", "passphrase": ""}
        return {
            "venue": venue,
            "configured": bool(creds.get("key") and creds.get("secret")),
            "key_masked": mask(creds.get("key", "")),
            "needs": VENUE_FIELDS.get(venue, ["key", "secret"]),
            "has_passphrase": bool(creds.get("passphrase")),
            "testnet": bool(row.get("testnet")),
            "trade_enabled": bool(row.get("trade_enabled")),
            "order_routing": venue in TRADABLE,
            "source": row.get("source"),
            "added": row.get("added"),
            "last_test": row.get("last_test"),
        }

    def listing(self) -> dict[str, Any]:
        return {
            "venues": [self.describe(v) for v in VENUE_FIELDS],
            "encryption": CRYPTO,
            "encryption_note": (
                "credentials are encrypted with Fernet"
                if CRYPTO == "fernet"
                else "cryptography is not installed — credentials are only obfuscated on disk; "
                "install cryptography and set FABL_SECRET_KEY before using real keys"
            ),
            "tradable": list(TRADABLE),
            "path": str(self.path),
        }
