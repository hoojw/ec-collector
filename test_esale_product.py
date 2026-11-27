import datetime as dt
import unittest
from decimal import Decimal

import esale_product


def config(schema=None, **store):
    payload = {"mysql": {"host": "127.0.0.1", "port": 3307, "user": "u", "password": "p",
                         "database": "store_db", "charset": "utf8mb4"},
               "store": {"client": "保民", "platform": "京东", **store}}
    if schema:
        payload["workbench"] = {"schema": schema, "table": "esale_system"}
    return payload


class FakeCursor:
    def __init__(self, column_exists: bool = True):
        self.statements = []
        self.batches = []
        self.column_exists = column_exists
        self._result = None
        # ``prune_stale`` issues several COUNT(*) queries and reads each answer;
        # a test that cares sets ``counts`` to the answers in order.
        self.counts = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        self.rowcount = 0
        if "information_schema.COLUMNS" in sql:
            self._result = (1 if self.column_exists else 0,)
        elif "COUNT(*)" in sql and self.counts:
            self._result = (self.counts.pop(0),)

    def executemany(self, sql, rows):
        self.batches.append((sql, list(rows)))

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, column_exists: bool = True):
        self.cursor_obj = FakeCursor(column_exists=column_exists)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class SettingsTest(unittest.TestCase):
    def test_store_section_supplies_client_and_platform(self):
        self.assertEqual(esale_product.settings(config()), ("保民", "京东"))

    def test_meituan_store_section_is_accepted(self):
        payload = {"mysql": {}, "meituan_store": {"client": "津南南无门诊", "platform": "美团"}}
        self.assertEqual(esale_product.settings(payload), ("津南南无门诊", "美团"))

    def test_missing_identity_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "client and platform"):
            esale_product.settings({"store": {"client": "保民"}})

    def test_schema_defaults_to_adjust_price(self):
        self.assertEqual(esale_product.schema_of(config()), "adjust_price")

    def test_configured_schema_wins(self):
        self.assertEqual(esale_product.schema_of(config(schema="adjust_price")), "adjust_price")


class EnsureTableTest(unittest.TestCase):
    def test_table_is_created_if_absent_with_the_esale_conventions(self):
        connection = FakeConnection()
        target = esale_product.ensure_table(connection, schema="adjust_price")
        self.assertEqual(target, "`adjust_price`.`esale_product`")
        sql, _ = connection.cursor_obj.statements[0]
        self.assertIn("CREATE TABLE IF NOT EXISTS `adjust_price`.`esale_product`", sql)
        self.assertIn("PRIMARY KEY (`客户`,`平台`,`唯一编码`)", sql)
        self.assertIn("utf8mb4", sql)
        for column in ("客户", "平台", "商品", "唯一编码", "价格", "销量", "库存", "销售额", "30天销量", "更新日期"):
            self.assertIn(f"`{column}`", sql)

    def test_existing_table_without_newer_columns_is_upgraded(self):
        connection = FakeConnection(column_exists=False)
        esale_product.ensure_table(connection, schema="adjust_price")
        statements = [sql for sql, _ in connection.cursor_obj.statements]
        self.assertEqual(len(statements), 5)
        self.assertIn("CREATE TABLE IF NOT EXISTS", statements[0])
        self.assertIn("information_schema.COLUMNS", statements[1])
        self.assertIn("ADD COLUMN `价格`", statements[2])
        self.assertIn("information_schema.COLUMNS", statements[3])
        self.assertIn("ADD COLUMN `30天销量`", statements[4])
        self.assertTrue(connection.committed)

    def test_existing_table_with_the_thirty_day_column_is_left_alone(self):
        connection = FakeConnection(column_exists=True)
        esale_product.ensure_table(connection, schema="adjust_price")
        statements = [sql for sql, _ in connection.cursor_obj.statements]
        self.assertEqual(len(statements), 3)
        self.assertNotIn("ADD COLUMN", " ".join(statements))


class ReplaceTest(unittest.TestCase):
    def rows(self):
        return [
            {"code": "1001", "name": "药A", "quantity": 3, "amount": Decimal("9.00"),
             "quantity_30": 11, "stock": 42},
            {"code": "1002", "name": "药B", "quantity": 0, "amount": None,
             "quantity_30": None, "stock": None},
        ]

    def test_pruning_deletes_only_the_rows_this_run_does_not_carry(self):
        connection = FakeConnection()
        count = esale_product.replace(connection, schema="adjust_price", client="保民", platform="京东",
                                      day=dt.date(2026, 9, 20), rows=self.rows())
        self.assertEqual(count, 2)
        self.assertTrue(connection.committed)
        delete_sql, delete_params = connection.cursor_obj.statements[0]
        self.assertIn("DELETE FROM `adjust_price`.`esale_product`", delete_sql)
        # Scoped to the slice AND to the codes this run left out, so a row that
        # another collector owns is never dropped.
        self.assertIn("`唯一编码` NOT IN (%s,%s)", delete_sql)
        self.assertEqual(delete_params, ("保民", "京东", "1001", "1002"))
        insert_sql, batch = connection.cursor_obj.batches[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", insert_sql)
        self.assertIn("`30天销量`", insert_sql)
        self.assertIn("`库存`", insert_sql)
        self.assertEqual(
            batch[0],
            ("保民", "京东", "药A", "1001", Decimal("3"), Decimal("42"),
             Decimal("9.00"), Decimal("11"), dt.date(2026, 9, 20)),
        )
        # A key the collector did not report must land as SQL NULL, not zero.
        self.assertEqual(batch[1][4:8], (Decimal("0"), None, None, None))
    def test_prune_false_deletes_nothing(self):
        """The 上架 feed's mode: upsert only, so skuRank's 销量 column survives."""
        connection = FakeConnection()
        rows = [{"code": "2001", "name": "药C", "quantity_30": 7, "stock": 5}]
        count = esale_product.replace(connection, schema="adjust_price", client="保民", platform="美团",
                                      day=dt.date(2026, 9, 20), rows=rows, prune=False)
        self.assertEqual(count, 1)
        self.assertTrue(connection.committed)
        self.assertEqual(connection.cursor_obj.statements, [])

    def test_commit_can_be_deferred_for_a_multi_step_transaction(self):
        connection = FakeConnection()
        esale_product.replace(
            connection, schema="adjust_price", client="保民", platform="美团",
            day=dt.date(2026, 9, 20),
            rows=[{"code": "2001", "name": "药C", "quantity": 4}],
            prune=False, commit=False,
        )
        self.assertFalse(connection.committed)

    def test_a_single_window_collector_only_fills_its_own_column(self):
        connection = FakeConnection()
        rows = [{"code": "2001", "name": "药C", "quantity_30": 7, "stock": 5}]
        esale_product.replace(connection, schema="adjust_price", client="保民", platform="美团",
                              day=dt.date(2026, 9, 20), rows=rows, prune=False)
        insert_sql, batch = connection.cursor_obj.batches[0]
        self.assertNotIn("`销量`=VALUES", insert_sql)   # 销量 stays untouched
        self.assertNotIn("`销售额`", insert_sql)
        self.assertIn("`30天销量`=VALUES(`30天销量`)", insert_sql)
        self.assertIn("`库存`=VALUES(`库存`)", insert_sql)
        # 销量 and 销售额 are absent, so the tuple is 客户/平台/商品/编码/库存/30天销量/日期.
        self.assertEqual(batch[0], ("保民", "美团", "药C", "2001",
                                    Decimal("5"), Decimal("7"), dt.date(2026, 9, 20)))

    def test_only_reported_columns_are_named_in_the_update_half(self):
        """A run that omits 30天销量 must not name it, or it would write NULL over it."""
        connection = FakeConnection()
        rows = [{"code": "2001", "name": "药C", "quantity": 4, "amount": Decimal("8.00")}]
        esale_product.replace(connection, schema="adjust_price", client="保民", platform="美团",
                              day=dt.date(2026, 9, 20), rows=rows, prune=False)
        insert_sql, batch = connection.cursor_obj.batches[0]
        self.assertIn("`销量`=VALUES(`销量`)", insert_sql)
        self.assertIn("`销售额`=VALUES(`销售额`)", insert_sql)
        self.assertIn("`商品`=VALUES(`商品`)", insert_sql)
        self.assertIn("`更新日期`=VALUES(`更新日期`)", insert_sql)
        # The columns this feed cannot see are absent from the statement, so the
        # other collector's values survive.
        self.assertNotIn("30天销量", insert_sql)
        self.assertNotIn("`库存`", insert_sql)
        # 客户/平台/唯一编码 identify the row and are never reassigned.
        self.assertNotIn("`客户`=VALUES", insert_sql)
        self.assertNotIn("`唯一编码`=VALUES", insert_sql)
        self.assertEqual(batch[0], ("保民", "美团", "药C", "2001", Decimal("4"),
                                    Decimal("8.00"), dt.date(2026, 9, 20)))

    def test_an_explicit_none_still_overwrites(self):
        """Reporting a key with None means "genuinely nothing", which must win."""
        connection = FakeConnection()
        rows = [{"code": "2001", "name": "药C", "quantity": None, "quantity_30": 7}]
        esale_product.replace(connection, schema="adjust_price", client="保民", platform="美团",
                              day=dt.date(2026, 9, 20), rows=rows, prune=False)
        insert_sql, batch = connection.cursor_obj.batches[0]
        self.assertIn("`销量`=VALUES(`销量`)", insert_sql)
        self.assertIsNone(batch[0][4])          # written as SQL NULL on purpose
        self.assertEqual(batch[0][5], Decimal("7"))

    def test_each_row_may_report_a_different_key_set(self):
        """The statement is built from the union, so a row missing a key inserts NULL."""
        connection = FakeConnection()
        rows = [{"code": "1", "name": "药A", "quantity": 3},
                {"code": "2", "name": "药B", "quantity_30": 9}]
        esale_product.replace(connection, schema="adjust_price", client="保民", platform="美团",
                              day=dt.date(2026, 9, 20), rows=rows, prune=False)
        insert_sql, batch = connection.cursor_obj.batches[0]
        self.assertEqual(len(batch), 2)
        # 销量 and 30天销量 are both in play (each row reported one), and the row
        # that omitted a key lands NULL in it rather than a stale value.
        self.assertEqual(batch[0][4:6], (Decimal("3"), None))
        self.assertEqual(batch[1][4:6], (None, Decimal("9")))

    def test_empty_collection_is_refused_instead_of_wiping_the_slice(self):
        connection = FakeConnection()
        with self.assertRaisesRegex(ValueError, "no rows"):
            esale_product.replace(connection, schema="adjust_price", client="保民", platform="京东",
                                  day=dt.date(2026, 9, 20), rows=[])
        self.assertEqual(connection.cursor_obj.statements, [])

    def test_empty_collection_is_allowed_when_the_platform_reported_no_sales(self):
        connection = FakeConnection()
        count = esale_product.replace(connection, schema="adjust_price", client="福康综合门诊",
                                      platform="美团", day=dt.date(2026, 9, 20), rows=[],
                                      allow_empty=True)
        self.assertEqual(count, 0)
        self.assertTrue(connection.committed)
        delete_sql, delete_params = connection.cursor_obj.statements[0]
        self.assertIn("DELETE FROM", delete_sql)
        self.assertEqual(delete_params, ("福康综合门诊", "美团"))
        # Nothing to insert, so no batch is emitted at all.
        self.assertEqual(connection.cursor_obj.batches, [])

    def test_duplicate_codes_are_refused(self):
        connection = FakeConnection()
        rows = [{"code": "1001", "name": "药A", "quantity": 1, "amount": None},
                {"code": "1001", "name": "药A", "quantity": 2, "amount": None}]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            esale_product.replace(connection, schema="adjust_price", client="保民", platform="京东",
                                  day=dt.date(2026, 9, 20), rows=rows)

    def test_row_without_a_code_is_refused(self):
        connection = FakeConnection()
        with self.assertRaisesRegex(ValueError, "empty"):
            esale_product.replace(connection, schema="adjust_price", client="保民", platform="京东",
                                  day=dt.date(2026, 9, 20),
                                  rows=[{"code": " ", "name": "药A", "quantity": 1, "amount": None}])


class PruneStaleTest(unittest.TestCase):
    def test_only_rows_outside_the_full_view_are_deleted(self):
        """The 上架 feed is the only caller: it alone sees every listed product."""
        connection = FakeConnection()
        connection.cursor_obj.counts = [5, 5]
        # 5 rows in, 5 codes supplied -> nothing droppable.  ``prune_stale``
        # re-counts before and after, so the fake must answer twice.
        removed = esale_product.prune_stale(connection, schema="adjust_price", client="保民",
                                            platform="美团", codes=["1", "2", "3", "4", "5"])
        self.assertEqual(removed, 0)
        delete_sql, delete_params = connection.cursor_obj.statements[1]
        self.assertIn("`唯一编码` NOT IN (%s,%s,%s,%s,%s)", delete_sql)
        self.assertEqual(delete_params, ("保民", "美团", "1", "2", "3", "4", "5"))
        self.assertTrue(connection.committed)

    def test_passing_no_codes_empties_the_slice(self):
        connection = FakeConnection()
        connection.cursor_obj.counts = [3, 0]
        removed = esale_product.prune_stale(connection, schema="adjust_price", client="保民",
                                            platform="美团", codes=[])
        self.assertEqual(removed, 3)
        delete_sql, delete_params = connection.cursor_obj.statements[1]
        self.assertIn("DELETE FROM", delete_sql)
        self.assertEqual(delete_params, ("保民", "美团"))

    def test_large_code_sets_use_one_complete_not_in_predicate(self):
        connection = FakeConnection()
        connection.cursor_obj.counts = [1200, 1200]
        esale_product.prune_stale(connection, schema="adjust_price", client="保民",
                                  platform="美团", codes=[str(i) for i in range(1200)])
        deletes = [s for s in connection.cursor_obj.statements if "NOT IN" in s[0]]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(len(deletes[0][1]) - 2, 1200)
        self.assertEqual(deletes[0][0].count("%s"), 1202)


class ResetMetricsTest(unittest.TestCase):
    def test_only_the_named_metrics_are_reset_in_the_store_slice(self):
        connection = FakeConnection()
        connection.cursor_obj.rowcount = 7
        esale_product.reset_metrics(
            connection, schema="adjust_price", client="保民", platform="美团",
            values={"销量": 0, "销售额": Decimal("0.00")}, commit=False,
        )
        sql, params = connection.cursor_obj.statements[0]
        self.assertIn("SET `销量`=%s,`销售额`=%s", sql)
        self.assertIn("WHERE `客户`=%s AND `平台`=%s", sql)
        self.assertEqual(params, (0, Decimal("0.00"), "保民", "美团"))
        self.assertFalse(connection.committed)

    def test_identifier_columns_cannot_be_reset(self):
        connection = FakeConnection()
        with self.assertRaisesRegex(ValueError, "unsupported"):
            esale_product.reset_metrics(
                connection, schema="adjust_price", client="保民", platform="美团",
                values={"客户": "别的客户"},
            )
        self.assertEqual(connection.cursor_obj.statements, [])


if __name__ == "__main__":
    unittest.main()
