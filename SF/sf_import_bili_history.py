#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SF B站历史基线合并导入器 (sf_import_bili_history.py)
======================================================
把主项目里人工导出的《B站后台视频数据.xlsx》(231 条) 合并进本项目的
sq_metrics.db,补上 SF 缺失的 B站历史台账。

背景
----
SF 采集器建立于 2026-08-12,当时**未导入**这份列表,导致 DB 里 B站 长期只有
1 条。主项目那份 xlsx 与 SF 各持一半数据、从未打通:

  xlsx      : 231 条,2021-12-31 ~ 2026-07-31,7 项聚合指标,**无 BV 号**
  SF DB     : 6 条(合并前),有 BV 号 + 逐日趋势 + 观众画像 + 完播/互动深度指标

本脚本按「发布时间(北京时间,秒级)」把两边配对,给 231 条补上 BV 号后入库,
使 SF 成为 B站 数据的唯一完整来源。

数据质量声明(重要,消费方必读)
------------------------------
xlsx 的指标是创作中心后台的**显示值**,实测结论:
  * ≥1万 的数值精度只到千位 —— 202 个 ≥1万 播放值 100% 能被 1000 整除、
    0 个反例;点赞 51/51、收藏 35/35、投币 2/2 同规律。即存在 ±500 量级
    取整误差(相对误差通常 <0.1%,但对小数值需谨慎)。
  * <1万 的数值为精确值。
  * 只有 2026-08-01 单一快照,无趋势/画像/完播率/互动率/涨粉。

因此入库快照的 source 标记为 `import_xlsx`,与 API 抓取的 `api` 严格区分。
**消费方不得把 source=import_xlsx 的数值当作精确值使用。**

匹配策略(两级)
--------------
1. 发布时间精确匹配(北京时间,秒级) —— 主力,实测命中 227/231 = 98.3%
2. 标题相似度匹配(difflib ratio ≥ 0.85,且发布时间须在 ±3 天内) —— 兜底
仍未命中者以合成 key `xlsx-{YYYYMMDDHHMMSS}` 入库,报告单列提示人工核对
(典型原因:稿件已从后台删除,或被平台永久清理)。

幂等性
------
重复执行安全:导入前会先删除本平台下 source='import_xlsx' 的既有快照再重建,
不会产生重复。**不会触及** source='api' 的抓取数据。

用法
----
  python sf_import_bili_history.py --dry-run          # 只配对+出报告,不写库
  python sf_import_bili_history.py                    # 正式导入
  python sf_import_bili_history.py --list-json L.json # 复用已有后台列表(省去现场抓取)
  python sf_import_bili_history.py --xlsx PATH        # 指定 xlsx 数据源
"""

import argparse
import datetime
import difflib
import json
import re
import shutil
import sys
from pathlib import Path

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sf_core import DB  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 主项目里人工导出的原始列表(数据源)
DEFAULT_XLSX = Path("/Users/summer/AQi-channel/data/B站后台视频数据.xlsx")
# 本项目内的数据源留档目录
IMPORT_DIR = ROOT / "data" / "imports"

# 快照时点:取导出文件的最早 mtime(~/Downloads 副本 2026-08-01 17:16)。
# 偏晚取值是审计上的安全方向 —— 宁可把快照判成「揭晓之后」而多余排除,
# 也不可标早导致「揭晓之后的数据」被当成复盘可用的历史快照。
SNAPSHOT_AT = "2026-08-01 17:16:00"
SOURCE = "import_xlsx"
TZ_CN = datetime.timezone(datetime.timedelta(hours=8))

XLSX_SHEET = "视频数据"
# xlsx 列序(第 0/1 列为标题/发布时间,其后为 7 项指标)
METRIC_COLS = ["views", "likes", "danmaku", "comments", "coins", "favorites", "shares"]
COL_CN = {"views": "播放数", "likes": "点赞", "danmaku": "弹幕", "comments": "评论",
          "coins": "投币", "favorites": "收藏", "shares": "分享"}
TITLE_SIM_MIN = 0.85
TIME_WINDOW_DAYS = 3


# ─── 读取数据源 ──────────────────────────────────────────────────────
def load_xlsx(path: Path) -> list:
    """读 xlsx → 结构化行列表。"""
    if not path.exists():
        print(f"❌ 找不到数据源: {path}")
        sys.exit(1)
    wb = openpyxl.load_workbook(path, data_only=True)
    if XLSX_SHEET not in wb.sheetnames:
        print(f"❌ xlsx 缺少 sheet「{XLSX_SHEET}」,实际: {wb.sheetnames}")
        sys.exit(1)
    ws = wb[XLSX_SHEET]
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0]:
            continue
        row = {"title": str(r[0]).strip(), "published_at": r[1]}
        for i, k in enumerate(METRIC_COLS):
            v = r[2 + i]
            row[k] = float(v) if isinstance(v, (int, float)) else None
        rows.append(row)
    return rows


def _norm_title(t) -> str:
    """标题归一:去空白/标点,统一全角竖线(B站后台把 ｜ 规范成 丨)。"""
    t = str(t or "").strip().lower()
    t = re.sub(r"[\s\u3000]+", "", t)
    return re.sub(r"[，。！？、,.!?：:；;\"'“”‘’（）()\[\]【】|｜丨~～…\-—–_*]", "", t)


def _fmt_dt(v) -> str:
    """xlsx 的发布时间 → 'YYYY-MM-DD HH:MM:SS'(可能是 datetime 或字符串)。"""
    if isinstance(v, datetime.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v or "").strip()


def _from_ptime(ptime) -> str:
    """后台列表项 → 'YYYY-MM-DD HH:MM:SS'。

    优先用 published_at_full(新版本 list_archives 才有);
    旧版缓存只有 ptime,此处兜底换算,保证报告字段不空。
    """
    if not ptime:
        return ""
    return datetime.datetime.fromtimestamp(int(ptime), TZ_CN).strftime("%Y-%m-%d %H:%M:%S")


def _to_dt(v):
    if isinstance(v, datetime.datetime):
        return v
    try:
        return datetime.datetime.strptime(str(v).strip()[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


# ─── 两级匹配 ────────────────────────────────────────────────────────
def build_time_index(archives: list) -> dict:
    idx = {}
    for a in archives:
        ptime = a.get("ptime") or 0
        if not ptime:
            continue
        key = datetime.datetime.fromtimestamp(int(ptime), TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        idx.setdefault(key, []).append(a)
    return idx


def match_one(row: dict, by_time: dict, archives: list) -> tuple:
    """返回 (archive|None, how)。how ∈ {time, title(x.xx), none}"""
    ts = _fmt_dt(row["published_at"])
    cands = by_time.get(ts)
    if cands:
        return cands[0], "time"

    # 兜底:标题相似度 + 时间窗约束(双重条件,避免误配到同名不同期的稿件)
    nt = _norm_title(row["title"])
    if not nt:
        return None, "none"
    dt = _to_dt(row["published_at"])
    best, best_ratio = None, 0.0
    for a in archives:
        ratio = difflib.SequenceMatcher(None, nt, _norm_title(a.get("title"))).ratio()
        if ratio <= best_ratio:
            continue
        if dt is not None and a.get("ptime"):
            a_dt = datetime.datetime.fromtimestamp(
                int(a["ptime"]), TZ_CN).replace(tzinfo=None)
            if abs((a_dt - dt).total_seconds()) > TIME_WINDOW_DAYS * 86400:
                continue
        best, best_ratio = a, ratio
    if best is not None and best_ratio >= TITLE_SIM_MIN:
        return best, f"title({best_ratio:.2f})"
    return None, "none"


def make_synthetic_key(row: dict, used: set) -> str:
    """未匹配行的合成 key(无 BV 号,无法与后台对应)。"""
    ts = _fmt_dt(row["published_at"]).replace("-", "").replace(":", "").replace(" ", "")
    base = f"xlsx-{ts or 'unknown'}"
    key, n = base, 1
    while key in used:
        n += 1
        key = f"{base}-{n}"
    used.add(key)
    return key


# ─── 主流程 ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="B站历史基线合并导入器")
    ap.add_argument("--xlsx", default=str(DEFAULT_XLSX), help="数据源 xlsx 路径")
    ap.add_argument("--list-json", help="复用已有后台列表 JSON(含 ptime);缺省现场抓取")
    ap.add_argument("--dry-run", action="store_true", help="只配对出报告,不写库")
    ap.add_argument("--report-out", help="配对报告写入该 JSON")
    ap.add_argument("--show", action="store_true", help="现场抓列表时有头模式(调试)")
    args = ap.parse_args()

    xlsx_path = Path(args.xlsx)
    print("=" * 72)
    print("B站历史基线合并导入  (source=%s, 快照时点=%s)" % (SOURCE, SNAPSHOT_AT))
    print("=" * 72)

    # ── 1. 数据源 ──
    xls_rows = load_xlsx(xlsx_path)
    print(f"\n[1] 数据源 {xlsx_path.name}: {len(xls_rows)} 条")
    dts = sorted(_fmt_dt(r["published_at"]) for r in xls_rows)
    print(f"    发布时间范围: {dts[0]} ~ {dts[-1]}")

    # 数据源留档(便于复现与审计)
    if not args.dry_run:
        IMPORT_DIR.mkdir(parents=True, exist_ok=True)
        dst = IMPORT_DIR / xlsx_path.name
        if not dst.exists() or dst.stat().st_size != xlsx_path.stat().st_size:
            shutil.copy2(xlsx_path, dst)
            print(f"    数据源已留档: {dst.relative_to(ROOT)}")

    # ── 2. 后台稿件列表 ──
    if args.list_json:
        archives = json.loads(Path(args.list_json).read_text(encoding="utf-8"))
        print(f"\n[2] 后台列表(复用缓存 {args.list_json}): {len(archives)} 条")
    else:
        print("\n[2] 现场抓取后台稿件列表 …")
        from sf_bilibili import list_archives
        archives = list_archives(headless=not args.show)
        print(f"    抓到 {len(archives)} 条")
    if not archives:
        print("❌ 后台列表为空,终止")
        sys.exit(1)
    if not archives[0].get("ptime"):
        print("❌ 列表缺少 ptime 字段,无法精确匹配。请用当前版本重新抓取。")
        sys.exit(1)

    by_time = build_time_index(archives)
    print(f"    可按发布时间(秒级)索引的唯一时点: {len(by_time)}")

    # ── 3. 两级匹配 ──
    db = DB()
    have = {v["video_key"] for v in db.list_videos(platform="bilibili")}

    matched_raw, unmatched = [], []
    how_count = {}
    for row in xls_rows:
        a, how = match_one(row, by_time, archives)
        how_count[how.split("(")[0]] = how_count.get(how.split("(")[0], 0) + 1
        if a:
            matched_raw.append((row, a, how))
        else:
            unmatched.append(row)

    # 数据源去重:xlsx 里存在同一视频被记两遍的行(实测 1 处 —— 标题多出 "sI"
    # 而发布时间与播放完全相同),匹配后会撞到同一 BV。保留「与后台标题最相似」
    # 的一条,其余计入 dropped 并在报告中说明。
    best_by_bv, dropped = {}, []
    for row, a, how in matched_raw:
        ratio = difflib.SequenceMatcher(
            None, _norm_title(row["title"]), _norm_title(a.get("title"))).ratio()
        cur = best_by_bv.get(a["bvid"])
        if cur is None:
            best_by_bv[a["bvid"]] = (row, a, how, ratio)
        elif ratio > cur[3]:
            dropped.append(cur)
            best_by_bv[a["bvid"]] = (row, a, how, ratio)
        else:
            dropped.append((row, a, how, ratio))
    matched = [(r, a, h) for (r, a, h, _) in best_by_bv.values()]

    print(f"\n[3] 匹配结果")
    print(f"    ✅ 命中 {len(matched)} / {len(xls_rows)} "
          f"= {len(matched) / len(xls_rows) * 100:.1f}%   {how_count}")
    print(f"    ❌ 未命中 {len(unmatched)}")
    for row in unmatched:
        print(f"       {_fmt_dt(row['published_at'])}  播放 {row['views']:,.0f}  "
              f"{row['title'][:44]}")
    if dropped:
        print(f"    ⚠️ 数据源重复行 {len(dropped)} 条(同一 BV 被多行命中,已保留最相似的一条):")
        for row, a, _how, ratio in dropped:
            print(f"       {a['bvid']}  丢弃「{row['title'][:36]}」相似度 {ratio:.2f}")

    # BV 唯一性断言(去重后不应再有冲突)
    assert len(matched) == len(best_by_bv), "BV 去重失败"

    # ── 4. 写库 ──
    pre_existing = [a["bvid"] for _, a, _ in matched if a["bvid"] in have]
    print(f"\n[4] 写库")
    print(f"    已入库(有 API 数据、本次仅补历史快照): {len(pre_existing)} "
          f"{pre_existing if pre_existing else ''}")
    print(f"    新建稿件: {len(matched) - len(pre_existing)}")
    print(f"    未命中、以合成 key 入库: {len(unmatched)}")

    if args.dry_run:
        print("\n[dry-run] 未写库。抽样预览(前 6 条):")
        for row, a, how in matched[:6]:
            print(f"    {a['bvid']}  {a['published_at']}  [{how}]  {a['title'][:40]}")
            print(f"        播放 {row['views']:,.0f} 点赞 {row['likes']:,.0f} "
                  f"收藏 {row['favorites']:,.0f} 投币 {row['coins']:,.0f} "
                  f"评论 {row['comments']:,.0f} 弹幕 {row['danmaku']:,.0f} "
                  f"分享 {row['shares']:,.0f}")
    else:
        # 幂等:清掉本平台既有 import_xlsx 快照(不动 api 数据)
        with db._conn() as c:
            cur = c.execute(
                """DELETE FROM metrics_snapshots WHERE source=?
                   AND video_id IN (SELECT id FROM videos WHERE platform='bilibili')""",
                (SOURCE,))
            purged = cur.rowcount
        if purged:
            print(f"    (幂等)清除既有 import_xlsx 快照 {purged} 条")

        n_vid, n_snap = 0, 0
        for row, a, how in matched:
            bvid = a["bvid"]
            vid = db.upsert_video(
                platform="bilibili", video_key=bvid,
                title=a.get("title") or row["title"],
                published_at=a.get("published_at") or _fmt_dt(row["published_at"])[:10],
                url=f"https://www.bilibili.com/video/{bvid}",
            )
            metrics = {k: row[k] for k in METRIC_COLS if row.get(k) is not None}
            db.add_snapshot(vid, "cumulative", metrics, source=SOURCE,
                            captured_at=SNAPSHOT_AT)
            n_vid += 1
            n_snap += 1

        used_keys = {v["video_key"] for v in db.list_videos(platform="bilibili")}
        for row in unmatched:
            key = make_synthetic_key(row, used_keys)
            vid = db.upsert_video(
                platform="bilibili", video_key=key,
                title=row["title"],
                published_at=_fmt_dt(row["published_at"])[:10],
                url=None,
            )
            metrics = {k: row[k] for k in METRIC_COLS if row.get(k) is not None}
            db.add_snapshot(vid, "cumulative", metrics, source=SOURCE,
                            captured_at=SNAPSHOT_AT)
            n_vid += 1
            n_snap += 1

        print(f"    ✅ 写入 {n_vid} 条稿件 / {n_snap} 条快照")

    # ── 5. 报告 ──
    report = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_file": str(xlsx_path),
        "snapshot_at": SNAPSHOT_AT,
        "source_tag": SOURCE,
        "xlsx_rows": len(xls_rows),
        "archives_total": len(archives),
        "matched": len(matched),
        "matched_by": how_count,
        "unmatched": [
            {"published_at": _fmt_dt(r["published_at"]), "title": r["title"],
             "views": r["views"]} for r in unmatched
        ],
        "dropped_duplicates": [
            {"bvid": a["bvid"], "dropped_title": row["title"],
             "kept_similarity": round(ratio, 3)}
            for row, a, _how, ratio in dropped
        ],
        "pairs": [
            {"bvid": a["bvid"],
             "published_at": a.get("published_at_full") or _from_ptime(a.get("ptime")),
             "how": how, "xlsx_title": row["title"], "bg_title": a.get("title")}
            for row, a, how in matched
        ],
        "pre_existing": pre_existing,
    }
    out = Path(args.report_out) if args.report_out else \
        ROOT / "data" / "imports" / \
        f"bili_history_match_{datetime.datetime.now().strftime('%Y%m%d_%H%M')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[5] 配对报告: {out}")

    print(f"\n{'=' * 72}")
    if args.dry_run:
        print("dry-run 结束,未写库。确认无误后去掉 --dry-run 正式执行。")
    else:
        n = db.list_videos(platform="bilibili")
        print(f"完成。DB 中 B站 稿件: {len(n)} 条")


if __name__ == "__main__":
    main()
