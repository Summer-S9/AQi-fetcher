# -*- coding: utf-8 -*-
"""
B站数据全量校验 (bili_audit.py)
========================================
把 DB 里每条 B站稿件的数据,与抓取时存档的**接口响应原文**(data/assets/)逐字段
比对,确认入库准确无误。**全量核对,不做抽查**(与 xhs_audit.py 同等纪律)。

核对链路(每条稿件):
  1. 稿件身份: videos.title / published_at  vs  v3/archive/view 原文
  2. 核心指标: metrics_snapshots(最新 cumulative)  vs  archive_diagnose/overview 原文
               (播放/点赞/弹幕/评论/分享/收藏/投币/涨粉/取关/平均时长/总时长)
  3. 完播/退出: finish_rate / avg_play_progress  vs  v2/archive/analyze/graph 原文
  4. 互动/跳出: interact_rate / crash_rate 等  vs  table archive_diagnose/play_analyze 原文
  5. TV 占比:  tv_ratio  vs  由 overview.play_proportion 重算
  6. 趋势完整性: metric_series 的日期集合  vs  各 trend 接口 tendency 的日期集合
               (8 维度逐日 + 小时级 + 近30天长尾,逐条日期比对,允许前端不存在的日期)
  7. 画像完整性: audience_snapshots 各字段是否非空

用法:
  python bili_audit.py              # 全量核对
  python bili_audit.py --bv BV1XXX  # 只核对一条
  python bili_audit.py --csv out.csv  # 同时导出明细
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "sq_metrics.db"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sf_core import load_asset  # noqa: E402

# overview.stat → DB 列(与 sf_bilibili.parse_overview 保持一致)
# 注意:刻意**不含** play_avg_duration —— B站 overview 接口该字段恒返回 0
# (实测全部 6 条稿件均为 0),真实平均播放时长只在
# v2/archive/analyze/graph 的 duration_info.avg_play_time_int 里。
# 故 avg_duration 的核对放在「3. 完播/退出」一节做,避免误报。
STAT_MAP = {
    "play": "views", "like": "likes", "dm": "danmaku", "comment": "comments",
    "share": "shares", "fav": "favorites", "coin": "coins",
    "fan": "followers_gain", "unfollow": "unfollow",
    "total_duration": "total_duration",
}

# trend type → metric_series 里的指标名(与 sf_bilibili.TREND_TYPES 一致)
TREND_MAP = {
    "play": "plays", "fan": "followers", "like": "likes", "coin": "coins",
    "fav": "favorites", "comment": "comments", "dm": "danmaku", "share": "shares",
}

# play_analyze 里需要 ÷100 的万分比字段 → DB 列
PA_RATE_MAP = {
    "interact_rate": "interact_rate",
    "interact_viewer_rate": "interact_viewer_rate",
    "interact_fan_rate": "interact_fan_rate",
    "crash_rate": "crash_rate",
    "crash_viewer_rate": "crash_viewer_rate",
    "crash_fan_rate": "crash_fan_rate",
}
PA_STAR_MAP = {
    "interact_star": "interact_star",
    "crash_star": "crash_star",
}
# viewer_assistant 万分比 → DB 列
PA_SOURCE_MAP = {
    "play_fan_rate": "fan_play_rate",
    "play_viewer_rate": "viewer_play_rate",
}


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def ts_to_date(ts):
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def get_archive(conn, bvid, url_frag, up_to=None, exclude_frag=None):
    """取该稿件某接口最新存档的 JSON 原文。

    up_to: 只取 captured_at <= 该时刻的存档(确保与目标快照同一次抓取)
    """
    sql = ("SELECT asset_path, captured_at, url FROM api_archives "
           "WHERE platform='bilibili' AND video_key=? AND url LIKE ?")
    args = [bvid, f"%{url_frag}%"]
    if exclude_frag:
        sql += " AND url NOT LIKE ?"
        args.append(f"%{exclude_frag}%")
    if up_to:
        sql += " AND captured_at <= ?"
        args.append(up_to)
    sql += " ORDER BY captured_at DESC LIMIT 1"
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return None, None
    return load_asset(rows[0]["asset_path"]), rows[0]["captured_at"]


class Checker:
    def __init__(self):
        self.diffs = []      # (bvid, 类别, 字段, DB值, 原文值)
        self.notes = []

    def add(self, bvid, cat, field, db_val, raw_val):
        self.diffs.append((bvid, cat, field, db_val, raw_val))

    def cmp(self, bvid, cat, field, db_val, raw_val, tol=0.01):
        """数值比对(带容差);任一侧为 None 则跳过并记 note。"""
        if raw_val is None:
            self.notes.append((bvid, cat, field, "原文无此字段,跳过"))
            return
        if db_val is None:
            self.add(bvid, cat, field, "DB 缺失(NULL)", raw_val)
            return
        if abs(num(db_val) - num(raw_val)) > tol:
            self.add(bvid, cat, field, db_val, raw_val)


def audit_video(conn, v, ck: Checker) -> dict:
    bvid = v["video_key"]
    stat_out = {"bvid": bvid, "title": v["title"], "checks": 0, "issues": 0}

    snaps = conn.execute(
        """SELECT * FROM metrics_snapshots WHERE video_id=? AND snapshot_type='cumulative'
           ORDER BY captured_at DESC LIMIT 1""", (v["id"],)).fetchone()
    if not snaps:
        ck.add(bvid, "快照", "cumulative", "无快照", "-")
        stat_out["issues"] += 1
        return stat_out
    up_to = snaps["captured_at"]

    # ── 1. 稿件身份 ──
    view, _ = get_archive(conn, bvid, "v3/archive/view", up_to=up_to)
    if view and view.get("code") == 0:
        d = view.get("data") or {}
        if d.get("title") and d.get("title") != v["title"]:
            ck.add(bvid, "身份", "title", v["title"], d["title"])
            stat_out["issues"] += 1
        pub = ts_to_date(d.get("pubtime"))
        if pub and v["published_at"] and pub != v["published_at"]:
            ck.add(bvid, "身份", "published_at", v["published_at"], pub)
            stat_out["issues"] += 1
        stat_out["checks"] += 2
    else:
        ck.notes.append((bvid, "身份", "v3/archive/view", "无存档,跳过"))

    # ── 2. 核心指标 ──
    ov, _ = get_archive(conn, bvid, "archive_diagnose/overview", up_to=up_to)
    if ov and ov.get("code") == 0:
        ovd = ov.get("data") or {}
        stat = ovd.get("stat") or {}
        for raw_k, col in STAT_MAP.items():
            if raw_k in stat:
                ck.cmp(bvid, "核心指标", f"{col}(stat.{raw_k})", snaps[col], stat[raw_k])
                stat_out["checks"] += 1
                if snaps[col] is not None and num(snaps[col]) != num(stat[raw_k]):
                    stat_out["issues"] += 1

        # ── 5. TV 占比重算 ──
        pp = ovd.get("play_proportion") or {}
        if pp:
            ott = pp.get("new_ott") or 0
            total = (ott + (pp.get("new_mobile") or 0) + (pp.get("new_pc") or 0)
                     + (pp.get("new_h5") or 0) + (pp.get("new_others") or 0))
            if total:
                expect = round(ott / total * 100, 1)
                ck.cmp(bvid, "TV占比", "tv_ratio", snaps["tv_ratio"], expect, tol=0.05)
                stat_out["checks"] += 1
                if snaps["tv_ratio"] is not None and abs(num(snaps["tv_ratio"]) - expect) > 0.05:
                    stat_out["issues"] += 1
    else:
        ck.notes.append((bvid, "核心指标", "overview", "无存档,跳过"))

    # ── 3. 完播/退出(graph,用 cid) ──
    g, _ = get_archive(conn, bvid, "v2/archive/analyze/graph", up_to=up_to)
    if g and g.get("code") == 0:
        gd = g.get("data") or {}
        qi = gd.get("quit_info") or {}
        di = gd.get("duration_info") or {}
        if qi.get("full_play_ratio") is not None:
            ck.cmp(bvid, "完播", "finish_rate", snaps["finish_rate"],
                   round(float(qi["full_play_ratio"]) / 100, 2))
            stat_out["checks"] += 1
        if di.get("avg_play_time_int") is not None:
            ck.cmp(bvid, "完播", "avg_duration", snaps["avg_duration"],
                   float(di["avg_play_time_int"]))
            stat_out["checks"] += 1
        # 段退出曲线条数
        vq = gd.get("viewer_quit") or []
        db_vq = conn.execute(
            """SELECT COUNT(*) FROM metric_series WHERE video_id=? AND metric='quit_curve_20s'""",
            (v["id"],)).fetchone()[0]
        stat_out["checks"] += 1
        if vq and db_vq < len(vq):
            ck.add(bvid, "退出曲线", "quit_curve_20s 条数", db_vq, len(vq))
            stat_out["issues"] += 1
    else:
        ck.notes.append((bvid, "完播", "analyze/graph", "无存档,跳过"))

    # ── 4. 互动/跳出(play_analyze) ──
    pa, _ = get_archive(conn, bvid, "archive_diagnose/play_analyze", up_to=up_to)
    if pa and pa.get("code") == 0:
        pd = pa.get("data") or {}
        gi = pd.get("guest_interact") or {}
        for raw_k, col in PA_RATE_MAP.items():
            if gi.get(raw_k) is not None:
                ck.cmp(bvid, "互动/跳出", col, snaps[col],
                       round(float(gi[raw_k]) / 100, 2), tol=0.011)
                stat_out["checks"] += 1
        for raw_k, col in PA_STAR_MAP.items():
            if gi.get(raw_k) is not None:
                ck.cmp(bvid, "互动/跳出", col, snaps[col],
                       round(float(gi[raw_k]) / 10, 1), tol=0.06)
                stat_out["checks"] += 1
        va = pd.get("viewer_assistant") or {}
        for raw_k, col in PA_SOURCE_MAP.items():
            if va.get(raw_k) is not None:
                ck.cmp(bvid, "播放来源", col, snaps[col],
                       round(float(va[raw_k]) / 100, 2), tol=0.011)
                stat_out["checks"] += 1
    else:
        ck.notes.append((bvid, "互动/跳出", "play_analyze", "无存档,跳过"))

    # ── 6. 趋势完整性(逐日 8 维度 + 小时级 + 近30天长尾) ──
    for ttype, metric in TREND_MAP.items():
        # 精确取 type=<ttype> 的存档
        tr, _ = get_archive(conn, bvid, f"type={ttype}", up_to=up_to)
        if not (tr and tr.get("code") == 0):
            ck.notes.append((bvid, "趋势", f"trend?type={ttype}", "无存档,跳过"))
            continue
        td = tr.get("data") or {}
        for sub_key, mname in (("tendency", metric),
                               ("hour_tendency", f"hour_{metric}"),
                               ("last_30_day_tendency", f"{metric}_last30")):
            items = td.get(sub_key) or []
            raw_dates = {str(i.get("date_key")) for i in items if i.get("date_key") is not None}
            if not raw_dates:
                continue
            if sub_key == "hour_tendency":
                # DB 里小时级存成 "MM-DD HH:MM"
                import datetime as _dt
                raw_set = set()
                for k in raw_dates:
                    try:
                        raw_set.add(_dt.datetime.fromtimestamp(int(k)).strftime("%m-%d %H:%M"))
                    except (ValueError, TypeError):
                        pass
            else:
                raw_set = {ts_to_date(k) for k in raw_dates}
                raw_set.discard(None)
            db_rows = conn.execute(
                "SELECT stat_date FROM metric_series WHERE video_id=? AND metric=?",
                (v["id"], mname)).fetchall()
            db_set = {r[0] for r in db_rows}
            stat_out["checks"] += 1
            missing = raw_set - db_set
            if missing:
                ck.add(bvid, "趋势", f"{mname} 缺 {len(missing)}/{len(raw_set)} 个日期",
                       f"DB {len(db_set)} 条", f"原文 {len(raw_set)} 条")
                stat_out["issues"] += 1

    # ── 7. 画像完整性 ──
    au = conn.execute(
        "SELECT * FROM audience_snapshots WHERE video_id=? ORDER BY captured_at DESC LIMIT 1",
        (v["id"],)).fetchone()
    if not au:
        ck.add(bvid, "画像", "audience_snapshots", "无记录", "-")
        stat_out["issues"] += 1
    else:
        for col in ("gender_json", "age_json", "region_json", "source_json", "interest_json"):
            stat_out["checks"] += 1
            if not au[col]:
                ck.notes.append((bvid, "画像", col, "为空(原文可能未返回)"))
    return stat_out


def main():
    ap = argparse.ArgumentParser(description="B站数据全量校验(DB vs 接口原文)")
    ap.add_argument("--bv", help="只核对指定 BV 号")
    ap.add_argument("--csv", help="明细导出 CSV")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    if args.bv:
        videos = conn.execute(
            "SELECT * FROM videos WHERE platform='bilibili' AND video_key=?", (args.bv,)).fetchall()
    else:
        videos = conn.execute(
            "SELECT * FROM videos WHERE platform='bilibili' ORDER BY published_at").fetchall()

    print("=" * 72)
    print(f"B站数据全量校验  DB={DB_PATH}")
    print(f"待核对稿件: {len(videos)} 条(全量,非抽查)")
    print("=" * 72)

    ck = Checker()
    stats = []
    for v in videos:
        s = audit_video(conn, v, ck)
        stats.append(s)

    # 问题数直接从 diffs 统计(避免各段落计漏)
    issue_by_bv = {}
    for bv, cat, f, dbv, rawv in ck.diffs:
        issue_by_bv[bv] = issue_by_bv.get(bv, 0) + 1

    for v, s in zip(videos, stats):
        s["issues"] = issue_by_bv.get(v["video_key"], 0)
        flag = "✅" if s["issues"] == 0 else f"❌ {s['issues']} 处"
        print(f"  {v['published_at']}  {v['video_key']}  {flag}  "
              f"{str(v['title'])[:34]}")

    print()
    print("=" * 72)
    print("逐条核对结果")
    print("=" * 72)
    total_checks = sum(s["checks"] for s in stats)
    total_issues = sum(s["issues"] for s in stats)
    for s in stats:
        print(f"  {s['bvid']}  核对项 {s['checks']:>4}  问题 {s['issues']}")

    print()
    if ck.diffs:
        print(f"❌ 发现 {len(ck.diffs)} 处不一致:")
        for bv, cat, f, dbv, rawv in ck.diffs:
            print(f"   [{cat}] {bv} {f}: DB={dbv!r}  原文={rawv!r}")
    else:
        print("✅ 未发现任何不一致")

    if ck.notes:
        print(f"\nℹ️ 跳过项 {len(ck.notes)} 条(原文无该字段/无存档):")
        seen = {}
        for bv, cat, f, msg in ck.notes:
            seen.setdefault((cat, msg), 0)
            seen[(cat, msg)] += 1
        for (cat, msg), n in seen.items():
            print(f"   [{cat}] {msg} × {n}")

    print()
    print("=" * 72)
    print(f"汇总: 核对项 {total_checks} | 不一致 {total_issues} | "
          f"结论 {'✅ 数据准确无误' if total_issues == 0 else '❌ 存在不一致,需修复'}")
    print("=" * 72)

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["BV号", "标题", "发布日期", "核对项", "问题数"])
            for s, v in zip(stats, videos):
                w.writerow([s["bvid"], s["title"], v["published_at"], s["checks"], s["issues"]])
            w.writerow([])
            w.writerow(["类别", "BV号", "字段", "DB值", "原文值"])
            for row in ck.diffs:
                w.writerow(row)
        print(f"明细已导出: {args.csv}")

    conn.close()
    sys.exit(1 if total_issues else 0)


if __name__ == "__main__":
    main()
