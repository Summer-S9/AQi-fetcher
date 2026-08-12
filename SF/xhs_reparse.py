# -*- coding: utf-8 -*-
"""
小红书深度数据重解析脚本 (xhs_reparse.py)
========================================
从 api_archives 已存的原始接口存档重新解析 metric_series,修复两个数据问题:
  1. hour 逐小时曲线被 UNIQUE(video_id, metric, stat_date) 覆盖(71条→4条)
     → 修复:stat_date 保留小时粒度 'YYYY-MM-DD HH:MM'
  2. trend_list 语义错误(date=发布后第N天, count=相对指数)
     → 修复:存 trend_heat_index (D+N) / trend_similar_index + summary 入画像

用法:
  python xhs_reparse.py [--video KEY]   # 全量或指定笔记
"""

import argparse
import datetime
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "sq_metrics.db"

# hour 列表 → metric 名(与抓取器一致)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", help="指定笔记ID(默认全量)")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 取视频列表
    if args.video:
        vids = conn.execute(
            "SELECT id, video_key, published_at FROM videos WHERE platform='xiaohongshu' AND video_key=?",
            (args.video,)).fetchall()
    else:
        vids = conn.execute(
            "SELECT id, video_key, published_at FROM videos WHERE platform='xiaohongshu'").fetchall()
    print(f"处理 {len(vids)} 篇笔记...")

    # 每篇的存档 URL 集合
    fixed_hour = 0
    fixed_trend = 0

    for v in vids:
        vid, vkey, pub = v["id"], v["video_key"], v["published_at"]
        # 1) 找该笔记最新的 note/base 存档(含 hour 曲线)
        rows = conn.execute(
            """SELECT body_json FROM api_archives
               WHERE platform='xiaohongshu' AND video_key=? AND url LIKE '%datacenter/note/base%'
               ORDER BY captured_at DESC LIMIT 1""", (vkey,)).fetchall()
        if rows:
            d = json.loads(rows[0]["body_json"])
            hour = (d.get("data") or {}).get("hour") or {}
            # 删除旧的 hour_* 数据后重写
            conn.execute("DELETE FROM metric_series WHERE video_id=? AND metric LIKE 'hour\\_%' ESCAPE '\\'", (vid,))
            for key, metric in HOUR_MAP.items():
                rows2 = hour.get(key) or []
                for r in rows2:
                    if r.get("date") is None:
                        continue
                    conn.execute(
                        """INSERT INTO metric_series (video_id, metric, stat_date, daily_value)
                           VALUES (?,?,?,?)""",
                        (vid, metric, ts_hour(r["date"]), r.get("count")))
                if rows2:
                    fixed_hour += len(rows2)

        # 2) 找该笔记最新的 audience/trend 存档
        rows = conn.execute(
            """SELECT body_json FROM api_archives
               WHERE platform='xiaohongshu' AND video_key=? AND url LIKE '%audience/trend%'
               ORDER BY captured_at DESC LIMIT 1""", (vkey,)).fetchall()
        if rows:
            d = json.loads(rows[0]["body_json"])
            data = d.get("data") or {}
            tl = data.get("trend_list") or []
            st = data.get("similar_trend_list") or []
            summary = data.get("summary")
            # 删除旧 trend 数据
            conn.execute(
                "DELETE FROM metric_series WHERE video_id=? AND metric IN ('trend_heat_index','trend_similar_index','daily_views')",
                (vid,))
            for r in tl:
                if r.get("date") is None:
                    continue
                conn.execute(
                    """INSERT INTO metric_series (video_id, metric, stat_date, daily_value)
                       VALUES (?,?,?,?)""",
                    (vid, "trend_heat_index", f"D+{r['date']}", r.get("count")))
            for r in st:
                if r.get("date") is None:
                    continue
                conn.execute(
                    """INSERT INTO metric_series (video_id, metric, stat_date, daily_value)
                       VALUES (?,?,?,?)""",
                    (vid, "trend_similar_index", f"D+{r['date']}", r.get("count")))
            if tl:
                fixed_trend += len(tl)
            # summary 存入画像 extra
            if summary:
                aud = conn.execute(
                    "SELECT id, extra_json FROM audience_snapshots WHERE video_id=? ORDER BY captured_at DESC LIMIT 1",
                    (vid,)).fetchone()
                if aud:
                    extra = json.loads(aud["extra_json"]) if aud["extra_json"] else {}
                    extra["trend_summary"] = summary
                    conn.execute("UPDATE audience_snapshots SET extra_json=? WHERE id=?",
                                 (json.dumps(extra, ensure_ascii=False), aud["id"]))

    conn.commit()
    print(f"✅ 重解析完成: 修复逐小时 {fixed_hour} 条 / 热度指数 {fixed_trend} 条")
    conn.close()


if __name__ == "__main__":
    main()
