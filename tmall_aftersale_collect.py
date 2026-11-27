#!/usr/bin/env python3
"""天猫售后全量明细采集（含历史退款，自动翻完所有页）。

在 Windows 147 采集端运行：附着已打开且已设好「快捷查询=全部 + 售后状态=全部」
的退款管理页，自动点「下一页」直到末页，抓每页 disputelistv2 响应，按售后单号
去重后上传 http://192.168.3.150:8765/upload/tmall-aftersale-all.json。

用法：
  .\\.venv\\Scripts\\python.exe tmall_aftersale_collect.py --port 9633
"""
from __future__ import annotations

import argparse
import base64
import json
import pathlib
import re
import time
from urllib.parse import quote

import requests

try:
    import websocket
except ImportError:
    print("MISSING websocket-client; .venv\\Scripts\\python.exe -m pip install websocket-client")
    raise SystemExit(2)

DEFAULT_URL = "https://myseller.taobao.com/home.htm/trade-platform/refund-list"
HINTS = ("refund-list", "myseller.taobao.com")
API = "mtop.alibaba.refundface2.disputeservice.qianniu.pc.disputelistv2"
BRIDGE_BASE = "http://192.168.3.150:8765/upload/"
OUT = "tmall_aftersale_all.json"


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=45, suppress_origin=True)
        self.mid = 0
        self.requests: dict[str, dict] = {}

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
            if rid and req.get("url", "").startswith("http"):
                self.requests[rid] = {"url": req.get("url", ""), "method": req.get("method", "GET")}
        elif m == "Network.responseReceived":
            rid = p.get("requestId")
            if rid in self.requests:
                self.requests[rid]["status"] = (p.get("response") or {}).get("status")

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

    def pump(self, sec, capture_api=None):
        end = time.time() + sec
        self.ws.settimeout(1.0)
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            self._handle(msg)
            if capture_api:
                for rid, e in list(self.requests.items()):
                    if capture_api in e.get("url", "") and "body" not in e:
                        try:
                            b = self.body(rid)
                            if b:
                                e["body"] = b
                        except Exception:
                            pass

    def js(self, expr, timeout=10):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True}, timeout=timeout)
        return ((r.get("result") or {}).get("value"))

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


SET_FILTER_JS = r"""
(() => {
  const vis = e => { const r=e.getBoundingClientRect(); return r.width>0 && r.height>0; };
  const clean = e => (e.innerText || e.textContent || '').replace(/\s+/g, '');
  const log = [];
  // 1) 快捷查询=全部：在 label 文本含「快捷查询」的容器内，找文本恰为「全部」的按钮
  const quick = [...document.querySelectorAll('div,section,span')].find(e =>
    clean(e).includes('快捷查询') && vis(e) && e.querySelectorAll('*').length < 40);
  let qroot = quick ? (quick.parentElement || quick) : document;
  const allBtn = [...qroot.querySelectorAll('button,span,a,li,label,div')].find(e =>
    clean(e) === '全部' && vis(e));
  if (allBtn) { allBtn.click(); log.push('quick-all'); }
  // 2) 售后状态=全部：找「售后状态」标签，点开后面的下拉，选文本恰为「全部」的项
  const slabel = [...document.querySelectorAll('label,span,div')].find(e =>
    clean(e).replace('：','').replace(':','').includes('售后状态') && vis(e) && clean(e).length < 12);
  if (slabel) {
    const box = slabel.parentElement || slabel;
    const trigger = [...box.querySelectorAll('input,div,span')].find(e => vis(e) &&
      (e.tagName==='INPUT' || clean(e).includes('已选择') || clean(e).includes('全部')));
    if (trigger) { trigger.click(); log.push('status-open'); }
    const opt = [...document.querySelectorAll('li,label,span,div')].find(e =>
      clean(e) === '全部' && vis(e) && e.closest('.jd-select-dropdown,.el-select-dropdown,[role=listbox]'));
    if (opt) { opt.click(); log.push('status-all'); }
  }
  return log.join(',') || 'nothing';
})()
"""

SEARCH_JS = r"""
(() => {
  const cand = [...document.querySelectorAll('button,a,span,div,input')].filter(e => {
    const t = ((e.innerText || '') + ' ' + (e.value || '')).replace(/\s+/g, '');
    const r = e.getBoundingClientRect();
    return t === '搜索售后单' && r.width > 0 && r.height > 0;
  });
  if (!cand.length) return 'none';
  cand[0].click();
  return 'clicked';
})()
"""

NEXT_JS = r"""
(() => {
  const cand = [...document.querySelectorAll('a,button,li,span,div')].filter(e => {
    const t = (e.innerText || e.textContent || '').replace(/\s+/g, '');
    const cls = String(e.className || '');
    const r = e.getBoundingClientRect();
    return t.includes('下一页') && r.width > 0 && r.height > 0 && !cls.includes('disabled');
  });
  if (!cand.length) return 'none';
  cand[cand.length - 1].click();
  return 'clicked';
})()
"""

PAGE_TEXT_JS = r"""
(() => { const t = document.body.innerText || '';
  const m = t.match(/(\d+)\/(\d+)/);
  return m ? m[0] : '';
})()
"""


def parse_row(raw: dict) -> dict:
    h = raw.get("disputeHeaderVO") or {}
    b = raw.get("disputeBodyVO") or {}
    nums = {}
    for n in h.get("numbers") or []:
        title = re.sub(r"\s+", "", str(n.get("title") or ""))
        nums[title] = n.get("value")
    item = b.get("itemInfo") or {}
    amt = b.get("amountInfo") or {}
    st = b.get("statusInfo") or {}
    reason = b.get("reasonInfo") or {}
    scenario = h.get("disputeScenario") or {}
    shop = h.get("shop") or {}
    return {
        "售后单号": raw.get("disputeId") or nums.get("售后单号"),
        "订单编号": nums.get("订单编号") or raw.get("mainOrderId"),
        "申请时间": nums.get("申请时间"),
        "售后类型": scenario.get("text"),
        "纠纷类型": (st.get("disputeType") or {}).get("text"),
        "退款状态": st.get("refundStatus"),
        "客服状态": st.get("csStatus"),
        "协商状态": st.get("negotiateStatus"),
        "商品标题": item.get("title"),
        "SKU": item.get("sku"),
        "条码": item.get("itemOuterId"),
        "退款金额": amt.get("refundFee"),
        "买家实付": amt.get("buyerActualFee"),
        "退款原因": reason.get("refundReason"),
        "买家": shop.get("name"),
        "审核状态": "未审核" if "未审核" in str((raw.get("reviewedLinkVO") or {}).get("reviewed")) else str((raw.get("reviewedLinkVO") or {}).get("reviewed") or "")[:40],
    }


def extract(payload: dict) -> tuple[list[dict], dict]:
    data = payload.get("data") or {}
    rd = data.get("resultData") or {}
    grid = rd.get("disputeGridInfoVO") or {}
    rows = [parse_row(x) for x in (grid.get("disputeGridVOList") or []) if isinstance(x, dict)]
    return rows, grid.get("paginationVO") or {}


def collect_bodies(cdp, by_id=None):
    """增量收集：只把能取到 body 的页写进 by_id，取不到的页不影响已收集数据。"""
    if by_id is None:
        by_id = {}
    last_page = {}
    for rid, e in list(cdp.requests.items()):
        if API not in e.get("url", ""):
            continue
        body = e.get("body") or ""
        if not body:
            continue
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        rows, page = extract(payload)
        if page:
            last_page = page
        for r in rows:
            key = str(r.get("售后单号") or "")
            if key:
                by_id[key] = r
    return by_id, last_page


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9633)
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--max-pages", type=int, default=120)
    ap.add_argument("--settle", type=float, default=8.0)
    ap.add_argument("--wait", type=float, default=2.5, help="每页翻页后等待秒数")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--upload-name", default="tmall-aftersale-all.json",
                    help="桥接上传文件名；增量任务用 tmall-aftersale-daily.json")
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
    print("attach_mode:", mode, "|", str(target.get("url"))[:100], flush=True)

    cdp = CDP(target["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Network.enable", {"maxTotalBufferSize": 268435456, "maxResourceBufferSize": 134217728})
    cdp.call("Runtime.enable")
    cdp.pump(0.5)

    if mode == "fresh-tab":
        print("新标签页已打开。请在页面设好「快捷查询=全部 + 售后状态=全部」后重跑本脚本。", flush=True)
        cdp.close()
        return

    # 主动点「搜索售后单」，用当前筛选（全部+全部）触发第 1 页请求
    search_result = cdp.js(SEARCH_JS)
    print("点击搜索售后单:", search_result, flush=True)
    cdp.pump(args.settle, capture_api=API)
    by_id, page = collect_bodies(cdp, {})
    total = int(page.get("total") or 0)
    print("初始页：累计 %d 条，分页 total=%s" % (len(by_id), total), flush=True)

    stale = 0
    for i in range(args.max_pages):
        before = len(by_id)
        result = cdp.js(NEXT_JS)
        if result == "none":
            print("没有「下一页」按钮，停止", flush=True)
            break
        cdp.pump(args.wait, capture_api=API)
        by_id, page = collect_bodies(cdp, by_id)
        after = len(by_id)
        print("  第 %d 次翻页: %s, 累计 %d 条" % (i + 1, result, after), flush=True)
        if after == before:
            stale += 1
            if stale >= 5:
                print("连续 5 页无新增，停止", flush=True)
                break
        else:
            stale = 0
        if total and after >= total:
            print("已抓满 total=%d，停止翻页" % total, flush=True)
            break

    records = list(by_id.values())
    records.sort(key=lambda x: str(x.get("申请时间") or ""), reverse=True)
    payload_out = {
        "platform": "天猫", "store": "保民", "source": API,
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pages_captured": len([1 for e in cdp.requests.values()
                               if API in e.get("url", "") and e.get("body")]),
        "count": len(records), "rows": records,
    }
    out = pathlib.Path(args.out)
    out.write_text(json.dumps(payload_out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("完成：接口页 %d，唯一售后单 %d -> %s" %
          (payload_out["pages_captured"], len(records), out), flush=True)
    try:
        resp = requests.put(BRIDGE_BASE + args.upload_name, data=out.read_bytes(), timeout=60)
        print("upload:", resp.status_code, resp.text[:200], flush=True)
    except Exception as ex:
        print("upload failed:", ex, flush=True)
    cdp.close()


if __name__ == "__main__":
    main()
