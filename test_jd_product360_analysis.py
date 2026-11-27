import unittest
from unittest import mock
from decimal import Decimal
import datetime as dt

import jd_product360_analysis as product360


OVERVIEW = """商品概况
动销
动销SPU数
37
环比
-2.63%
上架
上架SPU数
92
环比
+6.98%
动销SPU数
37
环比
-2.63%
SPU动销率
40.22%
环比
-8.98%
成交金额
￥946,159.83
环比
-20.42%
"""


DETAIL = """数据明细
1
[穆峰达]替尔泊肽注射液 2.4ml:30mg 1盒装
SPU: 1001
¥339,862.75
2
普通商品
SPU: 1002
¥20.00
"""


class Product360ParserTest(unittest.TestCase):
    def test_parse_overview_uses_product360_completed_period_values(self):
        value = product360.parse_overview(OVERVIEW)
        self.assertEqual(value["moving_spu_count"], Decimal("37"))
        self.assertEqual(value["listed_spu_count"], Decimal("92"))
        self.assertEqual(value["turnover_rate"], Decimal("0.4022"))
        self.assertEqual(value["transaction_amount"], Decimal("946159.83"))
        self.assertEqual(value["transaction_amount_ring_ratio"], Decimal("-0.2042"))

    def test_parse_detail_rows_keeps_tirzepatide_name_and_amount(self):
        value = product360.parse_detail_rows(DETAIL)
        self.assertEqual(len(value), 2)
        self.assertEqual(value[0]["amount"], Decimal("339862.75"))
        self.assertIn("替尔泊肽", value[0]["name"])

    def test_incomplete_platform_card_remains_unavailable(self):
        value = product360.parse_overview("""动销SPU数\n1\n环比\n-66.67%\n上架SPU数\n0\n环比\n--""")
        self.assertEqual(value["moving_spu_count"], Decimal("1"))
        self.assertEqual(value["listed_spu_count"], Decimal("0"))
        self.assertIsNone(value["turnover_rate"])
        self.assertIsNone(value["transaction_amount"])

    def test_explicit_empty_overview_is_a_completed_result(self):
        text = "商品概况\n当前：2026-09-14\n成交金额\n--\n环比\n--\n暂无数据"
        self.assertTrue(product360.overview_result_rendered(text))
        value = product360.parse_overview(text)
        self.assertIsNone(value["transaction_amount"])

    def test_loading_placeholder_without_empty_copy_is_not_complete(self):
        self.assertFalse(product360.overview_result_rendered("商品概况\n成交金额\n--\n环比\n--"))

    def test_collect_retries_one_transient_cdp_disconnect(self):
        expected = {"daily": {}, "near_30": {}}
        disconnected = product360.websocket.WebSocketConnectionClosedException("closed")
        with mock.patch.object(product360, "_collect_once", side_effect=[disconnected, expected]) as collect_once, \
             mock.patch.object(product360.time, "sleep") as sleep:
            self.assertEqual(product360.collect(9433), expected)
        self.assertEqual(collect_once.call_count, 2)
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()


class KeywordRowParserTest(unittest.TestCase):
    """The current Product360 renderer pads cells with tabs."""

    def test_parse_keyword_rows_accepts_tab_prefixed_amount(self):
        text = (
            "商品明细\n"
            "当前：2026-08-09 ~ 09-07\n"
            "数据明细\n"
            "1\n"
            "\t[穆峰达]替尔泊肽注射液 2.4ml:30mg 1盒装 医疗冷链\n"
            "SPU: 10034841422620\n"
            "\t\n"
            "¥343,402.90\n"
            "¥128,546.70\n"
            "+167.14%\n"
            "2\n"
            "\t普通止咳药\n"
            "SPU: 9999\n"
            "\t¥20.00\n"
        )
        rows = product360.parse_keyword_rows(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], Decimal("343402.90"))
        self.assertIn("替尔泊肽", rows[0]["name"])

    def test_parse_keyword_rows_skips_promo_copy_between_spu_and_amount(self):
        text = (
            "数据明细\n"
            "3\n"
            "[穆峰达]替尔泊肽注射液 2.4ml:40mg 1盒装\n"
            "SPU: 10035892443729\n"
            "有机会0出资享消费补贴券 立即投广\n"
            "\n"
            "¥3,180.00\n"
            "¥1,060.00\n"
            "3\n"
            "1\n"
        )
        rows = product360.parse_keyword_rows(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["amount"], Decimal("3180.00"))
        self.assertIn("替尔泊肽", rows[0]["name"])

    def test_parse_keyword_rows_no_tirzepatide_raises(self):
        text = "1\n\t普通商品\nSPU: 1002\n\t¥20.00\n"
        with self.assertRaises(RuntimeError):
            product360.parse_keyword_rows(text)

    def test_has_spu_rows_detects_row_blocks(self):
        self.assertTrue(product360.has_spu_rows("SPU: 1002\n¥1.00"))
        self.assertFalse(product360.has_spu_rows("SPU动销率\n40.22%"))


class NoPermissionDetectionTest(unittest.TestCase):
    """Product360 permission-denial must be detected before generic login copy."""

    def _page(self, url, text):
        page = mock.Mock()
        page.state.return_value = {"url": url, "text": text}
        return page

    def test_noauth_url_wins_over_login_markers(self):
        page = self._page(
            "https://jdsz.jd.com/szweb/view/noAuth/noAuth.html",
            "子账号无权限 账号登录",
        )
        with self.assertRaises(product360.NoPermissionError):
            product360.wait_for(page, lambda state: False, description="permission probe")

    def test_noauth_body_text_raises_no_permission(self):
        page = self._page(
            "https://jdsz.jd.com/szweb/view/product/product360.html",
            "提示：当前子账号暂无商智权限",
        )
        with self.assertRaises(product360.NoPermissionError):
            product360.wait_for(page, lambda state: False, description="permission probe")

    def test_login_marker_alone_stays_login_error(self):
        page = self._page("https://passport.jd.com/uc/login", "账号登录")
        with self.assertRaises(RuntimeError) as caught:
            product360.wait_for(page, lambda state: False, description="login probe")
        self.assertNotIsInstance(caught.exception, product360.NoPermissionError)

    def test_predicate_met_returns_state(self):
        page = self._page(
            "https://jdsz.jd.com/szweb/view/product/product360.html",
            "数据明细 商品信息",
        )
        state = product360.wait_for(page, lambda s: "数据明细" in s["text"], description="detail")
        self.assertIn("数据明细", state["text"])


def _near_30_marker() -> str:
    end = dt.datetime.now(product360.TZ).date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=29)
    return f"当前：{start:%Y-%m-%d} ~ {end:%m-%d}"


DETAIL_PAGE_TEXT = "\n".join(
    [
        "商品明细",
        _near_30_marker(),
        "实时",
        "昨天",
        "近7天",
        "近30天",
        "查询",
        "重置",
        "商品信息",
        "成交金额",
        "成交商品件数",
        "成交转化率",
        "1",
        "[穆峰达]替尔泊肽注射液 2.4ml:30mg 1盒装",
        "SPU: 10034841422620",
        "¥221,344.66",
        "¥248,507.60",
        "2",
        "[穆峰达]替尔泊肽注射液 2.4ml:20mg 1盒装 医疗冷链",
        "SPU: 10034742379484",
        "¥42,712.45",
        "¥140,976.32",
    ]
)


class TirzepatideScopeGuardTest(unittest.TestCase):
    """A detail-vs-overview scope mismatch must not kill the whole stage."""

    def _collect(self, tirzepatide, total):
        page = mock.Mock()
        with mock.patch.object(product360, "Page", return_value=page), \
             mock.patch.object(product360, "wait_for", return_value={"text": "", "url": ""}), \
             mock.patch.object(product360, "ensure_all_channel"), \
             mock.patch.object(product360, "select_period", return_value={"text": ""}), \
             mock.patch.object(product360, "parse_overview", side_effect=[
                 {"transaction_amount": Decimal("100")},
                 {"transaction_amount": Decimal(total)},
             ]), \
             mock.patch.object(product360, "collect_tirzepatide_amount", return_value=tirzepatide), \
             mock.patch.object(product360.sys, "stderr") as stderr:
            metrics = product360._collect_once(9433)
        page.close.assert_called_once_with()
        return metrics, stderr

    def test_detail_amount_above_the_overview_total_leaves_the_ratio_unknown(self):
        metrics, stderr = self._collect(Decimal("16006.00"), "4094.00")
        self.assertIsNone(metrics["near_30"]["tirzepatide_amount"])
        self.assertIn("exceeds near-30-day total", stderr.write.call_args_list[0][0][0])

    def test_detail_amount_within_the_total_is_kept(self):
        metrics, _ = self._collect(Decimal("2000.00"), "4094.00")
        self.assertEqual(metrics["near_30"]["tirzepatide_amount"], Decimal("2000.00"))


class DetailTableRenderedTest(unittest.TestCase):
    def test_header_pair_marks_the_detail_table_ready(self):
        self.assertTrue(product360.detail_table_rendered(DETAIL_PAGE_TEXT))
        # The overview cards also contain 成交金额; the pair must be required.
        self.assertFalse(product360.detail_table_rendered("商品概况\n成交金额\n￥1.00\n环比\n+1%"))
        self.assertFalse(product360.detail_table_rendered("商品明细\n近30天\n查询"))


class DetailPagerTest(unittest.TestCase):
    def test_click_detail_next_targets_the_following_page_item(self):
        page = mock.Mock()
        page.evaluate.return_value = True
        self.assertTrue(product360.Page.click_detail_next(page, 3))
        expression = page.evaluate.call_args[0][0]
        self.assertIn("点击第4页", expression)
        self.assertIn("data-event-content", expression)

    def test_click_detail_next_reports_absence_of_next_page(self):
        page = mock.Mock()
        page.evaluate.return_value = False
        self.assertFalse(product360.Page.click_detail_next(page, 5))


class KeywordFilterInputTest(unittest.TestCase):
    def test_type_keyword_filter_uses_the_detail_page_input(self):
        page = mock.Mock()
        page.evaluate.return_value = True
        product360.Page.type_keyword_filter(page, "替尔泊肽")
        expression = page.evaluate.call_args[0][0]
        self.assertIn("input.jd-input", expression)
        self.assertIn("关键词", expression)
        page.human.type_text.assert_called_once_with("替尔泊肽")
        page.human.press_key.assert_called_once_with("Enter", "Enter")

    def test_missing_keyword_input_raises(self):
        page = mock.Mock()
        page.evaluate.return_value = False
        with self.assertRaises(RuntimeError):
            product360.Page.type_keyword_filter(page, "替尔泊肽")


class _FakeClock:
    """Deterministic clock so wait_for retries never sleep in real time."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class DetailPageCollectionTest(unittest.TestCase):
    """collect_tirzepatide_amount must run on the standalone 商品明细 page."""

    def setUp(self):
        page_patcher = mock.patch.object(product360, "Page")
        self.addCleanup(page_patcher.stop)
        self.page_cls = page_patcher.start()
        self.page = self.page_cls.return_value
        self.page.state.return_value = {"url": product360.PRODUCT_DETAIL_URL, "text": DETAIL_PAGE_TEXT}
        self.page.evaluate.return_value = True
        self.page.click_detail_next.return_value = False
        clock = _FakeClock()
        fake_time = mock.Mock()
        fake_time.monotonic.side_effect = clock.monotonic
        fake_time.sleep.side_effect = clock.sleep
        time_patcher = mock.patch.object(product360, "time", fake_time)
        self.addCleanup(time_patcher.stop)
        time_patcher.start()

    def test_opens_the_detail_page_in_its_own_tab(self):
        product360.collect_tirzepatide_amount(9433)
        args, kwargs = self.page_cls.call_args
        self.assertEqual(args[0], 9433)
        self.assertEqual(kwargs.get("url"), product360.PRODUCT_DETAIL_URL)
        self.page.close.assert_called_once_with()

    def test_sums_keyword_filtered_transaction_amounts(self):
        self.assertEqual(product360.collect_tirzepatide_amount(9433), Decimal("264057.11"))

    def test_empty_keyword_result_is_zero(self):
        self.page.state.return_value = {
            "url": product360.PRODUCT_DETAIL_URL,
            "text": "\n".join(["商品明细", _near_30_marker(), "近30天", "查询", "商品信息", "成交商品件数", "暂无数据"]),
        }
        self.assertEqual(product360.collect_tirzepatide_amount(9433), Decimal("0"))

    def test_detail_failure_still_closes_the_tab(self):
        self.page.state.return_value = {"url": product360.PRODUCT_DETAIL_URL, "text": "商品概况"}
        with self.assertRaises(RuntimeError):
            product360.collect_tirzepatide_amount(9433)
        self.page.close.assert_called_once_with()
