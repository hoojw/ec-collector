#!/usr/bin/env python3
"""A 类字段补齐探测 v2：按已确权真实路由，逐页抓「接口 + 响应体」。

v1 的问题（2026-11-04 实测）：网络静默判定 2s×2≈4s 就退出，美团 SPA 子应用
还没渲染、业务 XHR 未发，抓到的全是首页框架接口；命中的 A 类词多为文案/HTML
误报。v2 改为：先轮询等页面渲染标志（跨 iframe 读正文），期间持续 pump 网络
避免丢事件，渲染后再收尾等待；并剔除 HTML 文档、收紧线索词、修好京东接口名。

只读红线：仅开 disposable tab、navigate、监听网络、读响应体；不写任何表、
不重新登录、不触发任何写请求。复用 /home/dataease/probe/probe_cdp.py 的 Tab。

用法：
  probe_a_class_pages.py meituan [port] [--only label1,label2] [--spu <spuId>]
  probe_a_class_pages.py jd      [port] [--only label1,label2]
  probe_a_class_pages.py --list
"""
from __future__ import annotations
import argparse
import datetime
import importlib.util
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/subsidy")
from probe_a_class_apis import find_ports  # noqa: E402

PROBE_CDP = "/home/dataease/probe/probe_cdp.py"
MT_BASE = "https://yiyao.meituan.com/main/frame#"

# (label, url, network_pattern, 渲染等待文案)
# 路由来源：美团 menu_mapping.json（无签名静态表）；京东 queryPinMenuTree 响应体。
MT_PAGES = [
    ("home_menu", MT_BASE + "/page/home", "yiyao.meituan.com", "首页"),
    ("product_list", MT_BASE + "/page/product/list/single", "yiyao.meituan.com", "30天销量"),
    ("product_examination", MT_BASE + "/page/product/examination", "yiyao.meituan.com", "商品体检"),
    ("product_audit", MT_BASE + "/page/product/list/spAudit", "yiyao.meituan.com", "审核"),
    ("message_center", MT_BASE + "/page/health/shop_setting/messageCenter", "yiyao.meituan.com", "消息"),
]

JD_PAGES = [
    ("product_list", "https://wares-jdm.jd.com/ware/wareList?activeTab=OnsaleWare",
     "sff.jd.com/api", "商品列表"),
    ("product_inspection", "https://wares-jdm.jd.com/ware/commodity-inspection",
     "sff.jd.com/api", "商品信息体检"),
    ("product_launch", "https://ware-jdm.jd.com/productlaunch/productIndex?source=1",
     "sff.jd.com/api", "类目|发布商品|商品发布|商品信息|选择"),
    ("product_qualification", "https://shop.jd.com/jdm/seller/shop/qualifications",
     "sff.jd.com/api", "资质"),
    ("business_qualification", "https://shop.jd.com/jdm/seller/shop/businessQualification",
     "sff.jd.com/api", "资质"),
    ("freight_template", "https://shop.jd.com/jdm/trade/delivery/freightTemplate/TempList",
     "sff.jd.com/api", "运费"),
    ("stock_manage", "https://ware.shop.jd.com/rest/shop/stock/manage/index",
     "sff.jd.com/api", "库存"),
    # 医生开方监控：京麦「交易 → 订单管理」下的三条真实 durl
    # （来源 queryPinMenuTree 响应体，2026-11-04 实测解出 293 个菜单节点）
    ("order_list", "https://shop.jd.com/jdm/trade/orders/order-list",
     "jd.com", "订单"),
    ("order_warning", "https://shop.jd.com/jdm/trade/risk/warning-center",
     "jd.com", "预警|订单"),
    ("rx_manage", "https://rx.shop.jd.com/rx/rxInfo_list.action?_JDMOMID_=1559",
     "jd.com", "处方"),
]

# 收紧为「短语」，避免 reason（JS 变量）/ 区域（UI 布局）/ 资质（菜单名）误报
A_HINTS = [
    "适应症", "用法用量", "批准文号", "电子说明书", "商品描述", "商品详情",
    "下架原因", "驳回原因", "违规原因", "处置通知", "平台处置", "违规预警",
    "违规管控", "商品信息违规", "商品下架", "资质审核", "资质更新",
    "禁售区域", "售卖区域", "可售区域", "限售", "可售时间", "售卖时段",
    "forbidSale", "saleArea", "limitSale", "saleTime", "offlineReason",
    "rejectReason", "indication", "instruction",
    "待开方", "开方超时", "审方状态", "处方状态", "开方时限", "开方时间",
    "接单时长", "orderStatus", "rxStatus", "prescriptionStatus",
]
BIZ = ("/api/", "/health/", "/gw/", "/retail", "/msg/", "/v1/", "/v2/",
       "sff.jd.com/api", "queryMenu", "poiList", "product", "ware", "sku",
       "rx.shop.jd.com", "jdm/trade", "order-list", "warning-center", "rxInfo")
SKIP_EXT = (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".ico", ".xml")

JS_TEXT = """(() => {
  const docs=[document];
  document.querySelectorAll('iframe').forEach(f=>{try{if(f.contentDocument)docs.push(f.contentDocument);}catch(_){}});
  return docs.map(d=>d.body?d.body.innerText:'').join(' ');
})()"""


def load_tab():
    if not Path(PROBE_CDP).exists():
        raise RuntimeError(f"缺少抓包基类: {PROBE_CDP}")
    spec = importlib.util.spec_from_file_location("probe_cdp", PROBE_CDP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Tab


def eval_js(tab, expr: str, timeout: float = 6.0):
    mid = tab.send("Runtime.evaluate", {
        "expression": expr, "returnByValue": True, "awaitPromise": True,
    })
    tab.ws.settimeout(2.0)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            raw = tab.ws.recv()
        except Exception:
            continue
        try:
            msg = json.loads(raw)
        except Exception:
            continue
        if msg.get("id") == mid:
            return ((msg.get("result") or {}).get("result") or {}).get("value")
        tab._handle(msg, "\x00")
    return None


def _is_biz(url: str) -> bool:
    low = url.lower()
    if any(low.split("?")[0].endswith(ext) for ext in SKIP_EXT):
        return False
    return any(k in low for k in BIZ)


def _latest_spu() -> tuple[str, str, str]:
    """从最近一次美团探测结果里取「当前店铺」的真实 spuId / poiId。

    商品编辑页 URL 必须带本店商品的 spuId；用别家店的 spuId 会 404。
    优先用最近一次抓到的 searchListPageV2 响应，而不是历史保民样本。
    """
    files = sorted(Path("/opt/subsidy").glob("probe_a_class_bodies_meituan_*.json"),
                   reverse=True)
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for _port, pages in data.items():
            for _label, res in pages.items():
                for e in res.get("entries", []):
                    if e.get("api", "").endswith("searchListPageV2"):
                        body = e.get("body") or ""
                        spu = re.search(r'"wm_product_spu_id":(\d+)', body)
                        poi = re.search(r'"wm_poi_id":(\d+)', body)
                        if spu:
                            return (spu.group(1),
                                    poi.group(1) if poi else "", path.name)
    return "", "", ""


def disp(url: str) -> str:
    """京东接口名在 ?api= 参数里，不能按 split('?')[0] 截断。"""
    if "api=" in url:
        return url.split("api=")[1].split("&")[0]
    return url.split("?")[0].replace("https://yiyao.meituan.com", "")


def capture(Tab, port: int, label: str, url: str, pattern: str,
            wait_text: str, settle: float = 18.0, tail: float = 8.0,
            body_cap: int = 4_000_000) -> dict:
    tab = Tab(port, "about:blank")
    try:
        tab.send("Network.enable", {
            "maxTotalBufferSize": 256 * 1024 * 1024,
            "maxResourceBufferSize": 128 * 1024 * 1024,
        })
        tab.send("Page.enable")
        tab.send("Runtime.enable")
        tab.pump(0.8, pattern)
        tab.send("Page.navigate", {"url": url})

        # 1) 等页面渲染标志；期间持续 pump，避免网络事件堆积丢失
        rendered = False
        deadline = time.time() + settle
        while time.time() < deadline:
            tab.pump(1.0, pattern)
            if wait_text:
                txt = eval_js(tab, JS_TEXT, timeout=5.0)
                if isinstance(txt, str) and any(w in txt for w in wait_text.split("|")):
                    rendered = True
                    break
            else:
                rendered = True

        # 2) 渲染后再收尾，业务 XHR 通常在渲染后发出
        seen, stable, tail_end = 0, 0, time.time() + tail
        while time.time() < tail_end:
            tab.pump(2.0, pattern)
            if len(tab.requests) == seen:
                stable += 1
                if stable >= 2:
                    break
            else:
                seen, stable = len(tab.requests), 0

        # 3) 文案没命中但页面确实发了大量业务请求，也算已渲染（如京东发布页标题不含关键词）
        rendered_by = "text" if rendered else ""
        if not rendered and len(tab.requests) >= 25:
            rendered, rendered_by = True, "api_count"

        entries = []
        for rid, info in tab.requests.items():
            u = info.get("url") or ""
            if info.get("method") == "OPTIONS" or info.get("status") != 200:
                continue
            if not _is_biz(u):
                continue
            body, b64 = tab.body(rid)
            if b64:
                import base64
                try:
                    body = base64.b64decode(body).decode("utf-8", "replace")
                except Exception:
                    pass
            # 剔除 HTML 文档（页面本身，不是数据接口）
            if not body or body.lstrip().lower().startswith(("<!", "<html")):
                continue
            hints = [h for h in A_HINTS if h in body]
            entries.append({
                "api": disp(u),
                "url": u,
                "method": info.get("method"),
                "postData": (info.get("postData") or "")[:2000],
                "status": info.get("status"),
                "body_len": len(body),
                "truncated": len(body) > body_cap,
                "hints": hints,
                "body": body[:body_cap],
            })
        entries.sort(key=lambda e: e["api"])
        return {"label": label, "url": url, "rendered": rendered,
                "rendered_by": rendered_by, "wait_text": wait_text,
                "count": len(entries), "entries": entries}
    finally:
        tab.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="A-class page/body probe v2")
    ap.add_argument("platform", nargs="?", choices=("meituan", "jd"))
    ap.add_argument("port", nargs="?", type=int, default=0)
    ap.add_argument("--spu", default="", help="美团商品编辑页 spuId")
    ap.add_argument("--only", default="", help="只跑指定 label，逗号分隔")
    ap.add_argument("--settle", type=float, default=18.0, help="等待渲染上限秒")
    ap.add_argument("--tail", type=float, default=8.0, help="渲染后收尾等待秒")
    ap.add_argument("--body-cap", type=int, default=4_000_000,
                    help="单响应体保存上限字节（默认 4MB，0 表示不截断）")
    ap.add_argument("--list", action="store_true", help="列出页面清单")
    args = ap.parse_args()

    all_pages = {"meituan": MT_PAGES, "jd": JD_PAGES}
    if args.list:
        for plat, pages in all_pages.items():
            print("===", plat)
            for label, url, _pat, word in pages:
                print(f"  {label:24s} wait={word!r:14s} {url}")
        return 0
    if not args.platform:
        ap.error("需要 platform 或 --list")

    ports = [args.port] if args.port else find_ports(args.platform)
    if not ports:
        print("未发现 %s 商家后台 Chrome 端口" % args.platform)
        return 1

    pages = list(all_pages[args.platform])
    if args.platform == "meituan":
        spu = args.spu
        poi, src = "", ""
        if not spu:
            spu, poi, src = _latest_spu()
            if spu:
                print("自动取用最近抓到的同店 spuId=%s (poiId=%s, 来源 %s)"
                      % (spu, poi or "?", src))
                print("   若当前浏览器不是这家店，请显式 --spu <纯数字>")
        if not spu:
            try:
                cat = json.loads(Path("/home/dataease/probe/mt_catalog.json").read_text(encoding="utf-8"))
                spu = str((cat.get("rows") or [{}])[0].get("id") or "")
                if spu:
                    print("警告: 回退到保民旧样本 spuId=%s，跨店可能 404" % spu)
            except Exception:
                spu = ""
        if spu:
            pages.append(("product_edit",
                          MT_BASE + f"/page/product/detail/product/{spu}/edit",
                          "yiyao.meituan.com", "商品信息|基本信息|编辑商品|商品详情|保存"))
        else:
            print("提示: 取不到 spuId，跳过商品编辑页；可显式 --spu <纯数字> 指定")

    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        pages = [p for p in pages if p[0] in wanted]
        if not pages:
            print("无匹配 label:", sorted(wanted))
            return 1

    Tab = load_tab()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = Path("/opt/subsidy/probe_a_class_bodies_%s_%s.json" % (args.platform, ts))
    all_results: dict = {}

    def flush():
        out_path.write_text(json.dumps(all_results, ensure_ascii=False, indent=1),
                            encoding="utf-8")

    for port in ports:
        print(f"\n========== 端口 {port} ({args.platform}) ==========")
        per_page: dict = {}
        all_results[str(port)] = per_page
        for label, url, pattern, wait_text in pages:
            try:
                cap = args.body_cap if args.body_cap > 0 else 10 ** 9
                res = capture(Tab, port, label, url, pattern, wait_text,
                              args.settle, args.tail, cap)
            except Exception as exc:
                print(f"  [{label}] 失败: {exc}")
                per_page[label] = {"error": str(exc)}
                flush()
                continue
            per_page[label] = res
            hits = [(e["api"], e["hints"]) for e in res["entries"] if e["hints"]]
            cut = sum(1 for e in res["entries"] if e.get("truncated"))
            flag = ("已渲染" if res["rendered"] else "未等到渲染标志")
            if res.get("rendered_by") == "api_count":
                flag = "已渲染(接口数兜底)"
            print(f"  [{label}] {res['count']} 个业务接口 / 命中 {len(hits)} / {flag}"
                  f" / 截断 {cut}")
            for name, hs in hits[:12]:
                print(f"      {name[:105]}  {','.join(hs[:4])}")
            flush()

    print("\n结果已写入:", out_path)
    print("终端只看摘要；完整 URL / POST body / 响应体见该文件。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrokenPipeError:
        pass
