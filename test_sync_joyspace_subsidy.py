import datetime as dt
import unittest

import sync_joyspace_subsidy as sync
from sync_joyspace_subsidy import EXPECTED_HEADERS, remap_sheet_rows


class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.log.append(("execute", " ".join(sql.split()), params))

    def executemany(self, sql, params):
        self.log.append(("executemany", " ".join(sql.split()), list(params)))


class _FakeConnection:
    def __init__(self):
        self.log = []

    def cursor(self):
        return _FakeCursor(self.log)


class RefreshTableTest(unittest.TestCase):
    """The JoySpace sync must feed both the history and the current snapshot."""

    def setUp(self):
        self.connection = _FakeConnection()
        self.row = [str(index) for index in range(len(EXPECTED_HEADERS))]
        sync.refresh_table(self.connection, dt.date(2026, 9, 22), [self.row])
        self.statements = self.connection.log

    def _find(self, kind, needle):
        return [entry for entry in self.statements if entry[0] == kind and needle in entry[1]]

    def test_daily_history_is_replaced_for_the_record_date_only(self):
        deletes = self._find("execute", "DELETE FROM `jd_subsidy_catalog_daily`")
        self.assertEqual(len(deletes), 1)
        self.assertIn("记录日期", deletes[0][1])
        self.assertEqual(deletes[0][2], (dt.date(2026, 9, 22),))

    def test_current_catalog_is_overwritten_wholesale(self):
        deletes = [entry for entry in self.statements if entry[0] == "execute" and entry[1] == "DELETE FROM `jd_subsidy_catalog`"]
        self.assertEqual(len(deletes), 1, self.statements)
        self.assertEqual(deletes[0][2], ())

    def test_current_catalog_carries_sync_provenance_columns(self):
        inserts = self._find("executemany", "INSERT INTO `jd_subsidy_catalog` (")
        self.assertEqual(len(inserts), 1)
        self.assertIn("`同步日期`", inserts[0][1])
        self.assertIn("`更新时间`", inserts[0][1])
        self.assertEqual(len(inserts[0][2][0]), len(EXPECTED_HEADERS) + 2)

    def test_daily_history_row_carries_the_record_date(self):
        inserts = self._find("executemany", "INSERT INTO `jd_subsidy_catalog_daily`")
        self.assertEqual(len(inserts), 1)
        self.assertEqual(len(inserts[0][2][0]), len(EXPECTED_HEADERS) + 1)
        self.assertEqual(inserts[0][2][0][0], dt.date(2026, 9, 22))


class SyncJoySpaceSubsidyTest(unittest.TestCase):
    def test_remaps_renamed_headers_and_ignores_extra_columns(self):
        source_headers = list(EXPECTED_HEADERS)
        source_headers[source_headers.index("大盘排序")] = "大盘顺序"
        source_headers[source_headers.index("前几个低价补贴")] = "推荐数量"
        source_headers.insert(3, "未来新增列")
        source_row = [f"value-{index}" for index in range(len(source_headers))]

        headers, rows = remap_sheet_rows(source_headers, [source_row])

        self.assertEqual(headers, EXPECTED_HEADERS)
        self.assertEqual(len(rows[0]), len(EXPECTED_HEADERS))
        self.assertNotIn(source_row[3], rows[0])
        self.assertEqual(rows[0][0], source_row[0])
        self.assertEqual(rows[0][1], source_row[1])

    def test_rejects_a_missing_required_header(self):
        source_headers = [
            header for header in EXPECTED_HEADERS if header != "策略ID"
        ]

        with self.assertRaisesRegex(ValueError, "missing required headers"):
            remap_sheet_rows(source_headers, [])

    def test_rejects_two_aliases_for_the_same_required_header(self):
        source_headers = list(EXPECTED_HEADERS) + ["大盘顺序"]

        with self.assertRaisesRegex(ValueError, "ambiguous required headers"):
            remap_sheet_rows(source_headers, [])


if __name__ == "__main__":
    unittest.main()
