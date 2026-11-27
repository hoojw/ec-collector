#!/usr/bin/env python3
"""Collect Meituan subsidy lists (subsidized/unsubsidized/unlisted) per store via CDP DOM.

Reads the rendered roo-tableNew tables inside the subsidy SPA iframe.
No signed requests, no headers/tokens persisted.
"""
from __future__ import annotations
import argparse, datetime as dt, json, re, time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests, websocket

TZ = ZoneInfo("Asia/Shanghai")
TARGET_URL = "https://yiyao.meituan.com/main/frame#/pages/health/marketing/low_price_campaign/list"
# (key, 页签文字, 该页签表头的唯一标识列)
TAB_SPECS = [
    ("subsidized",    "已补贴", "补贴生效范围"),
    ("unsubsidized",  "未补贴", "未补贴原因"),
    ("unlisted",      "未建品", "建议定价"),
]

class CdpPage:
    def __init__(self, port: int):
        self.base = f"http://127.0.0.1:{port}"
        url = quote(TARGET_URL, safe='')
        r = requests.put(f"{self.base}/json/new?{url}", timeout=15)
        if r.status_code == 405:
            r = requests.get(f"{self.base}/json/new?{url}", timeout=15)
        r.raise_for_status()
        t = r.json()
        self.tab_id = t["id"]
        self.ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=90)
        self.mid = 0

    def close(self):
        try:
            self.ws.close()
        finally:
            try:
                requests.get(f"{self.base}/json/close/{self.tab_id}", timeout=10)
            except Exception:
                pass

    def call(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") != self.mid:
                continue
            if "error" in m:
                raise RuntimeError(m["error"])
            return m.get("result", {})

    def ev(self, expression):
        r = self.call("Runtime.evaluate", {
            "expression": expression, "awaitPromise": True,
            "returnByValue": True, "timeout": 60000})
        remote = r.get("result", {})
        if "value" not in remote:
            raise RuntimeError(remote.get("description") or str(remote))
        return remote["value"]

FRAME_JS = r"""
  const f = Array.from(document.querySelectorAll('iframe')).find(x =>
     x.contentDocument && x.contentDocument.body &&
     (x.contentDocument.body.innerText||'').includes('补贴商品'));
"""

READ_JS = r"""
(() => {
  const TAB=__TAB__, MARK=__MARK__;
""" + FRAME_JS + r"""
  if(!f) return {ready:false, reason:'no iframe'};
  const d=f.contentDocument;
  const tabs=Array.from(d.querySelectorAll('.tab-text'));
  const hit=tabs.find(e=>(e.textContent||'').includes(TAB));
  if(!hit) return {ready:false, reason:'tab not found'};
  const tables=Array.from(d.querySelectorAll('table'));
  const ht=tables.find(t=>{const tr=t.querySelector('tr');
    return tr&&Array.from(tr.querySelectorAll('th,td')).some(c=>(c.textContent||'').includes('补贴商品'));});
  const head=ht?Array.from(ht.querySelector('tr').querySelectorAll('th,td'))
                 .map(c=>(c.textContent||'').trim()):[];
  // 表头标志不符 => 点击目标页签, 下一轮再读
  if(head.indexOf(MARK)<0){hit.click();return {ready:false,reason:'switching',head:head};}
  // 数据行在另一个 table (roo-tableNew 固定列分离结构)
  const dt=tables.find(t=>t.querySelectorAll('tr').length>1 &&
    /UPC|补贴中|建议定价|未补贴原因/.test(t.textContent||''));
  if(!dt) return {ready:false, reason:'no data table', head:head};
  const rows=Array.from(dt.querySelectorAll('tr'))
     .map(tr=>Array.from(tr.querySelectorAll('th,td')).map(c=>(c.textContent||'').trim()))
     .filter(r=>r.some(c=>c));
  const pag=d.querySelector('.roo-pagination');
  let total=0, current=1;
  if(pag){
    const m=(pag.textContent||'').match(/共(\d+)条/); if(m) total=parseInt(m[1]);
    const cur=pag.querySelector('li.active a, li.active');
    if(cur && parseInt((cur.textContent||'').trim())>0) current=parseInt(cur.textContent.trim());
  }
  return {ready:true, head:head, rows:rows, total:total, current:current};
})()
"""

NEXT_JS = r"""
(() => {
""" + FRAME_JS + r"""
  if(!f) return false;
  const pag=f.contentDocument.querySelector('.roo-pagination');
  if(!pag) return false;
  const li=Array.from(pag.querySelectorAll('li.arrow'))
             .find(x=>!/disabled/.test(x.className||''));
  const a=li&&li.querySelector('a[aria-label="Next"]');
  if(!a) return false;
  a.click(); return true;
})()
"""

def dec(s):
    if s is None:
        return None
    m = re.search(r'-?[\d,]+(?:\.\d+)?', str(s).replace('¥', '').replace('￥', ''))
    if not m:
        return None
    try:
        return Decimal(m.group().replace(',', ''))
    except InvalidOperation:
        return None

def _read(page, tab, marker, expect=None, timeout=40):
    expr = (READ_JS.replace('__TAB__', json.dumps(tab, ensure_ascii=False))
                   .replace('__MARK__', json.dumps(marker, ensure_ascii=False)))
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = page.ev(expr)
        if last.get('ready') and (expect is None or last.get('current') == expect):
            return last
        time.sleep(1.5)
    raise RuntimeError(f'tab[{tab}] not ready (expect page {expect}): {str(last)[:300]}')

def collect_tab(page, key, tab, marker, max_pages=80):
    r = _read(page, tab, marker)
    head, total, cur = r['head'], r['total'], r['current']
    data, seen_pages = [], set()
    old_first = tuple(r['rows'][0]) if r.get('rows') else None
    while cur not in seen_pages:
        seen_pages.add(cur)
        data.extend([row for row in r['rows'] if any(c for c in row)])
        if len(seen_pages) >= max_pages:
            break
        if not page.ev(NEXT_JS):
            break
        nxt = cur + 1
        try:
            r = _read(page, tab, marker, expect=nxt)
        except RuntimeError:
            break
        # 页码先于数据刷新, 等首行变化再认定新页数据已就绪
        deadline = time.time() + 25
        while (not r.get('rows') or tuple(r['rows'][0]) == old_first) and time.time() < deadline:
            time.sleep(1)
            try:
                r = _read(page, tab, marker, expect=nxt)
            except RuntimeError:
                break
        cur = r.get('current', nxt)
        old_first = tuple(r['rows'][0]) if r.get('rows') else None
    uniq, seen = [], set()
    for row in data:
        k = tuple(row)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(row)
    return {"key": key, "tab": tab, "head": head, "total": total,
            "pages": len(seen_pages), "rows": uniq, "collected": len(uniq)}

CAT_RE = re.compile(r'^\s*(处方药|OTC|非药品|保健品|医疗器械|中药饮片)')

def parse_product(cell):
    txt = (cell or '').strip()
    m = re.search(r'UPC:\s*(\d+)', txt)
    upc = m.group(1) if m else None
    head = txt[:m.start()] if m else txt
    cat = None
    cm = CAT_RE.match(head)
    if cm:
        cat, head = cm.group(1), head[cm.end():]
    brand = None
    bm = re.match(r'\s*\[([^\]]+)\]', head)
    if bm:
        brand, head = bm.group(1), head[bm.end():]
    name = re.split(r'\s*(?:货号|SKUID|商品类目)\s*:', head)[0].strip().strip('[] ')
    sm = re.search(r'SKUID:\s*(\d+)', txt)
    full = ('[' + brand + ']' if brand else '') + name
    return cat, full, upc, (sm.group(1) if sm else None)

def parse_price(text, key):
    text = text or ''
    if key == 'subsidized':
        a = re.search(r'补贴后为\s*([\d.]+)\s*元/件', text)
        s = re.search(r'每件\s*([\d.]+)\s*元补贴', text) or re.search(r'减\s*([\d.]+)\s*元', text)
        return {"补贴后价": dec(a.group(1) if a else None),
                "补贴金额": dec(s.group(1) if s else None)}
    if key == 'unsubsidized':
        t = re.search(r'补贴门槛:\s*¥?\s*([\d.]+)', text)
        p = re.search(r'我的价格:\s*¥?\s*([\d.]+)', text)
        d = re.search(r'至少再降\s*¥?\s*([\d.]+)', text)
        return {"补贴门槛": dec(t.group(1) if t else None),
                "我的价格": dec(p.group(1) if p else None),
                "至少再降": dec(d.group(1) if d else None)}
    e = re.search(r'预估每件补贴\s*([\d.]+)\s*元', text)
    return {"预估补贴": dec(e.group(1) if e else None)}

def parse_rows(res, client, day):
    head, key, rows = res['head'], res['key'], res['rows']
    out = []
    for row in rows:
        d = dict(zip(head, row))
        cat, name, upc, sku = parse_product(d.get('补贴商品', ''))
        rec = {"店铺": client, "客户": client, "数据日期": day, "表": key,
               "类别": cat, "商品": name, "UPC": upc, "SKUID": sku,
               "活动类型": d.get('活动类型'), "补贴状态": d.get('补贴状态'),
               "补贴生效范围": d.get('补贴生效范围'),
               "未补贴原因": d.get('未补贴原因'),
               "活动时间": d.get('活动时间'), "撤销补贴时间": d.get('撤销补贴时间'),
               "建议定价": dec(d.get('建议定价')) if key == 'unlisted' else None,
               "操作": d.get('操作'), "原始补贴收益": d.get('补贴收益'),
               "价格参考": d.get('价格参考')}
        # 未补贴的价格三要素在"价格参考"列, 其余表在"补贴收益"列
        rec.update(parse_price(d.get('价格参考') if key == 'unsubsidized' else d.get('补贴收益'), key))
        out.append(rec)
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, required=True)
    ap.add_argument('--client', default='保民')
    ap.add_argument('--date', default=dt.datetime.now(TZ).strftime('%Y-%m-%d'))
    ap.add_argument('--max-pages', type=int, default=80)
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    page = CdpPage(args.port)
    result = {"client": args.client, "date": args.date,
              "collected_at": dt.datetime.now(TZ).isoformat(), "tabs": {}}
    try:
        for key, tab, marker in TAB_SPECS:
            try:
                res = collect_tab(page, key, tab, marker, args.max_pages)
                recs = parse_rows(res, args.client, args.date)
                result['tabs'][key] = {"total": res['total'], "collected": res['collected'],
                                       "pages": res['pages'], "head": res['head'],
                                       "records": recs}
            except Exception as e:
                result['tabs'][key] = {"error": str(e)[:400]}
    finally:
        page.close()

    if args.dry_run:
        brief = {"client": result['client'], "date": result['date'],
                 "collected_at": result['collected_at'], "tabs": {}}
        for k, v in result['tabs'].items():
            if 'error' in v:
                brief['tabs'][k] = v
            else:
                b = {x: v[x] for x in ('total', 'collected', 'pages', 'head')}
                b['sample'] = v['records'][:2]
                brief['tabs'][k] = b
        print(json.dumps(brief, ensure_ascii=False, indent=2, default=str)[:24000])
    else:
        print(json.dumps(result, ensure_ascii=False, default=str)[:20000])
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
