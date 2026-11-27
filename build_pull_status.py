#!/usr/bin/env python3
"""店铺拉取状态（7.2）：从各平台已落库数据推导「有没有在跑、几点跑的、拉了多少」。

只读现有采集结果表，不触发采集、不改任何现有表。每次运行覆盖式刷新
`bi_pull_status`，并追加一条 `bi_pull_log`（巡检记录）。

判定规则：
- ok      最近拉取时间在 STALE_MINUTES 内，且当天有量
- stale   有历史数据但超过阈值未刷新（采集失败、登录失效、断数）
- missing 从未拉到 / 当天无记录

「登录状态」不靠猜：读该店铺采集单元 journal 里 login_recovery 最近一次的判定
（`{"state": "not_login_landing"}` 之类），映射成 已登录 / 未登录。
查不到日志就写「未探明」并说明原因，绝不用「拉取状态=ok」反推登录正常。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sqlite3
import subprocess
import sys

import pymysql

ROOT = pathlib.Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
STALE_MINUTES = 120

# 店铺 <-> 采集单元的登记表（STORE_LABEL / JD_STORE_KEY / MT_UNIT）。
CYCLES_DIR = pathlib.Path("/etc/subsidy-store-cycles")
LOGIN_LOOKBACK_DAYS = 3

# login_recovery 的判定 -> (登录状态, 说明)。表里没有的状态一律「未知」，
# 不往「已登录」上猜，避免看板出现假绿灯。
# 注意：采集器自己的结果也打 `"state": "success"`，所以下面的正则只认这份词表，
# 否则会把「采集成功」误读成「登录正常」。
LOGIN_STATES = {
    "not_login_landing": ("已登录", "采集检查时未落到登录页"),
    "submitted_and_redirected": ("已登录", "登录后已跳转"),
    "login_landing": ("未登录", "采集检查时停在登录页"),
    "awaiting_human": ("未登录", "等待人工验证"),
    "human_verification_required": ("未登录", "触发风控/需人工验证"),
    "autofill_not_ready": ("未登录", "登录页未自动填充，需人工"),
    "submit_not_found": ("未登录", "登录控件未找到"),
    "submit_not_pressed": ("未登录", "登录按钮未按下"),
    "frame_not_found": ("未知", "登录页框架未找到"),
    "detect_only": ("未知", "只做检测，未判定登录"),
    "dry_run_would_submit": ("未知", "演练模式，未真正登录"),
    "captured": ("未知", "抓取调试模式"),
}
STATE_RE = re.compile(
    r'"state":\s*"(' + "|".join(LOGIN_STATES) + r')"'
)
STAMP_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}:\d{2})")

# 天猫采集端（147）通过 receiver 回传的 state/error_code 里已经带登录/风控信号，
# 无需改采集器：采集器 _assert_page_ready 命中"请登录/访问受限/验证码"会抛异常，
# run_job 归类为 login_required / risk_control 并随数据包上传。
TMALL_DB = pathlib.Path("/opt/subsidy/tmall-ecommerce-data/state.sqlite3")
TMALL_STORE_KEYS = {"保民": "default", "璟和堂": "jinghetang"}
TMALL_LOGIN_CODES = {"login_required", "risk_control"}

JD_SOURCES = [
    ("保民", "jd_136186144"),
    ("百恩堂", "jd_67337414"),
    ("津南南无门诊", "jd_19924616"),
    ("福康综合门诊", "jd_135003681"),
    ("璟和堂", "jd_203228988"),
]
MT_SOURCES = [
    ("adjust_price", "保民"),
    ("baientang", "百恩堂"),
    ("mt_32187777", "津南南无门诊"),
    ("mt_33770492", "福康综合门诊"),
    ("mt_34461640", "璟和堂"),
]

# 跨店铺共享的数据源在 bi_pull_status 里统一挂在「全部」名下，没有独立登录态。
SHARED_CLIENT = "全部"

DDL = [
    """
    CREATE TABLE IF NOT EXISTS `bi_pull_status` (
      `巡检时间` DATETIME NOT NULL,
      `平台` VARCHAR(16) NOT NULL,
      `客户` VARCHAR(64) NOT NULL,
      `数据源` VARCHAR(64) NOT NULL,
      `登录状态` VARCHAR(16) NULL COMMENT '已登录/未登录/未探明/不适用',
      `登录状态说明` VARCHAR(255) NULL COMMENT '判定依据（journal 里的 state + 时间）',
      `拉取状态` VARCHAR(16) NOT NULL COMMENT 'ok/stale/missing',
      `最近拉取时间` DATETIME NULL,
      `今日拉取量` INT NULL,
      `异常说明` VARCHAR(255) NULL,
      PRIMARY KEY (`平台`,`客户`,`数据源`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
      COMMENT='店铺数据拉取状态（巡检快照）'
    """,
    """
    CREATE TABLE IF NOT EXISTS `bi_pull_log` (
      `巡检时间` DATETIME NOT NULL,
      `数据源` VARCHAR(64) NOT NULL,
      `结果` VARCHAR(16) NOT NULL,
      `行数` INT NULL,
      `说明` VARCHAR(255) NULL,
      PRIMARY KEY (`巡检时间`,`数据源`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
      COMMENT='拉取巡检日志（每次巡检落一条）'
    """,
]

# 老库已经建过表，CREATE TABLE IF NOT EXISTS 不会补列，这里单独升级一次。
MIGRATIONS = [
    "ALTER TABLE `bi_pull_status` ADD COLUMN `登录状态说明` VARCHAR(255) NULL "
    "COMMENT '判定依据（journal 里的 state + 时间）' AFTER `登录状态`",
]


def connect():
    mysql = json.loads(CONFIG.read_text(encoding="utf-8"))["mysql"]
    return pymysql.connect(
        host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
        password=mysql["password"], database=mysql.get("dw_database") or mysql["database"],
        charset=mysql.get("charset", "utf8mb4"), autocommit=False,
        init_command="SET time_zone='+08:00'",
    )


def ensure_tables(conn) -> None:
    with conn.cursor() as cursor:
        for ddl in DDL:
            cursor.execute(ddl)
        for ddl in MIGRATIONS:
            try:
                cursor.execute(ddl)
            except pymysql.err.OperationalError as exc:
                # 1060 = Duplicate column name，说明之前已经升级过
                if exc.args[0] != 1060:
                    raise
    conn.commit()


def store_units() -> dict[tuple[str, str], str]:
    """「(店铺, 平台) -> 采集单元」；单元名就是登录态的证据来源。"""
    units: dict[tuple[str, str], str] = {}
    for env in sorted(CYCLES_DIR.glob("*.env")):
        try:
            text = env.read_text(encoding="utf-8")
        except OSError:
            continue
        cfg = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            cfg[key.strip()] = value.strip()
        label = cfg.get("STORE_LABEL")
        if not label:
            continue
        if cfg.get("JD_STORE_KEY"):
            units[(label, "京东")] = f"jd-store-cycle@{cfg['JD_STORE_KEY']}.service"
        if cfg.get("MT_UNIT"):
            units[(label, "美团")] = cfg["MT_UNIT"]
    return units


def tmall_login_state(client: str) -> tuple[str, str]:
    """从 receiver 的 SQLite 读天猫该店最近一次采集的登录/风控信号。

    判定只看最近的采集批次（按 collected_at 倒序）：
    - error_code 命中 login_required / risk_control -> 未登录（说明带错误码）
    - state=success -> 已登录（采集时能正常读到页面数据）
    - state=failed/partial 但错误是 response_timeout 等 -> 未探明（不猜是登录问题）
    - 读不到该店记录 -> 未探明
    """
    store_key = TMALL_STORE_KEYS.get(client)
    if not store_key or not TMALL_DB.exists():
        return "未探明", "无天猫采集状态源"
    try:
        conn = sqlite3.connect(str(TMALL_DB), timeout=5)
        with conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT state, error_count, payload_path FROM tmall_ecommerce_collections "
                "WHERE store_key=? ORDER BY collected_at DESC LIMIT 12",
                (store_key,),
            )
            rows = cur.fetchall()
    except (OSError, sqlite3.Error):
        return "未探明", "读取天猫采集状态失败"
    if not rows:
        return "未探明", "receiver 无该店采集记录"
    for state, error_count, payload_path in rows:
        errs = []
        if payload_path:
            try:
                payload = json.loads(pathlib.Path(payload_path).read_text(encoding="utf-8"))
                errs = payload.get("errors") or []
            except (OSError, ValueError):
                errs = []
        codes = {str(e.get("error_code") or "") for e in errs if isinstance(e, dict)}
        hit = codes & TMALL_LOGIN_CODES
        if hit:
            return "未登录", f"采集报 {','.join(sorted(hit))}（登录过期/风控，需重新登录）"
        if state == "success" and not errs:
            return "已登录", "最近采集成功，页面可读"
    return "未探明", "近期采集均未见明确登录信号"


def journal_login_state(unit: str) -> tuple[str, str]:
    """读单元 journal 里最近一次登录页判定，返回 (登录状态, 说明)。

    只认 login_recovery 自己打印的 `"state"`，不做任何推测：
    日志读不到 / 窗口内没有判定 -> 未探明。
    """
    try:
        proc = subprocess.run(
            ["journalctl", "-u", unit, "--since", f"-{LOGIN_LOOKBACK_DAYS} days",
             "--no-pager", "-o", "short-iso"],
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return "未探明", f"读日志失败（{type(exc).__name__}）"
    if proc.returncode != 0:
        return "未探明", "读日志失败"
    state = None
    stamp = ""
    for line in proc.stdout.splitlines():
        match = STATE_RE.search(line)
        if not match:
            continue
        state = match.group(1)
        # short-iso 首列形如 2026-11-18T10:41:00+0800，取到分钟即可
        found = STAMP_RE.search(line)
        stamp = f"{found.group(2)}-{found.group(3)} {found.group(4)}" if found else ""
    if state is None:
        return "未探明", f"近 {LOGIN_LOOKBACK_DAYS} 天采集日志无登录判定"
    label, note = LOGIN_STATES.get(state, ("未知", f"无法判定的状态 {state}"))
    return label, f"{note}（{stamp}）"


def attach_login(rows: list[tuple]) -> list[tuple]:
    """把登录态补进每一行（行内第 5 列），并在末尾追加说明列。"""
    units = store_units()
    cache: dict[tuple[str, str], tuple[str, str]] = {}
    filled = []
    for row in rows:
        platform, client = row[1], row[2]
        key = (client, platform)
        if platform == "天猫":
            key2 = (client, platform)
            if key2 not in cache:
                cache[key2] = tmall_login_state(client)
            label, note = cache[key2]
        elif client == SHARED_CLIENT:
            label, note = "不适用", "跨店铺共享数据源，登录态随 joyspace-subsidy-sync 判定"
        elif key in units:
            if key not in cache:
                cache[key] = journal_login_state(units[key])
            label, note = cache[key]
        else:
            label, note = "未探明", "未登记采集单元，无法定位登录日志"
        filled.append((row[0], row[1], row[2], row[3], label, note) + row[5:])
    return filled


def judge(last_seen, today_rows, now, stale_minutes):
    if last_seen is None:
        return "missing", "从未拉到数据"
    lag = (now - last_seen).total_seconds() / 60.0
    if lag > stale_minutes:
        return "stale", f"已 {lag/60:.1f} 小时未刷新（最后 {last_seen:%m-%d %H:%M}）"
    if not today_rows:
        return "stale", "今日无新增行"
    return "ok", None


def collect_jd(conn, client: str, schema: str, day: dt.date, now: dt.datetime):
    rows = []
    # 1) 经营报告：库内 jd_business_report_daily，有采集时间
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT MAX(`采集时间`) FROM `{schema}`.`jd_business_report_daily`")
            last_seen = cursor.fetchone()[0]
            cursor.execute(
                f"SELECT COUNT(*) FROM `{schema}`.`jd_business_report_daily` WHERE `日期`=%s", (day,)
            )
            today_rows = cursor.fetchone()[0]
        status, note = judge(last_seen, today_rows, now, STALE_MINUTES)
        rows.append((now, "京东", client, "京东经营报告", None, status, last_seen, today_rows, note))
    except pymysql.err.ProgrammingError:
        rows.append((now, "京东", client, "京东经营报告", None, "missing", None, None,
                     f"库 {schema}.jd_business_report_daily 不存在"))

    # 2) 商品销量：实际落在 adjust_price.esale_product（jd_product_analysis_daily 是历史遗留表，无写入）
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM `esale_product` WHERE `客户`=%s AND `平台`='京东' AND `更新日期`=%s",
            (client, day),
        )
        today_rows = cursor.fetchone()[0]
        cursor.execute(
            "SELECT MAX(`更新日期`) FROM `esale_product` WHERE `客户`=%s AND `平台`='京东'",
            (client,),
        )
        last_date = cursor.fetchone()[0]
    if today_rows:
        # esale_product 只有「更新日期」，没有采集时刻，所以最近拉取时间只能落到当天 0 点
        status, note = "ok", "源表只有日期，无采集时刻（按当日是否有数判断）"
        last_seen = dt.datetime.combine(day, dt.time(0, 0))
    elif last_date:
        status, note = "stale", f"今日无数据，最后日期 {last_date}"
        last_seen = dt.datetime.combine(last_date, dt.time(0, 0))
    else:
        status, note, last_seen = "missing", "从未拉到数据", None
    rows.append((now, "京东", client, "京东商品销量", None, status, last_seen, today_rows, note))
    return rows


def collect_meituan(conn, schema: str, client: str, day: dt.date, now: dt.datetime):
    """美团业务报告表没有采集时间列，information_schema.update_time 对 InnoDB 不可靠
    （buffer pool 未刷盘时停在旧值），因此改用 `esale_system` 当天是否有行来判断。
    有当天行 = 今天跑过；跨平台周期是每 10 分钟，一天多轮都写同一行（upsert）。
    """
    source = "美团经营报告"
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM `{schema}`.`ops_business_report`")
            total = cursor.fetchone()[0]
    except pymysql.err.ProgrammingError:
        return [(now, "美团", client, source, None, "missing", None, None,
                 f"库 {schema}.ops_business_report 不存在")]
    with conn.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM `esale_system` WHERE `客户`=%s AND `平台`='美团' AND `日期`=%s",
            (client, day),
        )
        today_rows = cursor.fetchone()[0]
        cursor.execute(
            "SELECT MAX(`日期`) FROM `esale_system` WHERE `客户`=%s AND `平台`='美团'",
            (client,),
        )
        last_date = cursor.fetchone()[0]
    if today_rows:
        status, note = "ok", "源表无采集时间，按当日是否有数判断"
        last_seen = dt.datetime.combine(day, dt.time(0, 0))
    elif last_date:
        status = "stale"
        note = f"今日无数据，最后日期 {last_date}"
        last_seen = dt.datetime.combine(last_date, dt.time(0, 0))
    else:
        status, note, last_seen = "missing", "从未拉到数据", None
    return [(now, "美团", client, source, None, status, last_seen, today_rows, note)]


def collect_tmall(conn, day: dt.date, now: dt.datetime):
    sql = ("SELECT MAX(`日期`), COUNT(*) FROM `esale_system` "
           "WHERE `平台`='天猫' AND `日期`=%s")
    with conn.cursor() as cursor:
        cursor.execute(sql, (day,))
        _d, today_rows = cursor.fetchone()
        cursor.execute("SELECT 客户, MAX(`日期`) FROM `esale_system` WHERE `平台`='天猫' GROUP BY 客户")
        clients = cursor.fetchall()
    rows = []
    for client, last_date in clients:
        # 快照表没有采集时刻，不能用 23:59 冒充「刚拉过」，统一落到当天 0 点
        last_seen = dt.datetime.combine(last_date, dt.time(0, 0)) if last_date else None
        status, note = judge(last_seen, today_rows if last_date == day else 0, now, 60 * 26)
        if status == "ok":
            note = "源表只有日期，无采集时刻（按当日是否有数判断）"
        rows.append((now, "天猫", client, "天猫经营快照", None, status, last_seen,
                     today_rows if last_date == day else 0, note))
    return rows


def inactive_pairs(conn) -> set[tuple[str, str]]:
    """读取 bi_store_config 里 `是否启用=0` 的 (客户,平台)，巡检时跳过。"""
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT `客户`,`平台` FROM `bi_store_config` WHERE `是否启用`=0")
            return {(row[0], row[1]) for row in cursor.fetchall()}
    except pymysql.err.ProgrammingError:
        return set()


def collect_shared_catalog(conn, day: dt.date, now: dt.datetime):
    """巡检跨店铺共享的京东活动目录（JoySpace 表格快照）。

    这份目录每天 11:05 由 joyspace-subsidy-sync 刷新，失败方式最隐蔽：
    JoySpace cookie 过期时同步会在等待页面阶段中止，目录停在旧日期，
    而各店铺的匹配照常出数——只是政策是旧的。所以必须单独按同步日期巡检，
    不能只看「各店今天有没有行」。
    """
    source = "京东活动目录"
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT `同步日期`, `更新时间`, COUNT(*) FROM `jd_subsidy_catalog` "
                "GROUP BY `同步日期`, `更新时间` ORDER BY `更新时间` DESC LIMIT 1"
            )
            latest = cursor.fetchone()
    except pymysql.err.ProgrammingError:
        return [(now, "京东", SHARED_CLIENT, source, None, "missing", None, None,
                 "表 jd_subsidy_catalog 不存在")]

    if not latest or latest[0] is None:
        return [(now, "京东", SHARED_CLIENT, source, None, "missing", None, None,
                 "从未同步过活动目录")]

    synced, updated, total = latest[0], latest[1], latest[2]
    last_seen = updated or dt.datetime.combine(synced, dt.time(0, 0))
    if synced == day:
        status, note = "ok", f"目录 {total} 行"
    elif synced == day - dt.timedelta(days=1) and now.time() < dt.time(11, 30):
        # 每天 11:05 才刷新一次，早上巡检不该把它判成异常。
        status, note = "ok", f"今日 11:05 同步窗口未到（当前目录 {synced:%m-%d}）"
    else:
        status = "stale"
        note = (
            f"活动目录停在 {synced:%m-%d}（已 {(day - synced).days} 天未刷新），"
            f"多半是 JoySpace 登录态过期或表格表头变更"
        )
    return [(now, "京东", SHARED_CLIENT, source, None, status, last_seen, total, note)]


def main() -> int:
    parser = argparse.ArgumentParser(description="刷新店铺拉取状态")
    parser.add_argument("--date", help="巡检日期 YYYY-MM-DD，默认今天")
    args = parser.parse_args()

    now = dt.datetime.now().replace(microsecond=0)
    day = dt.date.fromisoformat(args.date) if args.date else now.date()

    conn = connect()
    try:
        ensure_tables(conn)
        skip = inactive_pairs(conn)
        rows: list[tuple] = []
        for client, schema in JD_SOURCES:
            if (client, "京东") in skip:
                continue
            rows.extend(collect_jd(conn, client, schema, day, now))
        for schema, client in MT_SOURCES:
            if (client, "美团") in skip:
                continue
            rows.extend(collect_meituan(conn, schema, client, day, now))
        rows.extend(
            row for row in collect_tmall(conn, day, now)
            if (row[2], "天猫") not in skip
        )
        rows.extend(collect_shared_catalog(conn, day, now))
        rows = attach_login(rows)

        status_sql = (
            "INSERT INTO `bi_pull_status` (`巡检时间`,`平台`,`客户`,`数据源`,`登录状态`,"
            "`登录状态说明`,`拉取状态`,`最近拉取时间`,`今日拉取量`,`异常说明`) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
            "`巡检时间`=VALUES(`巡检时间`),`登录状态`=VALUES(`登录状态`),"
            "`登录状态说明`=VALUES(`登录状态说明`),"
            "`拉取状态`=VALUES(`拉取状态`),`最近拉取时间`=VALUES(`最近拉取时间`),"
            "`今日拉取量`=VALUES(`今日拉取量`),`异常说明`=VALUES(`异常说明`)"
        )
        log_sql = (
            "INSERT INTO `bi_pull_log` (`巡检时间`,`数据源`,`结果`,`行数`,`说明`) "
            "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE `结果`=VALUES(`结果`)"
        )
        with conn.cursor() as cursor:
            # 本表是「最近一次巡检的快照」：先清掉上一轮的旧行，
            # 既包含停用客户，也包含已下线/改名的数据源（否则 old 数据源永远挂着 STALE）。
            cursor.execute("DELETE FROM `bi_pull_status` WHERE `巡检时间` < %s", (now,))
            if skip:
                cursor.executemany(
                    "DELETE FROM `bi_pull_status` WHERE `客户`=%s AND `平台`=%s",
                    sorted(skip),
                )
            cursor.executemany(status_sql, rows)
            cursor.executemany(log_sql, [
                (now, f"{row[1]}/{row[2]}/{row[3]}", row[6], row[8], row[9]) for row in rows
            ])
        conn.commit()

        print(f"巡检 {now:%Y-%m-%d %H:%M:%S}，共 {len(rows)} 条数据源")
        for row in rows:
            flag = {"ok": "OK  ", "stale": "STALE", "missing": "MISS "}.get(row[6], row[6])
            print(f"  {flag} {row[1]:<4} {row[2]:<14} {row[3]:<12} 登录={row[4]:<4} "
                  f"行数={row[8]} {row[9] or ''}")
        bad = [row for row in rows if row[6] != "ok"]
        print(f"异常 {len(bad)} 条")
        return 0
    finally:
        conn.close()


# EC_DW_RESET_PAUSED_20261126
if __name__ == '__main__':
    raise SystemExit('ec_dw has been cleared; writer paused pending new schema')

if __name__ == "__main__":
    sys.exit(main())
