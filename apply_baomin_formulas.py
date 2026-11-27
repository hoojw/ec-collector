#!/usr/bin/env python3
"""把保民三平台表中的红线价/利润改成逐商品 Excel 公式。

盒数引用保民表自身的盒数列；运费和费率从 成本&红线表.xlsx 的公式里解析；
报名活动价取保民表实际采集值。匹配不上的行留空。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from update_cost_from_wdt import clean_code, load_wdt, pick

DEFAULT_TARGET = pathlib.Path("/tmp/codebuddy-chat-logs/保民三平台活动及成本.xlsx")
DEFAULT_RED = pathlib.Path("/tmp/codebuddy-chat-logs/成本&红线表.xlsx")
HEADER_FILL = PatternFill("solid", fgColor="D9E1F2")
HEADER_FONT = Font(bold=True)
MONEY_FMT = "#,##0.00"
PCT_FMT = "0.00%"


def norm(s) -> str:
    s = str(s or "").lower()
    s = re.sub(r"[\[\]（）()/*\s\-—·:：,，。、]", "", s)
    for w in ("盒装", "片装", "粒装", "支装", "瓶装", "袋装", "/盒", "/瓶", "/袋"):
        s = s.replace(w, "")
    return s


def core(s) -> str:
    s = str(s or "")
    s = re.sub(r"^\[[^\]]*\]", "", s)
    m = re.match(r"^([\u4e00-\u9fa5]+)", s)
    return m.group(1) if m else norm(s)


def fmt_num(v) -> str:
    f = float(v)
    return str(int(f)) if f == int(f) else str(f)


def parse_pct(value) -> float | None:
    text = str(value or "").strip()
    m = re.search(r"-([0-9.]+)(%?)$", text)
    if not m:
        return None
    v = float(m.group(1))
    return v / 100 if m.group(2) == "%" else v


def load_redline(path: pathlib.Path):
    wb_f = load_workbook(path, read_only=False, data_only=False)
    wb_v = load_workbook(path, read_only=False, data_only=True)
    ws_f, ws_v = wb_f["成本价对比表"], wb_v["成本价对比表"]
    exact, by_core, by_core_all = {}, {}, {}
    for r in range(2, ws_f.max_row + 1):
        title = ws_f.cell(r, 2).value
        if not title:
            continue
        def freight_of(col: int) -> float:
            f = str(ws_f.cell(r, col).value or "")
            m = re.search(r"\+(\d+)\)", f)
            return float(m.group(1)) if m else 0.0

        d = ws_v.cell(r, 4).value
        try:
            boxes = float(d)
        except (TypeError, ValueError):
            boxes = 1.0
        item = {
            "row": r,
            "title": title,
            "cost": ws_v.cell(r, 3).value,
            "boxes": boxes,
            "freight_jd": freight_of(5),
            "freight_tm": freight_of(6),
            "freight_mt": freight_of(7),
            "jd_fee": parse_pct(ws_f.cell(r, 9).value) or 0.11,
            "tm_fee": parse_pct(ws_f.cell(r, 11).value) or 0.08,
            "mt_fee": parse_pct(ws_f.cell(r, 13).value) or 0.08,
        }
        exact[norm(title)] = item
        c = core(title)
        if c:
            by_core_all.setdefault(c, []).append(item)
            if c not in by_core:
                by_core[c] = item
    wb_f.close()
    wb_v.close()
    return exact, by_core, by_core_all


def load_wdt_costs():
    files = sorted(
        DEFAULT_TARGET.parent.glob("*库存查询*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return {}, {}
    return load_wdt(files[0])


def dose_mg(s) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(mg|g)\b", str(s or ""), re.I)
    if not m:
        return None
    v = float(m.group(1))
    return v * 1000 if m.group(2).lower() == "g" else v


def num_tokens(s) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", str(s or "")))


def spec_compatible(text, item) -> bool:
    td, idd = dose_mg(text), dose_mg(item.get("title"))
    if td is not None and idd is not None:
        return abs(td - idd) <= 0.01
    tt, it = num_tokens(text), num_tokens(item.get("title"))
    if tt and it:
        return bool(tt & it)
    return True


def match(name, spec, exact, by_core, by_core_all=None):
    n = norm(name)
    if n in exact:
        return exact[n]
    if spec:
        ns = norm(f"{name}{spec}")
        if ns in exact:
            return exact[ns]
    c = core(name)
    candidates = by_core_all.get(c, []) if by_core_all else ([by_core[c]] if c in by_core else [])
    text = f"{name}{spec or ''}"
    compatible = [item for item in candidates if spec_compatible(text, item)]
    if not compatible:
        return None
    if len(compatible) == 1:
        return compatible[0]
    tt = num_tokens(text)
    return max(compatible, key=lambda item: len(tt & num_tokens(item.get("title"))))


def resolve_item(codes, name, spec, exact, by_core, by_core_all, by_code):
    for code in codes:
        rec = by_code.get(clean_code(code))
        if rec:
            return match(rec["name"], rec["spec"], exact, by_core, by_core_all), rec
    return match(name, spec, exact, by_core, by_core_all), None


def red_formula(cost_ref, boxes_ref, factor, freight):
    if freight:
        return f"=({cost_ref}*{boxes_ref}+{fmt_num(freight)})/{factor}"
    return f"=({cost_ref}*{boxes_ref})/{factor}"


def profit_formula(price_ref, boxes_ref, red_ref, factor, fee):
    bundle_price = f"({price_ref}*{boxes_ref})"
    guard = f'OR({price_ref}="",{boxes_ref}="",{bundle_price}=0)'
    return (
        f'=IF({guard},"",({bundle_price}-{red_ref}*{factor})/{bundle_price}'
        f'-{fmt_num(fee)})'
    )


def ensure_col(ws, name: str) -> int:
    for i, c in enumerate(ws[1], 1):
        if c.value == name:
            return i
    col = ws.max_column + 1
    h = ws.cell(1, col, name)
    h.fill, h.font = HEADER_FILL, HEADER_FONT
    h.alignment = Alignment(horizontal="center", vertical="center")
    return col


def style_profit(ws, col: int):
    for r in range(2, ws.max_row + 1):
        ws.cell(r, col).number_format = PCT_FMT


def header_col(ws, name: str) -> int | None:
    for i, c in enumerate(ws[1], 1):
        if c.value == name:
            return i
    return None


def as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def activity_brands(raw):
    if not raw:
        return set()
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return {str(v).strip() for v in (data or {}).values() if v}
    except Exception:
        return set()


def tmall_brand_mismatch(ws, row):
    own = str(ws.cell(row, 5).value or "").strip()
    if not own:
        return False
    text = f"{ws.cell(row, 9).value or ''} {ws.cell(row, 10).value or ''}"
    if own in text:
        return False
    brands = activity_brands(ws.cell(row, 10).value)
    return bool(brands) and own not in brands


def build_source_map(ws, name_col, spec_col, exact, by_core, by_core_all, code_cols=None, by_code=None):
    result = {}
    for r in range(2, ws.max_row + 1):
        name = ws.cell(r, name_col).value
        spec = ws.cell(r, spec_col).value if spec_col else None
        if code_cols and by_code:
            item, _ = resolve_item(
                [ws.cell(r, c).value for c in code_cols], name, spec, exact, by_core, by_core_all, by_code
            )
        else:
            item = match(name, spec, exact, by_core, by_core_all)
        if item and item.get("row"):
            result.setdefault(item["row"], []).append(r)
    return result


def choose_source_row(candidates, target_price, source_price):
    if not candidates:
        return None
    if target_price is None:
        return candidates[0]
    scored = []
    for r in candidates:
        p = source_price(r)
        if p is not None:
            scored.append((abs(p - target_price), r))
    return min(scored)[1] if scored else candidates[0]


def apply(target: pathlib.Path, redline: pathlib.Path):
    exact, by_core, by_core_all = load_redline(redline)
    by_code, by_name = load_wdt_costs()
    wb = load_workbook(target)
    stats = {}

    # 美团：成本/红线/利润列已存在，活动价取「美团活动门槛价」×「门槛盒数」
    ws = wb["保民美团补贴活动"]
    n = 0
    for r in range(2, ws.max_row + 1):
        item, rec = resolve_item(
            [ws.cell(r, 5).value, ws.cell(r, 6).value],
            ws.cell(r, 7).value, None, exact, by_core, by_core_all, by_code,
        )
        red_col, profit_col = 36, 39
        cost = as_float(rec["cost"]) if rec and rec.get("cost") is not None else None
        if cost is not None and cost > 0:
            ws.cell(r, 33).value = cost
            ws.cell(r, 34).value = "旺店通库存成本"
        elif item and item.get("cost") is not None:
            cost = as_float(item["cost"])
            if cost is not None and cost > 0:
                ws.cell(r, 33).value = cost
                ws.cell(r, 34).value = "成本红线表"
        if cost is None or cost <= 0:
            ws.cell(r, 33).value = None
            ws.cell(r, 34).value = None
        if not item or cost is None or cost <= 0:
            ws.cell(r, red_col).value = None
            ws.cell(r, profit_col).value = None
            continue
        n += 1
        cost_ref = f"{get_column_letter(33)}{r}"
        red_ref = f"{get_column_letter(red_col)}{r}"
        price_ref, boxes_ref = f"AK{r}", f"S{r}"
        ws.cell(r, red_col).value = red_formula(cost_ref, boxes_ref, 0.91, item["freight_mt"])
        ws.cell(r, profit_col).value = profit_formula(
            price_ref, boxes_ref, red_ref, 0.91, item["mt_fee"]
        )
    style_profit(ws, 39)
    stats["保民美团补贴活动"] = n

    # 京东：红线列已存在，新增利润列；活动价用「单盒售价」
    ws = wb["保民京东小官补"]
    profit_col = ensure_col(ws, "京东利润点")
    n = 0
    for r in range(2, ws.max_row + 1):
        item, rec = resolve_item(
            [ws.cell(r, 4).value, ws.cell(r, 5).value],
            ws.cell(r, 7).value, ws.cell(r, 8).value, exact, by_core, by_core_all, by_code,
        )
        cost = as_float(rec["cost"]) if rec and rec.get("cost") is not None else None
        if cost is not None and cost > 0:
            ws.cell(r, 14).value = cost
            ws.cell(r, 15).value = "旺店通库存成本"
        elif item and item.get("cost") is not None:
            cost = as_float(item["cost"])
            if cost is not None and cost > 0:
                ws.cell(r, 14).value = cost
                ws.cell(r, 15).value = "成本红线表"
        if cost is None or cost <= 0:
            ws.cell(r, 14).value = None
            ws.cell(r, 15).value = None
        if not item or cost is None or cost <= 0:
            ws.cell(r, 18).value = None
            ws.cell(r, profit_col).value = None
            continue
        n += 1
        cost_ref, red_ref, price_ref = f"N{r}", f"R{r}", f"K{r}"
        boxes_ref = f"I{r}"
        ws.cell(r, 18).value = red_formula(cost_ref, boxes_ref, 0.84, item["freight_jd"])
        ws.cell(r, profit_col).value = profit_formula(
            price_ref, boxes_ref, red_ref, 0.84, item["jd_fee"]
        )
    style_profit(ws, profit_col)
    stats["保民京东小官补"] = n

    # 指数交易：新增固定成本价列，红线公式引用该列；优先旺店通成本，缺失时按红线表匹配兜底
    ws = wb["保民指数交易匹配"]
    cost_col = ensure_col(ws, "固定成本价")
    red_col = header_col(ws, "京东红线价") or 18
    n = 0
    wdt_n = 0
    for r in range(2, ws.max_row + 1):
        item = match(ws.cell(r, 2).value, None, exact, by_core, by_core_all)
        rec = pick(
            [ws.cell(r, 1).value],
            ws.cell(r, 2).value,
            ws.cell(r, 12).value or ws.cell(r, 17).value,
            by_code,
            by_name,
        )
        cost = rec["cost"] if rec and rec.get("cost") is not None else (item or {}).get("cost")
        cost = as_float(cost)
        if cost is None or cost <= 0:
            ws.cell(r, cost_col).value = None
            ws.cell(r, red_col).value = None
            continue
        ws.cell(r, cost_col).value = cost
        ws.cell(r, cost_col).number_format = MONEY_FMT
        if not item:
            ws.cell(r, red_col).value = None
            continue
        n += 1
        wdt_n += 1 if rec else 0
        cost_ref = f"{get_column_letter(cost_col)}{r}"
        ws.cell(r, red_col).value = red_formula(cost_ref, "1", 0.84, item["freight_jd"])
    stats["保民指数交易匹配"] = {"formula_rows": n, "wdt_cost_rows": wdt_n}

    # 天猫：红线列已存在，新增利润列；活动价用「单盒招商价」公式列
    ws = wb["天猫在售商品可报名匹配"]
    profit_col = ensure_col(ws, "天猫利润点")
    n = 0
    for r in range(2, ws.max_row + 1):
        item = match(ws.cell(r, 1).value, None, exact, by_core, by_core_all)
        cost = as_float(item["cost"]) if item else None
        if not item or cost is None or cost <= 0 or tmall_brand_mismatch(ws, r):
            ws.cell(r, 25).value = None
            ws.cell(r, profit_col).value = None
            continue
        n += 1
        red_ref, price_ref, boxes_ref = f"Y{r}", f"R{r}", f"P{r}"
        ws.cell(r, 25).value = red_formula(fmt_num(item["cost"]), boxes_ref, 0.87, item["freight_tm"])
        ws.cell(r, profit_col).value = profit_formula(
            price_ref, boxes_ref, red_ref, 0.87, item["tm_fee"]
        )
    style_profit(ws, profit_col)
    stats["天猫在售商品可报名匹配"] = n

    # 三平台汇总：红线仍按公式算；利润直接引用前面三张表已算好的单元格
    ws = wb["三平台活动汇总"]
    source_ws = {
        "mt": wb["保民美团补贴活动"],
        "jd": wb["保民京东小官补"],
        "tm": wb["天猫在售商品可报名匹配"],
    }
    source_maps = {
        "mt": build_source_map(source_ws["mt"], 7, None, exact, by_core, by_core_all, [5, 6], by_code),
        "jd": build_source_map(source_ws["jd"], 7, 8, exact, by_core, by_core_all, [4, 5], by_code),
        "tm": build_source_map(source_ws["tm"], 1, None, exact, by_core, by_core_all),
    }
    profit_cols = {
        "mt": header_col(source_ws["mt"], "实际利润点"),
        "jd": header_col(source_ws["jd"], "京东利润点"),
        "tm": header_col(source_ws["tm"], "天猫利润点"),
    }
    source_price = {
        "mt": lambda r: as_float(source_ws["mt"].cell(r, 37).value),
        "jd": lambda r: as_float(source_ws["jd"].cell(r, 11).value),
        "tm": lambda r: (
            (lambda q, p: q / p if q is not None and p else None)(
                as_float(source_ws["tm"].cell(r, 17).value),
                as_float(source_ws["tm"].cell(r, 16).value),
            )
        ),
    }
    cols = {
        "mt": {"red": 9, "price": 8, "factor": 0.91, "freight": "freight_mt",
               "profit": ensure_col(ws, "美团价后利润")},
        "jd": {"red": 14, "price": 12, "factor": 0.84, "freight": "freight_jd",
               "profit": ensure_col(ws, "京东价后利润")},
        "tm": {"red": 19, "price": 17, "factor": 0.87, "freight": "freight_tm",
               "profit": ensure_col(ws, "天猫价后利润")},
    }
    n = 0
    for r in range(2, ws.max_row + 1):
        item = match(ws.cell(r, 1).value, ws.cell(r, 3).value, exact, by_core, by_core_all)
        cost = as_float(ws.cell(r, 4).value)
        if not item or cost is None or cost <= 0:
            for cfg in cols.values():
                ws.cell(r, cfg["red"]).value = None
                ws.cell(r, cfg["profit"]).value = None
            continue
        n += 1
        cost_ref = f"D{r}"
        for label, cfg in cols.items():
            red_ref = f"{get_column_letter(cfg['red'])}{r}"
            ws.cell(r, cfg["red"]).value = red_formula(
                cost_ref, "1", cfg["factor"], item[cfg["freight"]]
            )
            candidates = source_maps[label].get(item.get("row"), [])
            src_row = choose_source_row(
                candidates, as_float(ws.cell(r, cfg["price"]).value), source_price[label]
            )
            pc = profit_cols[label]
            if src_row and pc:
                ws.cell(r, cfg["profit"]).value = (
                    f"='{source_ws[label].title}'!{get_column_letter(pc)}{src_row}"
                )
            else:
                ws.cell(r, cfg["profit"]).value = None
    for cfg in cols.values():
        style_profit(ws, cfg["profit"])
    stats["三平台活动汇总"] = n

    wb.save(target)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET)
    ap.add_argument("--redline", type=pathlib.Path, default=DEFAULT_RED)
    args = ap.parse_args()
    stats = apply(args.target, args.redline)
    print("formula rows by sheet:", stats)


if __name__ == "__main__":
    main()
