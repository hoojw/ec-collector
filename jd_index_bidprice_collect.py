#!/usr/bin/env python3
"""Collect 竞价详情 (lowest competitor price) for each 我的竞价 item via CDP.

For every row in the JD index-trade 我的竞价 list, click its "竞价详情" button
so the page issues getIndexWareTrade, capture sellerTradeInfo, take the minimum
price, and write it back to jd_index_quotation_daily.竞价最低价.
"""
from __future__ import annotations
import argparse, json, time, base64, pathlib
from urllib.parse import urlparse, parse_qs
import requests, websocket, pymysql

INDEX_URL = "https://shop.jd.com/jdm/cz/index-ware?mainType=canJoinWare"


class CDP:
    def __init__(self, ws):
        self.ws = websocket.create_connection(ws, timeout=40, suppress_origin=True)
        self.mid = 0; self.pending = {}; self.bodies = {}
    def send(self, m, p=None):
        self.mid += 1; self.ws.send(json.dumps({"id": self.mid, "method": m, "params": p or {}})); return self.mid
    def handle(self, msg):
        m = msg.get("method"); p = msg.get("params") or {}
        if m == "Network.requestWillBeSent":
            u = (p.get("request") or {}).get("url", "")
            if "getIndexWareTrade" in u:
                api = parse_qs(urlparse(u).query).get("api", [""])[0]
                post = (p.get("request") or {}).get("postData")
                isk = None
                try: isk = json.loads(post)["request"]["indexSkuId"]
                except Exception: pass
                self.pending[p["requestId"]] = isk
    def call(self, m, p=None, timeout=12):
        my = self.send(m, p); end = time.time()+timeout; self.ws.settimeout(1.0)
        while time.time() < end:
            try: msg = json.loads(self.ws.recv())
            except Exception: continue
            if msg.get("id") == my: return msg.get("result") or {}
            self.handle(msg)
        return {}
    def pump(self, sec):
        end = time.time()+sec; self.ws.settimeout(1.0)
        while time.time() < end:
            try: msg = json.loads(self.ws.recv())
            except Exception: continue
            self.handle(msg)
    def ev(self, e):
        r = self.call("Runtime.evaluate", {"expression": e, "returnByValue": True})
        if r.get("exceptionDetails"): return None
        return (r.get("result") or {}).get("value")
    def drain(self):
        out = {}
        for rid, isk in list(self.pending.items()):
            r = self.call("Network.getResponseBody", {"requestId": rid})
            body = r.get("body") or ""
            if r.get("base64Encoded"):
                try: body = base64.b64decode(body).decode("utf-8", "replace")
                except Exception: pass
            if isk and body:
                try:
                    d = json.loads(body); trades = (d.get("data") or {}).get("sellerTradeInfo") or []
                    prices = [float(t["price"]) for t in trades if t.get("price")]
                    if prices: out[isk] = min(prices)
                except Exception: pass
        self.pending.clear()
        return out
    def close(self):
        try: self.ws.close()
        except Exception: pass


PAGE_SIZE_JS = """(function(){
  var els=document.querySelectorAll('div,span,li,a');
  for(var i=0;i<els.length;i++){var el=els[i];if((el.textContent||'').trim()==='50条/页'&&el.children.length===0){el.click();return 'ok';}}
  return 'no';
})()"""

NEXT_JS = """(function(){
  var els=document.querySelectorAll('button,a,li,span,div');
  function act(el){var c=(el.className||'').toString();return !/disabled|is-disabled/i.test(c)&&el.getAttribute('aria-disabled')!=='true';}
  for(var i=0;i<els.length;i++){var el=els[i];var a=(el.getAttribute('aria-label')||'')+' '+(el.getAttribute('title')||'');
    if(/下一页|next page|^next$/i.test(a)&&act(el)){el.click();return 'ok';}}
  for(var i=0;i<els.length;i++){var el=els[i];if((el.textContent||'').trim()==='下一页'&&act(el)){el.click();return 'ok';}}
  return 'no';
})()"""

# click nth visible 竞价详情 button
def click_detail_js(n):
    return """(function(){
      var els=[];document.querySelectorAll('span,p,div,button').forEach(function(el){
        if((el.textContent||'').trim()==='竞价详情'&&el.children.length===0)els.push(el);});
      var r=el=>el.getBoundingClientRect();
      els=els.filter(function(e){var b=r(e);return b.width>0&&b.height>0;});
      if(els.length>%d){els[%d].click();return 'ok';}
      return 'no:'+els.length;
    })()""" % (n, n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/subsidy/config.json")
    ap.add_argument("--port", type=int, default=9433)
    ap.add_argument("--day", default="2026-11-24")
    ap.add_argument("--out", default="/opt/subsidy/exports/jd_index_bidprice_20261124.json")
    args = ap.parse_args()

    base = "http://127.0.0.1:%d" % args.port
    tabs = requests.get(base+"/json/list", timeout=10).json()
    t = next((x for x in tabs if x.get("type")=="page" and "index-ware" in str(x.get("url"))), None)
    if t is None:
        from urllib.parse import quote
        r = requests.put(base+"/json/new?"+quote(INDEX_URL, safe=""), timeout=20)
        if r.status_code==405: r = requests.get(base+"/json/new?"+quote(INDEX_URL, safe=""), timeout=20)
        t = r.json(); time.sleep(12)

    cdp = CDP(t["webSocketDebuggerUrl"])
    cdp.call("Page.enable"); cdp.call("Network.enable",{"maxTotalBufferSize":268435456}); cdp.call("Runtime.enable")
    # go to 我的竞价
    cdp.ev("(function(){var e=document.querySelectorAll('div,span,li');for(var i=0;i<e.length;i++){if((e[i].textContent||'').trim()==='我的竞价'&&e[i].children.length===0){e[i].click();return 1}}return 0})()")
    cdp.pump(3)
    print("page size 50:", cdp.ev(PAGE_SIZE_JS))
    cdp.pump(2)

    lowmap = {}
    max_pages = 10
    for page in range(1, max_pages+1):
        nbtn = cdp.ev("(function(){var n=0;document.querySelectorAll('span').forEach(function(el){var b=el.getBoundingClientRect();if((el.textContent||'').trim()==='竞价详情'&&b.width>0&&el.children.length===0)n++;});return n;})()")
        print("page",page,"竞价详情 buttons",nbtn)
        if not nbtn: break
        for i in range(int(nbtn)):
            r = cdp.ev(click_detail_js(i))
            cdp.pump(0.7)
            lowmap.update(cdp.drain())
        print("  cumulative lowest prices:",len(lowmap))
        if cdp.ev(NEXT_JS) != 'ok':
            print("no next page"); break
        cdp.pump(2)

    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps({"day":args.day,"min_price":lowmap,"count":len(lowmap)},ensure_ascii=False,indent=1),encoding="utf-8")
    print("saved",args.out,"count",len(lowmap))

    cfg = json.loads(pathlib.Path(args.config).read_text(encoding='utf-8'))['mysql']
    conn = pymysql.connect(host=cfg['host'],port=int(cfg['port']),user=cfg['user'],password=cfg['password'],database=cfg['database'],charset='utf8mb4')
    with conn.cursor() as cur:
        cur.execute("SHOW COLUMNS FROM jd_index_quotation_daily LIKE '竞价最低价'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE jd_index_quotation_daily ADD COLUMN `竞价最低价` DECIMAL(18,4) NULL AFTER `quotation`")
        for isk, price in lowmap.items():
            cur.execute("UPDATE jd_index_quotation_daily SET `竞价最低价`=%s WHERE `记录日期`=%s AND `indexSkuId`=%s", (price, args.day, isk))
    conn.commit(); conn.close()
    print("updated DB 竞价最低价 for",len(lowmap),"indexSkuId")
    cdp.close()


if __name__ == "__main__":
    main()
