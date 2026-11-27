import datetime as dt
import json
import unittest
from decimal import Decimal

import jd_product_sales_collect as collector


TODAY = dt.date(2026, 9, 20)

# The parameter JD's own product-360 drill-down shipped for 2026-09-13~09-19.
# Minting the same window must reproduce it byte for byte.
SHIPPED_PARAM = (
    "N4IgDgTg9gKgnmApiAXCAzmAriANCAEwEMAXRMASwGMBrRCANSIBstkVQqsIJEA7EgGlEcVIQDseEFx78STVuwDaIAEwAGVQDYAtOoCcOgIwBmKRu17DR-SAC6"
    "+KlAC2YIr2Gi0ACwBGUp1d3RAU2VBULXQM9LXNNKOtVewBfZKA"
)


def row(spu_id="1001", name="[格华止]盐酸二甲双胍片 0.5g*20片 3盒装", **overrides):
    base = {
        "spu_id": spu_id,
        "name": name,
        "pro_url": "//item.jd.com/10219573867402.html",
        "item_num": "",
        "img_src": "//img.jd.com/a.jpg",
        "cate_1": "13314",
        "cate_2": "13424",
        "cate_3": "13425",
        collector.AMOUNT: 49427.739999999976,
        collector.QUANTITY: 469,
        collector.CONVERSION_RATE: 0.292439,
        collector.PAGE_VIEWS: 3757,
        collector.VISITORS: 1402,
        f"{collector.AMOUNT}##compareValue": 44553.89000000001,
        f"{collector.QUANTITY}##compareValue": 426,
    }
    base.update(overrides)
    return base


def payload(rows, code=0, desc="success"):
    return {"header": {"code": code, "desc": desc}, "body": {"data": rows, "size": len(rows)}}


SUMMARY = {
    "spu_id": "合计",
    "$summary": True,
    "name": None,
    collector.AMOUNT: 108891.81999999976,
    collector.QUANTITY: 931,
    collector.CONVERSION_RATE: 0.21833,
    collector.PAGE_VIEWS: 9779,
    collector.VISITORS: 3797,
}


class WindowTest(unittest.TestCase):
    def test_near_7_days_ends_yesterday_and_compares_previous_span(self):
        self.assertEqual(
            collector.window_for("d7", TODAY),
            {
                "start": "2026-09-13",
                "end": "2026-09-19",
                "compare_start": "2026-09-06",
                "compare_end": "2026-09-12",
            },
        )

    def test_near_30_days_window(self):
        window = collector.window_for("d30", TODAY)
        self.assertEqual(window["start"], "2026-08-21")
        self.assertEqual(window["end"], "2026-09-19")
        self.assertEqual(window["compare_start"], "2026-07-22")

    def test_unknown_dimension_is_rejected(self):
        with self.assertRaises(KeyError):
            collector.window_for("d1", TODAY)


class MintUrlTest(unittest.TestCase):
    def test_minted_url_reproduces_the_link_jd_itself_shipped(self):
        self.assertEqual(
            collector.mint_url("d7", collector.window_for("d7", TODAY)),
            f"{collector.PAGE_URL}?productDetailUrlParams={SHIPPED_PARAM}",
        )

    def test_minted_parameter_is_url_safe(self):
        param = collector.mint_url("d30", collector.window_for("d30", TODAY)).split("=", 1)[1]
        self.assertTrue(param)
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$")
        self.assertEqual(sorted(set(param) - allowed), [])

    def test_different_windows_mint_different_urls(self):
        self.assertNotEqual(
            collector.mint_url("d7", collector.window_for("d7", TODAY)),
            collector.mint_url("d30", collector.window_for("d30", TODAY)),
        )


class AssertWindowTest(unittest.TestCase):
    def test_page_that_honoured_the_minted_window_passes(self):
        request = {"postData": json.dumps({"dateType": "d7", "startDate": "2026-09-13", "endDate": "2026-09-19"})}
        collector.assert_window(request, collector.window_for("d7", TODAY), "d7")

    def test_page_that_ignored_the_window_fails(self):
        request = {"postData": json.dumps({"dateType": "d7", "startDate": "2026-09-06", "endDate": "2026-09-12"})}
        with self.assertRaisesRegex(RuntimeError, "ignored the requested window"):
            collector.assert_window(request, collector.window_for("d7", TODAY), "d7")

    def test_missing_request_body_fails(self):
        with self.assertRaisesRegex(RuntimeError, "ignored the requested window"):
            collector.assert_window({}, collector.window_for("d7", TODAY), "d7")

    def test_page_size_is_read_from_the_pages_own_request(self):
        self.assertEqual(collector.request_page_size({"postData": '{"pageSize": 20}'}), 20)
        self.assertIsNone(collector.request_page_size({"postData": "not json"}))
        self.assertIsNone(collector.request_page_size({}))


class ParseResponseTest(unittest.TestCase):
    def test_summary_row_is_split_out_from_products(self):
        rows, summary = collector.parse_table_response(payload([SUMMARY, row()]), page_size=500)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["spu_id"], "1001")
        self.assertEqual(rows[0]["quantity"], 469)
        self.assertEqual(rows[0]["amount"], Decimal("49427.74"))
        self.assertEqual(rows[0]["quantity_prev"], 426)
        self.assertEqual(rows[0]["item_id"], "10219573867402")
        self.assertEqual(summary["spu_id"], "合计")
        self.assertEqual(summary["quantity"], 931)

    def test_blank_fields_become_none(self):
        rows, _ = collector.parse_table_response(payload([SUMMARY, row(item_num="  ", **{collector.QUANTITY: None})]))
        self.assertIsNone(rows[0]["item_num"])
        self.assertIsNone(rows[0]["quantity"])

    def test_missing_summary_row_fails(self):
        with self.assertRaisesRegex(RuntimeError, "summary"):
            collector.parse_table_response(payload([row()]), page_size=500)

    def test_row_without_spu_id_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no SPU id"):
            collector.parse_table_response(payload([SUMMARY, row(spu_id="")]), page_size=500)

    def test_row_without_name_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no product name"):
            collector.parse_table_response(payload([SUMMARY, row(name="")]), page_size=500)

    def test_full_page_is_treated_as_truncation(self):
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            collector.parse_table_response(payload([SUMMARY, row()]), page_size=2)

    def test_unknown_page_size_skips_the_truncation_guard(self):
        rows, _ = collector.parse_table_response(payload([SUMMARY, row()]))
        self.assertEqual(len(rows), 1)

    def test_gateway_error_code_fails(self):
        with self.assertRaisesRegex(RuntimeError, "code=-402"):
            collector.parse_table_response({"code": -402, "msg": "不安全的请求"})

    def test_header_error_code_fails(self):
        with self.assertRaisesRegex(RuntimeError, "desc=失败"):
            collector.parse_table_response(payload([SUMMARY, row()], code=10001, desc="失败"))

    def test_empty_data_fails(self):
        with self.assertRaisesRegex(RuntimeError, "no data rows"):
            collector.parse_table_response({"header": {"code": 0}, "body": {"data": []}})


class NormalizeRowTest(unittest.TestCase):
    def test_item_id_without_match_is_none(self):
        self.assertIsNone(collector.item_id_from_url("//item.jd.com/abc.html"))
        self.assertIsNone(collector.item_id_from_url(None))

    def test_dash_quantity_is_none(self):
        self.assertIsNone(collector.integer_or_none("-"))
        self.assertIsNone(collector.integer_or_none(""))

    def test_invalid_quantity_fails(self):
        with self.assertRaisesRegex(ValueError, "quantity"):
            collector.integer_or_none("十二")


class WaitForTableTest(unittest.TestCase):
    """The listener must not treat a challenged page as a successful run."""

    class Fake:
        request_id = None
        finished = False
        status = None

        def __init__(self, text):
            self._text = text

        def pump(self, seconds):
            pass

        def text(self):
            return self._text

    def test_login_marker_raises(self):
        page = self.Fake("账号登录\n扫码登录")
        with self.assertRaisesRegex(RuntimeError, "login"):
            collector.ProductDetailPage.wait_for_table(page, timeout=2)

    def test_no_permission_raises(self):
        page = self.Fake("子账号无权限")
        with self.assertRaises(collector.NoPermissionError):
            collector.ProductDetailPage.wait_for_table(page, timeout=2)


class CallTimeoutTest(unittest.TestCase):
    """A command reply must be waited for with the command timeout, not the
    one-second polling timeout ``pump`` leaves behind."""

    class FakeSocket:
        def __init__(self):
            self.timeouts = []
            self.sent = []
            self._replies = [json.dumps({"id": 1, "result": {"ok": True}})]

        def settimeout(self, value):
            self.timeouts.append(value)

        def send(self, payload):
            self.sent.append(payload)

        def recv(self):
            return self._replies.pop(0)

    def test_call_restores_the_command_timeout_after_polling(self):
        page = object.__new__(collector.ProductDetailPage)
        page.ws = self.FakeSocket()
        page.message_id = 0
        page._record = lambda message: None
        page.ws.timeouts.clear()

        result = collector.ProductDetailPage.call(page, "Runtime.evaluate")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(page.ws.timeouts, [collector.COMMAND_TIMEOUT])
        self.assertGreater(collector.COMMAND_TIMEOUT, 1.0)


class SalesRowTest(unittest.TestCase):
    def test_spu_id_becomes_the_unique_code(self):
        collected = {"rows": [{"spu_id": "1001", "name": "药A", "quantity": 3, "amount": Decimal("9.00")}]}
        self.assertEqual(
            collector.sales_rows(collected),
            [{"code": "1001", "name": "药A", "quantity": 3, "amount": Decimal("9.00")}],
        )


class RunTest(unittest.TestCase):
    def test_run_writes_rows_and_reports_totals(self):
        import tempfile
        import pathlib
        from unittest import mock

        collected = {
            "store": "测试旗舰店",
            "dimension": "d7",
            "window": collector.window_for("d7", TODAY),
            "rows": [
                {"spu_id": "1", "name": "药A", "quantity": 10, "amount": Decimal("100.00")},
                {"spu_id": "2", "name": "药B", "quantity": 5, "amount": Decimal("50.50")},
            ],
            "summary": {"spu_id": "合计", "quantity": 15},
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "rows.json"
            args = type("Args", (), {"port": 9333, "dimension": "d7", "out": out,
                                     "persist": False, "config": None, "state_file": None})()
            with mock.patch.object(collector, "collect", return_value=collected):
                result = collector.run(args)
            self.assertEqual(result["product_rows"], 2)
            self.assertEqual(result["quantity_total"], 15)
            self.assertEqual(result["amount_total"], "150.50")
            self.assertEqual(result["state"], "success")
            written = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(written["platform"], "京东")
            self.assertEqual(written["store"], "测试旗舰店")
            self.assertEqual(len(written["rows"]), 2)

    def test_persist_without_config_is_rejected(self):
        args = type("Args", (), {"port": 9333, "dimension": "d7", "out": None,
                                 "persist": True, "config": None, "state_file": None})()
        with self.assertRaisesRegex(ValueError, "--persist requires --config"):
            collector.run(args)

    def test_second_run_on_the_same_day_is_skipped(self):
        import tempfile
        import pathlib
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            state_file = pathlib.Path(tmp) / "product-sales.json"
            args = type("Args", (), {"port": 9333, "dimension": "d7", "out": None,
                                     "persist": False, "config": None, "state_file": state_file})()
            today = dt.datetime.now(collector.TZ).date().isoformat()
            state_file.write_text(json.dumps({
                "date": today,
                "state": "success",
                "collector_version": collector.COLLECTOR_VERSION,
                "dimensions": ["d7"],
            }), encoding="utf-8")
            with mock.patch.object(collector, "collect") as collect_mock:
                result = collector.run(args)
            self.assertEqual(result["state"], "skipped")
            collect_mock.assert_not_called()

    def test_state_file_records_the_snapshot(self):
        import tempfile
        import pathlib
        from unittest import mock

        collected = {"store": "测试旗舰店", "dimension": "d7",
                     "window": collector.window_for("d7", TODAY),
                     "rows": [{"spu_id": "1", "name": "药A", "quantity": 1, "amount": Decimal("1.00")}],
                     "summary": {"spu_id": "合计", "quantity": 1}}
        with tempfile.TemporaryDirectory() as tmp:
            state_file = pathlib.Path(tmp) / "nested" / "product-sales.json"
            args = type("Args", (), {"port": 9333, "dimension": "d7", "out": None,
                                     "persist": False, "config": None, "state_file": state_file})()
            with mock.patch.object(collector, "collect", return_value=collected):
                collector.run(args)
            written = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(written["product_rows"], 1)
            self.assertEqual(written["dimensions"], ["d7"])
            self.assertEqual(written["collector_version"], collector.COLLECTOR_VERSION)

    def test_old_collector_version_does_not_suppress_a_rerun(self):
        import tempfile
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            state_file = pathlib.Path(tmp) / "product-sales.json"
            today = dt.datetime.now(collector.TZ).date()
            state_file.write_text(json.dumps({
                "date": today.isoformat(),
                "collector_version": collector.COLLECTOR_VERSION - 1,
                "dimensions": ["d7", "d30"],
            }), encoding="utf-8")
            self.assertFalse(collector.collected_today(state_file, today, ["d7", "d30"]))

    def test_changed_dimensions_do_not_suppress_a_rerun(self):
        import tempfile
        import pathlib

        with tempfile.TemporaryDirectory() as tmp:
            state_file = pathlib.Path(tmp) / "product-sales.json"
            today = dt.datetime.now(collector.TZ).date()
            state_file.write_text(json.dumps({
                "date": today.isoformat(),
                "collector_version": collector.COLLECTOR_VERSION,
                "dimensions": ["d7"],
            }), encoding="utf-8")
            self.assertFalse(collector.collected_today(state_file, today, ["d7", "d30"]))

    def test_two_dimensions_merge_into_one_row_per_spu(self):
        import tempfile
        import pathlib
        from unittest import mock

        d7 = {"store": "测试旗舰店", "dimension": "d7",
              "window": collector.window_for("d7", TODAY),
              "rows": [{"spu_id": "1", "name": "药A", "quantity": 10, "amount": Decimal("100.00")},
                       {"spu_id": "2", "name": "药B", "quantity": 5, "amount": Decimal("50.50")}],
              "summary": {"spu_id": "合计", "quantity": 15}}
        d30 = {"store": "测试旗舰店", "dimension": "d30",
               "window": collector.window_for("d30", TODAY),
               "rows": [{"spu_id": "1", "name": "药A", "quantity": 70, "amount": Decimal("700.00")},
                        {"spu_id": "3", "name": "药C", "quantity": 2, "amount": Decimal("20.00")}],
               "summary": {"spu_id": "合计", "quantity": 72}}
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "rows.json"
            args = type("Args", (), {"port": 9333, "dimension": "d7,d30", "out": out,
                                     "persist": False, "config": None, "state_file": None})()
            with mock.patch.object(collector, "collect", side_effect=[d7, d30]) as collect_mock, \
                    mock.patch.object(collector.time, "sleep") as sleep_mock:
                result = collector.run(args)
            self.assertEqual([call.args[1] for call in collect_mock.call_args_list], ["d7", "d30"])
            # One pause, before the second dimension — never before the first.
            sleep_mock.assert_called_once_with(collector.INTER_DIMENSION_DELAY)
            self.assertEqual(result["dimensions"], ["d7", "d30"])
            self.assertEqual(result["quantity_total"], 15)
            self.assertEqual(result["quantity_30_total"], 72)
            by_code = {row["code"]: row for row in json.loads(out.read_text(encoding="utf-8"))["rows"]}
            self.assertEqual(by_code["1"]["quantity"], 10)
            self.assertEqual(by_code["1"]["quantity_30"], 70)
            # A SPU present in only one window keeps the other window unset, which
            # esale_product writes as SQL NULL.
            self.assertEqual(by_code["2"]["quantity"], 5)
            self.assertNotIn("quantity_30", by_code["2"])
            self.assertEqual(by_code["3"]["quantity_30"], 2)
            self.assertNotIn("quantity", by_code["3"])


class MergeDimensionsTest(unittest.TestCase):
    def test_rows_are_folded_by_spu_id(self):
        merged = collector.merge_dimensions({
            "d7": [{"code": "1", "name": "药A", "quantity": 10, "amount": Decimal("100.00")}],
            "d30": [{"code": "1", "name": "药A", "quantity": 70, "amount": Decimal("700.00")}],
        })
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["quantity"], 10)
        self.assertEqual(merged[0]["quantity_30"], 70)
        self.assertEqual(merged[0]["amount"], Decimal("100.00"))

    def test_single_dimension_leaves_the_other_window_unset(self):
        merged = collector.merge_dimensions({
            "d30": [{"code": "1", "name": "药A", "quantity": 70, "amount": None}],
        })
        self.assertEqual(merged[0]["quantity_30"], 70)
        self.assertNotIn("quantity", merged[0])

    def test_ordering_prefers_the_seven_day_volume(self):
        merged = collector.merge_dimensions({
            "d7": [{"code": "1", "name": "药A", "quantity": 1, "amount": None},
                   {"code": "2", "name": "药B", "quantity": 9, "amount": None}],
            "d30": [],
        })
        self.assertEqual([row["code"] for row in merged], ["2", "1"])


class ParseDimensionsTest(unittest.TestCase):
    def test_single_dimension(self):
        self.assertEqual(collector.parse_dimensions("d7"), ["d7"])

    def test_multiple_dimensions_preserve_order_and_drop_duplicates(self):
        self.assertEqual(collector.parse_dimensions("d30,d7,d30"), ["d30", "d7"])

    def test_unknown_dimension_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            collector.parse_dimensions("d7,d90")

    def test_empty_value_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            collector.parse_dimensions(" , ")


if __name__ == "__main__":
    unittest.main()
