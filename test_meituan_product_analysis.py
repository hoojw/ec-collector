import itertools
import unittest
from decimal import Decimal
from unittest import mock

import meituan_product_analysis as analysis


class MissingMetricTest(unittest.TestCase):
    def test_dash_chart_values_are_unavailable_not_a_collection_error(self):
        sale = [
            {"indexCode": "gtv_amt", "value": "-", "compareValue": "-"},
            {"indexCode": "sold_sku_cnt", "value": "-"},
            {"indexCode": "onsale_sku_sold_rate", "value": "-"},
        ]
        on_sale = [
            {"indexCode": "onsale_sku_cnt", "value": "0"},
            {"indexCode": "onsale_stock_sku_cnt", "value": "0"},
        ]
        value = analysis.summarize_period(sale, on_sale, [])
        self.assertIsNone(value["amount"])
        self.assertEqual(value["average_on_sale_skus"], Decimal("0"))
        self.assertIsNone(value["non_tirzepatide_ratio"])


class PaginationSettleTest(unittest.TestCase):
    """The Meituan backend answers inconsistently during the cold-start settle
    window (on-demand Chrome is restarted per cycle). fetch_sku_rows must retry
    the whole pagination instead of failing the collection cycle."""

    def _run(self, backend, *, attempts=5):
        """Drive fetch_sku_rows with a fake endpoint.

        backend maps pageNum -> list of (rows, totalSum) answers; the last answer
        repeats. Cursors persist across retries, like the live endpoint's state.
        """
        cursor = {}

        def fake_fetch_json(page, path, params):
            page_num = int(params["pageNum"])
            queue = backend.get(page_num)
            if queue is None:
                # An unlisted page: behave like a settled backend with no more rows.
                return {"code": 0, "data": {"currentPageData": [], "totalSum": last_total["v"]}}
            idx = min(cursor.get(page_num, 0), len(queue) - 1)
            rows, total = queue[idx]
            cursor[page_num] = cursor.get(page_num, 0) + 1
            last_total["v"] = total
            return {"code": 0, "data": {"currentPageData": rows, "totalSum": total}}

        last_total = {"v": 0}

        with mock.patch.object(analysis, "fetch_json", fake_fetch_json), \
                mock.patch.object(analysis.time, "sleep"):
            return analysis.fetch_sku_rows(None, {})

    def test_stable_single_page(self):
        rows = self._run({1: [([{"i": 1}] * 87, 87)]})
        self.assertEqual(len(rows), 87)

    def test_stable_multi_page_with_short_final_page(self):
        rows = self._run({
            1: [([{"i": 1}] * 100, 250)],
            2: [([{"i": 1}] * 100, 250)],
            3: [([{"i": 1}] * 50, 250)],
        })
        self.assertEqual(len(rows), 250)

    def test_genuinely_empty_catalog_is_not_retried(self):
        rows = self._run({1: [([], 0)]})
        self.assertEqual(rows, [])

    def test_empty_first_page_then_settles(self):
        rows = self._run({1: [([], 87), ([{"i": 1}] * 87, 87)]})
        self.assertEqual(len(rows), 87)

    def test_total_sum_shifts_before_completion_then_settles(self):
        """A page that does not yet cover its total, followed by a shifted total."""
        rows = self._run({
            1: [([{"i": 1}] * 100, 150), ([{"i": 1}] * 100, 100)],
        })
        self.assertEqual(len(rows), 100)

    def test_shrinking_total_on_first_page(self):
        rows = self._run({1: [([{"i": 1}] * 100, 250), ([{"i": 1}] * 100, 100)]})
        self.assertEqual(len(rows), 100)

    def test_empty_page_mid_walk_then_settles(self):
        rows = self._run({
            1: [([{"i": 1}] * 100, 150), ([{"i": 1}] * 100, 150)],
            2: [([], 150), ([{"i": 1}] * 50, 150)],
        })
        self.assertEqual(len(rows), 150)

    def test_never_settles_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._run({1: [([], 87)]})
        self.assertIn("did not settle", str(ctx.exception))

    def test_backend_serving_fewer_rows_than_advertised_raises(self):
        with self.assertRaises(RuntimeError):
            self._run({1: [([{"i": 1}] * 100, 250)], 2: [([], 250)]})

    def test_retry_is_bounded(self):
        calls = itertools.count()

        def fake_fetch_json(page, path, params):
            next(calls)
            return {"code": 0, "data": {"currentPageData": [], "totalSum": 87}}

        with mock.patch.object(analysis, "fetch_json", fake_fetch_json), \
                mock.patch.object(analysis.time, "sleep"):
            with self.assertRaises(RuntimeError):
                analysis.fetch_sku_rows(None, {})
        self.assertLessEqual(next(calls), 10)


if __name__ == "__main__":
    unittest.main()

