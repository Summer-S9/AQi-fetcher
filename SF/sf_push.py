# -*- coding: utf-8 -*-
"""
SF v1.0 推送脚本 (sf_push.py)
==============================
从独立工作区导出快照并拷入 AQi-channel/data/fetcher/snapshots/,
供盲测/复盘报告引用。主项目只接触快照,不接触实时 DB。

用法:
  python sf_push.py            # 导出 JSON+XLSX 并推送
  python sf_push.py --json     # 仅 JSON
  python sf_push.py --xlsx     # 仅 XLSX
  python sf_push.py --to DIR   # 推送到指定目录
"""

import argparse
import shutil
import sys
from pathlib import Path

from sf_core import DB, export_json, export_xlsx

# 主项目快照目录(默认)
DEFAULT_TARGET = Path.home() / "AQi-channel" / "data" / "fetcher" / "snapshots"


def main():
    ap = argparse.ArgumentParser(description="SF v1.0 快照推送")
    ap.add_argument("--json", action="store_true", help="仅 JSON")
    ap.add_argument("--xlsx", action="store_true", help="仅 XLSX")
    ap.add_argument("--to", default=str(DEFAULT_TARGET), help="目标目录")
    args = ap.parse_args()

    db = DB()
    target = Path(args.to)
    target.mkdir(parents=True, exist_ok=True)

    produced = []
    if not args.xlsx:  # 默认导出 JSON
        p = export_json(db)
        produced.append(p)
    if not args.json:  # 默认导出 XLSX
        p = export_xlsx(db)
        produced.append(p)

    for p in produced:
        dst = target / p.name
        shutil.copy2(p, dst)
        print(f"✅ {p.name} → {dst}")

    print(f"\n已推送 {len(produced)} 个快照到 {target}")
    print("主项目盲测/复盘仅使用快照文件,实时 DB 仍在独立工作区。")


if __name__ == "__main__":
    main()
