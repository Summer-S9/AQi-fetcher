# -*- coding: utf-8 -*-
"""
v1.2.0 一次性迁移脚本 (sf_migrate_assets.py)
============================================
把 api_archives 表存量 body_json(原始接口响应全文)落盘为独立资产文件,
回填 asset_path 指针,校验一致后 body_json 置空,使 DB 瘦身。

背景: v1.2.0 起原始响应不再入库(22MB 大头),改为 data/assets/{YYYY_MM}_{platform}/ 文件。
      迁移前请确认已备份 DB(data/backup/)。

用法: python sf_migrate_assets.py
"""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sf_core import DB, write_asset, load_asset

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "sq_metrics.db"


def main():
    db = DB()  # 确保 schema 含 asset_path 列
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, platform, video_key, url, captured_at, body_json FROM api_archives"
        " WHERE body_json IS NOT NULL AND body_json != ''"
    ).fetchall()
    print(f"待迁移存档: {len(rows)} 条")

    ok, fail = 0, 0
    for r in rows:
        try:
            body = r["body_json"] if isinstance(r["body_json"], str) else json.dumps(r["body_json"], ensure_ascii=False)
            rel = write_asset(r["platform"], r["video_key"] or "unknown", r["url"], body, r["captured_at"])
            conn.execute("UPDATE api_archives SET asset_path=? WHERE id=?", (rel, r["id"]))
            ok += 1
        except Exception as e:  # noqa: BLE001
            fail += 1
            print(f"  ✗ id={r['id']} {r['platform']}/{r['video_key']}: {e}")
    conn.commit()
    print(f"落盘完成: {ok} 成功, {fail} 失败")

    # 校验:全部行都有 asset_path;抽查 20 条内容一致
    total = conn.execute("SELECT COUNT(*) FROM api_archives").fetchone()[0]
    with_path = conn.execute("SELECT COUNT(*) FROM api_archives WHERE asset_path IS NOT NULL").fetchone()[0]
    print(f"指针回填: {with_path}/{total}")
    assert with_path == total, "存在未回填指针的行,终止清空"

    check = conn.execute(
        "SELECT id, video_key, url, captured_at, body_json, asset_path FROM api_archives"
        " WHERE asset_path IS NOT NULL ORDER BY id LIMIT 20").fetchall()
    bad = 0
    for r in check:
        arc = load_asset(r["asset_path"])
        expect = json.loads(r["body_json"]) if r["body_json"] else None
        if expect is not None and arc != expect:
            bad += 1
            print(f"  ✗ 内容不一致 id={r['id']} {r['asset_path']}")
    print(f"抽查 20 条内容比对: {20 - bad}/20 一致")
    assert bad == 0, "抽查不一致,终止清空"

    # 清空 body_json,DB 瘦身
    conn.execute("UPDATE api_archives SET body_json=NULL WHERE body_json IS NOT NULL")
    conn.commit()
    remain = conn.execute("SELECT COUNT(*) FROM api_archives WHERE body_json IS NOT NULL").fetchone()[0]
    print(f"body_json 置空完成,剩余非空 {remain} 条")

    conn.close()
    import os
    print(f"DB 大小: {os.path.getsize(DB_PATH) / 1024 / 1024:.2f} MB")
    print("✅ 迁移完成。可运行 python xhs_audit.py 验证资产读取链路。")


if __name__ == "__main__":
    main()
