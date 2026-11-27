#!/usr/bin/env python3
"""只读探测：京东商品列表「已下架 / 审核」页签的结构与字段。

红线：
- 不点任何写操作（不编辑、不下架、不上架、不提交）。
- 只做一次导航 + 一次页签切换 + 读 DOM，最多 3 次页面交互。
- 结果只写本地 JSON（exports/），不碰任何库表。

用法：/opt/subsidy/venv/bin/python probe_jd_offline_list.py [--port 9433]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

from jd_store_sku_web_collect import Page

BASE = "https://wares-jdm.jd.com/ware/wareList"
ROOT = os.path.dirname(os.path.abspath(__file__))

TABS_JS = r"""(() => {
  const vis = e => e && e.offsetParent;
  const kw = /全部商品|售卖中|已下架|待售|仓库中|审核|违规/;
  const seen = new Set(), out = [];
  document.querySelectorAll('button,[role=tab],li,div,span,a').forEach(e => {
    if (!vis(e)) return;
    const t = (e.innerText || '').trim();
    if (!t || t.length > 40 || !kw.test(t)) return;
    if (e.children.length > 2) return;
    const key = t + '|' + e.tagName;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({tag: e.tagName, text: t, cls: String(e.className).slice(0, 80)});
  });
  const iframes = [...document.querySelectorAll('iframe')].map(f => f.src).slice(0, 10);
  return {title: document.title, url: location.href, ready: document.readyState,
          tabs: out.slice(0, 40), iframes,
          text: ((document.body && document.body.innerText) || '').slice(0, 2500),
          buttons: [...document.querySelectorAll('button')].filter(vis)
                    .map(b => (b.innerText || '').trim()).filter(Boolean).slice(0, 40)};
})()"""

CLICK_JS_TMPL = r"""(() => {
  const want = %s;
  const vis = e => e && e.offsetParent;
  const hit = [...document.querySelectorAll('button,[role=tab],li,div,span,a')]
    .filter(vis)
    .filter(e => (e.innerText || '').trim().split('\n')[0].trim() === want && e.children.length <= 1)
    .sort((a, b) => a.children.length - b.children.length)[0];
  if (!hit) return {clicked: false, want};
  hit.click();
  return {clicked: true, want, text: hit.innerText.trim()};
})()"""

TABLE_JS = r"""(() => {
  const vis = e => e && e.offsetParent;
  const tables = [...document.querySelectorAll('table')].filter(vis)
    .sort((a, b) => b.querySelectorAll('tr').length - a.querySelectorAll('tr').length);
  const t = tables[0];
  const body = ((document.body && document.body.innerText) || '');
  const risk = /601|安全验证|滑块|验证码/.test(body.slice(0, 3000));
  if (!t) return {headers: [], rows: [], risk, text: body.slice(0, 1200)};
  const headers = [...t.querySelectorAll('thead th, thead td')]
    .map(x => (x.innerText || '').trim()).filter(Boolean);
  const rows = [...t.querySelectorAll('tbody tr')].slice(0, 10)
    .map(tr => [...tr.querySelectorAll('td')].map(td => (td.innerText || '').trim().slice(0, 100)));
  return {headers, rows, risk, text: body.slice(0, 800)};
})()"""


def wait_ready(page: Page, seconds: int = 25) -> dict:
    state = {}
    for _ in range(seconds):
        state = page.evaluate(TABS_JS)
        if state.get("ready") == "complete" and state.get("tabs"):
            return state
        time.sleep(1)
    return state


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9433)
    ap.add_argument("--url", default=BASE + "?activeTab=OfflineWare&businessModel=0")
    ap.add_argument("--tab", default="已下架", help="要点击的页签：已下架 / 商品审核 / 回收站")
    args = ap.parse_args()

    page = Page(args.port, url=args.url.split("?")[0], force_new=True)
    try:
        page.call("Page.enable")
        page.call("Page.navigate", {"url": args.url})
        state = wait_ready(page)
        print("标题:", state.get("title"))
        print("页签:", [t["text"] for t in state.get("tabs", [])])

        if state.get("risk"):
            print("检测到风控提示，中止", file=sys.stderr)
            return 2

        clicked = page.evaluate(CLICK_JS_TMPL % json.dumps(args.tab, ensure_ascii=False))
        print("点击页签:", clicked)
        time.sleep(4)
        for _ in range(10):
            table = page.evaluate(TABLE_JS)
            if table.get("rows"):
                break
            time.sleep(1)

        if table.get("risk"):
            print("表格页出现风控提示，中止", file=sys.stderr)
            return 2

        print("表头:", table.get("headers"))
        for row in table.get("rows", [])[:5]:
            print("  行:", row)

        out = {
            "采集时间": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "端口": args.port,
            "页面": state,
            "点击页签": clicked,
            "表格": table,
        }
        path = os.path.join(ROOT, "exports", "probe_jd_offline_%s.json" % dt.datetime.now().strftime("%Y%m%d_%H%M"))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print("已保存:", path)
        return 0 if table.get("rows") else 1
    finally:
        page.close()


if __name__ == "__main__":
    raise SystemExit(main())
