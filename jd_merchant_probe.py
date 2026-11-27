#!/usr/bin/env python3
"""只读探测：京东商家后台登录态为何被拦截。"""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import quote

import requests
import websocket

PORT = 9533
MERCHANT_URL = "https://wares-jdm.jd.com/ware/wareList?activeTab=OnsaleWare&businessModel=0"
SHOP_HOME = "https://shop.jd.com/"


def cdp(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") != mid:
            continue
        if msg.get("error"):
            raise RuntimeError(msg["error"])
        return msg.get("result", {})


def dump_cookies(ws, mid):
    r = cdp(ws, mid, "Network.getAllCookies")
    cookies = r.get("cookies", [])
    keep = []
    for c in cookies:
        dom = c.get("domain", "")
        if not any(k in dom for k in ("jd.com", "jd.hk", "jingdong")):
            continue
        name = c.get("name", "")
        if name in ("pin", "pt_key", "pt_pin", "thor", "whwswswws", "unick", "__jda", "__jdb", "__jdc", "__jdu", "shshshfpa", "mba_muid", "wskey"):
            keep.append({"name": name, "domain": dom[:40], "expires": c.get("expires"), "httpOnly": c.get("httpOnly")})
    return sorted(keep, key=lambda x: x["name"])


def main():
    targets = requests.get(f"http://127.0.0.1:{PORT}/json/list", timeout=10).json()
    print("=== 现有 targets ===")
    for t in targets:
        if t.get("type") == "page":
            print(f"  {str(t.get('url'))[:95]}")
            print(f"     title={str(t.get('title'))[:60]}")

    # 复用已有商家后台 tab，否则新建
    existing = next(
        (t for t in targets if t.get("type") == "page"
         and ("wares-jdm.jd.com" in str(t.get("url")) or "accountIntercept" in str(t.get("url")))),
        None,
    )
    if existing:
        print(f"\n>>> 复用 tab {existing['id'][:16]}  url={str(existing.get('url'))[:80]}")
        target, owns = existing, False
    else:
        print("\n>>> 新建 tab -> wares-jdm.jd.com")
        created = requests.put(f"http://127.0.0.1:{PORT}/json/new?{quote(MERCHANT_URL, safe='')}", timeout=15)
        created.raise_for_status()
        target, owns = created.json(), True

    ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)
    mid = 0

    mid += 1
    cdp(ws, mid, "Runtime.enable")
    mid += 1
    cdp(ws, mid, "Network.enable")

    print("\n=== 京东域 cookie ===")
    try:
        for c in dump_cookies(ws, mid):
            print(f"  {c['name']:<14} domain={c['domain']:<28} httpOnly={c['httpOnly']}")
    except Exception as exc:
        print(f"  取 cookie 失败: {exc}")

    # 当前页面内容
    t0 = time.monotonic()
    last = None
    for _ in range(12):
        mid += 1
        try:
            r = cdp(ws, mid, "Runtime.evaluate", {
                "expression": r"""(() => {
                  const t = (document.body && document.body.innerText || '').replace(/\s+/g,' ').trim();
                  return {url: location.href, title: document.title, text: t.slice(0, 700)};
                })()""",
                "returnByValue": True, "timeout": 8000})
            v = r.get("result", {}).get("value") or {}
        except Exception as exc:
            print(f"[{time.monotonic()-t0:5.1f}s] evaluate 失败: {str(exc)[:90]}")
            time.sleep(2)
            continue
        sig = f"{v.get('url')}|{v.get('text','')[:60]}"
        if sig != last:
            print(f"[{time.monotonic()-t0:5.1f}s] url  = {str(v.get('url'))[:110]}")
            print(f"          title= {str(v.get('title'))[:80]}")
            print(f"          text = {str(v.get('text'))[:420]}")
            last = sig
        time.sleep(2)

    # 截图
    try:
        mid += 1
        shot = cdp(ws, mid, "Page.captureScreenshot", {"format": "png"})
        data = shot.get("data")
        if data:
            out = "/tmp/jd_merchant.png"
            with open(out, "wb") as fh:
                fh.write(base64.b64decode(data))
            print(f"\n截图已保存: {out}")
    except Exception as exc:
        print(f"\n截图失败: {exc}")

    ws.close()


if __name__ == "__main__":
    main()
