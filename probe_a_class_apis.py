#!/usr/bin/env python3
"""A 类字段 API 只读探测 v7。

相对 v6 的关键修复：
- 美团后台菜单 / 业务页在 iframe#hashframe 内（见 /home/dataease/probe/click_route.py），
  顶层 document 查不到菜单 → 所有菜单点击返回 None。v7 统一遍历「顶层 + 所有同源 iframe」
  去读菜单、点击、清/读 performance、读正文。
- 菜单项不一定是 .el-menu-item：改用通用选择器 + 文本长度过滤 + 冒泡鼠标事件
  （dispatch mousedown/mouseup 到父链），避免点到「全站营销」之类的长文案卡片。
- 用「网络静默」(resource 计数连续稳定) 代替固定 sleep，等子页请求真正发完。
- 结果全量落盘 JSON，终端只打精简摘要；find_ports 只认商家后台域名。

用法：
  probe_a_class_apis.py meituan [instance]
  probe_a_class_apis.py jd [instance]
  probe_a_class_apis.py meituan [instance] <url>   # 仅抓指定 URL
"""
from __future__ import annotations
import datetime
import json
import sys
import time

import requests
from meituan_business_report import CdpPage

API_KEYS = [
    "/api/", "bizdata", ".ajax", "gateway", "szgateway", "retail/r", "lowcode",
    "/ware/", "/item/", "/goods", "/product", "/message", "/notice",
    "/audit", "/review", "/config", "/setting", "/price", "/stock", "/commodity",
    "notice", "/msg", "jdm", "ware", "goods", "poi", "retail", "violat", "forbid",
    "ban", "area", "region", "coverage", "detail", "sku", "spu", "inspection",
    "searchlistpagev2", "limitsale", "businessstatus", "score", "qualification",
]

MT_KEYWORDS = ("商品列表", "商品违规", "禁售管理", "售卖区域", "商品体检", "站内信", "消息")
JD_KEYWORDS = ("商品列表", "商品", "商品信息体检", "违规", "禁售", "配送", "库存", "消息")

MT_HOME = "https://yiyao.meituan.com/main/frame#/page/home"
JD_HOME = "https://shop.jd.com/jdm/home"

MT_DOMAIN = "yiyao.meituan.com"
JD_DOMAINS = ("shop.jd.com", "wares-jdm.jd.com", "jzt.jd.com", "wares.jd.com")

# 所有 JS 都自带「顶层 + 同源 iframe」文档/窗口集合，因为 CDP evaluate 彼此独立。
DOCS = "const docs=[document];document.querySelectorAll('iframe').forEach(f=>{try{if(f.contentDocument)docs.push(f.contentDocument);}catch(_){}});"
WINS = "const wins=[window];document.querySelectorAll('iframe').forEach(f=>{try{if(f.contentWindow)wins.push(f.contentWindow);}catch(_){}});"

JS_CLEAR = """(() => {
  __WINS__
  wins.forEach(w=>{try{w.performance.clearResourceTimings();}catch(_){}});
  return 1;
})()""".replace("__WINS__", WINS)

JS_APIS = """(() => {
  __WINS__
  const keys=__KEYS__;
  const u=[];
  wins.forEach(w=>{try{w.performance.getEntriesByType('resource')
    .forEach(r=>u.push(r.name));}catch(_){}});
  return [...new Set(u.filter(x=>keys.some(k=>x.toLowerCase().indexOf(k)>=0)))];
})()""".replace("__WINS__", WINS).replace("__KEYS__", json.dumps(API_KEYS, ensure_ascii=False))

JS_TEXT = """(() => {
  __DOCS__
  return docs.map(d=>d.body?d.body.innerText:'').join(' ')
    .replace(/\\s+/g,' ').trim().slice(0,600);
})()""".replace("__DOCS__", DOCS)

JS_COUNT = """(() => {
  __WINS__
  let c=0;
  wins.forEach(w=>{try{c+=w.performance.getEntriesByType('resource').length;}catch(_){}});
  return c;
})()""".replace("__WINS__", WINS)


def _short(u: str) -> str:
    u = (u or "").split("?")[0]
    if len(u) > 130:
        u = u[:130] + "..."
    return u


def listening_ports() -> set[int]:
    ports: set[int] = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    parts = line.split()
                    if len(parts) < 4 or parts[3] != "0A":
                        continue
                    ports.add(int(parts[1].split(":")[1], 16))
        except OSError:
            pass
    return ports


def chrome_ports() -> list[tuple[int, str, list[tuple[str, str]]]]:
    out: list[tuple[int, str, list[tuple[str, str]]]] = []
    for port in sorted(listening_ports()):
        try:
            info = requests.get(
                f"http://127.0.0.1:{port}/json/version", timeout=0.4
            ).json()
        except Exception:
            continue
        if "webSocketDebuggerUrl" not in info:
            continue
        tabs: list[tuple[str, str]] = []
        try:
            for tab in requests.get(
                f"http://127.0.0.1:{port}/json/list", timeout=0.4
            ).json():
                tabs.append((tab.get("url", ""), tab.get("title", "")))
        except Exception:
            pass
        out.append((port, info.get("Browser", ""), tabs))
    return out


def find_ports(platform: str) -> list[int]:
    if platform == "meituan":
        ok = lambda u: MT_DOMAIN in u
    else:
        ok = lambda u: any(d in u for d in JD_DOMAINS)
    return [
        port
        for port, _browser, tabs in chrome_ports()
        if any(ok(url) for url, _title in tabs)
    ]


def _enable(page: CdpPage) -> None:
    page.call("Page.enable")
    page.call("Runtime.enable")


def _read(page: CdpPage):
    apis = page.evaluate(JS_APIS)
    txt = page.evaluate(JS_TEXT)
    return apis, txt


def _wait_network_quiet(page: CdpPage, timeout: int = 10, stable: int = 2) -> None:
    """等「顶层 + iframe」的 resource 计数连续稳定，说明子页请求已发完。"""
    last = -1
    hits = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cnt = page.evaluate(JS_COUNT)
        except Exception:
            cnt = -1
        if cnt >= 0 and cnt == last:
            hits += 1
            if hits >= stable:
                return
        else:
            hits = 0
        last = cnt
        time.sleep(1)


def collect_apis(page: CdpPage, url: str, wait: int = 10):
    page.evaluate(JS_CLEAR)
    page.call("Page.navigate", {"url": url})
    _wait_network_quiet(page, timeout=wait)
    return _read(page)


def _menu_links(page: CdpPage):
    """跨 iframe 读菜单项 (文本, href)。文本过长的是内容卡片，过滤掉。"""
    js = """(() => {
      __DOCS__
      const out=[]; const seen=new Set();
      const sel='a[href],[role=menuitem],li,span,div,button,.el-menu-item,.el-submenu__title';
      docs.forEach(d=>{
        d.querySelectorAll(sel).forEach(el=>{
          const t=(el.innerText||'').replace(/\\s+/g,' ').trim();
          if(!t || t.length>12) return;
          const h=el.getAttribute('href')||el.getAttribute('index')||
                  el.getAttribute('data-href')||el.getAttribute('data-url')||'';
          const key=t+'|'+h;
          if(seen.has(key)) return; seen.add(key);
          out.push([t,h]);
        });
      });
      return out;
    })()""".replace("__DOCS__", DOCS)
    return page.evaluate(js)


def _goto(page: CdpPage, href: str):
    """按 href 导航；hash 路由必须用 Page.navigate（改 location.href 不加载页面）。"""
    if href.startswith("#"):
        base = page.evaluate("location.href.split('#')[0]")
        url = base + href
    elif href.startswith("http"):
        url = href
    elif href.startswith("/") or href.startswith("./"):
        url = page.evaluate("location.origin") + "/" + href.lstrip("./")
    else:
        return None
    page.call("Page.navigate", {"url": url})
    return url


def _click_menu(page: CdpPage, kw: str):
    """跨 iframe 按文本点击菜单；向父链补发 mousedown/mouseup（框架常监听父级）。"""
    js = """((kw) => {
      __DOCS__
      let best=null;
      docs.forEach(d=>{
        d.querySelectorAll('.el-submenu__title').forEach(e=>{try{e.click();}catch(_){}});
        const sel='a,li,span,div,button,[role=menuitem],.el-menu-item,.el-submenu__title';
        const cand=[...d.querySelectorAll(sel)].filter(e=>{
          const t=(e.innerText||'').replace(/\\s+/g,' ').trim();
          return t && t.length<=12 && t.includes(kw);
        }).sort((a,b)=>{
          const ta=(a.innerText||'').trim(), tb=(b.innerText||'').trim();
          return (ta.length-tb.length) || (a.children.length-b.children.length);
        });
        if(cand.length && !best){
          const el=cand[0];
          el.scrollIntoView({block:'center'});
          el.click();
          let p=el;
          while(p){
            try{p.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));}catch(_){}
            try{p.dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));}catch(_){}
            p=p.parentElement;
          }
          best=(el.innerText||'').replace(/\\s+/g,' ').trim();
        }
      });
      return best;
    })('__KW__')""".replace("__DOCS__", DOCS).replace("__KW__", kw)
    return page.evaluate(js)


def _open_detail(page: CdpPage, pattern: str):
    page.evaluate(JS_CLEAR)
    js = """((pat) => {
      __DOCS__
      const re=new RegExp(pat);
      for(const d of docs){
        const a=[...d.querySelectorAll('a')].find(x=>re.test(x.getAttribute('href')||''));
        if(a){a.click();return a.getAttribute('href');}
      }
      return null;
    })('__PAT__')""".replace("__DOCS__", DOCS).replace("__PAT__", pattern)
    href = page.evaluate(js)
    _wait_network_quiet(page, timeout=8)
    apis, txt = _read(page)
    return href, apis, txt


def _new_page(port: int) -> CdpPage | None:
    try:
        return CdpPage(port)
    except Exception as exc:
        print("  端口 %d 连接失败，2s 后重试: %s" % (port, exc))
        time.sleep(2)
        try:
            return CdpPage(port)
        except Exception as exc2:
            print("  端口 %d 重试仍失败，跳过: %s" % (port, exc2))
            return None


def run(platform: str, instance: str, url: str | None) -> None:
    ports = find_ports(platform)
    if not ports:
        print("未自动发现 %s 商家后台 Chrome 端口。本机 Chrome 端口：" % platform)
        for port, _browser, tabs in chrome_ports():
            print(f"  port={port}")
            for u, t in tabs:
                print(f"     - {t!r} | {u}")
        return

    tabs_by_port = {p: tabs for p, _b, tabs in chrome_ports()}
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = "/opt/subsidy/probe_a_class_%s_%s.json" % (platform, ts)
    all_results: dict = {}

    for port in ports:
        print(f"\n========== 端口 {port} ({platform}) ==========")
        results: dict = {"open": [], "links": [], "menu": []}
        try:
            if url:
                page = _new_page(port)
                if page:
                    try:
                        apis, txt = collect_apis(page, url, 14)
                        results["open"].append({"url": url, "apis": apis, "text": txt})
                        print("APIS(%d): %s" % (len(apis), " ".join(_short(u) for u in apis[:15])))
                    except Exception as exc:
                        print("  抓 %s 失败: %s" % (url, exc))
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass
                all_results[port] = results
                continue

            # 1) 已打开页面
            open_urls = [
                u for u, _t in tabs_by_port.get(port, [])
                if u.startswith("http")
            ][:6]
            for u in open_urls:
                page = _new_page(port)
                if not page:
                    continue
                try:
                    apis, txt = collect_apis(page, u, 10)
                    results["open"].append({"url": u, "apis": apis, "text": txt})
                    print("已打开页 %s -> %d 个API" % (_short(u), len(apis)))
                except Exception as exc:
                    print("  抓 %s 失败: %s" % (u, exc))
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass

            # 2) 菜单导航：跨 iframe 读链接 -> 导航 / 点击
            home = MT_HOME if platform == "meituan" else JD_HOME
            page = _new_page(port)
            if page is None:
                all_results[port] = results
                continue
            try:
                _enable(page)
                page.evaluate(JS_CLEAR)
                page.call("Page.navigate", {"url": home})
                _wait_network_quiet(page, timeout=12)
                links = _menu_links(page) or []
                results["links"] = [{"text": t, "href": h} for t, h in links]
                print("菜单链接 %d 条（已写入文件）" % len(links))

                kws = MT_KEYWORDS if platform == "meituan" else JD_KEYWORDS
                for kw in kws:
                    exact = next((h for t, h in links if t == kw), None)
                    loose = next((h for t, h in links if kw in t), None)
                    m = exact if exact is not None else loose
                    nav = None
                    try:
                        page.evaluate(JS_CLEAR)
                        if m and m.startswith(("#", "/", "http", "./")):
                            nav = _goto(page, m)
                        else:
                            hit = _click_menu(page, kw)
                            nav = "click:" + str(hit)
                        _wait_network_quiet(page, timeout=10)
                        apis, txt = _read(page)
                        print("  [%s] -> %s  %d API" % (kw, _short(nav), len(apis)))

                        detail = None
                        if kw == "商品列表":
                            pat = ("detail|goodsId|skuId|productId|spuId"
                                   if platform == "meituan"
                                   else "wareId|sku|detail|item|spu")
                            try:
                                h2, da, dt = _open_detail(page, pat)
                                detail = {"href": h2, "apis": da, "text": dt}
                                print("    详情 %s -> %d API" % (_short(h2), len(da)))
                            except Exception as exc:
                                print("    进详情失败:", exc)

                        results["menu"].append({
                            "kw": kw, "href": m, "nav": nav,
                            "apis": apis, "text": txt, "detail": detail,
                        })
                    except Exception as exc:
                        print("  [%s] 失败: %s" % (kw, exc))
                        results["menu"].append({"kw": kw, "href": m, "apis": [], "text": str(exc)})
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        except Exception as exc:
            print("端口 %d 整体异常: %s" % (port, exc))
        all_results[port] = results

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(all_results, handle, ensure_ascii=False, indent=2)
    print("\n结果已写入: %s" % out_path)
    print("终端只看摘要；完整 URL / 文本 / 菜单链接见该文件。")


def main() -> None:
    platform = sys.argv[1] if len(sys.argv) > 1 else "meituan"
    instance = sys.argv[2] if len(sys.argv) > 2 else ""
    url = sys.argv[3] if len(sys.argv) > 3 else None
    run(platform, instance, url)


if __name__ == "__main__":
    main()
