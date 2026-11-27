import datetime as dt
import json
import pathlib
import tempfile
import unittest

import tmall_ecommerce_to_mysql as sync


class RecordingCursor:
    def __init__(self):
        self.statements = []
        self.parameters = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, parameters=None):
        self.statements.append(statement)
        self.parameters.append(parameters)


class RecordingConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()

    def cursor(self):
        return self.cursor_instance


def daily_payload(datasets, *, state="success"):
    return {"state": state, "data": {name: {} for name in datasets}}


class UsableColumnsTest(unittest.TestCase):
    def columns(self, datasets):
        return sync.usable_columns(
            daily_payload(datasets), sync.DAILY_COLUMN_DATASETS, sync.DAILY_METRIC_COLUMNS
        )

    def test_full_success_snapshot_covers_every_column(self):
        self.assertEqual(
            self.columns(("portal", "macro", "item_rank", "on_sale")),
            sync.DAILY_METRIC_COLUMNS,
        )

    def test_partial_snapshot_without_item_rank_skips_only_that_column(self):
        # 商品排行 超时是近期最常见的 partial 形态，其余六列都拿到了数据。
        columns = self.columns(("portal", "macro", "on_sale"))
        self.assertNotIn("非替尔泊肽销售金额占比", columns)
        self.assertEqual(len(columns), len(sync.DAILY_METRIC_COLUMNS) - 1)

    def test_macro_dependent_columns_need_both_inputs(self):
        columns = self.columns(("portal", "macro"))
        self.assertIn("本月累计交易额", columns)
        # 动销率与未动销数量都依赖 macro ÷ on_sale，缺一不可。
        self.assertNotIn("商品动销率", columns)
        self.assertNotIn("未动销商品数量", columns)

    def test_portal_absent_means_turnover_is_not_written(self):
        self.assertNotIn("昨日交易额", self.columns(("macro", "on_sale")))


class PersistScopingTest(unittest.TestCase):
    def test_daily_writes_only_the_selected_columns(self):
        connection = RecordingConnection()
        columns = ("昨日交易额", "经营商品数量")
        metrics = {"昨日交易额": 100, "经营商品数量": 7, "本月累计交易额": 999}
        written = sync.persist_daily(
            connection,
            "adjust_price",
            "esale_system",
            dt.date(2026, 9, 23),
            metrics,
            {"client": "保民", "owner": "待维护", "platform": "天猫"},
            columns,
        )
        self.assertEqual(written, columns)
        statement = connection.cursor_instance.statements[0]
        self.assertIn("`昨日交易额`", statement)
        self.assertIn("`经营商品数量`", statement)
        # 未选中的列既不出现在 INSERT 列清单，也不出现在 UPDATE 子句。
        self.assertNotIn("`本月累计交易额`", statement)
        self.assertEqual(
            connection.cursor_instance.parameters[0],
            [dt.date(2026, 9, 23), "保民", "待维护", "天猫", 100, 7],
        )

    def test_daily_without_turnover_skips_the_week_ratio_update(self):
        connection = RecordingConnection()
        sync.persist_daily(
            connection,
            "adjust_price",
            "esale_system",
            dt.date(2026, 9, 23),
            {"经营商品数量": 7},
            {"client": "保民", "owner": "待维护", "platform": "天猫"},
            ("经营商品数量",),
        )
        # 只有一条 INSERT；昨日周环比 的 UPDATE 不应触发。
        self.assertEqual(len(connection.cursor_instance.statements), 1)

    def test_daily_with_turnover_recomputes_the_week_ratio(self):
        connection = RecordingConnection()
        sync.persist_daily(
            connection,
            "adjust_price",
            "esale_system",
            dt.date(2026, 9, 23),
            {"昨日交易额": 100},
            {"client": "保民", "owner": "待维护", "platform": "天猫"},
            ("昨日交易额",),
        )
        self.assertEqual(len(connection.cursor_instance.statements), 2)
        self.assertIn("昨日周环比", connection.cursor_instance.statements[1])

    def test_daily_with_no_usable_column_fails_loudly(self):
        with self.assertRaises(ValueError):
            sync.persist_daily(
                RecordingConnection(),
                "adjust_price",
                "esale_system",
                dt.date(2026, 9, 23),
                {},
                {"client": "保民", "owner": "待维护", "platform": "天猫"},
                (),
            )

    def test_every10_writes_only_the_after_sales_columns(self):
        connection = RecordingConnection()
        written = sync.persist_every10(
            connection,
            "adjust_price",
            "esale_system",
            dt.date(2026, 9, 23),
            {"待商家审核数量": 3, "即将自动退售后单": 1},
            {"client": "保民", "platform": "天猫"},
            ("待商家审核数量",),
        )
        self.assertEqual(written, ("待商家审核数量",))
        statement = connection.cursor_instance.statements[0]
        self.assertNotIn("`即将自动退售后单`", statement)


class SnapshotSelectionTest(unittest.TestCase):
    def write(self, root, job, name, payload):
        path = root / "received" / job / "2026" / "09" / "23" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_partial_is_accepted_and_failed_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, "daily", "a.json",
                       {"state": "failed", "slot_time": "2026-09-23T00:00:00+08:00"})
            self.write(root, "daily", "b.json",
                       {"state": "partial", "slot_time": "2026-09-22T00:00:00+08:00"})
            chosen = sync._latest_usable_snapshot(root, "daily")
            self.assertEqual(chosen["state"], "partial")

    def test_the_newest_usable_slot_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, "daily", "old.json",
                       {"state": "success", "slot_time": "2026-09-21T00:00:00+08:00"})
            self.write(root, "daily", "new.json",
                       {"state": "partial", "slot_time": "2026-09-23T00:00:00+08:00"})
            self.assertEqual(
                sync._latest_usable_snapshot(root, "daily")["slot_time"],
                "2026-09-23T00:00:00+08:00",
            )

    def test_only_failed_snapshots_yield_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.write(root, "every10", "a.json",
                       {"state": "failed", "slot_time": "2026-09-23T09:00:00+08:00"})
            self.assertIsNone(sync._latest_usable_snapshot(root, "every10"))


class ConfiguredStoresTest(unittest.TestCase):
    def test_legacy_single_store_config_yields_the_default_key(self):
        pairs = sync.configured_stores(
            {"tmall_store": {"client": "保民", "platform": "天猫"}}
        )
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0][0], "default")
        self.assertEqual(pairs[0][1]["client"], "保民")

    def test_explicit_store_key_is_honoured(self):
        pairs = sync.configured_stores(
            {"tmall_store": {"client": "保民", "store_key": "baomin"}}
        )
        self.assertEqual(pairs[0][0], "baomin")

    def test_multi_store_block_returns_every_store(self):
        pairs = sync.configured_stores({
            "tmall_stores": {
                "baomin": {"client": "保民"},
                "jinghetang": {"client": "璟和堂"},
            }
        })
        self.assertEqual([key for key, _ in pairs], ["baomin", "jinghetang"])

    def test_multi_store_entry_without_client_is_rejected(self):
        with self.assertRaises(ValueError):
            sync.configured_stores({"tmall_stores": {"jinghetang": {}}})


class StoreSnapshotPathTest(unittest.TestCase):
    def store_write(self, root, store_key, job, name, payload):
        path = root / "received" / store_key / job / "2026" / "09" / "23" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_default_store_keeps_the_legacy_directory(self):
        # 保民 的既有快照躺在 received/daily/... 下，必须继续读得到。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "received" / "daily" / "2026" / "09" / "23" / "a.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(
                {"state": "success", "slot_time": "2026-09-23T00:00:00+08:00"}
            ), encoding="utf-8")
            chosen = sync._latest_usable_snapshot(root, "daily", "default")
            self.assertIsNotNone(chosen)

    def test_default_store_also_reads_the_namespaced_directory(self):
        # 迁移后接收端把 保民 的新快照写进 received/default/...，落库必须同时看两边，
        # 否则数据会永久卡在迁移那一刻（2026-09-23 → 09-24 真实发生过）。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            legacy = root / "received" / "every10" / "2026" / "09" / "23" / "old.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps(
                {"state": "success", "slot_time": "2026-09-23T09:00:00+08:00"}
            ), encoding="utf-8")
            fresh = root / "received" / "default" / "every10" / "2026" / "09" / "24" / "new.json"
            fresh.parent.mkdir(parents=True, exist_ok=True)
            fresh.write_text(json.dumps(
                {"state": "success", "slot_time": "2026-09-24T11:30:00+08:00"}
            ), encoding="utf-8")

            chosen = sync._latest_usable_snapshot(root, "every10", "default")
            self.assertEqual(chosen["slot_time"], "2026-09-24T11:30:00+08:00")

    def test_a_named_store_never_falls_back_to_the_legacy_root(self):
        # received/every10 下的是 保民 的历史，不能被当成别的店的数据。
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            legacy = root / "received" / "every10" / "2026" / "09" / "23" / "a.json"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text(json.dumps(
                {"state": "success", "slot_time": "2026-09-23T09:00:00+08:00"}
            ), encoding="utf-8")
            self.assertIsNone(sync._latest_usable_snapshot(root, "every10", "jinghetang"))

    def test_default_scans_two_roots_while_a_named_store_scans_one(self):
        data = pathlib.Path("/data")
        self.assertEqual(
            sync.snapshot_roots(data, "default", "every10"),
            (data / "received" / "every10", data / "received" / "default" / "every10"),
        )
        self.assertEqual(
            sync.snapshot_roots(data, "jinghetang", "every10"),
            (data / "received" / "jinghetang" / "every10",),
        )

    def test_named_store_reads_its_own_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.store_write(root, "jinghetang", "daily", "a.json",
                             {"state": "success", "slot_time": "2026-09-23T00:00:00+08:00"})
            chosen = sync._latest_usable_snapshot(root, "daily", "jinghetang")
            self.assertEqual(chosen["slot_time"], "2026-09-23T00:00:00+08:00")

    def test_two_stores_never_read_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.store_write(root, "baomin", "daily", "a.json",
                             {"state": "success", "slot_time": "2026-09-22T00:00:00+08:00"})
            self.store_write(root, "jinghetang", "daily", "b.json",
                             {"state": "success", "slot_time": "2026-09-23T00:00:00+08:00"})
            self.assertEqual(
                sync._latest_usable_snapshot(root, "daily", "baomin")["slot_time"],
                "2026-09-22T00:00:00+08:00",
            )
            self.assertEqual(
                sync._latest_usable_snapshot(root, "daily", "jinghetang")["slot_time"],
                "2026-09-23T00:00:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
