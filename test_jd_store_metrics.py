import unittest
from decimal import Decimal

from jd_esale_after_sales_metrics import parse_dashboard, parse_list
from jd_store_deposit_metrics import parse_pending_deposit
from jd_store_quality_metrics import (
    LOW_SCORE_MAX,
    LOW_SCORE_MIN,
    ON_SALE_TAB_PREFIX,
    parse_scores,
    score_range_expression,
    select_on_sale_expression,
)


class JdStoreMetricsTest(unittest.TestCase):
    def test_after_sales_auto_review_supports_day_hour_minute_duration(self):
        self.assertEqual(parse_list("待审核(1)\n1天19时23分28秒后自动审核"), (1, 0))
        self.assertEqual(parse_list("待审核（1）\n11时59分后自动审核"), (1, 1))
        self.assertEqual(parse_list("待审核 0"), (0, 0))
        self.assertEqual(parse_list("待审核\n2\n13时后自动审核\n11时后自动审核"), (2, 1))

    def test_missing_after_sales_duration_is_source_empty_not_a_batch_failure(self):
        self.assertIsNone(parse_dashboard("售后服务\n暂无相关数据"))

    def test_pending_deposit_reads_value_adjacent_to_label(self):
        self.assertEqual(
            parse_pending_deposit("待缴保证金\n¥0.00\n保证金余额\n¥50000.00"),
            Decimal("0.00"),
        )

    def test_information_score_uses_commodity_inspection_sku_rows(self):
        scores = parse_scores(["""测试商品
商品编码：1001
SKU编码：2001
0
89
良好
另一商品
商品编码：1002
SKU编码：2002
1
90
良好"""])
        self.assertEqual(scores, {"2001": 89.0, "2002": 90.0})

    def test_information_score_skips_a_row_that_has_no_displayed_score(self):
        self.assertEqual(parse_scores(["未评分商品\n商品编码：1001\nSKU编码：2001\n-"]), {})

    def test_low_score_definition_is_the_inclusive_0_to_90_range(self):
        self.assertEqual((LOW_SCORE_MIN, LOW_SCORE_MAX), (0, 90))

    def test_score_range_expression_targets_both_placeholders(self):
        expression = score_range_expression(LOW_SCORE_MIN, LOW_SCORE_MAX)
        self.assertIn("开始分数", expression)
        self.assertIn("结束分数", expression)
        self.assertIn('"0"', expression)
        self.assertIn('"90"', expression)
        # Framework-controlled inputs ignore a plain .value assignment.
        self.assertIn("getOwnPropertyDescriptor", expression)
        self.assertIn("new Event('input'", expression)

    def test_score_range_expression_has_no_click_statement(self):
        # Setting the filter must never submit anything on its own.
        self.assertNotIn(".click()", score_range_expression(0, 90))

    def test_on_sale_tab_expression_selects_the_on_sale_scope(self):
        # The card opens on 「30天有销量商品」, which also counts off-sale products.
        expression = select_on_sale_expression()
        self.assertIn(ON_SALE_TAB_PREFIX, expression)
        self.assertEqual(ON_SALE_TAB_PREFIX, "售卖中商品")
        self.assertIn("score-card__tab", expression)


if __name__ == "__main__":
    unittest.main()
