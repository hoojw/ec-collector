#!/usr/bin/env python3
"""京东处方（问诊开方）监控采集：只读处方管理页，落两张表。

- `jd_prescription_daily`   处方单明细（一单一行，带原始行文本便于核对）
- `jd_prescription_status`  各店各页签计数（平台自己判定的 待开方 / 审核超时）

口径说明（重要）：
- 「应接单时限」由平台判定，我们无法也无需自己设阈值。所以这里**只存平台给的状态**
  （待开方 = 未超时；审核超时 = 平台判定超时），不自己算超时。
- 明细页没有「接单时间 / 开方时间」，因此「平均接单时长」这一列**采集不到**，
  表里只存 下单时间 / 登记时间 / 预约时间，能不能替代以后再说。
- 「医生名称 / 医院名称 / 处方科室 / 执业药师 / 处方编号」只在详情页，且是**正文里的
  「标签：值」纯文本**（不是 input.value，2026-11-19 实测）；客服上传 / 用户上传的处方
  这几列为空（本来就没有开方医生）。实测「处方科室」京东基本不填，取不到属正常。

用法：python jd_prescription_collect.py --port 9433 --client 保民 [--details 20]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
from urllib.parse import quote

import pymysql
import requests
import websocket

ROOT_CONFIG = "/opt/subsidy/config.json"
# 全部处方有 1.5 万单，抓全既没意义也会触发风控，只取活跃状态。
# 状态码来自页签 li[data-t]：1 待审核 / 9 审核超时 / 77 待开方 / 20 待补充处方。
# 实测 rxStatus + pageSize 直接生效，能一页取全某状态的全部单子，不用翻页。
LIST_URL = "https://rx.shop.jd.com/rx/rxInfo_list.action?_JDMOMID_=1559"
STATUS_URL = LIST_URL + "&rxStatus={code}&pageSize=200"
DETAIL_URL = "https://rx.shop.jd.com/rx/rxInfo_auditView.action?rxId={rx}"

TABS = ("待审核", "审核通过", "审核未通过", "已取消", "审核超时", "待开方", "待补充处方", "已完成")
COUNT_RE = re.compile(r"(" + "|".join(TABS) + r")\s*\((\d+)\)")

# 详情页正文形如「处方编号： 134580704877151 处方科室： 医院名称： 天津北辰保民医院有限公司」，
# 值就是标签后面那一段纯文本；空值时紧跟的是**下一个标签**，所以用标签集合做右边界。
DETAIL_LABELS = ("处方编号", "处方科室", "医院名称", "处方日期", "执业药师",
                 "医生名称", "诊断结果", "登记客服", "登记时间", "下单时间")
DETAIL_STOP = ("药品信息", "商品信息", "意见审核", "支付方式", "配送地址", "返回处方管理",
               "用户联系方式", "患者姓名", "与本人关系", "年龄", "性别", "证件类型",
               "证件号码", "证件图片", "过往病史", "个人习惯", "商品总额", "运费",
               "返现", "应付金额")

# 老版本页面把值放在 input.value 里，保留按 name 关键字兜底的映射（新页面取不到时才会用到）。
DETAIL_FIELDS = (
    ("doctor", "医生名称"),
    ("pharmacist", "执业药师"),
    ("pha", "执业药师"),
    ("hospital", "医院名称"),
    ("hos", "医院名称"),
    ("department", "处方科室"),
    ("rxd", "处方科室"),
    ("rxnumber", "处方编号"),
    ("prescriptionno", "处方编号"),
    ("rxn", "处方编号"),
)

DDL = [
    """
    CREATE TABLE IF NOT EXISTS `jd_prescription_daily` (
      `记录日期` DATE NOT NULL,
      `店铺` VARCHAR(64) NOT NULL,
      `rxId` VARCHAR(32) NOT NULL,
      `预约单号` VARCHAR(32) NULL,
      `订单号` VARCHAR(64) NULL,
      `预约时间` DATETIME NULL,
      `下单时间` DATETIME NULL,
      `登记时间` DATETIME NULL,
      `状态` VARCHAR(32) NULL,
      `医生名称` VARCHAR(64) NULL,
      `处方编号` VARCHAR(64) NULL,
      `处方科室` VARCHAR(64) NULL,
      `医院名称` VARCHAR(128) NULL,
      `执业药师` VARCHAR(64) NULL,
      `商品` VARCHAR(255) NULL,
      `SKU编码` VARCHAR(64) NULL,
      `应付金额` DECIMAL(12,2) NULL,
      `原始行` VARCHAR(1000) NULL,
      `采集时间` DATETIME NOT NULL,
      PRIMARY KEY (`记录日期`, `店铺`, `rxId`),
      KEY `idx_doctor` (`店铺`, `医生名称`),
      KEY `idx_status` (`记录日期`, `状态`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
      COMMENT='京东处方单（处方管理页只读采集）'
    """,
    """
    CREATE TABLE IF NOT EXISTS `jd_prescription_status_history` (
      `记录日期` DATE NOT NULL,
      `店铺` VARCHAR(64) NOT NULL,
      `rxId` VARCHAR(32) NOT NULL,
      `状态` VARCHAR(32) NOT NULL,
      `预约单号` VARCHAR(32) NULL,
      `订单号` VARCHAR(64) NULL,
      `预约时间` DATETIME NULL,
      `下单时间` DATETIME NULL,
      `登记时间` DATETIME NULL,
      `处方编号` VARCHAR(64) NULL,
      `医生名称` VARCHAR(64) NULL,
      `医院名称` VARCHAR(128) NULL,
      `处方科室` VARCHAR(64) NULL,
      `首次发现时间` DATETIME NOT NULL,
      `最后确认时间` DATETIME NOT NULL,
      `确认次数` INT NOT NULL DEFAULT 1,
      PRIMARY KEY (`记录日期`,`店铺`,`rxId`,`状态`),
      KEY `idx_rx_first` (`店铺`,`rxId`,`首次发现时间`),
      KEY `idx_status_first` (`记录日期`,`状态`,`首次发现时间`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
      COMMENT='京东处方状态历史：首次发现某状态的时间，按小时可追溯'
    """,
    """
    CREATE TABLE IF NOT EXISTS `jd_prescription_status` (
      `记录日期` DATE NOT NULL,
      `店铺` VARCHAR(64) NOT NULL,
      `待审核` INT NULL,
      `审核通过` INT NULL,
      `审核未通过` INT NULL,
      `已取消` INT NULL,
      `审核超时` INT NULL,
      `待开方` INT NULL,
      `待补充处方` INT NULL,
      `已完成` INT NULL,
      `采集时间` DATETIME NOT NULL,
      PRIMARY KEY (`记录日期`, `店铺`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
      COMMENT='京东处方各页签计数（平台判定口径）'
    """,
]


def connect():
    mysql = json.loads(open(ROOT_CONFIG, encoding="utf-8").read())["mysql"]
    return pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=mysql["database"],
        charset=mysql.get("charset", "utf8mb4"), autocommit=False,
        init_command="SET time_zone='+08:00'",
    )


def evaluate(ws_url: str, expr: str, timeout: int = 30):
    ws = websocket.create_connection(ws_url, timeout=timeout, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") != 1:
                continue
            return ((msg.get("result") or {}).get("result") or {}).get("value")
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass


def open_tab(port: int, url: str, ready_len: int = 800) -> str:
    base = f"http://127.0.0.1:{port}"
    created = requests.put(f"{base}/json/new?{quote(url, safe='')}", timeout=15)
    created.raise_for_status()
    ws_url = created.json()["webSocketDebuggerUrl"]
    for _ in range(15):
        time.sleep(3)
        state = evaluate(ws_url, "document.readyState + '|' + (document.body?document.body.innerText.length:0)")
        if not state:
            continue
        parts = str(state).split("|")
        if parts[0] == "complete" and int(parts[1] or 0) >= ready_len:
            break
    return ws_url


def navigate(ws_url: str, url: str, marker: str, ready_len: int = 300) -> bool:
    """复用同一个 tab 跳转。必须等地址真正变成目标页再读，否则会读到上一页的内容。"""
    ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                break
    finally:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
    for _ in range(15):
        time.sleep(2)
        state = evaluate(ws_url, "JSON.stringify([location.href, document.readyState, "
                                 "(document.body?document.body.innerText.length:0)])")
        if not state:
            continue
        try:
            href, ready, length = json.loads(state)
        except (ValueError, TypeError):
            continue
        if marker in str(href) and ready == "complete" and int(length or 0) >= ready_len:
            return True
    return False


def read_counts(ws_url: str) -> dict[str, int]:
    text = evaluate(ws_url, "(document.body?document.body.innerText:'').replace(/\\s+/g,' ')") or ""
    found = {name: 0 for name in TABS}
    for name, num in COUNT_RE.findall(text):
        found[name] = int(num)
    return found


ACTIVE_TABS = (
    (1, "待审核"),
    (9, "审核超时"),
    (77, "待开方"),
    (20, "待补充处方"),
)


def read_tabs(ws_url: str) -> list[dict]:
    """页签自带状态码（li[data-t]）与计数，比从正文猜状态可靠得多。"""
    raw = evaluate(ws_url, r"""JSON.stringify([...document.querySelectorAll('ul.fe-tab-hd li')]
        .map(e => {
          const txt = (e.innerText || '').replace(/\s+/g, '');
          const m = txt.match(/\((\d+)\)/);
          return {code: e.getAttribute('data-t'),
                  name: txt.replace(/\(\d+\)/, ''),
                  count: m ? Number(m[1]) : 0};
        }))""")
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []




def read_rows(ws_url: str) -> list[dict]:
    """处方列表不是标准表格（35 个 table 全是布局），所以按正文切块：

    每条记录以「预约单号：」开头，页面上的 详情 链接（rxInfo_auditView）按同一顺序排列，
    两边按下标配对即可拿到 rxId。
    """
    raw = evaluate(ws_url, r"""JSON.stringify((() => {
        const text = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
        const ids = [...document.querySelectorAll('a[href*="rxInfo_auditView"]')]
            .map(a => {
                const m = (a.getAttribute('href') || '').match(/rxId=(\d+)/);
                return m ? m[1] : null;
            });
        // 只按「预约单号：数字」切，避免筛选区那个没有值的「预约单号：」多切出一块
        const parts = text.split(/预约单号：(?=\d)/).slice(1);
        return parts.map((chunk, i) => ({
            rxId: ids[i] || null,
            text: ('预约单号：' + chunk).slice(0, 900)
        }));
    })())""")
    try:
        return json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []


def read_detail(ws_url: str) -> dict:
    """详情页正文 + 表单值一并取出（正文里的「标签：值」才是医生/医院/药师的来源）。"""
    raw = evaluate(ws_url, r"""JSON.stringify({
        text: (document.body ? document.body.innerText : '').replace(/\s+/g, ' '),
        inputs: [...document.querySelectorAll('input, textarea, select')]
            .map(el => ({k: (el.name || el.id || '').trim(), v: (el.value || '').trim()}))
            .filter(x => x.v && x.v.length < 120)
    })""")
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def parse_detail_text(text: str) -> dict[str, str | None]:
    """从详情页正文里按标签取值。

    空值是常态（客服/用户上传的处方没有医生、科室京东基本不填），所以取到的片段若含有
    「：」或过长，说明越界吃到了下一个字段，一律当空处理，宁缺毋滥。
    """
    flat = re.sub(r"[\s\u00a0]+", " ", text or "")
    stops = "|".join(DETAIL_LABELS + DETAIL_STOP)
    out: dict[str, str | None] = {}
    for label in DETAIL_LABELS:
        m = re.search(label + r"：\s*(.*?)\s*(?=" + stops + r"：|$)", flat)
        value = (m.group(1) if m else "").strip()
        if "：" in value or len(value) > 64:
            value = ""
        out[label] = value or None
    return out


def parse_row(text: str) -> dict:
    def grab(pattern: str) -> str | None:
        m = re.search(pattern, text)
        return m.group(1) if m else None

    when = grab(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
    sku = grab(r"SKU编码：(\d+)")
    goods = None
    amount = grab(r"应付金额：([\d.]+)")
    if amount:
        tail = text.split(f"应付金额：{amount}", 1)[1]
        goods = tail.split("SKU编码：", 1)[0].strip()[:255] if "SKU编码：" in tail else None
    status = None
    for name in ("待补充处方", "审核未通过", "审核超时", "审核通过", "等待审核", "待开方", "已取消", "已完成", "拆单中", "处方通过"):
        if name in text:
            status = name
            break
    return {
        "预约时间": when,
        "预约单号": grab(r"预约单号：(\d+)"),
        "订单号": grab(r"订单号：(\d+)"),
        "应付金额": amount,
        "商品": goods,
        "SKU编码": sku,
        "状态": status,
    }


def pick_dt(text: str, label: str) -> str | None:
    """时间字段只认标准时间戳；空值时后面紧跟的是下一个标签名（如「药品信息」），不能当值。"""
    m = re.search(label + r"：\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", text)
    return m.group(1) if m else None


def collect_detail(ws_url: str, rx: str) -> dict[str, str | None]:
    """打开单条处方详情，返回 医生/医院/科室/药师/处方编号/下单时间/登记时间。"""
    if not navigate(ws_url, DETAIL_URL.format(rx=rx), "rxInfo_auditView", ready_len=300):
        raise RuntimeError("详情页没打开")
    # 详情页是服务端渲染 + 少量 JS 回填，刚 complete 时字段可能还没就位，读两遍
    time.sleep(1.5)
    detail = read_detail(ws_url)
    if len(detail.get("inputs") or []) < 30:
        time.sleep(2.5)
        detail = read_detail(ws_url)
    text = detail.get("text") or ""
    fields = parse_detail_text(text)
    fields["下单时间"] = pick_dt(text, "下单时间") or fields.get("下单时间")
    fields["登记时间"] = pick_dt(text, "登记时间") or fields.get("登记时间")
    # 兜底：老版本页面把值放在 input.value 里，按 name 里的英文关键字映射
    for item in detail.get("inputs") or []:
        key = str(item.get("k") or "").lower()
        value = str(item.get("v") or "").strip()
        if not value:
            continue
        for needle, column in DETAIL_FIELDS:
            # 纯数字多半是隐藏 id；但处方编号本身就是数字，要放行
            if value.isdigit() and column != "处方编号":
                continue
            if needle in key and not fields.get(column):
                fields[column] = value[:128]
                break
    return fields


DETAIL_COLUMNS = ("下单时间", "登记时间", "医生名称", "医院名称", "处方科室", "执业药师", "处方编号")


def pending_detail_rows(client: str, limit: int) -> list[tuple]:
    """库里还没取到详情的单，按 rxId 直开详情页回补（老单也能打开，2026-11-19 实测）。

    客服上传 / 用户上传的处方本来就没有医生和处方编号，详情页给不出东西，
    这类单每次都会重新排队——所以调用方必须限次，不能无限重试。
    """
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT `记录日期`, `rxId` FROM `jd_prescription_daily` "
                "WHERE `店铺`=%s AND COALESCE(`医生名称`,'')='' AND COALESCE(`处方编号`,'')='' "
                "ORDER BY `记录日期` DESC, `rxId` DESC LIMIT %s",
                (client, limit),
            )
            return list(cursor.fetchall())
    finally:
        conn.close()


def save_details(client: str, updates: list[tuple]) -> int:
    """按 (记录日期, rxId) 回写详情列；空值不覆盖已有值。"""
    if not updates:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cursor:
            n = 0
            for day, rx, fields in updates:
                cursor.execute(
                    "UPDATE `jd_prescription_daily` SET "
                    "`医生名称`=COALESCE(%s,`医生名称`), `医院名称`=COALESCE(%s,`医院名称`), "
                    "`处方科室`=COALESCE(%s,`处方科室`), `执业药师`=COALESCE(%s,`执业药师`), "
                    "`处方编号`=COALESCE(%s,`处方编号`), `下单时间`=COALESCE(%s,`下单时间`), "
                    "`登记时间`=COALESCE(%s,`登记时间`) "
                    "WHERE `记录日期`=%s AND `店铺`=%s AND `rxId`=%s",
                    (fields.get("医生名称"), fields.get("医院名称"), fields.get("处方科室"),
                     fields.get("执业药师"), fields.get("处方编号"), fields.get("下单时间"),
                     fields.get("登记时间"), day, client, rx),
                )
                n += cursor.rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def save_status_history(client: str, day, now, records: list[dict]) -> int:
    payload = []
    for r in records:
        rx = r.get("rxId")
        if not rx:
            continue
        payload.append((
            day, client, str(rx), r.get("状态") or "未知", r.get("预约单号"), r.get("订单号"),
            r.get("预约时间"), r.get("下单时间"), r.get("登记时间"), r.get("处方编号"),
            r.get("医生名称"), r.get("医院名称"), r.get("处方科室"), now, now,
        ))
    if not payload:
        return 0
    conn = connect()
    try:
        with conn.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO `jd_prescription_status_history` "
                "(`记录日期`,`店铺`,`rxId`,`状态`,`预约单号`,`订单号`,`预约时间`,`下单时间`,`登记时间`,"
                "`处方编号`,`医生名称`,`医院名称`,`处方科室`,`首次发现时间`,`最后确认时间`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `最后确认时间`=VALUES(`最后确认时间`),`确认次数`=`确认次数`+1,"
                "`预约单号`=COALESCE(VALUES(`预约单号`),`预约单号`),"
                "`订单号`=COALESCE(VALUES(`订单号`),`订单号`),"
                "`预约时间`=COALESCE(VALUES(`预约时间`),`预约时间`),"
                "`下单时间`=COALESCE(VALUES(`下单时间`),`下单时间`),"
                "`登记时间`=COALESCE(VALUES(`登记时间`),`登记时间`),"
                "`处方编号`=COALESCE(VALUES(`处方编号`),`处方编号`),"
                "`医生名称`=COALESCE(VALUES(`医生名称`),`医生名称`),"
                "`医院名称`=COALESCE(VALUES(`医院名称`),`医院名称`),"
                "`处方科室`=COALESCE(VALUES(`处方科室`),`处方科室`)",
                payload,
            )
        conn.commit()
        return len(payload)
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="京东处方只读采集")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--client", required=True, help="店铺/客户名，如 保民")
    # 默认不点详情：处方明细另有系统在采，这里高频点详情只会增加封号风险。
    # 列表页的状态/计数已经够看板用；真要补详情再显式传 --details N。
    parser.add_argument("--details", type=int, default=0, help="最多抓多少单详情（默认 0，不点详情）")
    parser.add_argument("--backfill", type=int, default=0,
                        help="回补库里还缺医生名的老单详情，最多 N 单（按 rxId 直开，默认 0）")
    parser.add_argument("--date", help="记录日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    now = dt.datetime.now().replace(microsecond=0)
    day = dt.date.fromisoformat(args.date) if args.date else now.date()

    ws_url = open_tab(args.port, LIST_URL)
    tabs = read_tabs(ws_url)
    counts = {t["name"]: t["count"] for t in tabs}
    print(f"[{args.client}] 页签计数 {counts}")

    # 全部处方有一万多单，抓不完也没意义；只取活跃状态（待审核/超时/待开方/待补充）的明细
    records: list[dict] = []
    seen: set[str] = set()
    for code, name in ACTIVE_TABS:
        if not counts.get(name):
            continue
        if not navigate(ws_url, STATUS_URL.format(code=code), f"rxStatus={code}", ready_len=600):
            print(f"  状态 {name} 页没打开，跳过")
            continue
        rows = read_rows(ws_url)
        print(f"  页签 {name}: {len(rows)} 行（计数 {counts.get(name)}）")
        for row in rows:
            if not row["rxId"] or row["rxId"] in seen:
                continue
            seen.add(row["rxId"])
            parsed = parse_row(row["text"])
            parsed["rxId"] = row["rxId"]
            parsed["原始行"] = row["text"][:1000]
            if not parsed.get("状态"):
                parsed["状态"] = name
            parsed.update({"下单时间": None, "登记时间": None, "医生名称": None,
                           "处方编号": None, "处方科室": None, "医院名称": None, "执业药师": None})
            records.append(parsed)

    detail_budget = args.details
    for parsed in records:
        if not parsed["rxId"] or detail_budget <= 0:
            continue
        try:
            fields = collect_detail(ws_url, parsed["rxId"])
            for column in DETAIL_COLUMNS:
                parsed[column] = fields.get(column)
            detail_budget -= 1
        except Exception as exc:  # noqa: BLE001
            print(f"  详情失败 rxId={parsed['rxId']}: {type(exc).__name__}")

    print(f"[{args.client}] 明细 {len(records)} 条，抓到医生名 "
          f"{sum(1 for r in records if r.get('医生名称'))} 条")

    if args.backfill > 0:
        if args.dry_run:
            print("  --backfill 要写库，dry-run 下跳过")
        else:
            pending = pending_detail_rows(args.client, args.backfill)
            print(f"[{args.client}] 待回补详情的存量单 {len(pending)} 条")
            updates: list[tuple] = []
            for i, (pday, rx) in enumerate(pending):
                if i:
                    time.sleep(4)  # 逐单打开，拉开间隔，别把风控惹起来
                try:
                    fields = collect_detail(ws_url, str(rx))
                    updates.append((pday, rx, fields))
                    # 逐单落库：中途被打断（门店周期会停掉浏览器）也不丢已取到的数据
                    save_details(args.client, [(pday, rx, fields)])
                    print(f"  回补 {rx}: 医生={fields.get('医生名称')} 医院={fields.get('医院名称')}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  回补失败 rxId={rx}: {type(exc).__name__}")
            print(f"[{args.client}] 回补 {len(updates)} 单")

    if args.dry_run:
        for r in records[:5]:
            print("  ", {k: r.get(k) for k in ("rxId", "预约单号", "状态", "医生名称", "应付金额")})
        return 0

    conn = connect()
    try:
        with conn.cursor() as cursor:
            for ddl in DDL:
                cursor.execute(ddl)
            cursor.execute(
                "INSERT INTO `jd_prescription_status` (`记录日期`,`店铺`,`待审核`,`审核通过`,"
                "`审核未通过`,`已取消`,`审核超时`,`待开方`,`待补充处方`,`已完成`,`采集时间`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                "`待审核`=VALUES(`待审核`),`审核通过`=VALUES(`审核通过`),"
                "`审核未通过`=VALUES(`审核未通过`),`已取消`=VALUES(`已取消`),"
                "`审核超时`=VALUES(`审核超时`),`待开方`=VALUES(`待开方`),"
                "`待补充处方`=VALUES(`待补充处方`),`已完成`=VALUES(`已完成`),"
                "`采集时间`=VALUES(`采集时间`)",
                (day, args.client, counts["待审核"], counts["审核通过"], counts["审核未通过"],
                 counts["已取消"], counts["审核超时"], counts["待开方"], counts["待补充处方"],
                 counts["已完成"], now),
            )
            sql = (
                "INSERT INTO `jd_prescription_daily` (`记录日期`,`店铺`,`rxId`,`预约单号`,`订单号`,"
                "`预约时间`,`下单时间`,`登记时间`,`状态`,`医生名称`,`处方编号`,`处方科室`,"
                "`医院名称`,`执业药师`,`商品`,`SKU编码`,`应付金额`,`原始行`,`采集时间`) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE `状态`=VALUES(`状态`),"
                "`医生名称`=COALESCE(VALUES(`医生名称`),`医生名称`),"
                "`处方编号`=COALESCE(VALUES(`处方编号`),`处方编号`),"
                "`处方科室`=COALESCE(VALUES(`处方科室`),`处方科室`),"
                "`医院名称`=COALESCE(VALUES(`医院名称`),`医院名称`),"
                "`执业药师`=COALESCE(VALUES(`执业药师`),`执业药师`),"
                "`下单时间`=COALESCE(VALUES(`下单时间`),`下单时间`),"
                "`登记时间`=COALESCE(VALUES(`登记时间`),`登记时间`),"
                "`应付金额`=COALESCE(VALUES(`应付金额`),`应付金额`),"
                "`原始行`=VALUES(`原始行`),"
                "`采集时间`=VALUES(`采集时间`)"
            )
            payload = []
            for r in records:
                if not r.get("rxId"):
                    continue
                payload.append((
                    day, args.client, r["rxId"], r.get("预约单号"), r.get("订单号"),
                    r.get("预约时间"), r.get("下单时间"), r.get("登记时间"), r.get("状态"),
                    r.get("医生名称"), r.get("处方编号"), r.get("处方科室"), r.get("医院名称"),
                    r.get("执业药师"), r.get("商品"), r.get("SKU编码"), r.get("应付金额"),
                    r.get("原始行"), now,
                ))
            # 明细按「当天活跃单快照」维护：本轮没抓到的当天旧行要清掉，
            # 否则单子处理完还留在表里，看板上的单数会只增不减。
            # ⚠ 不能按采集时间整片删：详情（医生/登记时间/处方编号）是低频补采的，
            # 10 分钟一轮的列表任务会把详情行连带删掉、再以 NULL 重写（2026-11-19 实测）。
            # 改为只删「本轮批次里没有的单号」，详情列用 COALESCE 保留。
            order_nos = [r[3] for r in payload if r[3]]
            if order_nos:
                marks = ",".join(["%s"] * len(order_nos))
                cursor.execute(
                    "DELETE FROM `jd_prescription_daily` "
                    "WHERE `记录日期`=%s AND `店铺`=%s AND `采集时间` < %s "
                    "AND COALESCE(`预约单号`, '') NOT IN (" + marks + ")",
                    (day, args.client, now, *order_nos),
                )
            else:
                cursor.execute(
                    "DELETE FROM `jd_prescription_daily` "
                    "WHERE `记录日期`=%s AND `店铺`=%s AND `采集时间` < %s",
                    (day, args.client, now),
                )
            if payload:
                cursor.executemany(sql, payload)
        conn.commit()
        print(f"[{args.client}] 已写库 {len(payload)} 条")
    finally:
        conn.close()
    history_n = save_status_history(args.client, day, now, records)
    print(f"[{args.client}] 状态历史 {history_n} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
