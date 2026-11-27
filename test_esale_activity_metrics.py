import datetime as dt
import unittest

import esale_activity_metrics as metrics


class FakeCursor:
    def __init__(self, latest):
        self.latest = latest

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, *_args, **_kwargs):
        return None

    def fetchone(self):
        return (self.latest,)


class FakeConnection:
    def __init__(self, latest):
        self.latest = latest

    def cursor(self):
        return FakeCursor(self.latest)


class SequencedConnection:
    def __init__(self, values):
        self.values = iter(values)

    def cursor(self):
        return FakeCursor(next(self.values))


class EmptyMeituanSnapshotTest(unittest.TestCase):
    def test_workbench_identity_comes_from_store_config(self):
        value = metrics.workbench_settings(
            {
                "meituan_store": {"client": "天津西青福康综合门诊部", "owner": "待维护"},
                "tables": {"esale_system": "store_esale_system"},
            }
        )
        self.assertEqual(
            value,
            ("store_esale_system", "天津西青福康综合门诊部", "待维护"),
        )

    def test_generic_store_identity_supports_jd(self):
        value = metrics.workbench_settings(
            {"store": {"client": "福康综合门诊", "owner": "待维护"}}
        )
        self.assertEqual(value, ("esale_system", "福康综合门诊", "待维护"))

    def test_workbench_target_defaults_to_shared_schema(self):
        self.assertEqual(metrics.workbench_target({}), ("adjust_price", "esale_system"))

    def test_explicitly_allowed_empty_snapshot_is_zero(self):
        value = metrics.meituan_counts(
            FakeConnection(None),
            dt.date(2026, 9, 7),
            "subsidy_unsubsidized_daily",
            allow_empty=True,
        )
        self.assertEqual(value, (0, 0))

    def test_stale_snapshot_still_fails_when_empty_is_allowed(self):
        with self.assertRaisesRegex(RuntimeError, "latest is 2026-09-06"):
            metrics.meituan_counts(
                FakeConnection(dt.date(2026, 9, 6)),
                dt.date(2026, 9, 7),
                "subsidy_unsubsidized_daily",
                allow_empty=True,
            )

    def test_jd_allows_empty_match_when_the_catalog_snapshot_exists(self):
        # fetchone() sequence: match freshness, catalog row count, listing date.
        value = metrics.jd_counts(
            SequencedConnection([None, 17150, dt.date(2026, 9, 7)]),
            dt.date(2026, 9, 7),
            "jd_subsidy_sku",
            "jd_subsidy_catalog",
            "jd_store_sku_normalized_daily",
            allow_empty_match=True,
        )
        self.assertEqual(value, (0, 0))

    def test_jd_empty_catalog_snapshot_fails_loudly(self):
        with self.assertRaisesRegex(RuntimeError, "is empty"):
            metrics.jd_counts(
                SequencedConnection([dt.date(2026, 9, 7), 0, dt.date(2026, 9, 7)]),
                dt.date(2026, 9, 7),
                "jd_subsidy_sku",
                "jd_subsidy_catalog",
                "jd_store_sku_normalized_daily",
            )

    def test_jd_fallback_requires_one_shared_historical_partition(self):
        source_date = dt.date(2026, 9, 21)
        value = metrics.jd_latest_complete_source_date(
            FakeConnection(source_date),
            dt.date(2026, 9, 22),
            "jd_subsidy_sku",
            "jd_store_sku_normalized_daily",
        )
        self.assertEqual(value, source_date)


if __name__ == "__main__":
    unittest.main()
