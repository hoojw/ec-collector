#!/usr/bin/env python3
"""Export the JoySpace 小官补 workbook's three data sheets to a dated xlsx archive.

Reads the full UsedRange of each sheet through the embedded WPS OpenAPI (values
only -- cell formatting is intentionally not preserved) and writes one xlsx
containing the three sheets, archived under snapshots/<record_date>/.

This is a read-only viewer export: it never touches the database or the
existing sync_joyspace_subsidy.py ingestion logic.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from read_joyspace_sheets import RuntimeConnection, find_page

TZ = ZoneInfo("Asia/Shanghai")
SHEETS = ("小官补补贴清单", "已有指数商品清单", "指数补贴品")
DEFAULT_OUTPUT_ROOT = Path("/opt/subsidy/joyspace/snapshots")


def excel_column(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def read_sheet_raw(connection: RuntimeConnection, sheet_name: str, chunk_size: int):
    metadata = connection.evaluate(
        """
        (async function () {
          await WPSOpenApi.ready();
          var workbook = await WPSOpenApi.Application.ActiveWorkbook;
          var worksheets = await workbook.Worksheets;
          var sheet = await worksheets.Item(%s);
          var usedRange = await sheet.UsedRange;
          return {
            rows: Number(await (await usedRange.Rows).Count),
            columns: Number(await (await usedRange.Columns).Count)
          };
        })()
        """
        % json.dumps(sheet_name, ensure_ascii=False)
    )
    row_count = int(metadata["rows"])
    column_count = int(metadata["columns"])
    if row_count < 1 or column_count < 1:
        return []

    last_column = excel_column(column_count)
    rows = []
    for start in range(1, row_count + 1, chunk_size):
        end = min(start + chunk_size - 1, row_count)
        values = connection.evaluate(
            """
            (async function () {
              var workbook = await WPSOpenApi.Application.ActiveWorkbook;
              var worksheets = await workbook.Worksheets;
              var sheet = await worksheets.Item(%s);
              var range = await sheet.Range(%s);
              return await range.Value;
            })()
            """
            % (
                json.dumps(sheet_name, ensure_ascii=False),
                json.dumps(f"A{start}:{last_column}{end}"),
            )
        )
        chunk = values if isinstance(values, list) else []
        for row in chunk:
            cells = list(row or [])
            cells.extend([None] * (column_count - len(cells)))
            rows.append(cells[:column_count])
        logging.info("Read %s rows %s-%s", sheet_name, start, end)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9533)
    parser.add_argument("--record-date", type=dt.date.fromisoformat)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
    )
    parser.add_argument("--chunk-size", type=int, default=250)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    record_date = args.record_date or dt.datetime.now(TZ).date()
    output_dir = args.output_root / record_date.isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "joyspace_小官补.xlsx"
    tmp_path = output_dir / (output_path.name + ".tmp")

    page = find_page(args.port)
    runtime = RuntimeConnection(page["webSocketDebuggerUrl"])
    workbook = Workbook()
    workbook.remove(workbook.active)
    summary = {}
    try:
        for name in SHEETS:
            rows = read_sheet_raw(runtime, name, args.chunk_size)
            worksheet = workbook.create_sheet(title=name)
            for row in rows:
                worksheet.append(row)
            summary[name] = len(rows)
        workbook.save(tmp_path)
        tmp_path.replace(output_path)
    finally:
        runtime.websocket.close()

    print(
        json.dumps(
            {
                "record_date": record_date.isoformat(),
                "output": str(output_path),
                "sheets": summary,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
