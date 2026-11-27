#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一期界面数据导出：只有表头 + 数据 + 末尾一段备注，不放任何说明性标题/来源行。

工作表顺序按原型页面：经营总览 → 交易分析 → 售后 → 医生 → 活动核算 → 价格雷达 →
竞品新品 → 实时库存 → 商品主数据 → 店铺拉取状态 → 未动销/禁售名单/在售商品 → 数据来源。
合规校验、今日巡检、活动测算：不做。
"""
import json
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pymysql
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "exports")
OUT = os.path.join(OUT_DIR, "一期界面数据_%s.xlsx" % datetime.now().strftime("%Y%m%d"))

MONEY, PCT, INT, DATE, DT = "#,##0.00", "0.00%", "#,##0", "yyyy-mm-dd", "yyyy-mm-dd hh:mm:ss"

HEAD_FONT = Font(name="微软雅黑", size=10, bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="2E75B6")
NOTE_FONT = Font(name="微软雅黑", size=9, color="C00000")
CYC_FONT = Font(name="微软雅黑", size=9, bold=True, color="1F4E79")
BODY_FONT = Font(name="微软雅黑", size=9)

STORES = [("保民", "jd_136186144"),
          ("福康综合门诊", "jd_135003681"), ("璟和堂", "jd_203228988")]

# 每页数据拉取周期：table() 自动把它加成最后一列。值可以是字符串，也可以是函数（按行取）。
CYCLE = {
    "01_经营总览": "每 10 分钟（各店错峰）；12:05 / 17:05 两次快照",
    "02_交易分析": "每 10 分钟（各店错峰）；动销率随门店周期",
    "03_售后自动退款预警": "每 10 分钟（随美团门店周期）",
    "04_医生开方监控": "每 10 分钟（随京东门店周期）",
    "04B_暂停订单小时明细": "每 10 分钟（随京东门店周期），当前整点快照，一有暂停单即抓",
    "05_活动核算": lambda r: {"京东": "每天 11:05（数据停在 08-18）",
                              "天猫": "已停（最后 08-14）",
                              "美团": "每 10 分钟（随美团门店周期）"}.get(r[0], ""),
    "06_价格雷达": "一次性试爬（2026-11-18），无定时任务",
    "07_竞品新品": "一次性试爬（2026-11-18），无定时任务",
    "08_实时库存": "手动导入（旺店通导出后），最近 11-18",
    "09_商品主数据": "手动导入（旺店通导出后），最近 11-18",
    # ⚠ 这里是「巡检」周期，不是「采集」周期：pull-status.timer 只读库里的采集结果，
    # 真正的采集按门店周期每 10 分钟跑（各店错峰 :00/:02/:04/:08）。
    "10_店铺拉取状态": "巡检每 05 分（pull-status.timer，只读巡检、不采集）；"
                       "采集按门店每 10 分钟（保民 :00、百恩堂 :02、福康 :04、璟和堂 :08）",
    "11_未动销商品": "每 10 分钟（随京东门店周期）",
    "12_禁售名单": "手动导入（商家给名单后），11-17",
    "13_在售商品": "每 10 分钟（随京东门店周期），按天留一行",
}


def num(v):
    from decimal import Decimal
    return float(v) if isinstance(v, Decimal) else v


def fetch(con, sql, args=()):
    cur = con.cursor()
    cur.execute(sql, args)
    return [tuple(num(x) for x in r) for r in cur.fetchall()]


def table(ws, row, headers, rows, fmts=None, widths=None):
    fmts = fmts or {}
    cyc = CYCLE.get(ws.title)
    if callable(cyc):  # 同一张表内节奏不一致（如按平台）→ 只能加一列
        headers = list(headers) + ["拉取周期"]
        rows = [list(x) + [cyc(x)] for x in rows]
        if widths:
            widths = list(widths) + [30]
    for j, h in enumerate(headers, 1):
        c = ws.cell(row=row, column=j, value=h)
        c.font, c.fill = HEAD_FONT, HEAD_FILL
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 30
    for i, data in enumerate(rows, 1):
        for j, v in enumerate(data, 1):
            c = ws.cell(row=row + i, column=j, value=v)
            c.font = BODY_FONT
            if fmts.get(j):
                c.number_format = fmts[j]
            c.alignment = Alignment(vertical="center")
    if rows:
        ws.auto_filter.ref = "A%d:%s%d" % (row, get_column_letter(len(headers)), row + len(rows))
    if widths:
        for i, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A%d" % (row + 1)
    last = row + len(rows)
    if isinstance(cyc, str):  # 整表节奏一致 → 表下一行说明，不占列
        line = last + 1
        ws.merge_cells(start_row=line, start_column=1, end_row=line, end_column=max(len(headers), 4))
        c = ws.cell(row=line, column=1, value="数据拉取周期：%s" % cyc)
        c.font = CYC_FONT
        c.alignment = Alignment(vertical="center", wrap_text=True)
        return line + 2
    return last + 2


def notes(ws, row, lines, ncols):
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=max(ncols, 4))
    c = ws.cell(row=row, column=1, value="备注：\n" + "\n".join("· " + x for x in lines))
    c.font = NOTE_FONT
    c.alignment = Alignment(vertical="top", wrap_text=True)
    ws.row_dimensions[row].height = min(17 * len(lines) + 26, 340)
    return row + 2


def z(v):
    return 0.0 if v is None else v


def build_04(wb, con):
    """3.4 医生开方监控：医生层 + 页面计数 + 订单状态三张表。

    单独成函数是为了能只重建这一页（交付版工作簿里有别的页被手工调过）。
    """
    ws = wb.create_sheet("04_医生开方监控")
    doc = fetch(con, "SELECT 记录日期,店铺,医生,处方单数,待审核,待开方,审核超时,待补充,最近下单时间,医院,处方科室 "
                     "FROM v_doctor_prescription ORDER BY 记录日期 DESC,店铺,医生")
    rx = fetch(con, "SELECT 记录日期,店铺,待审核,审核通过,审核未通过,已取消,审核超时,待开方,待补充处方,已完成,采集时间 FROM jd_prescription_status ORDER BY 店铺")
    order = fetch(con, "SELECT 记录日期,店铺,待付款,待出库,已出库,平台判定超时,状态说明 FROM v_order_status ORDER BY 店铺")
    rows = []
    for x in doc:
        name = x[2] if x[2] and x[2] not in ("未标注医生", "") else "未采集"
        rows.append([x[0], x[1], name, "未采集（平台判定）", "未采集（无接诊时刻）", int(x[6] or 0), None,
                     "未采集", int(x[3] or 0), int(x[4] or 0), x[9], x[10]])
    r = table(ws, 1, ["记录日期", "店铺", "医生", "应接单时限", "平均接单时长", "超时单数", "开方及时率",
                      "状态", "处方单数", "待审核", "医院", "处方科室"], rows,
              {1: DATE, 6: INT, 9: INT, 10: INT}, widths=[12, 12, 12, 18, 20, 10, 14, 12, 10, 8, 26, 14])
    r = table(ws, r, ["记录日期", "店铺", "待审核", "审核通过", "审核未通过", "已取消", "审核超时",
                      "待开方", "待补充处方", "已完成", "采集时间"], rx,
              {1: DATE, 11: DT}, widths=[12, 12, 8, 8, 10, 8, 8, 8, 10, 8, 20])
    r = table(ws, r, ["记录日期", "店铺", "待付款", "待出库", "已出库", "平台判定超时", "状态说明"], order,
              {1: DATE, 3: INT, 4: INT, 5: INT, 6: INT}, widths=[12, 14, 8, 8, 8, 12, 30])
    notes(ws, r, [
        "医生、医院、处方科室、执业药师、处方编号取自单条处方详情的正文（rxInfo_auditView），按 rxId 逐单打开；11-19 起采集器已能解析，11-18 存量单也已按 rxId 回补成功。",
        "「医生」为「未采集」的是客服上传 / 用户上传的处方：这类单本来就没有开方医生，不是采集失败。",
        "「处方科室」多数单为空（京东不强制填），11-19 有单填了「普通内科」，取到就展示。",
        "平均接单时长/开方及时率为空：详情页只有下单时间与客服登记时间，没有医生接诊时刻。",
        "应接单时限、超时判定、医师资质要求平台未给出，要电商确认。",
        "「超时单数」取订单侧「平台判定超时（暂停单）」，与处方页「审核超时」不是同一指标。",
    ], 12)
    return ws


def build_04_detail(wb, con):
    """处方级小时明细：状态首次发现时间来自采集器新增的历史表。"""
    ws = wb.create_sheet("04A_处方小时明细")
    rows = fetch(con, """
        SELECT 记录日期, 店铺,
               DATE_FORMAT(首次发现时间, '%%Y-%%m-%%d %%H:00:00') AS 小时,
               '首次发现状态' AS 时间来源,
               rxId, 预约单号, 订单号, 处方编号, 预约时间, 下单时间, 登记时间,
               状态, 医院名称, 处方科室, 医生名称, 首次发现时间, 最后确认时间, 确认次数
        FROM jd_prescription_status_history
        WHERE 记录日期 = (SELECT MAX(记录日期) FROM jd_prescription_status_history)
          AND 状态 IN ('审核超时', '待开方', '待审核', '等待审核', '待补充处方')
        ORDER BY 小时, 店铺, rxId
    """)
    r = table(ws, 1,
              ["记录日期", "店铺", "小时", "时间来源", "rxId", "预约单号", "订单号", "处方编号",
               "预约时间", "下单时间", "登记时间", "状态", "医院名称", "处方科室", "医生名称",
               "首次发现时间", "最后确认时间", "确认次数"],
              rows, {1: DATE, 9: DT, 10: DT, 11: DT, 16: DT, 17: DT, 18: INT},
              widths=[12, 14, 18, 14, 18, 18, 20, 22, 20, 20, 20, 12, 26, 14, 12, 20, 20, 10])
    notes(ws, r, [
        "小时来自本轮采集首次发现该处方状态的时刻，不是平台内部状态变更的真实时刻。",
        "同一张处方单进入不同状态会分别记录；首次发现时间保留，最后确认时间每 10 分钟更新一次。",
        "医生字段保留平台原值，可由机构自行对应哪个时段是哪个医生。",
    ], 14)
    return ws


def build_04_pause(wb, con):
    """当前整点快照：只要时刻、暂停单数和金额，不输出订单明细。"""
    ws = wb.create_sheet("04B_暂停订单小时明细")
    hour = datetime.now(ZoneInfo("Asia/Shanghai")).replace(minute=0, second=0, microsecond=0)
    rows = fetch(con, """
        SELECT p.记录日期, p.店铺,
               COUNT(DISTINCT p.订单号) AS 暂停单数,
               COALESCE(SUM(a.应付金额), 0) AS 金额
        FROM jd_order_pause_history p
        LEFT JOIN (
            SELECT 店铺, 订单号, MAX(应付金额) AS 应付金额
            FROM jd_prescription_daily
            WHERE 记录日期 = (SELECT MAX(记录日期) FROM jd_prescription_daily)
            GROUP BY 店铺, 订单号
        ) a ON a.店铺 = p.店铺 AND a.订单号 = p.订单号
        WHERE p.记录日期 = (SELECT MAX(记录日期) FROM jd_order_pause_history)
        GROUP BY p.记录日期, p.店铺
        ORDER BY p.店铺
    """)
    data = [[day, shop, hour, count, amount] for day, shop, count, amount in rows]
    r = table(ws, 1, ["记录日期", "店铺", "时刻", "暂停单数", "金额"], data,
              {1: DATE, 3: DT, 4: INT, 5: MONEY},
              widths=[12, 16, 20, 12, 16])
    notes(ws, r, [
        "时刻为导出时的当前整点快照；暂停单数取暂停单历史表中当天仍暂停的订单去重数。",
        "金额按订单号关联处方表应付金额；同一订单多张处方只取一次金额。",
        "不再输出订单号、商品、物流单等明细。",
    ], 5)
    return ws


def build_13(wb, con):
    """在售商品快照。规格取京东「销售属性」原值——同一商品编码下 SKUID 不同就是不同规格。"""
    ws = wb.create_sheet("13_在售商品")
    rows = []
    for shop, sch in STORES:
        got = fetch(con, """SELECT 记录日期,商品编码,商品名称,销售属性,SKUID,京东价,商品状态
                            FROM `%s`.jd_store_sku_normalized_daily
                            WHERE 记录日期=(SELECT MAX(记录日期) FROM `%s`.jd_store_sku_normalized_daily)
                            ORDER BY 商品编码""" % (sch, sch))
        for day, code, name, prop, sku, price, state in got:
            rows.append([shop, day, code, name, prop, sku, price, state])
    r = table(ws, 1, ["店铺", "记录日期", "商品编码", "商品名称", "规格", "SKUID", "京东价", "商品状态"],
              rows, {2: DATE, 7: MONEY}, widths=[14, 12, 18, 46, 28, 20, 12, 10])
    notes(ws, r, [
        "共 %d 行（%s）。" % (len(rows), "、".join("%s %d" % (s, sum(1 for x in rows if x[0] == s)) for s, _ in STORES)),
        "「规格」取京东销售属性原值（如「50mg*30片 3盒装」）。同一商品编码下 SKUID 不同就是不同规格/包装数量，只看商品名称分不出来。",
        "是在售快照，状态全为「上架」，不含已下架商品；「可用库存」源页面全空，库存请看 08 页。",
        "南无 3 店停用未纳入；天猫/美团没有同结构快照。",
    ], 8)
    return ws


def main():
    cfg = json.load(open(os.path.join(ROOT, "config.json")))["mysql"]
    con = pymysql.connect(host=cfg["host"], port=cfg["port"], user=cfg["user"],
                          password=cfg["password"], database=cfg["database"], charset="utf8mb4")
    wb = Workbook()
    wb.remove(wb.active)

    # 京东 7 日动销率（SKU 口径：近7日有销量 SKU / 当日在售 SKU）
    jd_rate = {}
    for shop, sch in STORES:
        for day in ("2026-11-19", "2026-11-18"):
            got = fetch(con, """
                SELECT COUNT(DISTINCT s.SKUID), COUNT(DISTINCT CASE WHEN p.销量>0 THEN s.SKUID END),
                       COUNT(p.唯一编码)
                FROM `%s`.jd_store_sku_normalized_daily s
                LEFT JOIN esale_product p
                  ON p.唯一编码=s.商品编码 AND p.平台='京东' AND p.客户=%%s AND p.更新日期=%%s
                WHERE s.记录日期=%%s""" % sch, (shop, day, day))
            if got and got[0][0]:
                # esale_product 只保留当天快照：历史日期没有销量行时算不出动销率，留空而不是记 0
                jd_rate[(shop, day)] = (round(float(got[0][1]) / float(got[0][0]), 6)
                                        if got[0][2] else None)

    # ---------------------------------------------------------------- 01 经营总览
    ws = wb.create_sheet("01_经营总览")
    ov = fetch(con, "SELECT 店铺,平台,数据日期,本月累计交易额,昨日交易额,商品动销率 FROM v_shop_overview ORDER BY 平台,店铺")
    latest = max((x[2] for x in ov), default=None)
    goods = {(r0[0], r0[1]): r0[2] for r0 in fetch(
        con, "SELECT 客户,平台,经营商品数量 FROM esale_system WHERE 日期=(SELECT MAX(日期) FROM esale_system)")}
    mm_rate = {(r0[0], r0[1]): r0[2] for r0 in fetch(con, "SELECT 店铺,平台,`7日动销率` FROM v_shop_movement")}
    intra = fetch(con, """SELECT 客户,平台,时点,当日交易额 FROM ec_dw.esale_shop_intraday
                          WHERE 记录日期=%s AND 数据质量='ok'""", (latest,))
    i12 = {(a, p, "12"): float(s or 0) for a, p, t, s in intra if str(t) == "12"}
    i17 = {(a, p, "17"): float(s or 0) for a, p, t, s in intra if str(t) == "17"}
    rows = []
    for shop, plat, day, month, yest, mv in ov:
        if plat == "京东":
            rate = jd_rate.get((shop, str(day)))
        elif plat == "美团":
            rate = mm_rate.get((shop, "美团"))
        else:
            rate = mv
        r12, r17 = i12.get((shop, plat, "12")), i17.get((shop, plat, "17"))
        if plat == "天猫":
            ym, yy = month, yest  # 昨日/当日无来源，留空不补零
        else:
            # 京东/美团 平台显示「-」即为真实 0：昨日/本月补 0.00
            # 12/17 点只认快照里 ok 的记录（页面「-」由快照侧按 0 落），没落库就留空
            ym, yy = z(month), z(yest)
        rows.append([plat, shop, day, ym, yy, r12, r17, rate, z(goods.get((shop, plat)))])
    kpi = [
        ["本月累计交易额", round(sum(x[3] or 0 for x in rows), 2)],
        ["昨日交易额", round(sum(x[4] or 0 for x in rows), 2)],
        ["当日交易额（12点快照合计）", round(sum(x[5] for x in rows if x[5] is not None), 2) if any(x[5] is not None for x in rows) else None],
    ]
    r = table(ws, 1, ["指标", "数值"], kpi, {2: MONEY}, widths=[30, 18])
    r = table(ws, r, ["平台", "店铺", "数据日期", "本月累计交易额", "昨日交易额", "当日12点交易额",
                      "当日17点交易额", "商品动销率", "经营商品数量"], rows,
              {3: DATE, 4: MONEY, 5: MONEY, 6: MONEY, 7: MONEY, 8: PCT, 9: INT},
              widths=[10, 18, 12, 17, 15, 16, 16, 12, 13])
    snap12 = "已落库" if i12 else "未落库"
    snap17 = "已落库" if i17 else "未到点（17:05 才跑）"
    notes(ws, r, [
        "动销率口径：京东=近7日有销量 SKU ÷ 当日在售 SKU（已按要求改成 7 天）；美团=平台近7日；天猫=平台值。",
        "京东/美团 平台显示「-」= 真实 0 成交：昨日/本月按 0.00 落；当日列由快照侧按 0.00 落（福康、璟和堂即是）。",
        "天猫两家昨日/12 点/17 点没有来源，留空；本月取平台值。天猫脚本未改造。",
        "快照节奏：12 点 12:05、17 点 17:05；本表生成于 %s，12 点快照%s，17 点快照%s。" % (
            datetime.now().strftime("%H:%M"), snap12, snap17),
        "12/17 点只认快照 ok 记录：京东页面「-」已按 0.00 落；整行没解析出来或当天无记录的才留空（如璟和堂京东 11-18 12 点）。",
        "津南南无三家停用，不在表内。",
    ], 9)

    # ---------------------------------------------------------------- 02 交易分析
    ws = wb.create_sheet("02_交易分析")
    rows = fetch(con, """
        SELECT d.平台, d.数据日期, d.店铺, d.当日12点交易额, d.当日17点交易额, d.昨日交易额, d.本月累计,
               d.日环比, d.7日环比, d.30日环比, d.非替尔泊肽占比
        FROM v_shop_daily d WHERE d.数据日期 >= '2026-11-18'
        ORDER BY d.数据日期 DESC, d.平台, d.店铺""")
    snap = {(a, p, str(t), str(d)): float(s or 0) for a, p, t, d, s in fetch(con, """
        SELECT 客户,平台,时点,DATE_FORMAT(记录日期,'%%Y-%%m-%%d'),当日交易额
        FROM ec_dw.esale_shop_intraday WHERE 记录日期 >= '2026-11-18' AND 数据质量='ok'""")}
    # 平台口径日环比（美团商品分析 gtv_amt.compareValue = 昨日 vs 前日，与昨日交易额同源）
    plat_ring = {(r0[0], r0[1], str(r0[2])): r0[3] for r0 in fetch(con,
        "SELECT 客户,平台,日期,日环比平台 FROM esale_system WHERE 日期 >= '2026-11-18'")}
    out = []
    for x in rows:
        plat, day, shop = x[0], x[1], x[2]
        if plat == "京东":
            rate = jd_rate.get((shop, str(day)))
        elif plat == "美团":
            rate = mm_rate.get((shop, "美团"))
        else:
            rate = None
        r12, r17 = snap.get((shop, plat, "12", str(day))), snap.get((shop, plat, "17", str(day)))
        yest, month = x[5], x[6]
        if plat in ("京东", "美团"):
            yest = 0.0 if yest is None else float(yest)
            month = 0.0 if month is None else float(month)
        pring = plat_ring.get((shop, plat, str(day)))
        out.append([plat, day, shop, r12, r17, yest, month,
                    pring if pring is not None else x[7], x[8], x[9], rate, x[10], None])
    r = table(ws, 1, ["平台", "数据日期", "店铺", "当日12点交易额", "当日17点交易额", "昨日交易额", "本月累计",
                      "日环比", "7日环比", "30日环比", "7日动销率", "非替尔泊肽占比", "健康度"],
              out, {2: DATE, 4: MONEY, 5: MONEY, 6: MONEY, 7: MONEY, 8: PCT, 9: PCT, 10: PCT, 11: PCT, 12: PCT},
              widths=[8, 12, 18, 16, 16, 14, 15, 10, 10, 10, 10, 14, 10])
    latest_day = str(max(x[1] for x in rows))
    snap12 = "已落库" if any(k[2] == "12" and k[3] == latest_day for k in snap) else "未落库"
    snap17 = "已落库" if any(k[2] == "17" and k[3] == latest_day for k in snap) else "未到点（17:05 才跑）"
    notes(ws, r, [
        "只保留 11-18、11-19 两天，18 号之前已按要求剔除。",
        "京东/美团 昨日、本月为空即平台显示「-」（真实 0 成交），按 0.00 落；天猫无来源，留空。",
        "12 点列取 12:05 快照、17 点列取 17:05 快照；本表生成于 %s：12 点%s，17 点%s。" % (
            datetime.now().strftime("%H:%M"), snap12, snap17),
        "12/17 点只认快照 ok 记录；京东页面「-」（当天还没成交）由快照侧按 0.00 落，整行没解析出来的才留空；17 点空=17:05 未到点。",
        "日环比：美团取平台 compareValue（与昨日交易额同源）；京东平台「较昨日」的基数是首页口径、与本表前日对不上，故自算；前日为 0 时留空。",
        "7日环比自算（昨日 vs 上周同日）：平台无此字段，京东商品360 只有昨天/近30天两个周期；天猫不采。",
        "30日环比取平台「近30日环比」原值。",
        "7日动销率自算：京东=近7日有销量 SKU ÷ 当日在售 SKU；美团=有动销商品数 ÷ 有销量数据商品数；天猫无来源。11-18 及以前空，是销量表只留当天快照。",
        "单行空缺：璟和堂京东 11-18/11-19 日环比、福康两家日环比——前日为 0，除零无定义；璟和堂/福康京东 7日环比、福康京东 30日环比——上周同日或近30日无成交，平台给「-」。",
        "非替尔泊肽空：天猫保民 item_rank 未取到（待补采）、福康无成交。健康度口径未定。",
    ], 13)

    # ---------------------------------------------------------------- 03 售后自动退款预警
    ws = wb.create_sheet("03_售后自动退款预警")
    pend = fetch(con, """SELECT 店铺,售后单号,售后类型,申请时间,已等待小时,售后状态,处理类型,退款金额
                         FROM v_after_sale_pending ORDER BY 已等待小时 DESC""")
    # 天猫售后明细（791 条全量、按售后单号增量刷新）接入预警页：
    # 只取「进行中」的单（排除退款成功/关闭），字段对齐美团明细。
    tm_pend = fetch(con, """
        SELECT 店铺,售后单号,售后类型,申请时间,
               TIMESTAMPDIFF(HOUR,申请时间,NOW()) AS 已等待小时,
               退款状态 AS 售后状态, 退款状态 AS 处理类型, 退款金额
        FROM tmall_after_sale_all
        WHERE 退款状态 NOT LIKE '%%成功%%' AND 退款状态 NOT LIKE '%%关闭%%'
        ORDER BY 申请时间 DESC""")
    pend = list(pend) + list(tm_pend)
    summ = fetch(con, "SELECT 店铺,平台,数据日期,数据来源,待审核数量,即将自动退数量 FROM v_after_sale_summary ORDER BY 店铺,平台")
    rows = []
    for x in pend:
        # 只呈平台原始字段 + 由申请时间算出的已等待小时，不做超时/倒计时判定
        rows.append([x[0], x[1], x[2], x[3], x[5], int(x[4] or 0), x[6], x[7]])
    r = table(ws, 1, ["店铺", "售后单号", "类型", "提交时间", "平台售后状态",
                      "已等待小时", "处理类型", "退款金额"], rows,
              {4: DT, 6: INT, 8: MONEY}, widths=[14, 20, 12, 20, 20, 12, 14, 12])
    r = table(ws, r, ["店铺", "平台", "数据日期", "数据来源", "待审核数量", "即将自动退数量"], summ,
              {3: DATE, 5: INT, 6: INT}, widths=[16, 10, 12, 10, 15, 18])
    notes(ws, r, [
        "本页只放平台原始字段：店铺、售后单号、类型、提交时间、平台售后状态、处理类型、退款金额；已等待小时只由申请时间推算，不含任何判定。",
        "「自动退款倒计时」已删除：美团自动退款时限是 24 小时（原按 48h 推算，口径错误），京东/天猫时限不同，超时与待处理都留到平台对接后再算。",
        "「售后单号」不是订单号：平台售后列表不返回订单号，采集侧没有这个字段。",
        "明细含美团待处理单 + 天猫全量售后中「进行中」的单（排除退款成功/关闭）；京东只有汇总。天猫售后明细来自 147 每日采集的 tmall_after_sale_all（791 条全量、增量刷新），两套口径不能相加。第二张表的「即将自动退数量」是平台自己算的字段，原样搬运，不是我们判定。",
        "平台不提供「待审核→拦截中→已跟进→已完成」四态，只有平台售后状态。",
    ], 8)

    # ---------------------------------------------------------------- 04 医生开方监控
    build_04(wb, con)
    build_04_pause(wb, con)

    # ---------------------------------------------------------------- 05 活动核算
    ws = wb.create_sheet("05_活动核算")
    cost = {(r0[0], r0[1]): r0[2:] for r0 in fetch(con, "SELECT `69码`, 平台, 药品采购成本, 快递成本, 商品定价 FROM v_activity_base")}
    bm_sku = {}
    for plat, code, price in fetch(con, "SELECT 平台, `69码`, 平台售价 FROM v_sku_master WHERE 客户='保民'"):
        bm_sku.setdefault((plat, code), price)
    tm_name = {c: n for c, n in fetch(con, "SELECT 商品upc, MAX(通用名) FROM tmall_activity_sku_daily WHERE 商品upc IS NOT NULL GROUP BY 商品upc")}
    mt_name = {c: n for c, n in fetch(con, "SELECT `69码`, MAX(商品) FROM v_sku_master WHERE 平台='美团' GROUP BY `69码`")}
    jd = fetch(con, """
        SELECT 记录日期, 策略ID, 商品upc, 通用名称, 京东价, 成本价, 档位总运费
        FROM jd_subsidy_daily
        WHERE 记录日期 = (SELECT MAX(记录日期) FROM jd_subsidy_daily)
          AND 商品upc IN (SELECT `69码` FROM v_sku_master WHERE 客户='保民' AND 平台='京东')
        ORDER BY 商品upc""")
    tm = fetch(con, """
        SELECT 记录日期, 活动ID, 商品upc, 招商价格, 药品成本, 订单平均运费
        FROM tmall_activity_cost_freight_daily
        WHERE 记录日期 = (SELECT MAX(记录日期) FROM tmall_activity_cost_freight_daily)
          AND 商品upc IN (SELECT `69码` FROM v_sku_master WHERE 客户='保民' AND 平台='天猫')
        ORDER BY 商品upc""")
    mt = fetch(con, """SELECT upc, Top3LowestDiscountPrice, updated_at FROM mt_Top3LowestDiscountPrice
                       WHERE upc IN (SELECT `69码` FROM v_sku_master WHERE 客户='保民' AND 平台='美团')
                       ORDER BY upc""")
    rows = []
    for day, act, code, name, price, c1, f1 in jd:
        c2 = float(c1) if c1 not in (None, 0) else None
        f2 = float(f1) if f1 is not None else None
        rows.append(["京东", day, code, name or "", act, float(price) if price else None, bm_sku.get(("京东", code)),
                     c2, f2, round(c2 + f2, 2) if c2 is not None and f2 is not None else None])
    for day, act, code, price, c1, f1 in tm:
        c2 = float(c1) if c1 not in (None, 0) else None
        f2 = float(f1) if f1 is not None else None
        rows.append(["天猫", day, code, tm_name.get(code) or "", act, float(price) if price else None, bm_sku.get(("天猫", code)),
                     c2, f2, round(c2 + f2, 2) if c2 is not None and f2 is not None else None])
    for code, price, upd in mt:
        c2 = cost.get((code, "美团"), (None, None, None))[0]
        f2 = cost.get((code, "美团"), (None, None, None))[1]
        rows.append(["美团", upd.date() if hasattr(upd, "date") else upd, code, mt_name.get(code) or "",
                     "Top3最低折扣价", float(price) if price else None, bm_sku.get(("美团", code)),
                     float(c2) if c2 not in (None, 0) else None, float(f2) if f2 is not None else None,
                     round(float(c2 or 0) + float(f2 or 0), 2) if c2 not in (None, 0) and f2 is not None else None])
    rows.sort(key=lambda x: (x[0], x[2]))
    r = table(ws, 1, ["平台", "数据日期", "69码", "商品", "活动", "最低活动价", "商品定价",
                      "药品采购成本", "快递成本", "商品总成本"], rows,
              {2: DATE, 6: MONEY, 7: MONEY, 8: MONEY, 9: MONEY, 10: MONEY},
              widths=[8, 12, 18, 40, 22, 12, 12, 14, 12, 12])
    notes(ws, r, [
        "维度一（平台维度）：活动 / 最低活动价 / 商品定价，以及后续要接的平台补贴价格、物流配送费、商家活动支出、平台活动支出、技术服务费——同一 69 码在各平台的活动与补贴互不相同，必须按平台拆开。",
        "维度二（店铺维度）：药品采购成本 / 快递成本 / 商品总成本——各店铺的采购价和运费不同，本应一店一组。当前只有保民一家有成本数据，所以暂时不拆店铺列，全部按保民计；接第二家店时每店单独展开，不要跨店取一个成本。",
        "为什么活动走不了店铺维度：京东活动是策略级、天猫是活动级，接口都不带店铺；所以活动只能到平台、成本才能到店铺，两套维度共存。",
        "只取保民已上架的 69 码；商品定价取保民该平台售价。共 %d 行（京东 %d / 天猫 %d / 美团 %d）。" % (len(rows), len(jd), len(tm), len(mt)),
        "数据不是同一天：京东停在 2026-08-18（JoySpace 同步没跑），天猫停在 2026-08-14（管道已停），美团是 11-19。",
        "未采集：平台补贴价格、物流配送费、商家活动支出、平台活动支出、技术服务费、订单原始应收、买家实付、商家应收基数、预计商家收入、毛利（在结算账单里，待爬）。",
        "商品总成本 = 采购成本 + 快递成本，任一项为空则不计算；美团源表成本为 0，按空处理。",
    ], 10)

    # ---------------------------------------------------------------- 06 价格雷达
    ws = wb.create_sheet("06_价格雷达")
    cmp_rows = fetch(con, """SELECT 药品名,本店店铺,本店单盒价,竞品店铺,竞品单盒价,单盒价差,本店盒数,竞品盒数,竞品规格
                             FROM v_competitor_price_compare ORDER BY 药品名,竞品店铺""")
    rows = []
    for drug, my_shop, my_price, cp_shop, cp_price, diff, mb, cb, spec in cmp_rows:
        rate = round((float(my_price) - float(cp_price)) / float(my_price), 4) if my_price and cp_price else None
        level = "—"
        if rate is not None:
            level = "预警" if rate >= 0.10 else ("关注" if rate >= 0.03 else "正常")
        rows.append([drug, my_shop, float(my_price) if my_price is not None else None, cp_shop,
                     float(cp_price) if cp_price is not None else None, float(diff) if diff is not None else None,
                     rate, level, mb, cb, spec])
    r = table(ws, 1, ["药品", "本店店铺", "本店到手价", "竞品店铺", "竞品到手价", "单盒价差", "价差%", "判定",
                      "本店盒数", "竞品盒数", "竞品规格"], rows,
              {3: MONEY, 5: MONEY, 6: MONEY, 7: PCT}, widths=[12, 20, 13, 40, 13, 12, 10, 8, 10, 10, 28])
    notes(ws, r, [
        "「到手价」实际是单盒展示价：采集未登录、没进详情，拿不到券后/满减价。",
        "价差% =(本店单盒价 − 竞品单盒价)/本店单盒价；正数=竞品更便宜（对本店不利），≥10% 预警、3%–10% 关注。",
        "只有璟和堂京东店的 %d 条比价数据，其他店未采。" % len(rows),
    ], 11)

    # ---------------------------------------------------------------- 07 竞品新品
    ws = wb.create_sheet("07_竞品新品")
    rows = fetch(con, """SELECT 商品标题,店铺,展示价,采集日期,药品名,规格,盒数,单盒展示价,数据来源
                         FROM bi_competitor_price ORDER BY 采集日期 DESC, 药品名, 店铺""")
    out = [[x[0], x[1], x[2], x[3], "在售", x[4], x[5], x[6], x[7], x[8]] for x in rows]
    r = table(ws, 1, ["商品", "竞品店铺", "竞品价", "发现日期", "标记", "药品名", "规格", "盒数", "单盒展示价", "数据来源"],
              out, {3: MONEY, 4: DATE, 8: INT, 9: MONEY}, widths=[44, 36, 11, 11, 8, 12, 26, 7, 12, 14])
    notes(ws, r, [
        "共 %d 条 / %d 家竞品店，只有 2026-11-18 一个采集日，" % (len(rows), len({x[1] for x in rows})),
        "因此「新品/在售」无法区分，标记统一为在售；连续采 2 周后才有意义。",
        "竞品价是落地页展示价，不是到手价。",
    ], 10)

    # ---------------------------------------------------------------- 08 实时库存
    ws = wb.create_sheet("08_实时库存")
    stock = fetch(con, "SELECT 商品,仓库,批号,有效期,库存,锁定量,可用库存,商品编码,规格名称,待采购量,采购在途,效期状态,距离到期天数 FROM v_stock_current ORDER BY 商品,仓库")
    kpi = fetch(con, "SELECT COUNT(DISTINCT 商品编码), SUM(效期状态='近效期'), SUM(效期状态='已过期'), SUM(待采购量>0) FROM v_stock_current")[0]
    r = table(ws, 1, ["KPI", "数值"], [
        ["在库品种数", int(kpi[0])],
        ["近效期品种（60 天内）", int(kpi[1] or 0)],
        ["已过期品种", int(kpi[2] or 0)],
        ["待采购量 > 0 的品种", int(kpi[3] or 0)],
    ], {2: INT}, widths=[26, 12])
    r = table(ws, r, ["商品", "仓库", "批号", "效期", "库存", "锁定", "可用",
                      "商品编码", "规格名称", "待采购量", "采购在途", "效期状态", "距离到期天数"], stock,
              {4: DATE, 5: MONEY, 6: MONEY, 7: MONEY, 10: MONEY, 11: MONEY, 13: INT},
              widths=[44, 14, 30, 12, 12, 10, 12, 16, 24, 12, 12, 10, 14])
    notes(ws, r, [
        "共 %d 行；批号/效期为空：源表该商品+仓库没有批次记录，不代表没货。" % len(stock),
        "「待确认补货」指标没有：只有待采购量/采购在途，没有补货确认状态。",
        "近效期 %d 个、已过期 %d 个；测试仓库已排除。" % (int(kpi[1] or 0), int(kpi[2] or 0)),
    ], 13)

    # ---------------------------------------------------------------- 09 商品主数据
    ws = wb.create_sheet("09_商品主数据")
    sku = fetch(con, """
        SELECT `69码`, MAX(商品),
               COUNT(DISTINCT 平台),
               MAX(CASE WHEN 平台='京东' THEN 平台SKU END),
               COALESCE(MAX(CASE WHEN 平台='京东' AND 客户='保民' THEN 平台售价 END),
                        MAX(CASE WHEN 平台='京东' THEN 平台售价 END)),
               MAX(CASE WHEN 平台='美团' THEN 平台SKU END),
               COALESCE(MAX(CASE WHEN 平台='美团' AND 客户='保民' THEN 平台售价 END),
                        MAX(CASE WHEN 平台='美团' THEN 平台售价 END)),
               MAX(CASE WHEN 平台='天猫' THEN 平台SKU END),
               COALESCE(MAX(CASE WHEN 平台='天猫' AND 客户='保民' THEN 平台售价 END),
                        MAX(CASE WHEN 平台='天猫' THEN 平台售价 END)),
               MAX(禁售区域), MAX(NULLIF(可售卖时间,'')), MAX(数据日期)
        FROM v_sku_master GROUP BY `69码` ORDER BY COUNT(DISTINCT 平台) DESC, `69码`""")
    c3 = sum(1 for x in sku if int(x[2]) == 3)
    c2 = sum(1 for x in sku if int(x[2]) == 2)
    c1 = sum(1 for x in sku if int(x[2]) == 1)
    r = table(ws, 1, ["69码", "商品", "平台数", "京东SKU", "京东售价", "美团SKU", "美团售价",
                      "天猫SKU", "天猫售价", "禁售区域（省级黑名单）", "可售卖时间", "数据日期"],
              sku, {3: INT, 5: MONEY, 7: MONEY, 9: MONEY, 12: DATE},
              widths=[18, 44, 7, 18, 11, 18, 11, 18, 11, 34, 12, 12])
    notes(ws, r, [
        "共 %d 个 69 码：三平台齐全 %d、两平台 %d、单平台 %d。" % (len(sku), c3, c2, c1),
        "69 码来自旺店通商家编码解析，跨平台对齐就靠这一列；各平台 SKU 本来不同。",
        "美团「平台SKU」显示的就是 69 码本身：美团没有独立 SKU 编码。",
        "售价优先保民店，保民没上架时回退该平台任一店。",
        "禁售区域只有京东有值（按客户+商品名匹配），天猫/美团名单商家没提供；可售卖时间仅 9 行有「晚上上架」。",
    ], 12)

    # ---------------------------------------------------------------- 10 店铺拉取状态
    ws = wb.create_sheet("10_店铺拉取状态")
    ov2 = fetch(con, "SELECT 平台,覆盖店铺数,登录正常数,数据拉取正常数,今日拉取行数,巡检时间 FROM v_pull_overview ORDER BY FIELD(平台,'京东','天猫','美团','全部')")
    st = fetch(con, "SELECT 店铺,登录状态,拉取状态说明,最近拉取时间,今日拉取量,异常说明,店铺整体状态 FROM v_shop_pull_status ORDER BY 店铺")
    logs = fetch(con, "SELECT 数据源,时间,结果,行数,说明 FROM v_pull_log ORDER BY 时间 DESC, 数据源")
    r = table(ws, 1, ["平台", "覆盖店铺数", "登录正常数", "数据拉取正常数", "今日拉取行数", "巡检时间"], ov2,
              {2: INT, 3: INT, 4: INT, 5: INT, 6: DT}, widths=[10, 12, 12, 14, 14, 20])
    r = table(ws, r, ["店铺", "登录状态", "数据拉取", "最近拉取时间", "今日拉取量", "异常说明", "整体状态"], st,
              {4: DT, 5: INT}, widths=[26, 14, 26, 20, 12, 44, 12])
    r = table(ws, r, ["数据源", "时间", "结果", "行数", "说明"], logs, {2: DT, 4: INT},
              widths=[26, 20, 10, 10, 46])
    notes(ws, r, [
        "本页是巡检快照：pull-status.timer 每小时 05 分读一遍库里的采集结果，它自己不采集；真正的采集是按门店周期每 10 分钟跑的（各店错峰），所以「巡检时间」固定是整点后 05 分。",
        "「操作」列没有数据：后端没有重试接口。",
        "覆盖店铺数含 1 个非门店项（京东活动目录「全部」）；真实门店 10 家（南无 3 家停用不计）。",
        "天猫登录状态由 147 采集端回传的登录/风控信号判定（login_required/risk_control→未登录，最近采集成功→已登录）；最近拉取时间 00:00:00 表示当天未拉取，是占位值。",
    ], 8)

    # ---------------------------------------------------------------- 11 未动销商品
    ws = wb.create_sheet("11_未动销商品")
    rows = []
    for shop, sch in STORES:
        got = fetch(con, """
            SELECT s.商品编码, MAX(s.商品名称), COUNT(DISTINCT s.SKUID), MAX(s.京东价),
                   MAX(p.销量), MAX(p.`30天销量`)
            FROM `%s`.jd_store_sku_normalized_daily s
            LEFT JOIN esale_product p
              ON p.唯一编码=s.商品编码 AND p.平台='京东' AND p.客户=%%s AND p.更新日期=(SELECT MAX(更新日期) FROM esale_product)
            WHERE s.记录日期=(SELECT MAX(记录日期) FROM `%s`.jd_store_sku_normalized_daily)
            GROUP BY s.商品编码
            HAVING MAX(p.销量) IS NULL OR MAX(p.销量)=0
            ORDER BY MAX(s.京东价) DESC""" % (sch, sch), (shop,))
        for code, name, sku_n, price, s7, s30 in got:
            rows.append([shop, code, name, int(sku_n or 0), price,
                         float(s7) if s7 is not None else None, float(s30) if s30 is not None else None])
    r = table(ws, 1, ["店铺", "商品编码", "商品名称", "在售SKU数", "售价", "近7日销量", "近30日销量"],
              rows, {5: MONEY, 6: INT, 7: INT}, widths=[14, 18, 46, 10, 12, 11, 11])
    cnt = {}
    for x in rows:
        cnt[x[0]] = cnt.get(x[0], 0) + 1
    notes(ws, r, [
        "共 %d 个商品近 7 日无销量：%s。" % (len(rows), "、".join("%s %d" % (k, v) for k, v in cnt.items())),
        "近7日销量为空=京东没上报该商品销量，等同无销量；不是缺数。",
        "只覆盖京东 4 店；美团/天猫没有同粒度商品销量明细。",
    ], 7)

    # ---------------------------------------------------------------- 12 禁售名单
    ws = wb.create_sheet("12_禁售名单")
    bans = fetch(con, "SELECT 客户,商品名称,`69码`,省份,原始文本,数据日期 FROM bi_region_ban ORDER BY 客户,商品名称,省份")
    r = table(ws, 1, ["客户", "商品名称", "69码", "省份", "原始文本", "数据日期"], bans, {6: DATE},
              widths=[14, 44, 18, 12, 40, 12])
    notes(ws, r, [
        "共 %d 条，覆盖 %d 个商品、%d 个省份；先单独存放，下一步与「13_在售商品」按 69 码+省份比对。" % (
            len(bans), len({x[1] for x in bans}), len({x[3] for x in bans})),
        "只有京东侧名单（按客户+商品名录入），天猫/美团名单商家未提供。",
    ], 6)

    # ---------------------------------------------------------------- 13 在售商品
    build_13(wb, con)

    # ---------------------------------------------------------------- 14 数据拉取周期
    ws = wb.create_sheet("14_数据拉取周期")
    cyc = [
        ["esale_system（店铺经营指标）", "01、02", "每 10 分钟", "各店错峰：保民 :00/10/20…、百恩堂 :02…、福康 :04…、璟和堂 :08…", "store-cycle-*.timer → store-cycle@*.service", "正常", "京东走 run-jd-store-ops.sh，美团走 meituan-store-ops"],
        ["esale_shop_intraday（12/17 点快照）", "01、02", "每天 2 次", "12:05、17:05", "snapshot-shop-intraday@12/@17.timer", "正常", "过点不补；17 点会覆盖当日交易额"],
        ["esale_product（商品销量）", "02、11", "每 10 分钟", "随门店周期", "run-jd-store-product-analysis.sh", "正常", "京东只上报有销量的商品，空=无销量"],
        ["jd_store_sku_normalized_daily（商品快照）", "09、11、13", "每 10 分钟刷新，按天保留一行", "随京东门店周期", "jd_store_sku_web_collect.py", "正常", "在售商品/未动销/主数据的在售口径来源"],
        ["ops_after_sale_review_daily（售后明细）", "03", "每 10 分钟", "随美团门店周期", "meituan-after-sales-pending.service", "正常", "只有美团有明细，京东/天猫只有汇总"],
        ["jd_prescription_daily / jd_prescription_status（处方）", "04", "每 10 分钟", "随京东门店周期", "run-jd-prescription.sh", "正常（不采详情）", "调度参数 JD_PRESCRIPTION_DETAILS 默认 0，不点详情；要医生名需手动 --details"],
        ["jd_order_status_daily（订单状态）", "04", "每 10 分钟", "随京东门店周期", "run-jd-order-status.sh", "正常", "「超时单数」取这里的平台判定超时"],
        ["bi_pull_status / bi_pull_log（拉取巡检）", "10", "每小时", "每小时 05 分", "pull-status.timer", "正常", "改频次只改 pull-status.timer 的 OnCalendar"],
        ["jd_subsidy_daily / jd_subsidy_catalog_daily（京东活动）", "05", "每天 1 次", "11:05", "joyspace-subsidy-sync.timer", "跑了但数据停在 08-18", "任务在跑，目录没有新数据，应查 JoySpace 登录态/页面结构"],
        ["mt_Top3LowestDiscountPrice（美团活动价）", "05", "每 10 分钟", "随美团门店周期", "meituan_top3_lowest_discount_price.py", "正常", "只有活动价，无成本/运费"],
        ["tmall_activity_cost_freight_daily（天猫活动）", "05", "已停", "最后一次 08-14", "天猫活动管道", "停用", "恢复需手动补跑"],
        ["tmall-ecommerce-sync（天猫经营数据）", "01、02", "每 10 分钟", ":00/10/20…", "tmall-ecommerce-sync.timer", "任务在跑，昨日/本月未入库", "脚本未改造，是当前天猫两列空的直接原因"],
        ["bi_stock / bi_stock_batch / bi_stock_expiry（库存）", "08", "手动", "旺店通导出后导入", "import_wdt_base.py", "最近 11-18", "没有定时，按导出节奏"],
        ["bi_sku_platform / bi_sku_cost（商品主数据）", "09", "手动", "旺店通导出后导入", "import_wdt_base.py", "最近 11-18", "69 码对齐就靠这张表的商家编码"],
        ["bi_region_ban / bi_night_listing（禁售/夜间上架）", "12", "手动", "商家给名单后导入", "import_wdt_base.py", "11-17", "只有京东侧名单"],
        ["bi_competitor_price（竞品价格）", "06、07", "单批", "2026-11-18 一次", "试爬导入，无定时任务", "无定时", "要连续数据需挂日频采集"],
    ]
    r = table(ws, 1, ["数据源 / 表", "对应页面", "采集频率", "具体时点", "归属任务", "当前状态", "备注"], cyc,
              widths=[46, 10, 26, 40, 44, 22, 56])
    notes(ws, r, [
        "所有门店周期都在 /etc/systemd/system 下的 *.timer 里改 OnCalendar，改完 systemctl daemon-reload && systemctl restart <timer>。",
        "单店周期最长 2400 秒超时，多店并行受两个槽位信号量限制（store-cycle-slot0/1），排队时日志会写「waiting for slot」。",
        "处方详情、京东订单明细、天猫昨日/本月这三项没有定时任务，是当前数据缺口的主要来源。",
        "手动导入类（库存/主数据/禁售）没有定时，数据新鲜度取决于旺店通导出的时间点。",
    ], 7)

    os.makedirs(OUT_DIR, exist_ok=True)
    wb.save(OUT)
    print("输出:", OUT)
    for ws in wb.worksheets:
        print("  %-20s %d 行" % (ws.title, ws.max_row))


if __name__ == "__main__":
    main()
