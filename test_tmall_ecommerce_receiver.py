"""Tests for the multi-store Tmall e-commerce snapshot receiver.

Covers the two things this change must never break:
1. Two stores pushing to one port are stored separately and never mixed.
2. A single-store deployment keeps its historical single-directory layout.
"""

import json
import pathlib
import tempfile
import unittest

from tmall_ecommerce_receiver.app import TmallEcommerceApplication
from tmall_ecommerce_receiver.config import (
    ConfigurationError,
    load_settings,
)
from tmall_ecommerce_receiver.store import CollectionConflictError, CollectionStore


BAOMIN_TOKEN = "a" * 40
JINGHETANG_TOKEN = "b" * 40


def write_config(root: pathlib.Path, payload: dict) -> pathlib.Path:
    path = root / "receiver.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def daily_payload(collection_id: str, *, slot_time="2026-09-23T00:00:00+08:00"):
    return {
        "schema_version": 1,
        "collector": "tmall-patchright",
        "collection_id": collection_id,
        "job": "daily",
        "slot_time": slot_time,
        "collected_at": "2026-09-23T08:06:06+08:00",
        "state": "success",
        "partial": False,
        "errors": [],
        "data": {
            "portal": {"metrics": {"昨日交易额": "100"}},
            "macro": {"metrics": {"动销商品数": 5, "近30日支付金额": "1000", "近30日环比": "0.1"}},
            "item_rank": {"record_count": 1, "collected_rows": 1, "替尔泊肽支付金额合计": "10"},
            "on_sale": {"total": 10},
        },
    }


def every10_payload(collection_id: str, *, slot_time="2026-09-23T09:20:00+08:00"):
    return {
        "schema_version": 1,
        "collector": "tmall-patchright",
        "collection_id": collection_id,
        "job": "every10",
        "slot_time": slot_time,
        "collected_at": "2026-09-23T09:20:30+08:00",
        "state": "success",
        "partial": False,
        "errors": [],
        "data": {
            "after_sales": {
                "metrics": {"待审核或待商家处理数量": 3, "即将超时售后单数量": 1}
            }
        },
    }


class MultiStoreConfigTest(unittest.TestCase):
    def test_multi_store_requires_distinct_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = write_config(root, {
                "output_dir": str(root / "data"),
                "stores": {
                    "baomin": {"token_env": "T_A"},
                    "jinghetang": {"token_env": "T_B"},
                },
            })
            settings = load_settings_with_env(config, T_A=BAOMIN_TOKEN, T_B=JINGHETANG_TOKEN)
            self.assertTrue(settings.multi_store)
            self.assertEqual(settings.store_keys(), ("baomin", "jinghetang"))

    def test_shared_token_across_stores_is_rejected(self):
        # Two stores sharing one token would make attribution ambiguous, which
        # is exactly the mixing bug this change exists to prevent.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = write_config(root, {
                "output_dir": str(root / "data"),
                "stores": {
                    "baomin": {"token_env": "T_A"},
                    "jinghetang": {"token_env": "T_B"},
                },
            })
            with self.assertRaises(ConfigurationError):
                load_settings_with_env(config, T_A=BAOMIN_TOKEN, T_B=BAOMIN_TOKEN)

    def test_legacy_single_store_config_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = write_config(root, {
                "output_dir": str(root / "data"),
                "bearer_token_env": "TMALL_ECOMMERCE_INGEST_TOKEN",
            })
            settings = load_settings_with_env(
                config, TMALL_ECOMMERCE_INGEST_TOKEN=BAOMIN_TOKEN
            )
            self.assertTrue(settings.single_store)
            self.assertEqual(settings.store_keys(), ("default",))

    def test_invalid_store_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            config = write_config(root, {
                "output_dir": str(root / "data"),
                "stores": {"Bad_Key": {"token_env": "T_A"}},
            })
            with self.assertRaises(ConfigurationError):
                load_settings_with_env(config, T_A=BAOMIN_TOKEN)


class StoreIsolationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.store = CollectionStore(
            self.root / "state.sqlite3", self.root / "data", legacy_store_key="baomin"
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_two_stores_can_reuse_the_same_collection_id(self):
        # The Windows collectors for the two stores both name snapshots
        # daily-YYYYMMDD-run-HHMMSS; the same id must be storable per store.
        payload = daily_payload("daily-20260923-run-080606")
        first = self.store.save(payload, "baomin")
        second = self.store.save(payload, "jinghetang")
        self.assertFalse(first.duplicate)
        self.assertFalse(second.duplicate)
        self.assertNotEqual(first.payload_path, second.payload_path)
        self.assertIn("baomin", str(first.payload_path))
        self.assertIn("jinghetang", str(second.payload_path))

    def test_same_store_same_payload_is_a_duplicate(self):
        payload = daily_payload("daily-20260923-run-080606")
        self.store.save(payload, "baomin")
        again = self.store.save(payload, "baomin")
        self.assertTrue(again.duplicate)

    def test_same_id_different_content_in_one_store_conflicts(self):
        self.store.save(daily_payload("daily-20260923-run-080606"), "baomin")
        changed = daily_payload("daily-20260923-run-080606")
        changed["data"]["on_sale"]["total"] = 99
        with self.assertRaises(CollectionConflictError):
            self.store.save(changed, "baomin")

    def test_snapshots_are_written_under_the_store_directory(self):
        stored = self.store.save(every10_payload("every10-20260923-run-092011"), "jinghetang")
        self.assertEqual(
            stored.payload_path.parent,
            self.root / "data" / "received" / "jinghetang" / "every10" / "2026" / "09" / "23",
        )


class ApplicationRoutingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        config = write_config(self.root, {
            "output_dir": str(self.root / "data"),
            "stores": {
                "baomin": {"token_env": "T_A"},
                "jinghetang": {"token_env": "T_B"},
            },
        })
        settings = load_settings_with_env(config, T_A=BAOMIN_TOKEN, T_B=JINGHETANG_TOKEN)
        self.app = TmallEcommerceApplication(settings)
        self.path = settings.ingest_path

    def tearDown(self):
        self._tmp.cleanup()

    def post(self, token: str, payload: dict):
        return self.app.handle(
            "POST",
            self.path,
            {"authorization": f"Bearer {token}", "content-type": "application/json"},
            json.dumps(payload).encode("utf-8"),
        )

    def body(self, result):
        return json.loads(result.body.decode("utf-8"))

    def test_each_token_routes_to_its_own_store(self):
        first = self.body(self.post(BAOMIN_TOKEN, daily_payload("daily-20260923-run-080606")))
        second = self.body(self.post(JINGHETANG_TOKEN, daily_payload("daily-20260923-run-080606")))
        self.assertEqual(first["status"], "accepted")
        self.assertEqual(first["store_key"], "baomin")
        self.assertEqual(second["store_key"], "jinghetang")

    def test_unknown_token_is_rejected(self):
        result = self.post("z" * 40, daily_payload("daily-20260923-run-080606"))
        self.assertEqual(result.status, 401)
        self.assertEqual(self.body(result)["code"], "unauthorized")

    def test_healthz_lists_configured_stores(self):
        result = self.app.handle("GET", "/healthz", {}, b"")
        self.assertEqual(self.body(result)["stores"], ["baomin", "jinghetang"])


class LegacyUpgradeTest(unittest.TestCase):
    """A pre-existing single-store database must survive the upgrade."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.db = self.root / "state.sqlite3"
        self.data = self.root / "data"

    def tearDown(self):
        self._tmp.cleanup()

    def test_legacy_rows_are_attributed_to_the_legacy_store(self):
        import sqlite3

        self.data.mkdir(parents=True, exist_ok=True)
        legacy_file = self.data / "received" / "daily" / "2026" / "09" / "22" / "old.json"
        legacy_file.parent.mkdir(parents=True, exist_ok=True)
        legacy_file.write_text("{}", encoding="utf-8")
        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            CREATE TABLE tmall_ecommerce_collections (
                collection_id TEXT PRIMARY KEY,
                job TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                state TEXT NOT NULL,
                complete INTEGER NOT NULL,
                dataset_names TEXT NOT NULL,
                error_count INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_path TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO tmall_ecommerce_collections VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "daily-20260922-run-080606", "daily", "2026-09-22T00:00:00+08:00",
                "2026-09-22T08:06:06+08:00", 0, "success", 1, '["portal"]', 0,
                "deadbeef", str(legacy_file),
            ),
        )
        connection.commit()
        connection.close()

        # Constructing the store runs the in-place upgrade.
        store = CollectionStore(self.db, self.data, legacy_store_key="baomin")
        verify = sqlite3.connect(self.db)
        rows = verify.execute(
            "SELECT store_key, collection_id, payload_path FROM tmall_ecommerce_collections"
        ).fetchall()
        verify.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "baomin")
        # The historical file location is untouched; only the attribution changed.
        self.assertEqual(rows[0][2], str(legacy_file))

    def test_new_writes_still_work_after_upgrade(self):
        import sqlite3

        connection = sqlite3.connect(self.db)
        connection.execute(
            """
            CREATE TABLE tmall_ecommerce_collections (
                collection_id TEXT PRIMARY KEY,
                job TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                received_at INTEGER NOT NULL,
                state TEXT NOT NULL,
                complete INTEGER NOT NULL,
                dataset_names TEXT NOT NULL,
                error_count INTEGER NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_path TEXT NOT NULL
            )
            """
        )
        connection.commit()
        connection.close()
        store = CollectionStore(self.db, self.data, legacy_store_key="baomin")
        stored = store.save(daily_payload("daily-20260923-run-080606"), "baomin")
        self.assertFalse(stored.duplicate)


def load_settings_with_env(config_path: pathlib.Path, **env):
    """Load settings with a temporary environment overlay."""
    import os

    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        return load_settings(config_path)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
