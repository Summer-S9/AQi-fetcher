# -*- coding: utf-8 -*-
"""
SF v1.2.0 推送脚本 (sf_push.py)
===============================
按「年_月×平台」分目录导出交付物并拷入 AQi-channel/data/fetcher/snapshots/,
主项目按需只取目标月份,不再全量搬运。

用法:
  python sf_push.py --month 2026-09 --platform bilibili            # 交付指定月×平台
  python sf_push.py --month 2026-09                                 # 整个月(三平台)
  python sf_push.py --month 2026-09 --platform bilibili --split-video  # 单视频文件
  python sf_push.py --month 2026-09 --since 2026-10-01              # 增量:只含其后新快照
  python sf_push.py --all                                           # 所有有数据的月份×平台
  python sf_push.py --to DIR                                        # 指定目标目录

交付目录结构(与 export/ 下一致):
  {YYYY_MM}_{platform}/manifest.json  videos.json  snapshots.json  trends.json  audience.json
  (--split-video 时另含 videos/{video_key}.json)

注:全量单文件导出(本地归档)走 sf_fetch.py export,本脚本不再生成单文件。
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from sf_core import DB, export_monthly

# 主项目快照目录(默认)
DEFAULT_TARGET = Path.home() / "AQi-channel" / "data" / "fetcher" / "snapshots"
PLATFORMS = ("bilibili", "douyin", "xiaohongshu")


def list_month_platforms(db):
    """返回有数据的 (YYYY-MM, platform) 组合,按月份倒序。"""
    with db._conn() as c:
        rows = c.execute(
            "SELECT DISTINCT substr(published_at,1,7) ym, platform FROM videos"
            " WHERE published_at IS NOT NULL AND published_at != ''"
            " ORDER BY ym DESC, platform").fetchall()
    return [(r["ym"], r["platform"]) for r in rows]


def has_videos(db, ym, pf):
    with db._conn() as c:
        n = c.execute(
            "SELECT COUNT(*) FROM videos WHERE platform=? AND substr(published_at,1,7)=?",
            (pf, ym)).fetchone()[0]
    return n > 0


def main():
    ap = argparse.ArgumentParser(description="SF v1.2.0 按月×平台快照推送")
    ap.add_argument("--month", help="交付月份 YYYY-MM(如 2026-09),与 --all 二选一")
    ap.add_argument("--all", action="store_true", help="全量交付所有有数据的月份×平台")
    ap.add_argument("--platform", choices=PLATFORMS, help="平台(缺省时 --month 推三平台)")
    ap.add_argument("--split-video", action="store_true", help="快照按 video_key 拆单文件")
    ap.add_argument("--since", help="增量:快照仅含 captured_at >= 该值(如 2026-10-01)")
    ap.add_argument("--to", default=str(DEFAULT_TARGET), help="目标目录")
    args = ap.parse_args()

    if args.all and args.month:
        print("❌ --all 与 --month 互斥,只能选一个。")
        sys.exit(1)
    if not args.all and not args.month:
        print("需要 --month YYYY-MM 或 --all。示例: python sf_push.py --month 2026-09 --platform bilibili")
        sys.exit(1)

    db = DB()
    target = Path(args.to)

    # 确定 (year_month, platform) 组合
    if args.all:
        pairs = list_month_platforms(db)
    else:
        ym = args.month
        pairs = [(ym, p) for p in (PLATFORMS if not args.platform else (args.platform,))]
        pairs = [(m, p) for m, p in pairs if has_videos(db, m, p)]

    if not pairs:
        print("该月份×平台下没有视频数据,无需推送。")
        sys.exit(0)

    pushed = 0
    for ym, pf in pairs:
        out = export_monthly(db, ym, pf, split_video=args.split_video, since=args.since)
        dst = target / out.name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(out, dst)
        # 打印 manifest 摘要
        import json
        man = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        print(f"✅ {out.name}/ → {dst}  "
              f"(视频 {man['video_count']} · 快照 {man['files']['snapshots.json']} · "
              f"趋势 {man['files']['trends.json']} · 截止 {man['data_through']})")
        pushed += 1

    print(f"\n已推送 {pushed} 个交付目录到 {target}")
    print("主项目盲测/复盘仅使用快照,实时 DB 与原始资产仍在独立工作区。")


if __name__ == "__main__":
    main()
