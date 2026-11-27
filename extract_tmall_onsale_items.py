#!/usr/bin/env python3
"""Normalize raw Tmall on_sale rows from a daily JSON into match-script input."""
from __future__ import annotations

import argparse
import json
import pathlib
import re


def _text(value):
    if isinstance(value, dict):
        for key in ("title", "text", "copyText", "value"):
            if value.get(key):
                return str(value[key]).strip()
        for piece in value.get("desc") or []:
            if isinstance(piece, dict):
                for key in ("text", "copyText"):
                    if piece.get(key):
                        return str(piece[key]).strip()
        return ""
    return str(value or "").strip()


def title_of(row):
    for key in ("title", "itemTitle", "name", "itemDesc"):
        title = _text(row.get(key))
        if title:
            return title
    return ""


def generic_of(title):
    s = str(title or "").strip()
    m = re.match(r"^(?:\[([^\]]+)\]|【([^】]+)】)", s)
    if m:
        s = s[m.end():].strip()
    s = s.lstrip("[【")
    m = re.search(r"\d|[(（]", s)
    g = s[:m.start()] if m else s
    return re.sub(r"[【\[(（]+$", "", g).strip()


def first_number(value):
    if value is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", str(value).replace(",", "").replace("¥", ""))
    return float(m.group()) if m else None


def price_of(row):
    mp = row.get("managerPrice")
    if isinstance(mp, dict):
        return first_number(mp.get("currentPrice") or mp.get("price"))
    return first_number(mp)


def stock_of(row):
    mq = row.get("managerQuantityNew")
    if isinstance(mq, dict):
        return first_number(mq.get("text") or mq.get("value"))
    return first_number(mq)


def normalize(payload):
    data = payload.get("data") or {}
    on_sale = data.get("on_sale") or {}
    rows = on_sale.get("rows") or on_sale.get("items") or []
    items = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get("itemId") or row.get("item_id") or "").strip()
        if not item_id:
            continue
        title = title_of(row)
        items.append({
            "title": title,
            "generic": generic_of(title),
            "item_id": item_id,
            "price": price_of(row),
            "stock": stock_of(row),
        })
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    payload = json.loads(pathlib.Path(args.daily).read_text(encoding="utf-8"))
    items = normalize(payload)
    pathlib.Path(args.out).write_text(
        json.dumps(items, ensure_ascii=False), encoding="utf-8"
    )
    print(f"extracted {len(items)} on_sale items")


if __name__ == "__main__":
    main()
