#!/usr/bin/env python3
"""只读探测：京东当前登录账号 + 直接访问商家后台的真实结果。"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import quote

import requests
import websocket

PORT = 9533
MERCHANT_URL = "https://wares-jdm.jd.com/ware/wareList?activeTab=OnsaleWare&businessModel=0"


def cdp(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") != mid:
            continue
        if msg.get("error"):
            raise RuntimeError(msg["error"])
        return msg.get("result", {})


def cookie_values(ws, mid, names=("pin", "unick")):
    r = cdp(ws, mid, "Network.getAllCookies")
    out = {}
    for c in r.get("cookies", []):
        if c.get("name") in names and c.get("domain") == ".jd.com":
            out[c["name"]] = c.get("value")
    return out


def main():
    # 1) 当前 profile 里的京东账号
    tgt = requests.get(f"http://127.0.0.1:{PORT}/json/list", timeout=10).json()
    page = next(t for t in tgt if t.get("type") == "page")
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
    mid = 1
    cdp(ws, mid, "Runtime.enable")
    mid += 1
    cdp(ws, mid, "Network.enable")
    vals = cookie_values(ws, mid)
    print("=== profile 中的京东账号 cookie (.jd.com) ===")
    for k, v in vals.items():
        print(f"  {k} = {v}")
    ws.close()

    # 2) 新建 tab 直接访问商家后台，观察落点
    print(f"\n>>> 新建 tab -> {MERCHANT_URL}")
    created = requests.put(f"http://127.0.0.1:{PORT}/json/new?{quote(MERCHANT_URL, safe='')}", timeout=15)
    created.raise_for_status()
    tab = created.json()
    tid = tab["id"]

    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=30)
    mid = 1
    cdp(ws, mid, "Runtime.enable")
    mid += 1
    cdp(ws, mid, "Page.enable")

    t0 = time.monotonic()
    last = None
    for _ in range(15):
        mid += 1
        try:
            r = cdp(ws, mid, "Runtime.evaluate", {
                "expression": r"""(() => {
                  const t = (document.body && document.body.innerText || '').replace(/\s+/g,' ').trim();
                  return {url: location.href, title: document.title, ready: document.readyState, text: t.slice(0, 600)};
                })()""",
                "returnByValue": True, "timeout": 8000})
            v = r.get("result", {}).get("value") or {}
        except Exception as exc:
            print(f"[{time.monotonic()-t0:5.1f}s] {str(exc)[:90]}")
            time.sleep(2)
            continue
        sig = f"{v.get('url')}|{v.get('text','')[:70]}"
        if sig != last:
            print(f"[{time.monotonic()-t0:5.1f}s] url  = {str(v.get('url'))[:115]}")
            print(f"          title= {str(v.get('title'))[:80]}  ready={v.get('ready')}")
            print(f"          text = {str(v.get('text'))[:400]}")
            last = sig
        time.sleep(2)

    try:
        mid += 1
        shot = cdp(ws, mid, "Page.captureScreenshot", {"format": "png"})
        if shot.get("data"):
            with open("/tmp/jd_merchant_new.png", "wb") as fh:
                fh.write(base64.b64decode(shot["data"]))
            print("\n截图: /tmp/jd_merchant_new.png")
    except Exception as exc:
        print(f"截图失败: {exc}")

    # 保留这个 tab（后续好定位），不关闭
    print(f"\n保留 tab id={tid[:16]}")
    ws.close()


if __name__ == "__main__":
    main()
