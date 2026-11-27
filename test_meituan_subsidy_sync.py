import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch

from meituan_subsidy_sync import (
    LIST_FILTER_TYPES,
    calculate_focus_response_average,
    collect_records,
    deduplicate_rows,
    detect_price_triggered_nodes,
    datetime_from_epoch,
    load_configured_focus_upcs,
    normalize_record,
    should_collect,
    validate_rows,
)


class MeituanSubsidySyncTest(unittest.TestCase):
    record_date = dt.date(2026, 8, 19)

    def test_empty_focus_table_disables_cross_source_lookup(self):
        with patch("meituan_subsidy_sync.load_focus_upcs") as loader:
            self.assertEqual(load_configured_focus_upcs({"focus_products": ""}), [])
        loader.assert_not_called()

    def test_focus_response_average_uses_latest_prior_exit(self):
        class Cursor:
            def __init__(self):
                self.sql = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params=None):
                self.sql.append(sql)
                self.params = params

            def fetchone(self):
                return (Decimal("42.50"),)

        cursor = Cursor()

        class Connection:
            def cursor(self):
                return cursor

        value = calculate_focus_response_average(
            Connection(), "subsidy_product_nodes", ["690001", "690002"]
        )

        query = next(sql for sql in cursor.sql if sql.startswith("WITH R AS"))
        self.assertEqual(value, Decimal("42.50"))
        self.assertEqual(cursor.params, ("690001", "690002"))
        self.assertIn("MAX(O.`时间`)", query)
        self.assertIn("I.`原因` LIKE '%%售价降低%%'", query)
        self.assertIn("O.`时间`<I.`时间`", query)
        self.assertIn("I.`upc` IN (%s,%s)", query)

    def test_approved_maps_status_and_sale_without_price_comparison(self):
        row = normalize_record(
            {
                "id": "a",
                "upcCode": "690001",
                "skuName": "测试商品",
                "actType": 17,
                "minDiscountPrice": 20,
                "currentPrice": 10,
                "mtPrice": 0.6,
            },
            1,
            self.record_date,
        )
        self.assertEqual(row[0], self.record_date)
        self.assertEqual(row[3], Decimal("20"))
        self.assertEqual(row[4], Decimal("10"))
        self.assertEqual(row[5], Decimal("0.6"))
        self.assertEqual(row[6], 1)
        self.assertIsNone(row[7])
        self.assertEqual(row[8], "折扣")

    def test_unapproved_maps_status_zero(self):
        row = normalize_record(
            {
                "id": "b",
                "upcCode": "690002",
                "skuName": "测试多盒",
                "actType": 29,
                "minDiscountPrice": 30,
                "currentPrice": 31,
                "reduceAmount": 2.4,
                "levelItemNumber": 2,
                "singleItemPoiCharge": 0.6,
            },
            0,
            self.record_date,
        )
        self.assertEqual(row[5], Decimal("2.4"))
        self.assertEqual(row[6], 0)
        self.assertEqual(row[8], "多盒")
        self.assertEqual(row[9], 2)
        self.assertEqual(row[10], Decimal("0.6"))

    def test_multi_box_burden_falls_back_to_total(self):
        row = normalize_record(
            {
                "id": "c",
                "upcCode": "690003",
                "skuName": "测试回退",
                "actType": 29,
                "minDiscountPrice": 30,
                "reduceAmount": 8.5,
                "levelItemNumber": 5,
                "poiCharge": 4,
            },
            0,
            self.record_date,
        )
        self.assertEqual(row[5], Decimal("8.5"))
        self.assertEqual(row[10], Decimal("0.8"))

    def test_reject_time_seconds_maps_to_beijing_datetime(self):
        value = datetime_from_epoch(1787636058, field="rejectTime")
        self.assertEqual(value, dt.datetime(2026, 8, 25, 13, 34, 18))

    def test_reject_time_milliseconds_is_also_supported(self):
        value = datetime_from_epoch("1787636058000", field="rejectTime")
        self.assertEqual(value, dt.datetime(2026, 8, 25, 13, 34, 18))

    def test_daily_output_contains_reject_time(self):
        row = normalize_record(
            {
                "id": "shape",
                "upcCode": "690007",
                "skuName": "结构测试",
                "actType": 17,
                "minDiscountPrice": 20,
                "rejectTime": 1787636058,
            },
            1,
            self.record_date,
        )
        self.assertEqual(len(row), 11)
        self.assertEqual(row[6], 1)
        self.assertEqual(row[7], dt.datetime(2026, 8, 25, 13, 34, 18))
        self.assertEqual(row[8], "折扣")

    @staticmethod
    def node_row(upc, activity, sale_price, threshold_price, status):
        return (
            dt.date(2026, 8, 27),
            upc,
            "测试商品",
            Decimal(str(threshold_price)),
            Decimal(str(sale_price)),
            None,
            status,
            None,
            activity,
            1,
            Decimal("0"),
        )

    def test_price_drop_records_entry_for_same_activity(self):
        previous = {
            ("690001", "折扣"): {
                "sale_price": Decimal("20.1"),
                "threshold_price": Decimal("20"),
                "status": 0,
            }
        }
        observed = dt.datetime(2026, 8, 27, 10, tzinfo=dt.timezone.utc)
        nodes = detect_price_triggered_nodes(
            previous, [self.node_row("690001", "折扣", 20, 20, 1)], observed
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0][2:4], ("入补", "售价降低"))
        self.assertEqual(len(nodes[0]), 5)

    def test_threshold_drop_records_exit_for_same_activity(self):
        previous = {
            ("690002", "多盒"): {
                "sale_price": Decimal("30"),
                "threshold_price": Decimal("30"),
                "status": 1,
            }
        }
        observed = dt.datetime(2026, 8, 27, 18, tzinfo=dt.timezone.utc)
        nodes = detect_price_triggered_nodes(
            previous, [self.node_row("690002", "多盒", 30, 29.9, 0)], observed
        )
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0][2:4], ("出补", "门槛价格降低"))

    def test_threshold_rise_records_entry(self):
        previous = {
            ("690005", "折扣"): {
                "sale_price": Decimal("20"),
                "threshold_price": Decimal("19.9"),
                "status": 0,
            }
        }
        observed = dt.datetime(2026, 8, 27, 18, tzinfo=dt.timezone.utc)
        nodes = detect_price_triggered_nodes(
            previous, [self.node_row("690005", "折扣", 20, 20, 1)], observed
        )
        self.assertEqual(nodes[0][2:4], ("入补", "门槛价格升高"))

    def test_sale_price_rise_records_exit(self):
        previous = {
            ("690006", "折扣"): {
                "sale_price": Decimal("20"),
                "threshold_price": Decimal("20"),
                "status": 1,
            }
        }
        observed = dt.datetime(2026, 8, 27, 18, tzinfo=dt.timezone.utc)
        nodes = detect_price_triggered_nodes(
            previous, [self.node_row("690006", "折扣", 20.1, 20, 0)], observed
        )
        self.assertEqual(nodes[0][2:4], ("出补", "售价升高"))

    def test_combined_entry_reasons_are_preserved(self):
        previous = {
            ("690007", "多盒"): {
                "sale_price": Decimal("20.1"),
                "threshold_price": Decimal("19.9"),
                "status": 0,
            }
        }
        observed = dt.datetime(2026, 8, 27, 18, tzinfo=dt.timezone.utc)
        nodes = detect_price_triggered_nodes(
            previous, [self.node_row("690007", "多盒", 20, 20, 1)], observed
        )
        self.assertEqual(nodes[0][3], "售价降低、门槛价格升高")

    def test_state_change_without_expected_price_driver_is_not_recorded(self):
        previous = {
            ("690003", "折扣"): {
                "sale_price": Decimal("20"),
                "threshold_price": Decimal("20"),
                "status": 0,
            }
        }
        observed = dt.datetime(2026, 8, 27, 18, tzinfo=dt.timezone.utc)
        nodes = detect_price_triggered_nodes(
            previous, [self.node_row("690003", "折扣", 20, 20, 1)], observed
        )
        self.assertEqual(nodes, [])

    def test_activity_type_is_part_of_comparison_key(self):
        previous = {
            ("690004", "折扣"): {
                "sale_price": Decimal("20.1"),
                "threshold_price": Decimal("20"),
                "status": 0,
            },
            ("690004", "多盒"): {
                "sale_price": Decimal("30"),
                "threshold_price": Decimal("30"),
                "status": 1,
            },
        }
        observed = dt.datetime(2026, 8, 27, 18, tzinfo=dt.timezone.utc)
        nodes = detect_price_triggered_nodes(
            previous,
            [
                self.node_row("690004", "折扣", 20, 20, 1),
                self.node_row("690004", "多盒", 30, 29.9, 0),
            ],
            observed,
        )
        self.assertEqual([(node[1], node[2]) for node in nodes], [("折扣", "入补"), ("多盒", "出补")])

    def test_duplicate_business_key_is_rejected(self):
        base = normalize_record(
            {
                "id": "d",
                "upcCode": "690004",
                "skuName": "重复商品",
                "actType": 17,
                "minDiscountPrice": 20,
            },
            0,
            self.record_date,
        )
        duplicate = base[:4] + (Decimal("19"),) + base[5:]
        with self.assertRaises(ValueError):
            validate_rows([base, duplicate])

    def test_exact_duplicate_business_row_is_collapsed(self):
        row = normalize_record(
            {
                "id": "d",
                "upcCode": "690004",
                "skuName": "重复商品",
                "actType": 17,
                "minDiscountPrice": 20,
            },
            0,
            self.record_date,
        )
        with self.assertLogs(level="WARNING"):
            self.assertEqual(deduplicate_rows([row, row]), [row])

    def test_conflicting_duplicate_business_row_is_rejected_before_write(self):
        row = normalize_record(
            {
                "id": "d",
                "upcCode": "690004",
                "skuName": "重复商品",
                "actType": 17,
                "minDiscountPrice": 20,
            },
            0,
            self.record_date,
        )
        conflicting = row[:4] + (Decimal("19"),) + row[5:]
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            deduplicate_rows([row, conflicting])

    def test_list_filter_types_are_explicit(self):
        self.assertEqual(LIST_FILTER_TYPES, {1: 1, 0: 0})

    def test_unapproved_only_collects_price_reason(self):
        base = {"status": 0, "rej": [{"rejText": "价格不符合补贴要求"}]}
        self.assertTrue(should_collect(base, 0))
        self.assertTrue(should_collect({"status": 2, "rej": [{"rejText": "价格不符合补贴要求"}]}, 0))
        self.assertFalse(should_collect({"status": 0, "rej": [{"rejText": "商品已下架"}]}, 0))

    @patch("meituan_subsidy_sync.fetch_page")
    def test_invalid_product_is_skipped_without_discarding_valid_rows(self, fetch_page):
        fetch_page.return_value = {
            "data": {
                "total": 2,
                "data": [
                    {
                        "id": "invalid",
                        "status": 0,
                        "rej": [{"rejText": "价格不符合补贴要求"}],
                        "upcCode": "690005",
                        "skuName": "字段残缺的多盒商品",
                        "actType": 29,
                        "minDiscountPrice": None,
                        "levelItemNumber": None,
                    },
                    {
                        "id": "valid",
                        "status": 0,
                        "rej": [{"rejText": "价格不符合补贴要求"}],
                        "upcCode": "690006",
                        "skuName": "正常折扣商品",
                        "actType": 17,
                        "minDiscountPrice": 20,
                    },
                ],
            }
        }

        with self.assertLogs(level="WARNING") as logs:
            rows = collect_records(object(), 0, self.record_date, 100)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "690006")
        self.assertIn("id=invalid", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
