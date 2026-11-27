#!/usr/bin/env python3
"""Check whether the authorized JoySpace subsidy sheet is readable."""

from __future__ import annotations

import argparse
import json
import time

from read_joyspace_sheets import RuntimeConnection, find_page


SHEET_NAME = "小官补补贴清单"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9533)
    parser.add_argument("--retry-seconds", type=int, default=30)
    args = parser.parse_args()

    deadline = time.monotonic() + max(args.retry_seconds, 0)
    error = None
    sheet = None
    while True:
        connection = None
        try:
            page = find_page(args.port)
            connection = RuntimeConnection(page["webSocketDebuggerUrl"])
            sheet = connection.evaluate(
                """
                (async function () {
                  await WPSOpenApi.ready();
                  var workbook = await WPSOpenApi.Application.ActiveWorkbook;
                  var worksheets = await workbook.Worksheets;
                  var sheet = await worksheets.Item(%s);
                  var usedRange = await sheet.UsedRange;
                  return {
                    name: String(await sheet.Name),
                    rows: Number(await (await usedRange.Rows).Count),
                    columns: Number(await (await usedRange.Columns).Count)
                  };
                })()
                """
                % json.dumps(SHEET_NAME, ensure_ascii=False)
            )
            break
        except Exception as caught_error:
            error = caught_error
            if time.monotonic() >= deadline:
                break
            time.sleep(2)
        finally:
            if connection is not None:
                connection.websocket.close()

    if sheet is None:
        print(
            json.dumps(
                {
                    "logged_in": False,
                    "reason": "JoySpace subsidy sheet is unavailable",
                    "detail": str(error)[:300],
                },
                ensure_ascii=False,
            )
        )
        return 2

    print(
        json.dumps(
            {
                "logged_in": True,
                "sheet": sheet["name"],
                "rows": sheet["rows"],
                "columns": sheet["columns"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
