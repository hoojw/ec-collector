import datetime as dt
import json
import tempfile
import pathlib
import unittest
from decimal import Decimal
from unittest import mock

import meituan_product_sales_collect as collector


TODAY = dt.date(2026, 9, 20)


def row(**overrides):
    base = {
        "skuCode": "69512835505361",
        "upcCode": "69512835505361",
        "productName": "[顺尔宁]孟鲁司特钠片10mg*5片/盒",
        "productStatus": "售卖中",
        "gtvProdCnt": "4,568",
        "gtvAmt": "36,957.15",
        "gtvUserCnt": "1,235",
        "gtvUnitPrice": "8.09",
        "discountPrice": "8.00",
        "stock": "4,967",
        "secondCategoryName": "呼吸系统",
        "thirdCategoryName": "哮喘",
        "gtvProdCntTb": "190.96%",
        "gtvAmtTb": "184.89%",
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


class WindowTest(unittest.TestCase):
    def test_near_7_days_uses_last_7_days_code(self):
        window = collector.window_for("d7", TODAY)
        self.assertEqual(window["dateType"], "last_7_days")
        self.assertEqual(window["startDate"], "20260913")
        self.assertEqual(window["endDate"], "20260919")
        self.assertEqual(window["compareStartDate"], "20260906")
        self.assertEqual(window["compareEndDate"], "20260912")
        self.assertEqual(window["start"], "2026-09-13")
        self.assertEqual(window["end"], "2026-09-19")

    def test_single_day_window(self):
        window = collector.window_for("d1", TODAY)
        self.assertEqual(window["dateType"], "dt")
        self.assertEqual(window["startDate"], "20260919")
        self.assertEqual(window["endDate"], "20260919")
        self.assertEqual(window["compareStartDate"], "20260918")

    def test_near_30_days_window(self):
        window = collector.window_for("d30", TODAY)
        self.assertEqual(window["dateType"], "last_30_days")
        self.assertEqual(window["startDate"], "20260821")


class RequestParamsTest(unittest.TestCase):
    def test_params_match_the_page_own_request(self):
        params = collector.request_params(collector.window_for("d7", TODAY), 2)
        self.assertEqual(params["dateType"], "last_7_days")
        self.assertEqual(params["pageNum"], 2)
        self.assertEqual(params["pageSize"], collector.PAGE_SIZE)
        self.assertEqual(params["productStatus"], "")
        self.assertEqual(params["isNmdCharge"], "")
        self.assertEqual(params["csecversion"], "4.3.0")


class NormalizeRowTest(unittest.TestCase):
    def test_row_is_normalized(self):
        normalized = collector.normalize_row(row())
        self.assertEqual(normalized["sku_code"], "69512835505361")
        self.assertEqual(normalized["quantity"], 4568)
        self.assertEqual(normalized["amount"], Decimal("36957.15"))
        self.assertEqual(normalized["buyers"], 1235)
        self.assertEqual(normalized["quantity_tb"], Decimal("1.909600"))
        self.assertEqual(normalized["status"], "售卖中")

    def test_optional_comparison_columns_may_be_absent(self):
        raw = row()
        del raw["gtvProdCntTb"]
        del raw["gtvAmtTb"]
        normalized = collector.normalize_row(raw)
        self.assertIsNone(normalized["quantity_tb"])
        self.assertIsNone(normalized["amount_tb"])

    def test_missing_required_column_fails_with_available_columns(self):
        raw = row()
        del raw["gtvProdCnt"]
        with self.assertRaisesRegex(RuntimeError, "gtvProdCnt"):
            collector.normalize_row(raw)

    def test_blank_name_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no product name"):
            collector.normalize_row(row(productName="  "))

    def test_blank_sku_code_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no skuCode"):
            collector.normalize_row(row(skuCode="", upcCode=""))


class PercentTest(unittest.TestCase):
    def test_percent_string_becomes_ratio(self):
        self.assertEqual(collector.percent_or_none("7,600.00%"), Decimal("76.000000"))
        self.assertEqual(collector.percent_or_none("-12.5%"), Decimal("-0.125000"))

    def test_blank_percent_is_none(self):
        self.assertIsNone(collector.percent_or_none("--"))
        self.assertIsNone(collector.percent_or_none(None))


class FetchRowsTest(unittest.TestCase):
    def test_pagination_stops_at_total_sum(self):
        page = FakePage([
            {"code": 0, "data": {"totalSum": 3, "currentPageData": [row(skuCode="1"), row(skuCode="2")]}},
            {"code": 0, "data": {"totalSum": 3, "currentPageData": [row(skuCode="3")]}},
        ])
        rows = collector.fetch_rows(page, collector.window_for("d7", TODAY))
        self.assertEqual([r["skuCode"] for r in rows], ["1", "2", "3"])
        self.assertEqual(len(page.expressions), 2)

    def test_api_error_fails(self):
        page = FakePage([{"code": 403, "msg": "forbidden"}])
        with self.assertRaisesRegex(RuntimeError, "code=403"):
            collector.fetch_rows(page, collector.window_for("d7", TODAY))

    def test_incomplete_pagination_fails(self):
        page = FakePage([
            {"code": 0, "data": {"totalSum": 5, "currentPageData": [row()]}},
            {"code": 0, "data": {"totalSum": 5, "currentPageData": []}},
        ])
        with self.assertRaisesRegex(RuntimeError, "ended early"):
            collector.fetch_rows(page, collector.window_for("d7", TODAY))

    def test_total_change_between_pages_fails(self):
        page = FakePage([
            {"code": 0, "data": {"totalSum": 3, "currentPageData": [row(skuCode="1"), row(skuCode="2")]}},
            {"code": 0, "data": {"totalSum": 4, "currentPageData": [row(skuCode="3")]}},
        ])
        with self.assertRaisesRegex(RuntimeError, "total changed"):
            collector.fetch_rows(page, collector.window_for("d7", TODAY))


class RunTest(unittest.TestCase):
    @staticmethod
    def collected():
        return {
            "collected_at": "2026-09-20T16:10:43+08:00",
            "window": {"code": "d7", "date_type": "last_7_days", "start": "2026-09-13", "end": "2026-09-19"},
            "rows": [
                {"sku_code": "1", "upc_code": "1", "product_name": "药A", "quantity": 10, "amount": Decimal("100.00")},
                {"sku_code": "2", "upc_code": "2", "product_name": "药B", "quantity": 0, "amount": None},
            ],
        }

    def test_run_writes_rows_and_reports_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "rows.json"
            args = type("Args", (), {"port": 9333, "window": "d7", "out": out,
                                     "persist": False, "config": None, "state_file": None})()
            with mock.patch.object(collector, "collect", return_value=self.collected()):
                result = collector.run(args)
            self.assertEqual(result["sku_rows"], 2)
            self.assertEqual(result["quantity_total"], 10)
            self.assertEqual(result["amount_total"], "100.00")
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(written["platform"], "美团")
            self.assertEqual(written["quantity_field"], "gtvProdCnt")

    def test_current_collector_version_skips_a_second_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "date": dt.datetime.now(collector.TZ).date().isoformat(),
                "collector_version": collector.COLLECTOR_VERSION,
            }), encoding="utf-8")
            args = type("Args", (), {"port": 9333, "window": "d7", "out": None,
                                     "persist": False, "config": None, "state_file": state})()
            with mock.patch.object(collector, "collect") as collect_mock:
                result = collector.run(args)
            self.assertEqual(result["state"], "skipped")
            collect_mock.assert_not_called()

    def test_old_collector_version_runs_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = pathlib.Path(tmp) / "state.json"
            state.write_text(json.dumps({
                "date": dt.datetime.now(collector.TZ).date().isoformat(),
                "collector_version": collector.COLLECTOR_VERSION - 1,
            }), encoding="utf-8")
            args = type("Args", (), {"port": 9333, "window": "d7", "out": None,
                                     "persist": False, "config": None, "state_file": state})()
            with mock.patch.object(collector, "collect", return_value=self.collected()):
                result = collector.run(args)
            self.assertEqual(result["state"], "dry_run")

    def test_persist_resets_then_upserts_in_one_transaction(self):
        config = {
            "mysql": {},
            "store": {"client": "保民", "platform": "美团"},
            "workbench": {"schema": "adjust_price"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = pathlib.Path(tmp) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            args = type("Args", (), {"port": 9333, "window": "d7", "out": None,
                                     "persist": True, "config": config_path, "state_file": None})()
            connection = mock.MagicMock()
            with mock.patch.object(collector, "collect", return_value=self.collected()), \
                    mock.patch.object(collector.esale_product, "connect", return_value=connection), \
                    mock.patch.object(collector.esale_product, "ensure_table"), \
                    mock.patch.object(collector.esale_product, "reset_metrics", return_value=5) as reset, \
                    mock.patch.object(collector.esale_product, "replace", return_value=2) as replace:
                result = collector.run(args)
            self.assertEqual(result["state"], "persisted")
            self.assertEqual(result["reset"], 5)
            self.assertEqual(result["persisted"], 2)
            self.assertEqual(reset.call_args.kwargs["values"], {"销量": 0, "销售额": Decimal("0.00")})
            self.assertFalse(reset.call_args.kwargs["commit"])
            self.assertFalse(replace.call_args.kwargs["prune"])
            self.assertFalse(replace.call_args.kwargs["commit"])
            connection.commit.assert_called_once()
            connection.rollback.assert_not_called()
            connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
