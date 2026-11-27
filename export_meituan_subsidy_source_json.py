#!/usr/bin/env python3
"""Build a self-explanatory JSON source file for stakeholder review.

Contains: data-source description, why fields are now visible, field mapping,
and RAW API records (unmodified) so the numbers can be traced to the source.
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, time
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests, websocket

TZ = ZoneInfo("Asia/Shanghai")
TARGET_URL = (
    "https://yiyao.meituan.com/main/frame#/pages/health/marketing/"
    "low_price_campaign/list"
)
LIST_API = "/api/health/activity/apply/b/activity/apply/lst/sku/list?source=1"
FILTERS = {"已补贴": 1, "未补贴": 0, "未建品": 2}
# 截图里出现的 UPC -> 说明（用于定位老板看到的那些行）
FOCUS_UPC = {
    "6900233282201": "卡博平 9.7（截图第1行）",
    "6975026065552": "华堂宁 141.9（截图第10行）",
    "9999900936650": "穆峰达替尔泊肽 811.9（截图倒数第2行）",
    "9999901019924": "信尔美玛仕度肽 727.2（截图最后1行）",
}
FIELD_MAP = {
    "原价": "orgPrice",
    "当前售价": "currentPrice",
    "补贴门槛": "minDiscountPrice",
    "补贴后价": "discountPrice",
    "单件补贴": "mtPrice",
    "商家承担": "poiCharge",
    "门槛盒数": "levelItemNumber",
}


class Cdp:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        r = requests.put(f"{self.base}/json/new?{quote(TARGET_URL, safe='')}", timeout=15)
        if r.status_code == 405:
            r = requests.get(f"{self.base}/json/new?{quote(TARGET_URL, safe='')}", timeout=15)
        r.raise_for_status()
        t = r.json()
        self.id = t["id"]
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=60)
        self.mid = 0
        time.sleep(6)

    def ev(self, expr):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True, "timeout": 60000}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") != self.mid:
                continue
            if "error" in m:
                raise RuntimeError(m["error"])
            return m["result"]["result"].get("value")

    def close(self):
        try:
            self.ws.close()
        finally:
            try:
                requests.get(f"{self.base}/json/close/{self.id}", timeout=10)
            except Exception:
                pass


def fetch(c, ft, page_no, size=100):
    body = json.dumps({"sortType": 1, "filterType": ft, "sortField": 40,
        "actTypes": [17, 29], "medicineTypeList": [],
        "pageNo": page_no, "pageSize": size}, ensure_ascii=False, separators=(",", ":"))
    expr = ("(async()=>{const r=await fetch(%s,{method:'POST',credentials:'include',"
            "headers:{'Content-Type':'application/json'},body:%s});return await r.text();})()"
            % (json.dumps(LIST_API), json.dumps(body)))
    return json.loads(c.ev(expr))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--client", default="保民")
    ap.add_argument("--out", required=True)
    ap.add_argument("--sample-size", type=int, default=10)
    args = ap.parse_args()

    c = Cdp(args.port)
    raw = {}
    try:
        for label, ft in FILTERS.items():
            payload = fetch(c, ft, 1, max(100, args.sample_size))
            data = payload.get("data") or {}
            records = data.get("data") or []
            raw[label] = {
                "首页返回总数_total": data.get("total"),
                "本文件保留的原始记录条数": min(len(records), args.sample_size),
                "原始记录": records[:args.sample_size],
            }
    finally:
        c.close()

    # 从已补贴原始记录中挑出截图 UPC，单独列证
    focus = []
    for rec in raw.get("已补贴", {}).get("原始记录", []):
        upc = str(rec.get("upcCode") or "")
        if upc in FOCUS_UPC:
            focus.append({
                "UPC": upc,
                "定位": FOCUS_UPC[upc],
                "商品": rec.get("skuName"),
                "字段原值": {excel: rec.get(api) for excel, api in FIELD_MAP.items()},
            })

    doc = {
        "_文件用途": "给业务方对需求用：展示美团补贴数据的来源、字段来源，以及为何页面上看不到价格但数据里有。",
        "生成时间": dt.datetime.now(TZ).isoformat(),
        "店铺": args.client,
        "数据源": {
            "业务页面": TARGET_URL,
            "数据接口": LIST_API,
            "请求方式": "POST（由上述页面自身加载列表时发起，复用浏览器已登录会话，非爬虫伪造）",
            "请求参数说明": {
                "filterType": "1=已补贴，0=未补贴，2=未建品（对应页面三个页签）",
                "actTypes": "[17=单盒补贴, 29=多盒补贴]",
                "pageNo/pageSize": "分页；采集用 pageSize=100",
                "sortType/sortField/medicineTypeList": "页面默认排序与筛选",
            },
            "抓取方式": "在已登录的店铺浏览器里执行 fetch，读取接口返回的原始 JSON；不修改、不推算数值。",
        },
        "为什么之前看不到、现在能看到": (
            "页面上每个商品只渲染成『补贴中』三个字和一句活动话术，"
            "接口原始 JSON 里其实带有 orgPrice/currentPrice/minDiscountPrice/"
            "discountPrice/mtPrice 等价格字段，只是前端没有展示成人眼可见的列。"
            "本数据直接取接口原始字段，所以能看到页面上不显示的价格。"
            "数值未做任何加工或编造，可与下方『原始记录』逐字段核对。"
        ),
        "字段映射_Excel列_对应接口字段": FIELD_MAP,
        "截图行的字段原值对照": focus,
        "原始接口记录": raw,
    }
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"out": str(out), "bytes": out.stat().st_size,
        "focus_matched": len(focus),
        "counts": {k: v["首页返回总数_total"] for k, v in raw.items()}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
