import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from meituan_top3_lowest_discount_price import (
    compact,
    collect_rows,
    compare_date_for,
    normalize_rows,
    parse_price,
    upsert_rows,
)


class MeituanTop3LowestDiscountPriceTest(unittest.TestCase):
    def test_compare_date_is_previous_day(self):
        now = dt.datetime(2026, 9, 14, 16, 5, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(compare_date_for(now), dt.date(2026, 9, 13))
        self.assertEqual(compact(dt.date(2026, 9, 13)), "20260913")

    def test_compare_date_crosses_month_boundary(self):
        now = dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

        self.assertEqual(compare_date_for(now), dt.date(2026, 8, 31))

    def test_parse_price_strips_grouping_separator(self):
        self.assertEqual(parse_price("1,000.90", field="price"), Decimal("1000.90"))
        self.assertEqual(parse_price("7.90", field="price"), Decimal("7.90"))

    def test_parse_price_rejects_empty_and_non_numeric(self):
        for value in ("", " ", None, "-", "abc"):
            with self.assertRaises(ValueError):
                parse_price(value, field="price")

    def test_normalize_rows_uses_upc_and_price_only(self):
        rows = normalize_rows(
            [
                {
                    "upcCode": "9999901023919",
                    "skuCode": "9999901023919",
                    "productName": "[穆峰达]替尔泊肽注射液10mg",
                    "gtvSoldTop3LowestDiscountPrice": "388.88",
                    "discountPrice": "396.40",
                },
                {
                    "upcCode": "6974866240013",
                    "skuCode": "6974866240105",
                    "gtvSoldTop3LowestDiscountPrice": "20.10",
                },
            ]
        )

        self.assertEqual(
            rows,
            [("9999901023919", Decimal("388.88")), ("6974866240013", Decimal("20.10"))],
        )

    def test_normalize_rows_prefers_upc_over_sku_code(self):
        rows = normalize_rows(
            [{"upcCode": "111", "skuCode": "222", "gtvSoldTop3LowestDiscountPrice": "1.00"}]
        )

        self.assertEqual(rows, [("111", Decimal("1.00"))])

    def test_normalize_rows_skips_missing_upc(self):
        with self.assertLogs(level="WARNING"):
            rows = normalize_rows([{"upcCode": "", "gtvSoldTop3LowestDiscountPrice": "1.00"}])
        self.assertEqual(rows, [])

    def test_normalize_rows_skips_missing_price(self):
        with self.assertLogs(level="WARNING"):
            rows = normalize_rows([{"upcCode": "111", "gtvSoldTop3LowestDiscountPrice": ""}])
        self.assertEqual(rows, [])

    def test_collect_rows_counts_skipped_source_records_for_pagination(self):
        payload = {
            "data": {
                "totalSum": 2,
                "currentPageData": [
                    {"upcCode": "111", "gtvSoldTop3LowestDiscountPrice": "1.00"},
                    {"upcCode": "222", "gtvSoldTop3LowestDiscountPrice": ""},
                ],
            }
        }
        with patch("meituan_top3_lowest_discount_price.fetch_page", return_value=payload) as fetch:
            with self.assertLogs(level="WARNING"):
                rows = collect_rows(object(), dt.date(2026, 9, 14), 20)
        self.assertEqual(rows, [("111", Decimal("1.00"))])
        fetch.assert_called_once()

    def test_upsert_writes_with_updated_at_and_keyed_by_upc(self):
        class Cursor:
            rowcount = 2

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def executemany(self, sql, rows):
                self.sql = sql
                self.rows = rows

        cursor = Cursor()

        class Connection:
            def cursor(self):
                return cursor

        observed_at = dt.datetime(2026, 9, 14, 16, 5, 0)
        rows = [("111", Decimal("1.00")), ("222", Decimal("2.00"))]
        written = upsert_rows(Connection(), "mt_Top3LowestDiscountPrice", rows, observed_at)

        self.assertEqual(written, 2)
        self.assertIn("ON DUPLICATE KEY UPDATE", cursor.sql)
        self.assertIn("`updated_at`", cursor.sql)
        self.assertEqual(
            cursor.rows,
            [("111", Decimal("1.00"), observed_at), ("222", Decimal("2.00"), observed_at)],
        )

    def test_upsert_skips_database_when_no_rows(self):
        class Connection:
            def cursor(self):
                raise AssertionError("database must not be touched")

        written = upsert_rows(Connection(), "mt_Top3LowestDiscountPrice", [], dt.datetime.now())

        self.assertEqual(written, 0)


if __name__ == "__main__":
    unittest.main()
