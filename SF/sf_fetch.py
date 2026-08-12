# -*- coding: utf-8 -*-
"""
SQ 数据采集 CLI 入口 (sq_fetch.py)
==================================
统一入口:
  python sq_fetch.py login <bilibili|douyin|xiaohongshu>   # 登录并保存会话
  python sq_fetch.py fetch <platform> --url <链接>          # 抓取指定稿件
  python sq_fetch.py fetch <platform> --bv <BV号>           # 按 ID 抓取
  python sq_fetch.py status                                  # 查看库内稿件与快照
  python sq_fetch.py export [--xlsx|--json]                  # 导出数据
"""

import argparse
import sys

from sf_core import DB, export_json, export_xlsx


def cmd_status(args):
    db = DB()
    videos = db.list_videos()
    if not videos:
        print("库内暂无稿件。先运行 fetch 抓取。")
        return
    print(f"{'平台':<12} {'稿件ID':<16} {'标题':<30} {'快照数':<6} {'逐日天数':<8}")
    for v in videos:
        snaps = db.get_snapshots(v["id"])
        daily = db.get_daily(v["id"])
        print(f"{v['platform']:<12} {v['video_key']:<16} {(v['title'] or '')[:28]:<30} {len(snaps):<6} {len(daily):<8}")
    print(f"\n共 {len(videos)} 条稿件。")


def cmd_export(args):
    db = DB()
    if args.xlsx:
        p = export_xlsx(db)
    elif args.json:
        p = export_json(db)
    else:
        p = export_xlsx(db)
        p2 = export_json(db)
        print(f"已导出: {p}\n        {p2}")
        return
    print(f"已导出: {p}")


def cmd_fetch(args):
    platform = args.platform
    if not args.url and not args.bv:
        print("需要 --url 或 --bv 指定稿件。")
        sys.exit(1)
    target = args.url or args.bv

    if platform == "bilibili":
        from sf_bilibili import fetch_video
        fetch_video(target, headless=not args.show)
    elif platform == "douyin":
        from sf_douyin import fetch_videos
        # 抖音链接/ID:提取数字 ID
        import re
        m = re.search(r"(\d{15,20})", target)
        fetch_videos(target_id=m.group(1) if m else target, headless=not args.show)
    elif platform == "xiaohongshu":
        from sf_xiaohongshu import fetch_notes, _extract_note_id
        # 从链接/ID 提取 24 位 hex 笔记 ID
        import re as _re
        m = _re.search(r"[0-9a-f]{24}", target)
        fetch_notes(target_id=m.group(0) if m else None, headless=not args.show)
    else:
        print(f"未知平台: {platform}")
        sys.exit(1)


def cmd_login(args):
    from sf_login import login
    login(args.platform, headless=False)


def main():
    ap = argparse.ArgumentParser(description="SQ 数据采集 CLI")
    sub = ap.add_subparsers(dest="cmd")

    p_login = sub.add_parser("login", help="登录平台创作后台")
    p_login.add_argument("platform", choices=["bilibili", "douyin", "xiaohongshu"])

    p_fetch = sub.add_parser("fetch", help="抓取指定稿件数据")
    p_fetch.add_argument("platform", choices=["bilibili", "douyin", "xiaohongshu"])
    p_fetch.add_argument("--url", help="稿件链接")
    p_fetch.add_argument("--bv", help="BV号(仅B站)")
    p_fetch.add_argument("--show", action="store_true", help="有头模式(调试)")

    sub.add_parser("status", help="查看库内稿件")
    p_exp = sub.add_parser("export", help="导出数据")
    p_exp.add_argument("--xlsx", action="store_true")
    p_exp.add_argument("--json", action="store_true")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(0)

    if args.cmd == "login":
        cmd_login(args)
    elif args.cmd == "fetch":
        cmd_fetch(args)
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "export":
        cmd_export(args)


if __name__ == "__main__":
    main()
