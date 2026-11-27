import datetime as dt
import json
import pathlib
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

import meituan_listed_products_collect as collector


TODAY = dt.date(2026, 9, 21)

CONFIG = {
    "mysql": {"host": "127.0.0.1", "port": 3307, "user": "u", "password": "p",
              "database": "adjust_price", "charset": "utf8mb4"},
    "meituan_store": {"poi_id": "32581900", "client": "保民", "platform": "美团"},
    "store": {"client": "保民", "platform": "美团"},
}


def row(**overrides):
    """One ``searchListPageV2`` row, trimmed to the fields the collector reads."""
    base = {
        "id": 27693458919,
        "name": "[伊迈格]托拉塞米片20mg*6片/板/盒",
        "upcCode": "6921619514884",
        "salesNumInThirtyDays": 7,
        "sellCount": 7,
        "sellStatus": 0,
        "isStopSell": 0,
        "medicineType": 2,
        "medicineTypeName": "处方药",
        "wmProductSkus": [{"id": 52401453302, "spec": "20mg*6片/板/盒", "price": 8.5,
                           "stock": 13, "upcCode": "6921619514884"}],
    }
    base.update(overrides)
    return base


class FakePage:
    """Minimal stand-in for ``CdpPage`` that replays queued API payloads."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.expressions = []

    def evaluate(self, expression):
        self.expressions.append(expression)
        payload = self.payloads.pop(0)
        return json.dumps({"status": 200, "body": json.dumps(payload, ensure_ascii=False)})


class PoiIdTest(unittest.TestCase):
    def test_poi_id_comes_from_the_meituan_store_section(self):
        self.assertEqual(collector.poi_id(CONFIG), "32581900")

    def test_flat_store_poi_id_is_accepted(self):
        payload = {"store": {"poi_id": "33770492", "client": "福康综合门诊", "platform": "美团"}}
        self.assertEqual(collector.poi_id(payload), "33770492")

    def test_missing_poi_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "poi_id"):
            collector.poi_id({"store": {"client": "保民", "platform": "美团"}})


class OpenProductListTest(unittest.TestCase):
    """The list route must be the mounted page, not the analysis route.

    Signing happens per page module: firing searchListPageV2 from the analysis
    page returned non-JSON for stores without the list module resident.
    """

    class FakeCdp:
        def __init__(self, states):
            self.states = list(states)
            self.calls = []

        def call(self, method, params=None):
            self.calls.append((method, params))
            return {}

        def evaluate(self, expression):
            return self.states.pop(0)

    def test_navigates_to_the_list_route_and_waits_for_the_column(self):
        page = self.FakeCdp([{"ready": False, "challenged": False},
                             {"ready": True, "challenged": False}])
        with mock.patch.object(collector.time, "sleep") as sleep:
            collector.open_product_list(page)
        navigate = [c for c in page.calls if c[0] == "Page.navigate"]
        self.assertEqual(len(navigate), 1)
        self.assertEqual(navigate[0][1]["url"], collector.LIST_URL)
        self.assertIn("/page/product/list/single", navigate[0][1]["url"])
        sleep.assert_called_once_with(1)

    def test_a_challenge_is_reported_instead_of_hammering(self):
        page = self.FakeCdp([{"ready": False, "challenged": True}])
        with self.assertRaisesRegex(RuntimeError, "challenged"):
            collector.open_product_list(page)

    def test_a_page_that_never_renders_times_out(self):
        page = self.FakeCdp([{"ready": False, "challenged": False}])
        with mock.patch.object(collector.time, "sleep"), \
             mock.patch.object(collector.time, "monotonic", side_effect=[0, 0, 61, 61]):
            with self.assertRaisesRegex(RuntimeError, "did not finish loading"):
                collector.open_product_list(page)


class RequestParamsTest(unittest.TestCase):
    def test_params_match_the_page_own_request(self):
        params = collector.request_params("32581900", 2)
        self.assertEqual(params["wmPoiId"], "32581900")
        self.assertEqual(params["pageNum"], "2")
        self.assertEqual(params["pageSize"], "100")
        self.assertEqual(params["state"], "0")
        self.assertEqual(params["problemType"], "1")
        self.assertEqual(params["needCombinationSpu"], "-1")


class NormalizeRowTest(unittest.TestCase):
    def test_row_is_normalized(self):
        normalized = collector.normalize_row(row())
        self.assertEqual(normalized["spu_id"], "27693458919")
        self.assertEqual(normalized["upc_code"], "6921619514884")
        self.assertEqual(normalized["sales_30"], 7)
        self.assertEqual(normalized["stock"], 13)
        self.assertEqual(normalized["price"], Decimal("8.50"))
        self.assertEqual(normalized["medicine_type_name"], "处方药")

    def test_spu_upc_falls_back_to_the_first_sku(self):
        raw = row(upcCode="")
        self.assertEqual(collector.sku_upc(raw), "6921619514884")

    def test_no_upc_anywhere_is_none(self):
        raw = row(upcCode="", wmProductSkus=[{"id": 1, "price": 1, "stock": 2, "upcCode": ""}])
        self.assertIsNone(collector.sku_upc(raw))

    def test_stock_is_summed_over_skus(self):
        raw = row(wmProductSkus=[{"id": 1, "stock": 10}, {"id": 2, "stock": 5}])
        self.assertEqual(collector.sku_stock(raw), 15)

    def test_stock_is_none_when_no_sku_reports_one(self):
        raw = row(wmProductSkus=[{"id": 1, "spec": "x"}])
        self.assertIsNone(collector.sku_stock(raw))

    def test_missing_thirty_day_field_fails_loudly(self):
        raw = row()
        del raw["salesNumInThirtyDays"]
        with self.assertRaisesRegex(RuntimeError, "salesNumInThirtyDays"):
            collector.normalize_row(raw)

    def test_blank_name_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no product name"):
            collector.normalize_row(row(name="  "))

    def test_missing_id_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no product id"):
            collector.normalize_row(row(id=""))

    def test_zero_sales_is_kept_as_zero_not_dropped(self):
        normalized = collector.normalize_row(row(salesNumInThirtyDays=0, sellCount=0))
        self.assertEqual(normalized["sales_30"], 0)


class FetchRowsTest(unittest.TestCase):
    def test_pagination_stops_at_total_count(self):
        page = FakePage([
            {"code": 0, "data": {"totalCount": 3, "productList": [row(id=1), row(id=2)]}},
            {"code": 0, "data": {"totalCount": 3, "productList": [row(id=3)]}},
        ])
        rows = collector.fetch_rows(page, "32581900")
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])
        self.assertEqual(len(page.expressions), 2)

    def test_api_error_fails(self):
        page = FakePage([{"code": 403, "msg": "forbidden"}])
        with self.assertRaisesRegex(RuntimeError, "code=403"):
            collector.fetch_rows(page, "32581900")

    def test_incomplete_pagination_fails(self):
        page = FakePage([
            {"code": 0, "data": {"totalCount": 5, "productList": [row()]}},
            {"code": 0, "data": {"totalCount": 5, "productList": []}},
        ])
        with self.assertRaisesRegex(RuntimeError, "ended early"):
            collector.fetch_rows(page, "32581900")

    def test_total_change_between_pages_fails(self):
        page = FakePage([
            {"code": 0, "data": {"totalCount": 3, "productList": [row(id=1), row(id=2)]}},
            {"code": 0, "data": {"totalCount": 4, "productList": [row(id=3)]}},
        ])
        with self.assertRaisesRegex(RuntimeError, "totalCount changed"):
            collector.fetch_rows(page, "32581900")

    def test_missing_product_list_fails(self):
        page = FakePage([{"code": 0, "data": {"totalCount": 0}}])
        with self.assertRaisesRegex(RuntimeError, "no productList"):
            collector.fetch_rows(page, "32581900")


class SalesRowsTest(unittest.TestCase):
    def test_upc_is_preferred_and_seven_day_sales_is_left_null(self):
        collected = {"rows": [
            {"upc_code": "6921619514884", "spu_id": "27693458919",
             "product_name": "药A", "sales_30": 7, "stock": 13},
            {"upc_code": None, "spu_id": "27693458920",
             "product_name": "药B", "sales_30": 0, "stock": None},
        ]}
        rows = collector.sales_rows(collected)
        self.assertEqual(rows[0], {"code": "6921619514884", "name": "药A", "price": None,
                                   "quantity_30": 7, "stock": 13})
        self.assertEqual(rows[1]["code"], "27693458920")
        # 销量/销售额 must stay absent so this feed cannot clobber them.
        self.assertNotIn("quantity", rows[0])
        self.assertNotIn("amount", rows[0])


class RunTest(unittest.TestCase):
    def collected(self):
        return {
            "collected_at": "2026-09-21T10:30:00+08:00",
            "poi_id": "32581900",
            "window": {"code": "d30", "date_type": "last_30_days",
                       "field": "salesNumInThirtyDays"},
            "rows": [
                {"spu_id": "1", "upc_code": "6921619514884", "product_name": "药A",
                 "sales_30": 10, "stock": 5},
                {"spu_id": "2", "upc_code": "6921619514885", "product_name": "药B",
                 "sales_30": 0, "stock": 0},
            ],
        }

    def args(self, tmp, **overrides):
        base = {"port": 9333, "out": None, "persist": False,
                "config": pathlib.Path(tmp) / "config.json", "state_file": None}
        base.update(overrides)
        return type("Args", (), base)()

    def test_run_writes_rows_and_reports_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = pathlib.Path(tmp) / "config.json"
            config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
            out = pathlib.Path(tmp) / "rows.json"
            with mock.patch.object(collector, "collect", return_value=self.collected()):
                result = collector.run(self.args(tmp, out=out, config=config_path))
            self.assertEqual(result["product_rows"], 2)
            self.assertEqual(result["sales_30_total"], 10)
            self.assertEqual(result["zero_sales_rows"], 1)
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(written["platform"], "美团")
            self.assertEqual(written["window"]["field"], "salesNumInThirtyDays")

    def test_config_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "config is required"):
                collector.run(self.args(tmp, config=None))

    def test_state_file_skips_a_second_run_same_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = pathlib.Path(tmp) / "config.json"
            config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
            state = pathlib.Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "date": dt.datetime.now(collector.TZ).date().isoformat(),
                "collector_version": collector.COLLECTOR_VERSION,
            }),
                             encoding="utf-8")
            with mock.patch.object(collector, "collect") as fake:
                result = collector.run(self.args(tmp, config=config_path, state_file=state))
            self.assertEqual(result["state"], "skipped")
            fake.assert_not_called()

    def test_persist_upserts_without_pruning_and_then_prunes_stale(self):
        """The 上架 feed must not wipe 销量, but must still drop delisted products.

        销量/销售额 belong to the skuRank feed, which this collector cannot see.
        An unconditional slice wipe therefore destroyed them (the 2026-09-21
        incident: the 14:41 上架 run deleted the 10:39 skuRank rows).  The fix is
        prune=False plus an explicit prune_stale against this feed's own codes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            config_path = pathlib.Path(tmp) / "config.json"
            config_path.write_text(json.dumps(CONFIG), encoding="utf-8")
            connection = mock.MagicMock()
            with mock.patch.object(collector, "collect", return_value=self.collected()), \
                 mock.patch.object(collector.esale_product, "ensure_table"), \
                 mock.patch.object(collector.esale_product, "connect", return_value=connection), \
                 mock.patch.object(collector.esale_product, "replace",
                                   return_value=2) as replace, \
                 mock.patch.object(collector.esale_product, "prune_stale",
                                   return_value=1) as prune:
                result = collector.run(self.args(tmp, config=config_path, persist=True))
            self.assertEqual(result["state"], "persisted")
            self.assertEqual(result["persisted"], 2)
            self.assertEqual(result["pruned"], 1)
            self.assertFalse(replace.call_args.kwargs["prune"])
            self.assertEqual(prune.call_args.kwargs["codes"], ["6921619514884", "6921619514885"])
            self.assertEqual(prune.call_args.kwargs["client"], "保民")
            self.assertEqual(prune.call_args.kwargs["platform"], "美团")
            self.assertFalse(replace.call_args.kwargs["commit"])
            self.assertFalse(prune.call_args.kwargs["commit"])
            connection.commit.assert_called_once()
            connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
