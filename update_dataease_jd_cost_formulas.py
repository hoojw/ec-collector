#!/usr/bin/env python3
"""Update JD cost formulas in DataEase and synchronize chart field caches."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pymysql


DATASET_ID = "1279882657748094976"
MULTIBOX_COST_ID = "7488151493330079744"
TOTAL_COST_ID = "7488152388071591936"
UNIT_TOTAL_COST_ID = "7488159217367388160"
PACKAGING_COST_ID = "7488162154256797696"
ACTUAL_BOXES_ID = "1785397717921"
COST_PRICE_ID = "1785312881633"
COST_FLOOR_ID = "1785312881634"
CHART_JSON_COLUMNS = (
    "x_axis",
    "x_axis_ext",
    "y_axis",
    "y_axis_ext",
    "ext_stack",
    "ext_tooltip",
    "view_fields",
)

FORMULAS = {
    MULTIBOX_COST_ID: (
        "COALESCE(\n"
        f"    NULLIF([{COST_FLOOR_ID}], 0),\n"
        f"    NULLIF([{COST_PRICE_ID}], 0)\n"
        f") * [{ACTUAL_BOXES_ID}]"
    ),
    UNIT_TOTAL_COST_ID: (
        f"[{TOTAL_COST_ID}] / NULLIF([{ACTUAL_BOXES_ID}], 0)"
    ),
    PACKAGING_COST_ID: (
        "CASE\n"
        f"WHEN [{ACTUAL_BOXES_ID}] <= 4 THEN 0.33\n"
        "ELSE 0.6\n"
        "END"
    ),
}


def mysql_root_password(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MYSQL_ROOT_PASSWORD="):
            return line.split("=", 1)[1]
    raise RuntimeError("MYSQL_ROOT_PASSWORD was not found")


def update_cached_fields(value: str | None, formulas: dict[str, str]):
    if not value:
        return value
    payload = json.loads(value)
    if not isinstance(payload, list):
        return value
    changed = False
    for field in payload:
        field_id = str(field.get("id", ""))
        if field_id in formulas:
            field["originName"] = formulas[field_id]
            changed = True
    if not changed:
        return value
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def main() -> None:
    connection = pymysql.connect(
        host="127.0.0.1",
        port=3307,
        user="root",
        password=mysql_root_password(
            Path("/opt/dataease2.0/conf/mysql.env")
        ),
        database="dataease",
        charset="utf8mb4",
        autocommit=False,
    )
    backup_dir = Path("/opt/subsidy/history/files")
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / (
        f"dataease-jd-cost-formulas-before-{timestamp}.json"
    )

    try:
        with connection.cursor() as cursor:
            field_ids = tuple(
                {
                    *FORMULAS,
                    TOTAL_COST_ID,
                }
            )
            placeholders = ",".join(["%s"] * len(field_ids))
            cursor.execute(
                f"""
                SELECT id, name, origin_name
                FROM core_dataset_table_field
                WHERE dataset_group_id = %s
                  AND id IN ({placeholders})
                ORDER BY id
                """,
                (DATASET_ID, *field_ids),
            )
            fields_before = cursor.fetchall()
            if len(fields_before) != len(field_ids):
                raise RuntimeError(
                    "Expected DataEase calculated fields were not found"
                )

            cursor.execute(
                f"""
                SELECT id, title, {", ".join(CHART_JSON_COLUMNS)}
                FROM core_chart_view
                WHERE table_id = %s
                """,
                (DATASET_ID,),
            )
            charts_before = cursor.fetchall()
            backup_path.write_text(
                json.dumps(
                    {
                        "dataset_id": DATASET_ID,
                        "fields": fields_before,
                        "charts": charts_before,
                    },
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
            backup_path.chmod(0o600)

            for field_id, formula in FORMULAS.items():
                cursor.execute(
                    """
                    UPDATE core_dataset_table_field
                    SET origin_name = %s
                    WHERE dataset_group_id = %s
                      AND id = %s
                    """,
                    (formula, DATASET_ID, field_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Failed to update DataEase field {field_id}"
                    )

            formulas = {
                str(field_id): origin_name
                for field_id, _, origin_name in fields_before
            }
            formulas.update(FORMULAS)
            for chart in charts_before:
                chart_id = chart[0]
                updated_values = [
                    update_cached_fields(value, formulas)
                    for value in chart[2:]
                ]
                assignments = ", ".join(
                    f"{column} = %s" for column in CHART_JSON_COLUMNS
                )
                cursor.execute(
                    f"""
                    UPDATE core_chart_view
                    SET {assignments}
                    WHERE id = %s
                    """,
                    (*updated_values, chart_id),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        json.dumps(
            {
                "dataset_id": DATASET_ID,
                "updated_fields": list(FORMULAS),
                "backup": str(backup_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
