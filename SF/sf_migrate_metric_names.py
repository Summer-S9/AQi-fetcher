# -*- coding: utf-8 -*-
"""
一次性迁移:统一 B站趋势指标命名 (sf_migrate_metric_names.py)
========================================
背景
----
v1.0 时期的 B站抓取器把逐日趋势写成 `daily_plays` / `daily_followers`,
后来统一为标准命名 `plays` / `followers`(见 sf_bilibili.TREND_TYPES)。
结果:`BV1rnaJzpEFv`(2026-08-11 抓取,唯一一条 v1.0 时期的数据)的
播放/转粉趋势挂在旧名下,导致:

  1. 同一份交付物里出现两套命名(其余 5 条稿件都用 `plays`/`followers`);
  2. 任何按 `plays` 查询的消费方(主项目/报告/审计)读不到这条稿件的数据。

本脚本把旧命名**就地改名**为统一命名。改名而非删除,是为了保住当初
抓到的那段日期区间数据(趋势接口只给"近 30 天",无法事后补抓历史窗口)。

同时报告 `retention_20s` 这类"只有一条稿件有、现行代码已不再产出"的
遗留指标(仅报告,不自动删除)。

用法
----
  python sf_migrate_metric_names.py --dry-run   # 只看会改什么
  python sf_migrate_metric_names.py             # 执行改名
"""

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "sq_metrics.db"
BACKUP_DIR = ROOT / "data" / "backup"

# 旧命名 → 标准命名
RENAME_MAP = {
    "daily_plays": "plays",
    "daily_followers": "followers",
}

# 只报告、不处理的遗留指标(现行代码不再产出)
LEGACY_REPORT_ONLY = ("retention_20s",)


def main():
    ap = argparse.ArgumentParser(description="统一 B站趋势指标命名")
    ap.add_argument("--dry-run", action="store_true", help="只预览,不写库")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份(不建议)")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"❌ 数据库不存在: {DB_PATH}")
        sys.exit(1)

    if not args.dry_run and not args.no_backup:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dst = BACKUP_DIR / "sq_metrics_pre_migrate_metric_names.db"
        shutil.copy2(DB_PATH, dst)
        print(f"✅ 已备份: {dst}")

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    print("=" * 68)
    print(f"数据库: {DB_PATH}")
    print("=" * 68)

    # ── 1. 预览受影响范围 ──
    print("\n[1] 需改名的旧指标:")
    total_renamed = 0
    plans = []
    for old, new in RENAME_MAP.items():
        rows = conn.execute(
            """SELECT s.video_id, v.video_key, COUNT(*) n, MIN(s.stat_date) d1, MAX(s.stat_date) d2
               FROM metric_series s JOIN videos v ON s.video_id = v.id
               WHERE s.metric = ? GROUP BY s.video_id ORDER BY v.video_key""",
            (old,)).fetchall()
        if not rows:
            print(f"  {old}: 无数据,跳过")
            continue
        for r in rows:
            # 检查改名后是否与已有标准名冲突(同 video+date)
            conflicts = conn.execute(
                """SELECT COUNT(*) FROM metric_series a
                   WHERE a.video_id=? AND a.metric=?
                     AND EXISTS (SELECT 1 FROM metric_series b
                                 WHERE b.video_id=a.video_id AND b.metric=?
                                   AND b.stat_date=a.stat_date)""",
                (r["video_id"], old, new)).fetchone()[0]
            print(f"  {r['video_key']}  {old} → {new}  "
                  f"{r['n']} 行 ({r['d1']} ~ {r['d2']})  冲突 {conflicts} 行")
            plans.append((old, new, r["video_id"], r["n"], conflicts))
            total_renamed += r["n"]

    # ── 2. 执行改名 ──
    print("\n[2] 执行改名:")
    if not plans:
        print("  无需改动")
    for old, new, vid, n, conflicts in plans:
        if conflicts:
            # 有冲突的行直接删(标准名已有同日期数据,以新抓的为准)
            conn.execute(
                """DELETE FROM metric_series
                   WHERE video_id=? AND metric=?
                     AND stat_date IN (SELECT stat_date FROM metric_series
                                       WHERE video_id=? AND metric=?)""",
                (vid, old, vid, new))
            print(f"  video_id={vid} {old}: 先删除 {conflicts} 行与 {new} 冲突的旧数据")
        conn.execute("UPDATE metric_series SET metric=? WHERE video_id=? AND metric=?",
                     (new, vid, old))
        print(f"  video_id={vid}  {old} → {new}  已改 {n} 行")
    if not args.dry_run:
        conn.commit()

    # ── 3. 遗留指标报告 ──
    print("\n[3] 遗留指标(仅报告,现行代码不再产出):")
    for m in LEGACY_REPORT_ONLY:
        rows = conn.execute(
            """SELECT v.video_key, COUNT(*) n FROM metric_series s
               JOIN videos v ON s.video_id=v.id WHERE s.metric=?
               GROUP BY s.video_id""", (m,)).fetchall()
        if rows:
            for r in rows:
                print(f"  {m}: {r['video_key']}  {r['n']} 行")
        else:
            print(f"  {m}: 无数据")

    # ── 4. 结果核对:旧名应已清空 ──
    print("\n[4] 核对:")
    for old, new in RENAME_MAP.items():
        left_old = conn.execute("SELECT COUNT(*) FROM metric_series WHERE metric=?",
                                (old,)).fetchone()[0]
        n_new = conn.execute("SELECT COUNT(*) FROM metric_series WHERE metric=?",
                             (new,)).fetchone()[0]
        flag = "✅" if left_old == 0 else "❌"
        print(f"  {flag} {old} 剩余 {left_old} 行 | {new} 现有 {n_new} 行")

    conn.close()
    print()
    if args.dry_run:
        print("[dry-run] 未写入数据库。")
    else:
        print(f"迁移完成: 改名 {total_renamed} 行")


if __name__ == "__main__":
    main()
