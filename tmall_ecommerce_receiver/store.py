"""Atomic and idempotent storage for Tmall e-commerce snapshots.

Snapshots are namespaced by store: ``received/<store_key>/<job>/<yyyy>/<mm>/<dd>/``.
The SQLite bookkeeping table carries the store key and uses
``(store_key, collection_id)`` as its identity, so two stores are free to reuse
the same collection id without colliding.

Existing single-store deployments are upgraded in place: rows that predate the
store column are backfilled to the configured single store key, and their files
keep living in the legacy ``received/<job>/...`` location (their stored
``payload_path`` is absolute, so reads stay valid).
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class CollectionConflictError(RuntimeError):
    """A collection id was reused with different content."""


@dataclass(frozen=True)
class StoredCollection:
    store_key: str
    collection_id: str
    duplicate: bool
    job: str
    slot_time: str
    state: str
    complete: bool
    payload_sha256: str
    payload_path: pathlib.Path


class CollectionStore:
    def __init__(
        self,
        database_path: pathlib.Path,
        output_dir: pathlib.Path,
        legacy_store_key: str = "default",
    ) -> None:
        self.database_path = database_path
        self.output_dir = output_dir
        self.received_dir = output_dir / "received"
        # Rows written before the store column existed are attributed to this
        # key.  With a single configured store it names that store; with many,
        # it is the historical owner of the pre-existing directory.
        self.legacy_store_key = legacy_store_key
        self.database_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.received_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in {self.database_path.parent, self.output_dir, self.received_dir}:
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            existing = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='tmall_ecommerce_collections'"
            ).fetchone()
            if existing is not None:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(tmall_ecommerce_collections)"
                    )
                }
                if "complete" not in columns:
                    self._rebuild_with_complete(connection)
                elif "store_key" not in columns:
                    self._add_store_key(connection)
            else:
                self._create_table(connection)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tmall_ecommerce_received "
                "ON tmall_ecommerce_collections(job, received_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tmall_ecommerce_slot "
                "ON tmall_ecommerce_collections(job, slot_time)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tmall_ecommerce_store_slot "
                "ON tmall_ecommerce_collections(store_key, job, slot_time)"
            )

    @staticmethod
    def _create_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS tmall_ecommerce_collections (
                store_key TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                job TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                state TEXT NOT NULL,
                complete INTEGER NOT NULL,
                dataset_names TEXT NOT NULL,
                error_count INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_path TEXT NOT NULL,
                PRIMARY KEY (store_key, collection_id)
            )
            """
        )

    def _rebuild_with_complete(self, connection: sqlite3.Connection) -> None:
        """Upgrade the oldest schema shape (no ``complete`` column)."""
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE tmall_ecommerce_collections "
            "RENAME TO tmall_ecommerce_collections_legacy"
        )
        self._create_table(connection)
        connection.execute(
            """
            INSERT INTO tmall_ecommerce_collections (
                store_key, collection_id, job, slot_time, collected_at,
                received_at, state, complete, dataset_names,
                error_count, payload_sha256, payload_path
            )
            SELECT ?, collection_id, job, slot_time, collected_at,
                received_at, 'success', 1, '', 0,
                payload_sha256, payload_path
            FROM tmall_ecommerce_collections_legacy
            """,
            (self.legacy_store_key,),
        )
        connection.execute("DROP TABLE tmall_ecommerce_collections_legacy")
        connection.commit()

    def _add_store_key(self, connection: sqlite3.Connection) -> None:
        """Upgrade the single-store schema to carry a store key."""
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE tmall_ecommerce_collections "
            "RENAME TO tmall_ecommerce_collections_legacy"
        )
        self._create_table(connection)
        connection.execute(
            """
            INSERT INTO tmall_ecommerce_collections (
                store_key, collection_id, job, slot_time, collected_at,
                received_at, state, complete, dataset_names,
                error_count, payload_sha256, payload_path
            )
            SELECT ?, collection_id, job, slot_time, collected_at,
                received_at, state, complete, dataset_names,
                error_count, payload_sha256, payload_path
            FROM tmall_ecommerce_collections_legacy
            """,
            (self.legacy_store_key,),
        )
        connection.execute("DROP TABLE tmall_ecommerce_collections_legacy")
        connection.commit()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=10, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=10000")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _serialized(payload: dict[str, Any]) -> bytes:
        return json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")

    def store_dir(self, store_key: str, job: str, slot: datetime) -> pathlib.Path:
        """Directory holding one store's snapshots for a job and day."""
        return (
            self.received_dir
            / store_key
            / job
            / f"{slot.year:04d}"
            / f"{slot.month:02d}"
            / f"{slot.day:02d}"
        )

    def save(self, payload: dict[str, Any], store_key: str) -> StoredCollection:
        raw = self._serialized(payload)
        digest = hashlib.sha256(raw).hexdigest()
        collection_id = str(payload["collection_id"])
        job = str(payload["job"])
        slot_time = str(payload["slot_time"])
        collected_at = str(payload["collected_at"])
        state = str(payload["state"])
        complete = state == "success"
        dataset_names = json.dumps(
            sorted((payload.get("data") or {}).keys()), ensure_ascii=False
        )
        error_count = len(payload.get("errors") or [])
        slot = datetime.fromisoformat(slot_time.replace("Z", "+00:00"))
        destination_dir = self.store_dir(store_key, job, slot)
        destination_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = destination_dir / f"{collection_id}.json"
        temporary = destination_dir / f".{collection_id}.{uuid.uuid4().hex}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.write(b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT collection_id, job, slot_time, state, complete,
                           payload_sha256, payload_path
                    FROM tmall_ecommerce_collections
                    WHERE store_key=? AND collection_id=?
                    LIMIT 1
                    """,
                    (store_key, collection_id),
                ).fetchone()
                if row is not None:
                    connection.rollback()
                    if str(row[5]) != digest:
                        raise CollectionConflictError(
                            "collection id already exists with different content"
                        )
                    return StoredCollection(
                        store_key=store_key,
                        collection_id=str(row[0]),
                        duplicate=True,
                        job=str(row[1]),
                        slot_time=str(row[2]),
                        state=str(row[3]),
                        complete=bool(row[4]),
                        payload_sha256=digest,
                        payload_path=pathlib.Path(str(row[6])),
                    )
                os.replace(temporary, destination)
                connection.execute(
                    """
                    INSERT INTO tmall_ecommerce_collections (
                        store_key, collection_id, job, slot_time, collected_at,
                        received_at, state, complete, dataset_names,
                        error_count, payload_sha256, payload_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        store_key,
                        collection_id,
                        job,
                        slot_time,
                        collected_at,
                        int(time.time()),
                        state,
                        int(complete),
                        dataset_names,
                        error_count,
                        digest,
                        str(destination),
                    ),
                )
                connection.commit()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return StoredCollection(
            store_key=store_key,
            collection_id=collection_id,
            duplicate=False,
            job=job,
            slot_time=slot_time,
            state=state,
            complete=complete,
            payload_sha256=digest,
            payload_path=destination,
        )
