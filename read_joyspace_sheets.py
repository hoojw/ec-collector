#!/usr/bin/env python3
"""Read JoySpace sheet metadata through the embedded WPS OpenAPI."""

from __future__ import annotations

import argparse
import json

import requests
import websocket


def find_page(port: int) -> dict:
    response = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=10)
    response.raise_for_status()
    for target in response.json():
        if (
            target.get("type") == "page"
            and "joyspace.jd.com/sheets/" in target.get("url", "")
        ):
            return target
    raise RuntimeError("JoySpace sheet page was not found")


class RuntimeConnection:
    def __init__(self, websocket_url: str):
        self.websocket = websocket.create_connection(websocket_url, timeout=30)
        self.message_id = 0
        self.contexts = self._enable_runtime()

    def _enable_runtime(self) -> list[dict]:
        self.message_id += 1
        current_id = self.message_id
        self.websocket.send(json.dumps({"id": current_id, "method": "Runtime.enable"}))
        contexts = []
        while True:
            message = json.loads(self.websocket.recv())
            if message.get("method") == "Runtime.executionContextCreated":
                contexts.append(message.get("params", {}).get("context", {}))
            if message.get("id") == current_id:
                return contexts

    def office_context_id(self) -> int:
        for context in self.contexts:
            if (
                context.get("origin") == "https://joyspace-office.jd.com"
                and context.get("auxData", {}).get("isDefault")
            ):
                return int(context["id"])
        raise RuntimeError("JoySpace WebOffice execution context was not found")

    def evaluate(self, expression: str):
        self.message_id += 1
        current_id = self.message_id
        self.websocket.send(
            json.dumps(
                {
                    "id": current_id,
                    "method": "Runtime.evaluate",
                    "params": {
                        "contextId": self.office_context_id(),
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "timeout": 60_000,
                    },
                }
            )
        )
        while True:
            message = json.loads(self.websocket.recv())
            if message.get("id") != current_id:
                continue
            result = message.get("result", {})
            if result.get("exceptionDetails"):
                description = (
                    result.get("result", {}).get("description")
                    or result["exceptionDetails"].get("text")
                )
                raise RuntimeError(description)
            return result.get("result", {}).get("value")


SHEET_METADATA_SCRIPT = """
(async function () {
  await WPSOpenApi.ready();
  var app = WPSOpenApi.Application;
  var workbook = await app.ActiveWorkbook;
  var worksheets = await workbook.Worksheets;
  var count = Number(await worksheets.Count);
  var result = [];
  for (var index = 1; index <= count; index += 1) {
    var sheet = await worksheets.Item(index);
    var usedRange = await sheet.UsedRange;
    result.push({
      index: index,
      name: String(await sheet.Name),
      rows: Number(await (await usedRange.Rows).Count),
      columns: Number(await (await usedRange.Columns).Count)
    });
  }
  return result;
})()
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9533)
    parser.add_argument("--sample-rows", type=int, default=0)
    args = parser.parse_args()
    page = find_page(args.port)
    connection = RuntimeConnection(page["webSocketDebuggerUrl"])
    result = {"sheets": connection.evaluate(SHEET_METADATA_SCRIPT)}
    if args.sample_rows:
        if args.sample_rows < 1 or args.sample_rows > 20:
            raise ValueError("--sample-rows must be between 1 and 20")
        result["sample"] = connection.evaluate(
            """
            (async function () {
              await WPSOpenApi.ready();
              var workbook = await WPSOpenApi.Application.ActiveWorkbook;
              var worksheets = await workbook.Worksheets;
              var sheet = await worksheets.Item(1);
              var range = await sheet.Range("A1:U%s");
              return await range.Value;
            })()
            """
            % args.sample_rows
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
