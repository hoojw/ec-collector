import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import jd_store_sku_web_collect
from jd_store_sku_web_collect import (
    INDEX_VERSION,
    LIST_ROWS_JS,
    build_rows,
    has_withheld_product_rows,
    index_path_for,
    load_index,
    needs_refresh,
    parse_product_row,
    parse_sku_rows,
    row_from_index,
    save_index,
    seed_index_from_database,
    sku_entry,
)


PRODUCT_ROW = """[格华止]盐酸二甲双胍片 0.5g*20片 5盒装
信息分95
商品编码：100012345678
创建时间：2026-09-10 09:12:33
修改时间：2026-09-24 10:12:33"""

SKU_ROW = """0.5g*20片*5盒
SKU编码：10219573867403
商家SKU编码：6934567890123*5
¥26.50"""


class WithheldProductRowsTest(unittest.TestCase):
    def test_positive_tab_count_with_empty_table_is_withheld(self):
        self.assertTrue(has_withheld_product_rows("售卖中 (91)\n共0条", 0))

    def test_genuinely_empty_store_is_not_withheld(self):
        self.assertFalse(has_withheld_product_rows("售卖中 (0)\n共0条", 0))

    def test_nonempty_table_is_not_withheld(self):
        self.assertFalse(has_withheld_product_rows("售卖中（91）\n共91条", 91))


WITHHELD_STATE = {"text": "售卖中 (91)\n共0条", "total": "0"}
RECOVERED_STATE = {"text": "售卖中 (91)\n共91条", "total": "91"}


class WithheldRiskStateRecoveryTest(unittest.TestCase):
    """Code 601 is carried by the cached sgm state, so the collector drops it and re-reads."""

    def test_recovery_clears_the_state_reloads_and_returns_the_list(self):
        page = mock.MagicMock()
        page.evaluate.return_value = 9
        with mock.patch.object(jd_store_sku_web_collect, "_await_list", return_value=RECOVERED_STATE) as awaited:
            result = jd_store_sku_web_collect._clear_withheld_risk_state(page)
        self.assertEqual(result, RECOVERED_STATE)
        page.evaluate.assert_called_once_with(jd_store_sku_web_collect.RISK_STATE_JS)
        page.call.assert_any_call("Page.enable")
        page.call.assert_any_call("Page.reload", {"ignoreCache": False})
        awaited.assert_called_once_with(page)

    def test_only_the_sgm_state_is_touched(self):
        script = jd_store_sku_web_collect.RISK_STATE_JS
        self.assertIn("'WQ_'", script)
        self.assertIn("__sgm__", script)
        # The login session and every other device identifier measured on
        # 2026-09-28 must survive: clearing them alone did not lift code 601.
        for untouched in ("cookie", "shshshfp", "3AB9D23F7A4B3", "deviceId", "JDst_", "__we_m_"):
            self.assertNotIn(untouched, script)

    def test_a_withheld_listing_is_re_read_before_the_snapshot_is_abandoned(self):
        page = mock.MagicMock()
        page.evaluate.return_value = None
        with mock.patch.object(jd_store_sku_web_collect, "Page", return_value=page), \
                mock.patch.object(jd_store_sku_web_collect, "_await_list", return_value=WITHHELD_STATE), \
                mock.patch.object(jd_store_sku_web_collect, "_reselect_on_sale_tab", return_value=WITHHELD_STATE), \
                mock.patch.object(jd_store_sku_web_collect, "_clear_withheld_risk_state",
                                  return_value=RECOVERED_STATE) as recovery, \
                mock.patch.object(jd_store_sku_web_collect, "_ensure_page_size",
                                  return_value={"pageSize": "100"}), \
                mock.patch.object(jd_store_sku_web_collect, "_await_rendered_rows"), \
                mock.patch.object(jd_store_sku_web_collect, "_rewind_to_first_page"), \
                mock.patch.object(jd_store_sku_web_collect, "_read_pages", return_value=[]), \
                mock.patch.object(jd_store_sku_web_collect, "_expand_products", return_value={}):
            expected, products = jd_store_sku_web_collect.collect_page_rows(9533, click_delay_ms=3000)
        self.assertEqual(expected, 91)
        self.assertEqual(products, [])
        recovery.assert_called_once_with(page)

    def test_a_still_withheld_listing_still_preserves_the_previous_snapshot(self):
        page = mock.MagicMock()
        page.evaluate.return_value = None
        with mock.patch.object(jd_store_sku_web_collect, "Page", return_value=page), \
                mock.patch.object(jd_store_sku_web_collect, "_await_list", return_value=WITHHELD_STATE), \
                mock.patch.object(jd_store_sku_web_collect, "_reselect_on_sale_tab", return_value=WITHHELD_STATE), \
                mock.patch.object(jd_store_sku_web_collect, "_clear_withheld_risk_state",
                                  return_value=WITHHELD_STATE):
            with self.assertRaises(jd_store_sku_web_collect.SourceUnavailable):
                jd_store_sku_web_collect.collect_page_rows(9533, click_delay_ms=3000)


class RefreshPolicyTest(unittest.TestCase):
    """Only a genuinely moved 修改时间 may cost an expansion request."""

    def test_an_unknown_product_is_expanded(self):
        self.assertTrue(needs_refresh("changed", None, "2026-09-24 10:12:33"))

    def test_an_unchanged_product_is_not_expanded(self):
        entry = {"修改时间": "2026-09-24 10:12:33", "SKU": {"1": {}}}
        self.assertFalse(needs_refresh("changed", entry, "2026-09-24 10:12:33"))

    def test_a_moved_timestamp_forces_an_expansion(self):
        entry = {"修改时间": "2026-09-20 08:00:00", "SKU": {"1": {}}}
        self.assertTrue(needs_refresh("changed", entry, "2026-09-24 10:12:33"))

    def test_a_missing_timestamp_is_treated_as_unchanged(self):
        # The page withheld the column; re-expanding on uncertainty would bring
        # back the request burst this collector exists to remove.
        entry = {"修改时间": "2026-09-20 08:00:00", "SKU": {"1": {}}}
        self.assertFalse(needs_refresh("changed", entry, None))

    def test_all_rebuilds_regardless_of_the_index(self):
        entry = {"修改时间": "2026-09-24 10:12:33", "SKU": {"1": {}}}
        self.assertTrue(needs_refresh("all", entry, "2026-09-24 10:12:33"))
        self.assertTrue(needs_refresh("all", None, None))

    def test_none_never_expands(self):
        self.assertFalse(needs_refresh("none", None, "2026-09-24 10:12:33"))
        self.assertFalse(needs_refresh("none", {"修改时间": "old"}, "2026-09-24 10:12:33"))

    def test_a_seeded_entry_only_records_its_baseline(self):
        # Seeded entries carry no timestamp yet.  Expanding them would rebuild
        # the whole store on the first run, which is the burst being avoided.
        seeded = {"修改时间": None, "SKU": {"1": {}}}
        self.assertFalse(needs_refresh("changed", seeded, "2026-09-24 10:12:33"))


class IndexIoTest(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.path = self.directory / "jd-sku-index.json"

    def test_a_missing_index_starts_empty(self):
        self.assertEqual(load_index(self.path), {"version": INDEX_VERSION, "products": {}})

    def test_an_unreadable_index_starts_empty_instead_of_failing_the_run(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_index(self.path)["products"], {})

    def test_a_foreign_version_is_discarded(self):
        self.path.write_text(json.dumps({"version": 99, "products": {"1": {}}}), encoding="utf-8")
        self.assertEqual(load_index(self.path)["products"], {})

    def test_round_trip_preserves_products(self):
        save_index(self.path, {"version": INDEX_VERSION, "products": {"1001": {"修改时间": "x"}}})
        self.assertEqual(load_index(self.path)["products"]["1001"]["修改时间"], "x")

    def test_save_leaves_no_temporary_file_behind(self):
        save_index(self.path, {"version": INDEX_VERSION, "products": {}})
        self.assertEqual(sorted(p.name for p in self.directory.iterdir()), ["jd-sku-index.json"])

    def test_an_explicit_override_wins_over_the_config_directory(self):
        override = self.directory / "elsewhere" / "index.json"
        self.assertEqual(index_path_for(Path("/opt/subsidy/stores/jd-1/config.json"), override), override)

    def test_the_default_sits_beside_the_store_config(self):
        self.assertEqual(
            index_path_for(Path("/opt/subsidy/stores/jd-1/config.json")),
            Path("/opt/subsidy/stores/jd-1/jd-sku-index.json"),
        )


class SkuIndexRoundTripTest(unittest.TestCase):
    def setUp(self):
        self.rows = parse_sku_rows([SKU_ROW], "100012345678", "[格华止]盐酸二甲双胍片", "格华止")

    def test_a_parsed_row_is_rebuilt_identically_from_its_index_entry(self):
        self.assertEqual(len(self.rows), 1)
        entry = sku_entry(self.rows[0])
        rebuilt = row_from_index(self.rows[0][0], entry, "100012345678", "[格华止]盐酸二甲双胍片", "格华止")
        self.assertEqual(rebuilt, self.rows[0])

    def test_the_index_entry_carries_the_normalized_upc_fields(self):
        entry = sku_entry(self.rows[0])
        self.assertEqual(entry["基础UPC"], "6934567890123")
        self.assertEqual(entry["组合数量"], 5)
        self.assertEqual(entry["京东价"], "26.50")


class ProductRowParseTest(unittest.TestCase):
    def test_it_extracts_code_name_and_brand(self):
        self.assertEqual(
            parse_product_row(PRODUCT_ROW),
            ("100012345678", "[格华止]盐酸二甲双胍片 0.5g*20片 5盒装", "格华止"),
        )

    def test_it_skips_the_information_score_line_when_naming_the_product(self):
        row = "阿莫西林胶囊\n信息分88\n商品编码：1002"
        self.assertEqual(parse_product_row(row)[1], "阿莫西林胶囊")

    def test_a_row_without_a_product_code_is_not_a_product(self):
        self.assertIsNone(parse_product_row("SKU编码：10219573867403"))


class SkuRowParseTest(unittest.TestCase):
    def test_it_parses_sku_code_attribute_and_price(self):
        row = parse_sku_rows([SKU_ROW], "100012345678", "商品", "品牌")[0]
        self.assertEqual(row[0], "10219573867403")
        self.assertEqual(row[3], "6934567890123*5")
        self.assertEqual(row[5], "0.5g*20片*5盒")
        self.assertEqual(row[7], "26.50")

    def test_a_sku_without_a_price_is_a_hard_failure(self):
        with self.assertRaises(ValueError):
            parse_sku_rows([SKU_ROW.replace("¥26.50", "")], "100012345678", "商品", "品牌")

    def test_a_sku_without_a_sales_attribute_is_a_hard_failure(self):
        without_attribute = "SKU编码：10219573867403\n商家SKU编码：6934567890123*5\n¥26.50"
        with self.assertRaises(ValueError):
            parse_sku_rows([without_attribute], "100012345678", "商品", "品牌")

    def test_rows_without_a_sku_code_are_ignored(self):
        self.assertEqual(parse_sku_rows(["展开中…"], "100012345678", "商品", "品牌"), [])


class BuildRowsTest(unittest.TestCase):
    """The merge that keeps the daily snapshot complete without re-reading it."""

    OLD_STAMP = "2026-09-10 09:00:00"
    NEW_STAMP = "2026-09-24 10:12:33"

    def setUp(self):
        fresh = parse_sku_rows([SKU_ROW], "100012345678", "新名称", "格华止")
        self.index = {
            "version": INDEX_VERSION,
            "products": {
                "100012345678": {
                    "商品名称": "旧名称",
                    "品牌": "格华止",
                    "修改时间": self.OLD_STAMP,
                    "SKU": {fresh[0][0]: sku_entry(fresh[0])},
                },
            },
        }

    def _product(self, **overrides):
        product = {"code": "100012345678", "name": "新名称", "brand": "格华止",
                   "modified": self.NEW_STAMP, "text": "", "sku_texts": None}
        product.update(overrides)
        return product

    def test_a_refreshed_product_uses_the_freshly_read_detail(self):
        rows, summary = build_rows([self._product(sku_texts=[SKU_ROW])], self.index)

        self.assertEqual(summary["refreshed_products"], 1)
        self.assertEqual(summary["unchanged_products"], 0)
        self.assertEqual(rows[0][2], "新名称")
        self.assertEqual(len(rows), 1)

    def test_an_unchanged_product_reuses_its_indexed_detail(self):
        rows, summary = build_rows([self._product(modified=self.OLD_STAMP, sku_texts=None)], self.index)

        self.assertEqual(summary["refreshed_products"], 0)
        self.assertEqual(summary["unchanged_products"], 1)
        # The product's own name still comes from the current listing.
        self.assertEqual(rows[0][2], "新名称")
        self.assertEqual(rows[0][0], "10219573867403")

    def test_an_unknown_product_without_detail_is_reported_not_dropped(self):
        rows, summary = build_rows(
            [self._product(code="100099999999", name="新品", brand="", sku_texts=None)], self.index
        )

        self.assertEqual(rows, [])
        self.assertEqual(summary["missing_products"], ["100099999999"])

    def test_a_failed_expansion_reuses_the_detail_but_holds_the_old_timestamp(self):
        # Asked for but empty: the standing detail survives, and the indexed
        # timestamp must NOT advance, or the next run would stop retrying.
        rows, summary = build_rows([self._product(sku_texts=[])], self.index)

        self.assertEqual(summary["failed_products"], ["100012345678"])
        self.assertEqual(summary["refreshed_products"], 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(summary["entries"]["100012345678"]["修改时间"], self.OLD_STAMP)

    def test_the_carried_forward_index_tracks_the_new_timestamp(self):
        _, summary = build_rows([self._product(sku_texts=[SKU_ROW])], self.index)
        self.assertEqual(summary["entries"]["100012345678"]["修改时间"], self.NEW_STAMP)

    def test_a_dropped_product_leaves_the_carried_index(self):
        # The listing no longer contains it, so it must not survive in the index.
        _, summary = build_rows([self._product(sku_texts=[SKU_ROW])], self.index)
        self.assertEqual(sorted(summary["entries"]), ["100012345678"])


class FakeCursor:
    """Stand-in for a pymysql DictCursor; records what was asked of it."""

    def __init__(self, latest, rows):
        self.latest = latest
        self.rows = rows
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return {"latest": self.latest}

    def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self, *args, **kwargs):
        return self._cursor

    def close(self):
        self.closed = True


def _seed_row(**overrides):
    row = {
        "SKUID": "10219573867403", "商品编码": "100012345678", "商品名称": "商品",
        "商家SKU原值": "6934567890123*5", "基础UPC": "6934567890123",
        "组合数量": 5, "标注组合数量": 5, "包装单位": "盒",
        "销售属性": "0.5g*20片*5盒", "品牌": "格华止", "京东价": "26.50",
        "解析状态": "ok", "解析说明": "",
    }
    row.update(overrides)
    return row


class SeedFromDatabaseTest(unittest.TestCase):
    """Filling the index from the standing snapshot, so the first run is light."""

    CONFIG = {"mysql": {"host": "h", "port": 3307, "user": "u", "password": "p", "database": "d"}}

    def _seed(self, latest, rows):
        cursor = FakeCursor(latest, rows)
        connection = FakeConnection(cursor)
        with mock.patch.object(jd_store_sku_web_collect.pymysql, "connect", return_value=connection):
            result = seed_index_from_database(self.CONFIG)
        self.assertTrue(connection.closed)
        return result, cursor

    def test_it_reads_the_newest_snapshot_date(self):
        result, cursor = self._seed(dt.date(2026, 9, 14), [_seed_row()])

        self.assertEqual(result["snapshot_date"], "2026-09-14")
        self.assertIn("MAX(`记录日期`)", cursor.queries[0][0])
        # The second query is scoped to that date, never the whole table.
        self.assertEqual(cursor.queries[1][1], (dt.date(2026, 9, 14),))

    def test_rows_are_grouped_into_products_with_no_timestamp_yet(self):
        result, _ = self._seed(dt.date(2026, 9, 14), [
            _seed_row(),
            _seed_row(SKUID="10219573867404", 销售属性="0.5g*20片*10盒", 京东价="52.00"),
        ])

        self.assertEqual(sorted(result["products"]), ["100012345678"])
        entry = result["products"]["100012345678"]
        self.assertIsNone(entry["修改时间"])
        self.assertEqual(sorted(entry["SKU"]), ["10219573867403", "10219573867404"])
        self.assertEqual(entry["SKU"]["10219573867404"]["京东价"], "52.00")

    def test_an_empty_snapshot_yields_nothing_to_seed(self):
        result, _ = self._seed(None, [])

        self.assertEqual(result["products"], {})
        self.assertIsNone(result["snapshot_date"])

    def test_rows_without_a_sku_or_product_code_are_skipped(self):
        result, _ = self._seed(dt.date(2026, 9, 14), [
            _seed_row(SKUID=""),
            _seed_row(SKUID="10219573867405", 商品编码=""),
        ])

        self.assertEqual(result["products"], {})

    def test_the_seeded_entry_round_trips_back_into_a_persistable_row(self):
        result, _ = self._seed(dt.date(2026, 9, 14), [_seed_row()])
        entry = result["products"]["100012345678"]["SKU"]["10219573867403"]
        row = row_from_index("10219573867403", entry, "100012345678", "商品", "格华止")

        self.assertEqual(row[0], "10219573867403")
        self.assertEqual(row[3], "6934567890123*5")
        self.assertEqual(row[4]["base_upc"], "6934567890123")
        self.assertEqual(row[7], "26.50")


class ListRowsExpressionTest(unittest.TestCase):
    def test_the_row_expression_targets_the_modification_column(self):
        self.assertIn("修改时间", LIST_ROWS_JS)
        # The column must be located through the header, not guessed by position.
        self.assertIn("querySelectorAll('th')", LIST_ROWS_JS)
        # A timestamp fallback exists for layouts whose cells do not line up.
        self.assertIn("match(/\\d{4}-\\d{2}-\\d{2}", LIST_ROWS_JS)

    def test_the_row_expression_never_clicks_anything(self):
        self.assertNotIn(".click()", LIST_ROWS_JS)


if __name__ == "__main__":
    unittest.main()
