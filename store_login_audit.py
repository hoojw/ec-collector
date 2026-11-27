#!/usr/bin/env python3
"""只读巡检：逐个店铺/实例检查浏览器登录态。不触发任何采集或写库。"""

from __future__ import annotations

import json
import subprocess
import time

import requests
import websocket

# 实例 -> (端口, 期望站点)
MEITUAN = {
    "baientang-E": (9360, "百恩堂(快递电商)"),
    "baomin-E": (9333, "保民(快递电商)"),
    "fukan-E": (9351, "福康综合门诊(快递电商)"),
    "nanwu-E": (9341, "津南南无门诊(快递电商)"),
    "baientang-prescription": (9361, "百恩堂(处方)"),
    "baomin-prescription": (9362, "保民(处方)"),
    "hean-prescription": (9350, "hean(处方)"),
    "nanwu-prescription": (9364, "津南南无门诊(处方)"),
}

LOGIN_HINTS = ("authme", "passport", "login", "sso", "unifiedlogin")
RISK_TEXT = ("请登录", "请先登录", "扫码登录", "验证码", "安全验证", "异常流量", "访问受限", "账号拦截")

RESULTS: list[dict] = []


def sh(cmd: str) -> str:
    return subprocess.run(["bash", "-lc", cmd], capture_output=True, text=True).stdout.strip()


def cdp_call(ws, mid, method, params=None):
    ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") != mid:
            continue
        if msg.get("error"):
            raise RuntimeError(msg["error"])
        return msg.get("result", {})


def wait_cdp(port: int, seconds: int = 50) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            requests.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
            return True
        except Exception:
            time.sleep(2)
    return False


def probe(port: int) -> dict:
    """读取该端口第一个 page target 的登录相关状态。"""
    targets = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=8).json()
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        return {"ok": None, "reason": "无 page target"}
    page = pages[0]
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20)
    try:
        mid = 1
        cdp_call(ws, mid, "Runtime.enable")
        mid += 1
        expr = r"""(() => {
          const text = (document.body && document.body.innerText) || '';
          const url = location.href;
          return {
            url: url,
            title: document.title,
            head: text.replace(/\s+/g, ' ').slice(0, 240),
            blank: url === 'about:blank' && !text.trim()
          };
        })()"""
        r = cdp_call(ws, mid, "Runtime.evaluate", {"expression": expr, "returnByValue": True, "timeout": 8000})
        v = r.get("result", {}).get("value") or {}
    finally:
        ws.close()

    if v.get("blank"):
        return {"ok": None, "reason": "未启动过(about:blank)", **v}
    url_l = str(v.get("url", "")).lower()
    if any(h in url_l for h in LOGIN_HINTS):
        return {"ok": False, "reason": "被重定向到登录页", **v}
    if any(m in str(v.get("head", "")) for m in RISK_TEXT):
        return {"ok": False, "reason": "页面出现登录/风控提示", **v}
    return {"ok": True, "reason": "已登录", **v}


def audit_meituan(name: str, port: int, label: str) -> None:
    unit = f"meituan-store-chrome@{name}"
    started = False
    if sh(f"systemctl is-active {unit} 2>/dev/null") != "active":
        sh(f"systemctl start {unit}")
        started = True
    ready = wait_cdp(port)
    if not ready:
        RESULTS.append({"group": "美团", "store": label, "instance": name, "port": port,
                        "login": "未知", "detail": "CDP 端口未就绪"})
        if started:
            sh(f"systemctl stop {unit}")
        return
    try:
        info = probe(port)
    except Exception as exc:
        info = {"ok": None, "reason": f"{type(exc).__name__}: {exc}"}
    login = {True: "已登录", False: "未登录", None: "未知"}[info.get("ok")]
    RESULTS.append({"group": "美团", "store": label, "instance": name, "port": port,
                    "login": login, "detail": info.get("reason"),
                    "title": str(info.get("title"))[:40], "url": str(info.get("url"))[:70],
                    "head": str(info.get("head"))[:100], "we_started": started})
    print(f"  [{login}] {label:<22} {name:<24} {info.get('reason')}")
    print(f"        url   = {str(info.get('url'))[:100]}")
    print(f"        title = {str(info.get('title'))[:70]}")
    print(f"        text  = {str(info.get('head'))[:180]}")
    if started:
        sh(f"systemctl stop {unit}")
        print("        (已停止本次临时启动的浏览器)")


if __name__ == "__main__":
    print("=" * 78)
    print("美团各实例登录态")
    print("=" * 78)
    for name, (port, label) in MEITUAN.items():
        audit_meituan(name, port, label)

    print()
    print("=" * 78)
    print("京东 (共享 JoySpace profile, CDP 9533)")
    print("=" * 78)
    try:
        targets = requests.get("http://127.0.0.1:9533/json/list", timeout=8).json()
        for t in targets:
            if t.get("type") != "page":
                continue
            print(f"  - {str(t.get('url'))[:95]}")
            print(f"    title = {str(t.get('title'))[:70]}")
    except Exception as exc:
        print(f"  CDP 9533 不可达: {exc}")
    try:
        info = probe(9533)
        print(f"  首个页面判定: {info.get('reason')}")
    except Exception as exc:
        print(f"  probe 失败: {exc}")

    print()
    print("=" * 78)
    print("汇总")
    print("=" * 78)
    for r in RESULTS:
        print(f"  {r['login']:<6} {r['group']:<4} {r['store']:<22} {r['instance']:<24} {r['detail']}")
    print(json.dumps({"results": RESULTS}, ensure_ascii=False))
