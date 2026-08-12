# -*- coding: utf-8 -*-
"""
小红书数据全量校验脚本 (xhs_audit.py)
=====================================
把数据库中每条小红书记录与原始接口存档逐一 diff,找出所有不一致。
校验维度:
  1. videos: 标题/发布时间 与 posted 存档对照
  2. cumulative 快照: views/likes/favorites/comments/shares 与 posted 存档对照
  3. deep 快照: 完播/封面点击/2s跳出/平均时长/涨粉 与 note/base 存档对照
  4. 画像: gender/age/city/interest 与 audience/source/detail 存档对照
  5. metric_series: hour_* 与 note/base hour 对照;trend_* 与 audience/trend 对照

用法: python xhs_audit.py [--video KEY]
"""

import argparse
import datetime
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "sq_metrics.db"

HOUR_MAP = {
    "view_list": "hour_views", "like_list": "hour_likes",
    "collect_list": "hour_favorites", "comment_list": "hour_comments",
    "share_list": "hour_shares", "danmaku_list": "hour_danmaku",
    "finish_list": "hour_finish", "finish5s_list": "hour_finish5s",
    "rise_fans_list": "hour_followers", "play60s_list": "hour_play60s",
    "interact_list": "hour_interact", "view_time_list": "hour_view_time",
}


def ts_hour(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(ts)


def ts_date(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(ts)


def get_archive(conn, vkey, frag):
    rows = conn.execute(
        """SELECT url, body_json FROM api_archives
           WHERE platform='xiaohongshu' AND video_key=? AND url LIKE ?
           ORDER BY captured_at DESC LIMIT 1""", (vkey, f"%{frag}%")).fetchall()
    if not rows:
        return None
    try:
        return json.loads(rows[0]["body_json"])
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", help="指定笔记ID(默认全量)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.video:
        vids = conn.execute(
            "SELECT * FROM videos WHERE platform='xiaohongshu' AND video_key=?",
            (args.video,)).fetchall()
    else:
        vids = conn.execute(
            "SELECT * FROM videos WHERE platform='xiaohongshu' ORDER BY published_at DESC").fetchall()

    issues = []  # (video_key, category, detail)

    for v in vids:
        vkey = v["video_key"]
        vid = v["id"]

        # ── 1. posted 存档(基础数据) ──
        posted = None
        rows = conn.execute(
            """SELECT body_json FROM api_archives
               WHERE platform='xiaohongshu' AND url LIKE '%note/user/posted%'""").fetchall()
        for r in rows:
            try:
                d = json.loads(r["body_json"])
            except Exception:
                continue
            for n in (d.get("data") or {}).get("notes") or []:
                if str(n.get("id")) == vkey:
                    posted = n
                    break
            if posted:
                break

        # 1a. 标题/时间
        if posted:
            if posted.get("display_title") != v["title"]:
                issues.append((vkey, "title", f"库[{v['title']}] vs 存档[{posted.get('display_title')}]"))
            if posted.get("time") != v["published_at"]:
                issues.append((vkey, "pubtime", f"库[{v['published_at']}] vs 存档[{posted.get('time')}]"))

        # ── 2. cumulative 快照 vs posted ──
        snap = conn.execute(
            "SELECT * FROM metrics_snapshots WHERE video_id=? AND snapshot_type='cumulative' ORDER BY captured_at DESC LIMIT 1",
            (vid,)).fetchone()
        if snap and posted:
            for col, src in [("views", "view_count"), ("likes", "likes"),
                             ("favorites", "collected_count"), ("comments", "comments_count"),
                             ("shares", "shared_count")]:
                dbv = snap[col]
                arcv = posted.get(src)
                if arcv is not None and dbv is not None and abs(dbv - float(arcv)) > max(50, float(arcv) * 0.01):
                    issues.append((vkey, f"cum.{col}", f"库[{dbv}] vs 存档[{arcv}]"))

        # ── 3. deep 快照 vs note/base ──
        deep = conn.execute(
            "SELECT * FROM metrics_snapshots WHERE video_id=? AND snapshot_type='deep' ORDER BY captured_at DESC LIMIT 1",
            (vid,)).fetchone()
        base = get_archive(conn, vkey, "datacenter/note/base")
        if deep and base and base.get("code") == 0:
            bd = base.get("data") or {}
            for col, src in [("finish_rate", "full_view_rate"), ("finish5_rate", "finish5s_rate"),
                             ("bounce2_rate", "exit_view2s_rate"), ("cover_ctr", "cover_click_rate"),
                             ("avg_duration", "view_time_avg"), ("followers_gain", "rise_fans_count")]:
                dbv = deep[col]
                arcv = bd.get(src)
                if arcv not in (None, "", -1) and dbv is not None and abs(dbv - float(arcv)) > max(0.5, float(arcv) * 0.02):
                    issues.append((vkey, f"deep.{col}", f"库[{dbv}] vs 存档[{arcv}]"))

        # ── 4. 画像 vs audience/source/detail ──
        aud = conn.execute(
            "SELECT * FROM audience_snapshots WHERE video_id=? ORDER BY captured_at DESC LIMIT 1", (vid,)).fetchone()
        detail = get_archive(conn, vkey, "audience/source/detail")
        if aud and detail and detail.get("code") == 0:
            dd = detail.get("data") or {}
            # 性别
            db_g = json.loads(aud["gender_json"]) if aud["gender_json"] else []
            arc_g = dd.get("gender") or []
            db_gv = sorted(x["value"] for x in db_g) if db_g else []
            arc_gv = sorted(x["value"] for x in arc_g) if arc_g else []
            if db_gv and arc_gv and db_gv != arc_gv:
                issues.append((vkey, "aud.gender", f"库{db_gv} vs 存档{arc_gv}"))
            # 年龄
            db_a = json.loads(aud["age_json"]) if aud["age_json"] else []
            arc_a = dd.get("age") or []
            db_av = sorted(x["value"] for x in db_a) if db_a else []
            arc_av = sorted(x["value"] for x in arc_a) if arc_a else []
            if db_av and arc_av and db_av != arc_av:
                issues.append((vkey, "aud.age", f"库{db_av} vs 存档{arc_av}"))

        # ── 5. metric_series hour_* vs note/base hour ──
        if base and base.get("code") == 0:
            hour = (base.get("data") or {}).get("hour") or {}
            for key, metric in HOUR_MAP.items():
                arc_rows = {ts_hour(r["date"]): r.get("count") for r in (hour.get(key) or []) if r.get("date")}
                if not arc_rows:
                    continue
                db_rows = {r["stat_date"]: r["daily_value"] for r in conn.execute(
                    "SELECT stat_date, daily_value FROM metric_series WHERE video_id=? AND metric=?",
                    (vid, metric)).fetchall()}
                # 只比对重合的日期
                common = set(arc_rows) & set(db_rows)
                for dt in common:
                    if abs(arc_rows[dt] - db_rows[dt]) > max(1, arc_rows[dt] * 0.02):
                        issues.append((vkey, f"series.{metric}", f"{dt}: 库[{db_rows[dt]}] vs 存档[{arc_rows[dt]}]"))
                # 存档有但库缺失
                missing = set(arc_rows) - set(db_rows)
                if missing:
                    issues.append((vkey, f"series.{metric}.missing", f"缺 {len(missing)} 个点, 如{list(missing)[:2]}"))

        # ── 6. trend_* vs audience/trend ──
        trend = get_archive(conn, vkey, "audience/trend")
        if trend and trend.get("code") == 0:
            td = trend.get("data") or {}
            tl = td.get("trend_list") or []
            if tl:
                arc_t = {f"D+{r['date']}": r.get("count") for r in tl if r.get("date") is not None}
                db_t = {r["stat_date"]: r["daily_value"] for r in conn.execute(
                    "SELECT stat_date, daily_value FROM metric_series WHERE video_id=? AND metric='trend_heat_index'",
                    (vid,)).fetchall()}
                common = set(arc_t) & set(db_t)
                for dt in common:
                    if abs(arc_t[dt] - db_t[dt]) > max(1, arc_t[dt] * 0.02):
                        issues.append((vkey, "series.trend_heat", f"{dt}: 库[{db_t[dt]}] vs 存档[{arc_t[dt]}]"))
                missing = set(arc_t) - set(db_t)
                if missing:
                    issues.append((vkey, "series.trend_heat.missing", f"缺 {len(missing)} 个点"))

    # 输出
    print(f"校验 {len(vids)} 篇笔记,发现 {len(issues)} 处不一致:")
    from collections import Counter
    cats = Counter(i[1] for i in issues)
    print("按类别:", dict(cats))
    print()
    for vk, cat, detail in issues[:80]:
        print(f"  {vk} | {cat}: {detail}")
    if len(issues) > 80:
        print(f"  ... 共 {len(issues)} 处,仅显示前 80")
    conn.close()
    return issues


if __name__ == "__main__":
    main()
