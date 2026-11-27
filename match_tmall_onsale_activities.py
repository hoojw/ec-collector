#!/usr/bin/env python3
"""Match 保民 on-sale Tmall items against all 可报名 activities (离线匹配).

For each on-sale item, find activities by generic name (exact first, then
brand+generic), expand to one row per (item, activity, sku), persist to
tmall_onsale_activity_match_daily.  This is the local equivalent of "searching
可报名 for each store item" without relying on the flaky UI search.
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, re, unicodedata
from zoneinfo import ZoneInfo
import pymysql

TZ = ZoneInfo("Asia/Shanghai")
TABLE = "tmall_onsale_activity_match_daily_v2"


def norm(s):
    return re.sub(r"\s+", "", str(s or "")).strip()


def brand_of(title):
    m = re.match(r"^\[([^\]]+)\]", str(title or "").strip())
    return norm(m.group(1)) if m else ""


def brands_in(raw):
    if not raw:
        return set()
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
        return set(norm(v) for v in (d or {}).values() if v)
    except Exception:
        return set(norm(v) for v in re.findall(r'"([^"]+)"', str(raw)))


def brand_ok(own_brand, activity):
    """活动明确指定其他品牌时，不再按通用名/规格串到我方面商品。"""
    if not own_brand:
        return True
    text = norm(f"{activity.get('活动商品名') or ''} {activity.get('商品标题') or ''}")
    if own_brand in text:
        return True
    brands = brands_in(activity.get('品牌'))
    return not brands or own_brand in brands


def norm_spec(s):
    """规格标准化：统一单位/符号，忽略 *1瓶 等数量为 1 的包装层与中间包装分隔符。"""
    t = unicodedata.normalize("NFKC", str(s or "")).lower()
    for a, b in (("×", "*"), ("x", "*"), ("µ", "μ"), ("ug", "μg"), ("mcg", "μg"),
                 ("（", "("), ("）", ")"), ("：", ":")):
        t = t.replace(a, b)
    t = re.sub(r"\s+", "", t)
    t = re.sub(r"\*1(瓶|板|袋|支|盒|管)", "", t)
    t = re.sub(r"/(瓶|板|袋|支|管)(?=/|$)", "", t)
    return t


def spec_of(title):
    """从 '[品牌]通用名规格' 的标题中截取规格串，用于结果表对照展示。"""
    body = str(title or "").strip().split("]", 1)[-1]
    m = re.search(r"\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|mcg|ml|%|IU|iu|万单位|单位)", body, re.I)
    if not m:
        return ""
    start = m.start()
    if start > 0 and body[start - 1] == "(":
        start -= 1
    return body[start:].strip()


PACK_UNITS = ("片", "粒", "丸", "袋", "支", "泡", "揿", "喷", "贴", "枚")


def pack_tokens(s):
    """提取规格中的包装数量 token，如 7片/28片/2支，忽略剂量部分。"""
    t = str(s or "")
    return set(f"{n}{u}" for n, u in re.findall(
        r"(\d+(?:\.\d+)?)\s*(片|粒|丸|袋|支|泡|揿|喷|贴|枚)", t))


def to_mg(value, unit):
    """剂量统一换算成 mg（g→1000mg），便于 0.15g 与 150mg 对齐。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    u = unit.lower()
    if u == 'g':
        return v * 1000
    if u == 'mg':
        return v
    return None


def dose_set(s):
    """提取剂量(mg)集合，忽略 ml/IU 等容量/效价单位。"""
    return set(v for v in (
        to_mg(n, u) for n, u in re.findall(
            r'(\d+(?:\.\d+)?)\s*(mg|g)\b', str(s or ''), re.I)) if v is not None)


def main_dose(s):
    """取第一个 mg/g 剂量(mg)，作为主药剂量；复方辅料不参与比对。"""
    m = re.search(r'(\d+(?:\.\d+)?)\s*(mg|g)\b', str(s or ''), re.I)
    return to_mg(m.group(1), m.group(2)) if m else None


def dose_conflict(item_spec, activity):
    """比较主剂量，避免复方辅料(如 12.5mg)掩盖主药差异。"""
    od = main_dose(item_spec)
    ad = main_dose(activity.get('规格')) or main_dose(activity.get('活动商品名'))
    return od is not None and ad is not None and abs(od - ad) > 0.01


def fuzzy_spec_status(item_spec, activity):
    """返回 (是否命中, 匹配类型)。

    - 剂量两边都有且冲突 → 不匹配（避免 50mg 匹配到 100mg）
    - 剂量一致/活动无剂量 → 命中，分别标记「规格模糊」「规格待确认」
    """
    onsale_pack = pack_tokens(item_spec)
    if not onsale_pack:
        return False, ''
    act_text = f"{activity.get('规格') or ''} {activity.get('活动商品名') or ''}"
    if not (onsale_pack & pack_tokens(act_text)):
        return False, ''
    if dose_conflict(item_spec, {'规格': activity.get('规格'), '活动商品名': activity.get('活动商品名')}):
        return False, ''
    act_dose = main_dose(act_text)
    if act_dose is not None:
        return True, '规格模糊'
    return True, '规格待确认'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="/opt/subsidy/config.json")
    ap.add_argument("--items", default="/home/dataease/baomin_tmall_onsale_20261123.json")
    ap.add_argument("--day", default="2026-11-24")
    ap.add_argument("--out", default="/opt/subsidy/exports/tmall_onsale_activity_match_20261124.json")
    args = ap.parse_args()

    items = json.loads(pathlib.Path(args.items).read_text(encoding="utf-8"))
    mysql = json.loads(pathlib.Path(args.config).read_text(encoding="utf-8"))["mysql"]
    conn = pymysql.connect(host=mysql["host"], port=int(mysql["port"]), user=mysql["user"],
                           password=mysql["password"], database=mysql["database"],
                           charset=mysql.get("charset", "utf8mb4"), autocommit=False)
    with conn.cursor() as cur:
        cur.execute("""SELECT `活动ID`,`活动类型`,`报名状态`,`是否补贴`,`服务费`,`通用名`,`规格`,
                       `活动商品名`,`天猫商品ID`,`品牌`,`skuId`,`skuItemId`,`包装档位`,`招商价格`,`活动开始`,`活动结束`
                       FROM tmall_activity_available_daily WHERE 记录日期=%s""", (args.day,))
        cols = [d[0] for d in cur.description]
        acts = [dict(zip(cols, r)) for r in cur.fetchall()]

    matched_rows = []
    for it in items:
        g = norm(it.get("generic"))
        if not g:
            generic_source = str(it.get("title") or "").strip()
            m = re.match(r"^(?:\[([^\]]+)\]|【([^】]+)】)", generic_source)
            if m:
                generic_source = generic_source[m.end():].strip()
            generic_source = generic_source.lstrip("[【")
            m = re.search(r"\d|[(（]", generic_source)
            g = norm(generic_source[:m.start()] if m else generic_source)
            g = re.sub(r"[【\[(（]+$", "", g)
        b = brand_of(it.get("title"))
        title = it.get("title")
        title_text = str(title or "").strip()
        norm_title = norm_spec(title_text)

        item_spec = title_text.split("]", 1)[-1]

        # 先严格匹配（同样要求剂量不冲突），命中不足再用模糊匹配
        cand = [a for a in acts if norm(a.get("通用名")) == g
                and norm_spec(a.get("规格")) and norm_spec(a.get("规格")) in norm_title
                and not dose_conflict(item_spec, a)]
        match_kind = "规格精确"
        if not cand:
            fuzzy = [(a, fuzzy_spec_status(item_spec, a)) for a in acts
                     if norm(a.get("通用名")) == g]
            fuzzy = [(a, st) for a, (ok, st) in fuzzy if ok]
            cand = [a for a, st in fuzzy]
            match_kind = fuzzy[0][1] if fuzzy else ""
        if not cand and g:
            fuzzy = [(a, fuzzy_spec_status(item_spec, a)) for a in acts
                     if g in norm(a.get("通用名")) and b and b in brands_in(a.get("品牌"))]
            fuzzy = [(a, st) for a, (ok, st) in fuzzy if ok]
            cand = [a for a, st in fuzzy]
            match_kind = fuzzy[0][1] if fuzzy else ""
        cand = [a for a in cand if brand_ok(b, a)]
        for a in cand:
            r = dict(a)
            # 在售商品字段用明确的前缀，避免与活动字段(天猫商品ID/通用名等)重名
            r["在售商品标题"] = title_text
            r["在售商品ID"] = it.get("item_id")
            r["在售售价"] = it.get("price")
            r["库存"] = it.get("stock")
            r["本店品牌"] = b
            r["在售规格"] = spec_of(title_text)
            r["搜索通用名"] = g
            r["_match_kind"] = match_kind
            matched_rows.append(r)

    def jsonable(v):
        if isinstance(v, dt.date):
            return v.isoformat()
        try:
            from decimal import Decimal
            if isinstance(v, Decimal):
                return float(v)
        except Exception:
            pass
        return v
    clean_rows = [{k: jsonable(v) for k, v in r.items()} for r in matched_rows]
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps({
        "day": args.day, "item_count": len(items), "match_count": len(matched_rows),
        "matched_items": len(set(r["在售商品ID"] for r in matched_rows)), "rows": clean_rows,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("items", len(items), "matched_rows", len(matched_rows),
          "matched_items", len(set(r["在售商品ID"] for r in matched_rows)))

    with conn.cursor() as cur:
        cur.execute(f"""CREATE TABLE IF NOT EXISTS `{TABLE}` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `记录日期` DATE NOT NULL,
            `在售商品标题` VARCHAR(500) DEFAULT NULL,
            `在售商品ID` VARCHAR(50) DEFAULT NULL,
            `在售售价` DECIMAL(12,2) DEFAULT NULL,
            `库存` DECIMAL(12,2) DEFAULT NULL,
            `本店品牌` VARCHAR(100) DEFAULT NULL,
            `在售规格` VARCHAR(255) DEFAULT NULL,
            `搜索通用名` VARCHAR(255) DEFAULT NULL,
            `活动ID` VARCHAR(50) DEFAULT NULL,
            `活动类型` VARCHAR(50) DEFAULT NULL,
            `报名状态` VARCHAR(20) DEFAULT NULL,
            `是否补贴` VARCHAR(20) DEFAULT NULL,
            `服务费` VARCHAR(50) DEFAULT NULL,
            `通用名` VARCHAR(255) DEFAULT NULL,
            `规格` VARCHAR(255) DEFAULT NULL,
            `活动商品名` VARCHAR(500) DEFAULT NULL,
            `skuId` VARCHAR(60) DEFAULT NULL,
            `包装档位` VARCHAR(50) DEFAULT NULL,
            `招商价格` DECIMAL(12,2) DEFAULT NULL,
            `活动开始` VARCHAR(40) DEFAULT NULL,
            `活动结束` VARCHAR(40) DEFAULT NULL,
            `match_type` VARCHAR(20) DEFAULT NULL,
            `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uk_day_item_act_sku` (`记录日期`,`在售商品ID`,`活动ID`,`skuId`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        cur.execute(f"SHOW COLUMNS FROM `{TABLE}` LIKE '在售规格'")
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE `{TABLE}` ADD COLUMN `在售规格` VARCHAR(255) DEFAULT NULL AFTER `本店品牌`")
        sql = f"""INSERT INTO `{TABLE}`
            (`记录日期`,`在售商品标题`,`在售商品ID`,`在售售价`,`库存`,`本店品牌`,`在售规格`,`搜索通用名`,
             `活动ID`,`活动类型`,`报名状态`,`是否补贴`,`服务费`,`通用名`,`规格`,`活动商品名`,
             `skuId`,`包装档位`,`招商价格`,`活动开始`,`活动结束`,`match_type`)
            VALUES ({','.join(['%s']*22)})
            ON DUPLICATE KEY UPDATE `招商价格`=VALUES(`招商价格`),`报名状态`=VALUES(`报名状态`)"""
        vals = []
        for r in matched_rows:
            vals.append((
                args.day, r.get("在售商品标题"), r.get("在售商品ID"), r.get("在售售价"), r.get("库存"),
                r.get("本店品牌"), r.get("在售规格"), r.get("搜索通用名"), r.get("活动ID"), r.get("活动类型"),
                r.get("报名状态"), r.get("是否补贴"), r.get("服务费"), r.get("通用名"), r.get("规格"),
                r.get("活动商品名"), r.get("skuId"), r.get("包装档位"), r.get("招商价格"),
                r.get("活动开始"), r.get("活动结束"), r.get("_match_kind"),
            ))
        cur.execute(f"DELETE FROM `{TABLE}` WHERE 记录日期=%s", (args.day,))
        for i in range(0, len(vals), 500):
            cur.executemany(sql, vals[i:i+500])
    conn.commit(); conn.close()
    print("persisted", len(matched_rows), "rows into", TABLE)


if __name__ == "__main__":
    main()
