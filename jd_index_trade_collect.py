#!/usr/bin/env python3
"""Collect JD "指数交易" (index-trade) data for the configured store via CDP.

Two data sets, both read-only (the page itself issues the signed requests):

  1) 我的竞价  - IndexQuotationService.pageVenderQuotationRecord
                 the index-trade items this store has already joined / quoted.
  2) 竞价活动  - IndexWarePromoService.pageIndexWarePromoActivities
                 the promo activities and whether this store signed up.

A logged-in JD Chrome tab must already be open at the index-ware page.  The
collector attaches to it, clicks the tabs with element.click(), records every
matching sff API response body, paginates, and persists into daily tables.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import time
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo

import pymysql
import requests
import websocket

TZ = ZoneInfo("Asia/Shanghai")
INDEX_URL = "https://shop.jd.com/jdm/cz/index-ware?mainType=canJoinWare"
API_QUOTATION = "IndexQuotationService.pageVenderQuotationRecord"
API_PROMO = "IndexWarePromoService.pageIndexWarePromoActivities"
API_WARE = "IndexWareService.pageVenderIndexWare"

TABLE_QUOTATION = "jd_index_quotation_daily"
TABLE_PROMO = "jd_index_promo_activity_daily"
TABLE_WARE = "jd_index_ware_daily"
TABLE_GAP = "jd_index_joinable_gap_daily"


def ddl(conn):
    with conn.cursor() as cur:
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE_QUOTATION}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `记录日期` DATE NOT NULL,
            `店铺` VARCHAR(64) NOT NULL DEFAULT '',
            `bidSheetId` VARCHAR(64) DEFAULT NULL,
            `id_报价` VARCHAR(64) DEFAULT NULL,
            `skuId` VARCHAR(64) DEFAULT NULL,
            `skuName` VARCHAR(255) DEFAULT NULL,
            `indexSkuId` VARCHAR(64) DEFAULT NULL,
            `indexSkuName` VARCHAR(255) DEFAULT NULL,
            `indexSpuId` VARCHAR(64) DEFAULT NULL,
            `indexSkuAttrList` TEXT DEFAULT NULL,
            `skuAttrList` TEXT DEFAULT NULL,
            `quotation` DECIMAL(18,4) DEFAULT NULL,
            `activityId` VARCHAR(64) DEFAULT NULL,
            `promotionId` VARCHAR(64) DEFAULT NULL,
            `status` INT DEFAULT NULL,
            `statusReason` VARCHAR(128) DEFAULT NULL,
            `orderNum` VARCHAR(32) DEFAULT NULL,
            `indexSalesRatio` VARCHAR(32) DEFAULT NULL,
            `topDurationMinutes` VARCHAR(32) DEFAULT NULL,
            `sellNum` VARCHAR(32) DEFAULT NULL,
            `remainStock` VARCHAR(32) DEFAULT NULL,
            `stockSoldOut` TINYINT(1) DEFAULT NULL,
            `autoQuotation` TINYINT(1) DEFAULT NULL,
            `autoFollowPrice` TINYINT(1) DEFAULT NULL,
            `hasAuthenticationForQuotation` TINYINT(1) DEFAULT NULL,
            `createTime` VARCHAR(32) DEFAULT NULL,
            `validTime` BIGINT DEFAULT NULL,
            `endTime` BIGINT DEFAULT NULL,
            `recommendMessage` TEXT DEFAULT NULL,
            `stockUrl` VARCHAR(512) DEFAULT NULL,
            `raw_json` LONGTEXT DEFAULT NULL,
            `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_day_quote` (`记录日期`,`id_报价`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE_PROMO}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `记录日期` DATE NOT NULL,
            `店铺` VARCHAR(64) NOT NULL DEFAULT '',
            `activityId` VARCHAR(64) DEFAULT NULL,
            `activityName` VARCHAR(255) DEFAULT NULL,
            `activityDesc` VARCHAR(255) DEFAULT NULL,
            `bidStatus` INT DEFAULT NULL,
            `activityStatus` INT DEFAULT NULL,
            `signed` TINYINT(1) DEFAULT NULL,
            `activityLeftTimeStr` VARCHAR(64) DEFAULT NULL,
            `submitStartTime` BIGINT DEFAULT NULL,
            `submitEndTime` BIGINT DEFAULT NULL,
            `activityStartTime` BIGINT DEFAULT NULL,
            `activityEndTime` BIGINT DEFAULT NULL,
            `raw_json` LONGTEXT DEFAULT NULL,
            `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_day_activity` (`记录日期`,`activityId`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE_WARE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `记录日期` DATE NOT NULL,
            `店铺` VARCHAR(64) NOT NULL DEFAULT '',
            `skuId` VARCHAR(64) DEFAULT NULL,
            `skuName` VARCHAR(255) DEFAULT NULL,
            `specAttrs` VARCHAR(255) DEFAULT NULL,
            `indexPrice` DECIMAL(18,4) DEFAULT NULL,
            `salePrice` DECIMAL(18,4) DEFAULT NULL,
            `status` INT DEFAULT NULL,
            `raw_json` LONGTEXT DEFAULT NULL,
            `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_day_ware` (`记录日期`,`店铺`,`skuId`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE_GAP}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `记录日期` DATE NOT NULL,
            `店铺` VARCHAR(64) NOT NULL DEFAULT '',
            `skuId` VARCHAR(64) DEFAULT NULL,
            `skuName` VARCHAR(255) DEFAULT NULL,
            `specAttrs` VARCHAR(255) DEFAULT NULL,
            `indexPrice` DECIMAL(18,4) DEFAULT NULL,
            `salePrice` DECIMAL(18,4) DEFAULT NULL,
            `raw_json` LONGTEXT DEFAULT NULL,
            `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_day_gap` (`记录日期`,`店铺`,`skuId`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
    conn.commit()


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=40, suppress_origin=True)
        self.mid = 0
        self.pending = {}      # requestId -> api
        self.bodies = {}       # requestId -> body
        self.only_api = None   # 当前只捕获指定接口，避免串到其它页签响应

    def send(self, method, params=None):
        self.mid += 1
        self.ws.send(json.dumps({"id": self.mid, "method": method, "params": params or {}}))
        return self.mid

    def handle(self, msg):
        m = msg.get("method")
        p = msg.get("params") or {}
        if m == "Network.requestWillBeSent":
            u = (p.get("request") or {}).get("url", "")
            if "sff.jd.com/api" in u:
                api = parse_qs(urlparse(u).query).get("api", [""])[0]
                wanted = {"quotation": API_QUOTATION, "promo": API_PROMO, "ware": API_WARE}.get(self.only_api)
                if wanted and wanted not in api:
                    return
                if API_QUOTATION in api or API_PROMO in api or API_WARE in api:
                    self.pending[p["requestId"]] = api.rsplit(".", 1)[-1]
        elif m == "Network.responseReceived":
            rid = p.get("requestId")
            if rid in self.pending:
                self.pending[rid] = (self.pending[rid], p.get("response", {}).get("status"))

    def call(self, method, params=None, timeout=15, collect=False):
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
            self.handle(msg)
        return {}

    def pump(self, sec):
        end = time.time() + sec
        self.ws.settimeout(1.0)
        while time.time() < end:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                continue
            self.handle(msg)

    def ev(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
        if r.get("exceptionDetails"):
            return None
        return (r.get("result") or {}).get("value")

    def drain_bodies(self):
        import base64
        for rid in list(self.pending):
            try:
                r = self.call("Network.getResponseBody", {"requestId": rid})
            except Exception:
                continue
            body = r.get("body") or ""
            if r.get("base64Encoded"):
                try:
                    body = base64.b64decode(body).decode("utf-8", "replace")
                except Exception:
                    pass
            self.bodies[rid] = body
        return list(self.bodies.values())

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def click_tab(cdp, label):
    expr = """(function(){
      var want=%s;
      var els=document.querySelectorAll('div,span,a,li,button,p');
      for(var i=0;i<els.length;i++){
        var el=els[i];
        if((el.textContent||'').trim()===want){ el.click(); return true; }
      } return false;
    })()""" % json.dumps(label)
    return cdp.ev(expr)


NEXT_JS = """(function(){
  var els=document.querySelectorAll('.btn-next,[aria-label="下一页"],li[class*=next],a[class*=next]');
  for(var i=0;i<els.length;i++){
    var el=els[i];
    if(el.getAttribute('aria-label')==='下一页' || (el.className||'').toString().indexOf('btn-next')>=0){
      if(el.getAttribute('aria-disabled')==='true' || (el.className||'').toString().indexOf('is-disabled')>=0) return 'disabled';
      el.click(); return 'ok';
    }
  }
  return 'no';
})()"""


def _rows_from(bodies, rows, total_holder):
    for body in bodies:
        try:
            data = json.loads(body)
        except Exception:
            continue
        payload = data.get("data") or {}
        if "totalCount" in payload:
            total_holder[0] = int(payload.get("totalCount") or 0)
        for it in (payload.get("list") or []):
            rows.append(it)


def collect_dataset(cdp, tab_label, api_key, page_size):
    cdp.pending.clear()
    cdp.bodies.clear()
    cdp.only_api = {API_QUOTATION: "quotation", API_PROMO: "promo", API_WARE: "ware"}.get(api_key, api_key)
    ok = click_tab(cdp, tab_label) if tab_label else True
    cdp.pump(3)
    rows = []
    total_holder = [None]
    _rows_from(cdp.drain_bodies(), rows, total_holder)
    total = total_holder[0]
    if total is None:
        # An already-active tab may reuse its rendered cache and emit no XHR.
        # Trigger only the visible read-only query control, never bid controls.
        queried = cdp.ev("""(() => {
          for (const el of document.querySelectorAll('button,[role="button"]')) {
            const box=el.getBoundingClientRect();
            if(box.width>0 && box.height>0 && ['查询','搜索','刷新'].includes((el.innerText||'').trim())) {
              el.click();return true;
            }
          }
          return false;
        })()""")
        if queried:
            cdp.pump(3)
            _rows_from(cdp.drain_bodies(), rows, total_holder)
            total = total_holder[0]
    cdp.pending.clear()
    cdp.bodies.clear()

    seen_pages = 1
    max_pages = 60
    while total and len(rows) < total and seen_pages < max_pages:
        cdp.pending.clear()
        cdp.bodies.clear()
        res = cdp.ev(NEXT_JS)
        if res in ("no", "disabled"):
            break
        cdp.pump(2.5)
        before = len(rows)
        _rows_from(cdp.drain_bodies(), rows, total_holder)
        if len(rows) == before:
            break
        seen_pages += 1

    # dedupe by id / activityId
    key = "activityId" if api_key == API_PROMO else ("skuId" if api_key == API_WARE else "id")
    uniq = {}
    for it in rows:
        k = it.get(key) or json.dumps(it, ensure_ascii=False, sort_keys=True)
        uniq[k] = it
    return ok, total, list(uniq.values())


def attach(port):
    base = "http://127.0.0.1:%d" % port
    tabs = requests.get(base + "/json/list", timeout=10).json()
    t = next((x for x in tabs if x.get("type") == "page" and "index-ware" in str(x.get("url"))), None)
    if t is None:
        from urllib.parse import quote
        r = requests.put(base + "/json/new?" + quote(INDEX_URL, safe=""), timeout=20)
        if r.status_code == 405:
            r = requests.get(base + "/json/new?" + quote(INDEX_URL, safe=""), timeout=20)
        t = r.json()
        time.sleep(12)
    return t


def persist(config, day, store, quotes, promos, wares):
    mysql = config["mysql"]
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
                           password=mysql["password"], database=mysql["database"],
                           charset=mysql.get("charset", "utf8mb4"), autocommit=False)
    counts = {}
    try:
        ddl(conn)
        with conn.cursor() as cur:
            q_sql = f"""INSERT INTO `{TABLE_QUOTATION}`
                (`记录日期`,`店铺`,`bidSheetId`,`id_报价`,`skuId`,`skuName`,`indexSkuId`,`indexSkuName`,
                 `indexSpuId`,`indexSkuAttrList`,`skuAttrList`,`quotation`,`activityId`,`promotionId`,
                 `status`,`statusReason`,`orderNum`,`indexSalesRatio`,`topDurationMinutes`,`sellNum`,
                 `remainStock`,`stockSoldOut`,`autoQuotation`,`autoFollowPrice`,
                 `hasAuthenticationForQuotation`,`createTime`,`validTime`,`endTime`,`recommendMessage`,
                 `stockUrl`,`raw_json`)
                VALUES ({','.join(['%s']*31)})
                ON DUPLICATE KEY UPDATE `raw_json`=VALUES(`raw_json`),`quotation`=VALUES(`quotation`),
                `status`=VALUES(`status`),`statusReason`=VALUES(`statusReason`)"""
            q_vals = []
            for it in quotes:
                q_vals.append((
                    day, store, it.get("bidSheetId"), it.get("id"), it.get("skuId"), it.get("skuName"),
                    it.get("indexSkuId"), it.get("indexSkuName"), it.get("indexSpuId"),
                    json.dumps(it.get("indexSkuAttrList"), ensure_ascii=False) if it.get("indexSkuAttrList") else None,
                    json.dumps(it.get("skuAttrList"), ensure_ascii=False) if it.get("skuAttrList") else None,
                    it.get("quotation"), it.get("activityId"), it.get("promotionId"), it.get("status"),
                    it.get("statusReason"), it.get("orderNum"), it.get("indexSalesRatio"),
                    it.get("topDurationMinutes"), it.get("sellNum"), it.get("remainStock"),
                    int(bool(it.get("stockSoldOut"))) if it.get("stockSoldOut") is not None else None,
                    it.get("autoQuotation"), it.get("autoFollowPrice"),
                    int(bool(it.get("hasAuthenticationForQuotation"))) if it.get("hasAuthenticationForQuotation") is not None else None,
                    it.get("createTime"), it.get("validTime"), it.get("endTime"),
                    it.get("recommendMessage"), it.get("stockUrl"),
                    json.dumps(it, ensure_ascii=False),
                ))
            if q_vals:
                cur.executemany(q_sql, q_vals)
            counts["quotation"] = len(q_vals)

            p_sql = f"""INSERT INTO `{TABLE_PROMO}`
                (`记录日期`,`店铺`,`activityId`,`activityName`,`activityDesc`,`bidStatus`,
                 `activityStatus`,`signed`,`activityLeftTimeStr`,`submitStartTime`,`submitEndTime`,
                 `activityStartTime`,`activityEndTime`,`raw_json`)
                VALUES ({','.join(['%s']*14)})
                ON DUPLICATE KEY UPDATE `raw_json`=VALUES(`raw_json`),`signed`=VALUES(`signed`),
                `activityStatus`=VALUES(`activityStatus`)"""
            p_vals = []
            for it in promos:
                p_vals.append((
                    day, store, it.get("activityId"), it.get("activityName"), it.get("activityDesc"),
                    it.get("bidStatus"), it.get("activityStatus"),
                    int(bool(it.get("signed"))) if it.get("signed") is not None else None,
                    it.get("activityLeftTimeStr"), it.get("submitStartTime"), it.get("submitEndTime"),
                    it.get("activityStartTime"), it.get("activityEndTime"),
                    json.dumps(it, ensure_ascii=False),
                ))
            if p_vals:
                cur.executemany(p_sql, p_vals)
            counts["promo"] = len(p_vals)

            w_sql = f"""INSERT INTO `{TABLE_WARE}`
                (`记录日期`,`店铺`,`skuId`,`skuName`,`specAttrs`,`indexPrice`,`salePrice`,`status`,`raw_json`)
                VALUES ({','.join(['%s']*9)})
                ON DUPLICATE KEY UPDATE `raw_json`=VALUES(`raw_json`),`skuName`=VALUES(`skuName`),
                `specAttrs`=VALUES(`specAttrs`),`indexPrice`=VALUES(`indexPrice`),`salePrice`=VALUES(`salePrice`)"""
            w_vals = []
            for it in wares:
                spec = it.get("specAttrs")
                if isinstance(spec, (list, dict)):
                    spec = json.dumps(spec, ensure_ascii=False)
                w_vals.append((
                    day, store, it.get("skuId"), it.get("skuName"), spec,
                    it.get("indexPrice"), it.get("salePrice"), it.get("status"),
                    json.dumps(it, ensure_ascii=False),
                ))
            if w_vals:
                cur.executemany(w_sql, w_vals)
            counts["ware"] = len(w_vals)

            gap_sql = f"""INSERT IGNORE INTO `{TABLE_GAP}`
                (`记录日期`,`店铺`,`skuId`,`skuName`,`specAttrs`,`indexPrice`,`salePrice`,`raw_json`)
                SELECT w.`记录日期`,w.`店铺`,w.`skuId`,w.`skuName`,w.`specAttrs`,
                       w.`indexPrice`,w.`salePrice`,w.`raw_json`
                FROM `{TABLE_WARE}` w
                LEFT JOIN `{TABLE_QUOTATION}` q
                  ON q.`记录日期`=w.`记录日期` AND q.`店铺`=w.`店铺`
                 AND q.`skuId`=w.`skuId` AND q.`status` IN (20,31)
                WHERE w.`记录日期`=%s AND w.`店铺`=%s AND q.`id` IS NULL"""
            cur.execute(gap_sql, (day, store))
            counts["gap"] = cur.rowcount

            # 本店可售卖商品兜底：全局在售快照（当前仅保民有稳定数据）
            if store == "保民":
                sku_gap_sql = f"""INSERT IGNORE INTO `{TABLE_GAP}`
                    (`记录日期`,`店铺`,`skuId`,`skuName`,`specAttrs`,`salePrice`,`raw_json`)
                    SELECT %s,%s,s.`SKUID`,s.`商品名称`,s.`销售属性`,s.`京东价`,
                           JSON_OBJECT('商品编码',s.`商品编码`,'商品状态',s.`商品状态`)
                    FROM `jd_store_sku_normalized_daily` s
                    LEFT JOIN `{TABLE_QUOTATION}` q
                      ON q.`记录日期`=s.`记录日期`
                     AND CAST(q.`skuId` AS CHAR) COLLATE utf8mb4_unicode_ci=CAST(s.`SKUID` AS CHAR) COLLATE utf8mb4_unicode_ci
                     AND q.`status` IN (20,31)
                    WHERE s.`记录日期`=(SELECT MAX(`记录日期`) FROM `jd_store_sku_normalized_daily`)
                      AND s.`商品状态`='上架' AND q.`id` IS NULL"""
                cur.execute(sku_gap_sql, (day, store))
                counts["gap"] = cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/subsidy/config.json")
    ap.add_argument("--port", type=int, default=9433)
    ap.add_argument("--store", default="保民")
    ap.add_argument("--day", default="")
    ap.add_argument("--dump-dir", default="/opt/subsidy/exports")
    args = ap.parse_args()

    day = args.day or dt.datetime.now(TZ).date().isoformat()
    cfg = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))

    target = attach(args.port)
    cdp = CDP(target["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Network.enable", {"maxTotalBufferSize": 268435456, "maxResourceBufferSize": 134217728})
    cdp.call("Runtime.enable")
    cdp.pump(1)

    # 我的竞价
    ok_q, total_q, quotes = collect_dataset(cdp, "我的竞价", API_QUOTATION, 10)
    print("我的竞价: clicked=%s total=%s rows=%d" % (ok_q, total_q, len(quotes)))
    # 竞价活动
    ok_p, total_p, promos = collect_dataset(cdp, "竞价活动", API_PROMO, 12)
    print("竞价活动: clicked=%s total=%s rows=%d" % (ok_p, total_p, len(promos)))
    # 全部商品：本店可售卖的指数交易商品，减去上面已参加的活动即差集
    ok_w, total_w, wares = collect_dataset(cdp, "全部商品", API_WARE, 12)
    print("全部商品: clicked=%s total=%s rows=%d" % (ok_w, total_w, len(wares)))

    dump = {
        "day": day, "store": args.store,
        "quotation_total": total_q, "quotation_rows": quotes,
        "promo_total": total_p, "promo_rows": promos,
        "ware_total": total_w, "ware_rows": wares,
    }
    pathlib.Path(args.dump_dir).mkdir(parents=True, exist_ok=True)
    out = pathlib.Path(args.dump_dir) / ("jd_index_trade_%s.json" % day.replace("-", ""))
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=1), encoding="utf-8")
    print("dump ->", out)

    counts = persist(cfg, day, args.store, quotes, promos, wares)
    print("persisted:", counts)
    cdp.close()


if __name__ == "__main__":
    main()
