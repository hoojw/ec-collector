#!/usr/bin/env python3
"""
Server-side feasibility script for Meituan subsidy Excel export.

This script intentionally does not scrape table rows. It reuses a logged-in
browser session and calls the same export endpoints that the web page's export
button uses:

1. Open subsidy campaign page.
2. Trigger an export task from browser JavaScript.
3. Poll export task list until the signed download URL is ready.
4. Download the xlsx into /opt/subsidy/inbox.

The server still needs a valid Meituan login session. On Linux, run Chrome with
a persistent profile and log in once through VNC/noVNC before scheduling this.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import sys
import time
from urllib.parse import quote

import requests
import websocket


LIST_URL = (
    "https://yiyao.meituan.com/main/frame#/pages/health/marketing/"
    "low_price_campaign/list?tab=subsidyProduct&filterType=0"
)

TAB_LABEL = {
    1: "已补贴",
    2: "未补贴",
    3: "未建品",
}


def log(message: str) -> None:
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", text).strip()


class CdpPage:
    def __init__(self, port: int, url: str):
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self._msg_id = 0
        self.ws = websocket.create_connection(self._new_page(url), timeout=30)
        self.call("Runtime.enable")
        time.sleep(4)

    def _new_page(self, url: str) -> str:
        encoded = quote(url, safe="")
        resp = requests.put(f"{self.base}/json/new?{encoded}", timeout=10)
        if resp.status_code == 405:
            resp = requests.get(f"{self.base}/json/new?{encoded}", timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data["webSocketDebuggerUrl"]

    def call(self, method: str, params=None):
        self._msg_id += 1
        payload = {"id": self._msg_id, "method": method}
        if params is not None:
            payload["params"] = params
        self.ws.send(json.dumps(payload))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._msg_id:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg.get("result")

    def run_js(self, js: str):
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": js,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": 60000,
            },
        )
        remote = result.get("result", {})
        if "value" in remote:
            return remote["value"]
        if "description" in remote:
            return remote["description"]
        return None

    def page_info(self):
        return {
            "url": self.run_js("location.href"),
            "title": self.run_js("document.title"),
            "body": self.run_js("document.body ? document.body.innerText.slice(0, 300) : ''"),
        }


def run_json(tab, js: str):
    raw = tab.run_js(js)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            info = tab.page_info() if hasattr(tab, "page_info") else {}
            raise RuntimeError(f"JavaScript did not return JSON. raw={raw!r}, page={info}") from exc
    return raw


def open_export_page(port: int):
    return CdpPage(port, LIST_URL)


def trigger_export(tab, filter_type: int) -> str:
    extend = {
        "sortType": 1,
        "filterType": filter_type,
        "sortField": 40,
        "actTypes": [17, 29],
        "medicineTypeList": [],
    }
    js = f"""
    (() => {{
    var ext = encodeURIComponent(JSON.stringify({json.dumps(extend, ensure_ascii=False)}));
    var url = '/api/health/activity/apply/b/activity/export/lst?source=1&lstExportType=3&extend=' + ext;
    var x = new XMLHttpRequest();
    x.open('GET', url, false);
    x.send();
    var j = JSON.parse(x.responseText);
    return JSON.stringify({{code:j.code, msg:j.msg, exportId:(j.data&&j.data.exportId), raw:j}});
    }})()
    """
    result = run_json(tab, js)
    if result.get("code") != 0 or not result.get("exportId"):
        raise RuntimeError(f"trigger export failed: {result}")
    return str(result["exportId"])


def wait_download_url(tab, export_id: str, timeout: int):
    now = int(time.time())
    start = now - 3 * 24 * 3600
    js = f"""
    (() => {{
    var x = new XMLHttpRequest();
    x.open('GET','/api/health/activity/apply/b/activity/export/list?source=1&pageNo=1&pageSize=30&startDate={start}&endDate=' + Math.floor(Date.now()/1000), false);
    x.send();
    return x.responseText;
    }})()
    """

    deadline = time.time() + timeout
    while time.time() < deadline:
        body = tab.run_js(js)
        payload = json.loads(body)
        rows = (((payload or {}).get("data") or {}).get("data") or [])
        for row in rows:
            if str(row.get("id")) != str(export_id):
                continue
            urls = row.get("urls") or []
            if row.get("status") == 1 and urls and urls[0].get("url"):
                return urls[0].get("content") or f"{export_id}.xlsx", urls[0]["url"]
            log(f"export {export_id} status={row.get('status')}, waiting...")
        time.sleep(2)

    raise TimeoutError(f"export task timeout: {export_id}")


def download(url: str, save_path: pathlib.Path) -> int:
    resp = requests.get(url, timeout=90)
    resp.raise_for_status()
    save_path.write_bytes(resp.content)
    return len(resp.content)


def export_one(tab, filter_type: int, out_dir: pathlib.Path, timeout: int) -> pathlib.Path:
    label = TAB_LABEL.get(filter_type, str(filter_type))
    log(f"{label}: trigger export")
    export_id = trigger_export(tab, filter_type)
    log(f"{label}: exportId={export_id}")
    source_name, url = wait_download_url(tab, export_id, timeout)

    today = dt.date.today().strftime("%Y%m%d")
    out_name = f"{label}_{today}.xlsx"
    save_path = out_dir / safe_name(out_name)
    size = download(url, save_path)
    log(f"{label}: downloaded {save_path} ({size} bytes), source={source_name}")
    print(f"EXPORT_PATH={save_path}", flush=True)
    return save_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", default="/opt/subsidy/inbox")
    parser.add_argument("--user-data-dir", default="/opt/meituan-chrome-profile")
    parser.add_argument("--port", type=int, default=9223)
    parser.add_argument("--filter-type", type=int, default=2, choices=[1, 2, 3])
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.user_data_dir).mkdir(parents=True, exist_ok=True)

    log(f"connecting chrome devtools on 127.0.0.1:{args.port}")
    tab = open_export_page(args.port)
    log("opened Meituan export page")
    export_one(tab, args.filter_type, out_dir, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
