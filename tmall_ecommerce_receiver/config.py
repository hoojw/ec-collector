"""Configuration loader for the Tmall e-commerce snapshot receiver.

The receiver serves one HTTP port but may carry several Tmall stores.  Each
store is identified by its own ingest token, and snapshots are written under a
per-store directory so two stores can never overwrite each other.

Two config shapes are accepted:

Legacy (single store)::

    {
      "bearer_token_env": "TMALL_ECOMMERCE_INGEST_TOKEN",
      "output_dir": "/opt/subsidy/tmall-ecommerce-data"
    }

Multi store::

    {
      "output_dir": "/opt/subsidy/tmall-ecommerce-data",
      "stores": {
        "baomin":     { "token_env": "TMALL_INGEST_TOKEN_BAOMIN" },
        "jinghetang": { "token_env": "TMALL_INGEST_TOKEN_JINGHETANG" }
      }
    }

In the legacy shape the store key defaults to ``default_store`` (``default``),
which reproduces the historical single-directory layout exactly.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any


class ConfigurationError(ValueError):
    """Raised when receiver configuration is incomplete or unsafe."""


STORE_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def _positive_int(value: Any, field: str, minimum: int = 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{field} must be an integer") from exc
    if result < minimum:
        raise ConfigurationError(f"{field} must be >= {minimum}")
    return result


@dataclass(frozen=True)
class StoreSettings:
    """One Tmall store reachable through the shared ingest endpoint."""

    store_key: str
    bearer_token: str


@dataclass(frozen=True)
class Settings:
    listen_host: str
    listen_port: int
    ingest_path: str
    output_dir: pathlib.Path
    database_path: pathlib.Path
    max_body_bytes: int
    stores: tuple[StoreSettings, ...]

    @property
    def multi_store(self) -> bool:
        """True when more than one store key is configured."""
        return len(self.stores) > 1

    @property
    def single_store(self) -> bool:
        """True when exactly one store key is configured."""
        return len(self.stores) == 1

    def store_keys(self) -> tuple[str, ...]:
        return tuple(store.store_key for store in self.stores)


def _resolve_token_env(payload: dict[str, Any], env_name: str, label: str) -> str:
    token = os.environ.get(env_name, "").strip()
    if len(token) < 32:
        raise ConfigurationError(
            f"{label} environment variable {env_name} must contain at least 32 characters"
        )
    return token


def _parse_stores(payload: dict[str, Any]) -> tuple[StoreSettings, ...]:
    raw_stores = payload.get("stores")
    if raw_stores is None:
        # Legacy single-store shape: one token, one implicit store key.
        token_env = str(
            payload.get("bearer_token_env") or "TMALL_ECOMMERCE_INGEST_TOKEN"
        ).strip()
        default_key = str(payload.get("default_store") or "default").strip()
        if not STORE_KEY_RE.fullmatch(default_key):
            raise ConfigurationError(
                "default_store must match [a-z][a-z0-9-]{0,31}"
            )
        token = _resolve_token_env(payload, token_env, "ingest token")
        return (StoreSettings(store_key=default_key, bearer_token=token),)

    if not isinstance(raw_stores, dict) or not raw_stores:
        raise ConfigurationError("stores must be a non-empty object")

    seen_tokens: set[str] = set()
    stores: list[StoreSettings] = []
    for store_key in sorted(raw_stores):
        if not STORE_KEY_RE.fullmatch(str(store_key)):
            raise ConfigurationError(
                f"store key {store_key!r} must match [a-z][a-z0-9-]{{0,31}}"
            )
        entry = raw_stores[store_key]
        if not isinstance(entry, dict):
            raise ConfigurationError(f"stores.{store_key} must be an object")
        token_env = str(entry.get("token_env") or "").strip()
        if not token_env:
            raise ConfigurationError(
                f"stores.{store_key}.token_env is required in multi-store mode"
            )
        token = _resolve_token_env(payload, token_env, f"token for store {store_key}")
        if token in seen_tokens:
            raise ConfigurationError(
                f"store {store_key} reuses an ingest token already bound to another store"
            )
        seen_tokens.add(token)
        stores.append(StoreSettings(store_key=str(store_key), bearer_token=token))
    return tuple(stores)


def load_settings(path: str | pathlib.Path) -> Settings:
    config_path = pathlib.Path(path)
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"invalid JSON config: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("config root must be an object")

    ingest_path = str(
        payload.get("ingest_path") or "/internal/tmall/ecommerce-collections"
    ).strip()
    if not ingest_path.startswith("/") or "?" in ingest_path or "#" in ingest_path:
        raise ConfigurationError("ingest_path must be an absolute path without query text")

    output_dir = pathlib.Path(
        str(payload.get("output_dir") or "/opt/subsidy/tmall-ecommerce-data")
    )
    database_path = pathlib.Path(
        str(payload.get("database_path") or output_dir / "state.sqlite3")
    )
    if not output_dir.is_absolute() or not database_path.is_absolute():
        raise ConfigurationError("output_dir and database_path must be absolute")

    listen_port = _positive_int(payload.get("listen_port", 8096), "listen_port")
    if listen_port > 65535:
        raise ConfigurationError("listen_port must be <= 65535")

    return Settings(
        listen_host=str(payload.get("listen_host") or "127.0.0.1").strip(),
        listen_port=listen_port,
        ingest_path=ingest_path,
        output_dir=output_dir,
        database_path=database_path,
        max_body_bytes=_positive_int(
            payload.get("max_body_bytes", 5 * 1024 * 1024), "max_body_bytes"
        ),
        stores=_parse_stores(payload),
    )
