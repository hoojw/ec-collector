#!/usr/bin/env python3
"""用旺店通「库存查询」中的成本价更新保民三平台表的固定成本价。

匹配优先级：
1. 编码精确匹配 + 商品名/规格兼容；
2. 编码精确匹配 + 通用名一致（兼容 14片*2板 与 28片/板 等同规格写法）；
3. 商品名完全一致。
严格校验规格数字，避免无编码时同条码不同规格串价。
"""
from __future__ import annotations

import argparse
import pathlib
import re
from openpyxl import load_workbook


DEFAULT_WDT = pathlib.Path("/tmp/codebuddy-chat-logs/库存查询20261126.xlsx")
DEFAULT_TARGET = pathlib.Path("/tmp/codebuddy-chat-logs/保民三平台活动及成本.xlsx")


def norm(s) -> str:
    s = str(s or "").lower()
    s = re.sub(r"[\[\]（）()/*\s\-—·:：,，。、+＋]", "", s)
    s = s.replace("左氨氯", "左旋氨氯")
    return s


def spec_norm(s) -> str:
    return re.sub(r"[\s/]", "", str(s or "").lower())


def spec_tokens(s) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", spec_norm(s)))


def spec_ok(sheet_spec, wdt_spec, sheet_name="", wdt_name="") -> bool:
    ss, ws = spec_norm(sheet_spec), spec_norm(wdt_spec)
    if not ss:
        return True
    if ss == ws or ss in ws or ws in ss:
        return True
    if norm(sheet_name) == norm(wdt_name):
        return True
    a, b = spec_tokens(ss), spec_tokens(ws)
    if a and b:
        return a.issubset(b) or b.issubset(a)
    return False


def clean_code(v) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def load_wdt(path: pathlib.Path):
    wb = load_workbook(path, read_only=False, data_only=True)
    ws = wb.active
    hdr = {c.value: i + 1 for i, c in enumerate(ws[1])}
    by_code, by_name = {}, {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        cost = row[hdr["成本价"] - 1]
        name = row[hdr["货品名称"] - 1]
        spec = row[hdr["规格名称"] - 1]
        if cost is None or name is None or cost <= 0:
            continue
        try:
            cost = float(cost)
        except (TypeError, ValueError):
            continue
        rec = {"name": str(name), "spec": str(spec or ""), "cost": cost}
        by_name.setdefault(norm(name), rec)
        for key in ("货品编码", "商家编码", "主条码", "规格辅助码"):
            code = clean_code(row[hdr[key] - 1])
            if code:
                by_code.setdefault(code, rec)
    wb.close()
    return by_code, by_name


def generic(s) -> str:
    s = str(s or "")
    s = re.sub(r"^\[[^\]]*\]", "", s)
    m = re.match(r"^([\u4e00-\u9fa5]+)", s)
    return m.group(1) if m else ""


def pick(codes, name, spec, by_code, by_name):
    candidates = []
    for code in codes:
        code = clean_code(code)
        if code and code in by_code:
            candidates.append(by_code[code])
    for rec in candidates:
        if norm(name) == norm(rec["name"]):
            return rec
        if norm(name) and norm(name) in norm(rec["name"]) and spec_ok(spec, rec["spec"], name, rec["name"]):
            return rec
    g = generic(name)
    if g:
        for rec in candidates:
            if generic(rec["name"]) == g:
                return rec
    if not candidates:
        return by_name.get(norm(name))
    return None


def apply(wdt: pathlib.Path, target: pathlib.Path):
    by_code, by_name = load_wdt(wdt)
    wb = load_workbook(target)
    stats = {}

    configs = [
        {
            "sheet": "保民美团补贴活动",
            "cost": 33,
            "source": 34,
            "name": 7,
            "spec": None,
            "codes": [5, 6],
        },
        {
            "sheet": "保民京东小官补",
            "cost": 14,
            "source": 15,
            "name": 7,
            "spec": 8,
            "codes": [4, 5],
        },
        {
            "sheet": "三平台活动汇总",
            "cost": 4,
            "source": 5,
            "name": 1,
            "spec": 3,
            "codes": [],
        },
    ]

    for cfg in configs:
        ws = wb[cfg["sheet"]]
        matched = changed = 0
        for r in range(2, ws.max_row + 1):
            name = ws.cell(r, cfg["name"]).value
            spec = ws.cell(r, cfg["spec"]).value if cfg["spec"] else ""
            codes = [ws.cell(r, c).value for c in cfg["codes"]]
            rec = pick(codes, name, spec, by_code, by_name)
            if not rec:
                continue
            matched += 1
            old = ws.cell(r, cfg["cost"]).value
            try:
                same = old is not None and abs(float(old) - rec["cost"]) < 0.005
            except (TypeError, ValueError):
                same = False
            if not same:
                changed += 1
                ws.cell(r, cfg["cost"]).value = rec["cost"]
                ws.cell(r, cfg["source"]).value = "旺店通库存成本"
        stats[cfg["sheet"]] = {"matched": matched, "changed": changed}

    wb.save(target)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wdt", type=pathlib.Path, default=DEFAULT_WDT)
    ap.add_argument("--target", type=pathlib.Path, default=DEFAULT_TARGET)
    args = ap.parse_args()
    print(apply(args.wdt, args.target))


if __name__ == "__main__":
    main()
