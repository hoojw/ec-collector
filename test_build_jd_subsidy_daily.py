import unittest

from build_jd_subsidy_daily import (
    DEFAULT_OUTPUT_TABLE,
    qualified_name,
    quote_identifier,
    table_settings,
)


class BuildJdSubsidyDailyTest(unittest.TestCase):
    def test_default_table_settings(self):
        settings = table_settings({})
        self.assertEqual(settings["output_table"], DEFAULT_OUTPUT_TABLE)
        self.assertEqual(
            settings["catalog_table"],
            "jd_subsidy_catalog",
        )
        self.assertEqual(
            settings["cost_table"],
            "excel_Result 1_1aa17d71d5",
        )

    def test_table_settings_can_be_overridden(self):
        settings = table_settings(
            {
                "tables": {
                    "jd_subsidy_daily": "custom_output",
                    "jd_subsidy_catalog": "custom_catalog",
                    "jd_cost_table": "custom_cost",
                }
            }
        )
        self.assertEqual(settings["output_table"], "custom_output")
        self.assertEqual(settings["catalog_table"], "custom_catalog")
        self.assertEqual(settings["cost_table"], "custom_cost")

    def test_identifier_quoting(self):
        self.assertEqual(
            qualified_name("dataease", "excel_Result 1"),
            "`dataease`.`excel_Result 1`",
        )
        with self.assertRaises(ValueError):
            quote_identifier("bad`name")


if __name__ == "__main__":
    unittest.main()
