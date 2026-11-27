#!/usr/bin/env python3
"""Attach to the open 天猫 after-sales (refund-list) tab, reload, and capture
all XHR/Fetch/mtop responses, then upload the capture to the 150 file bridge.

Run on the Windows machine with the logged-in Chrome (CDP port 9633).
Read-only: only reloads the page; never clicks 同意/拒绝/退款 or changes anything.
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import time
from urllib.parse import quote

import requests

try:
    import websocket
except ImportError:
    print("MISSING websocket-client; run: .venv\\Scripts\\python.exe -m pip install websocket-client")
    raise SystemExit(2)

DEFAULT_URL = "https://myseller.taobao.com/home.htm/trade-platform/refund-list"
HINTS = ("refund-list", "refund", "myseller.taobao.com")
BRIDGE = "http://192.168.3.150:8765/upload/tmall-aftersale-capture.json"


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=40, suppress_origin=True)
        self.mid = 0
        self.requests = {}

    def send(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        return self.mid

    def _handle(self, msg):
        m = msg.get("method")
        p = msg.get("params") or {}
        if m == "Network.requestWillBeSent":
            req = p.get("request") or {}
            rid = p.get("requestId")
            if not rid:
                return
            u = req.get("url", "")
            if u.startswith("http") and not any(x in u for x in (".js", ".css", ".png", ".woff", ".ico", "retcode", "beacon", "arms", "sgm")):
                self.requests[rid] = {"url": u, "method": req.get("method", "GET"),
                                      "postData": req.get("postData"), "type": p.get("type", "")}
        elif m == "Network.responseReceived":
            rid = p.get("requestId")
            if rid in self.requests:
                resp = p.get("response") or {}
                self.requests[rid]["status"] = resp.get("status")
                self.requests[rid]["mime"] = resp.get("mimeType", "")

    def call(self, method, params=None, timeout=15):
        my = self.send(method, params)
        end = time.time() + timeout
        self.ws.settimeout(1.0)
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            if msg.get("id") == my:
                return msg.get("result") or {}
            self._handle(msg)
        return {}

    def pump(self, sec):
        end = time.time() + sec
        self.ws.settimeout(1.0)
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            self._handle(msg)

    def body(self, rid):
        r = self.call("Network.getResponseBody", {"requestId": rid})
        data = r.get("body") or ""
        if r.get("base64Encoded"):
            try:
                data = base64.b64decode(data).decode("utf-8", "replace")
            except Exception:
                pass
        return data

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9633)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--settle", type=float, default=15.0)
    ap.add_argument("--listen", type=float, default=60.0,
                    help="extra seconds to listen while you click refund tabs/pages")
    ap.add_argument("--out", default="aftersale_capture.json")
    args = ap.parse_args()

    base = "http://127.0.0.1:%d" % args.port
    tabs = requests.get(base + "/json/list", timeout=10).json()
    target = next((t for t in tabs if t.get("type") == "page"
                   and any(h in str(t.get("url")) for h in HINTS)), None)
    mode = "existing-tab"
    if target is None:
        r = requests.put(base + "/json/new?" + quote(args.url, safe=""), timeout=20)
        if r.status_code == 405:
            r = requests.get(base + "/json/new?" + quote(args.url, safe=""), timeout=20)
        target = r.json()
        mode = "fresh-tab"
    print("attach_mode:", mode, "|", str(target.get("url"))[:100])

    cdp = CDP(target["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Network.enable", {"maxTotalBufferSize": 268435456, "maxResourceBufferSize": 134217728})
    cdp.call("Runtime.enable")
    cdp.pump(0.5)
    if mode == "existing-tab":
        cdp.call("Page.reload", {"ignoreCache": False})
    else:
        cdp.call("Page.navigate", {"url": args.url})
    cdp.pump(args.settle)
    if args.listen > 0:
        print("listening %gs for manual interaction (click refund tabs/pages)..." % args.listen)
        cdp.pump(args.listen)

    final = cdp.call("Runtime.evaluate", {
        "expression": "({url:location.href,title:document.title,text:(document.body&&document.body.innerText||'').slice(0,1500)})",
        "returnByValue": True}, timeout=10)
    page_info = ((final.get("result") or {}).get("value")) or {}

    captured = []
    for rid, e in list(cdp.requests.items()):
        b = cdp.body(rid) if e.get("status") else ""
        item = dict(e)
        item["body_length"] = len(b)
        item["body"] = b[:200000]
        captured.append(item)
    captured.sort(key=lambda x: x.get("url", ""))

    payload = {
        "attached_tab": target.get("url"), "attach_mode": mode,
        "page_after": page_info, "captured_count": len(captured), "captured": captured,
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("page_after:", json.dumps(page_info, ensure_ascii=False)[:800])
    print("captured:", len(captured), "->", str(out))
    for e in captured:
        if e.get("body_length", 0) > 0:
            print("  [%s] %-5s %-6s len=%-7s %s" % (
                e.get("status"), e.get("method"), e.get("type"),
                e.get("body_length"), (e.get("url") or "")[:120]))

    try:
        resp = requests.put(BRIDGE, data=out.read_bytes(), timeout=60)
        print("upload:", resp.status_code, resp.text[:200])
    except Exception as ex:
        print("upload failed:", ex)
    cdp.close()


if __name__ == "__main__":
    main()
