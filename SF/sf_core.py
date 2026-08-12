# -*- coding: utf-8 -*-
"""
SQ 数据采集核心库 (sf_core)
==================================
职责:
  1. SQLite 数据仓库 (data/sq_metrics.db) 建表与读写
  2. 三平台异构字段 → 统一标准字段映射
  3. T+7 快照判定 (发布日 +7 天)
  4. 数据导出 xlsx / JSON

用法:
  from sf_core import DB, normalize, is_t7_due

作者: WorkBuddy / AQi-channel 项目
创建: 2026-08-11
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

# ─── 路径 ────────────────────────────────────────────────────────────
# SF v1.0 独立工作区: ~/AQi-fetcher/SF/*.py, ROOT = ~/AQi-fetcher
ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "sq_metrics.db"
EXPORT_DIR = ROOT / "data" / "fetcher"
SESSION_DIR = ROOT / "data" / "fetcher" / "sessions"
# 导出快照目录(带时间戳,供拷入 AQi-channel 使用)
SNAPSHOT_DIR = ROOT / "export"

# ─── 平台字段映射 ───────────────────────────────────────────────────
# 各平台后台导出/抓取到的原始字段 → 统一标准字段
# 标准字段集 (snapshot 表列):
#   platform, video_key(平台唯一ID), title, published_at,
#   views(播放/观看), likes, favorites(收藏), comments, shares,
#   coins(投币,B站), danmaku(弹幕,B站),
#   finish_rate(完播率), finish5_rate(5s完播率), cover_ctr(封面点击率),
#   bounce2_rate(2s跳出率), avg_duration(平均播放时长), followers_gain(涨粉),
#   tv_ratio(TV端占比), source(数据来源)
PLATFORM_FIELD_MAP = {
    "bilibili": {
        "title": "标题", "published_at": "发布时间", "views": "播放数",
        "likes": "点赞", "danmaku": "弹幕", "comments": "评论",
        "coins": "投币", "favorites": "收藏", "shares": "分享",
    },
    "douyin": {
        "title": "作品名称", "published_at": "发布时间", "views": "播放量",
        "finish_rate": "完播率", "finish5_rate": "5s完播率",
        "cover_ctr": "封面点击率", "bounce2_rate": "2s跳出率",
        "avg_duration": "平均播放时长", "likes": "点赞量", "shares": "分享量",
    },
    "xiaohongshu": {
        "title": "笔记标题", "published_at": "发布时间", "views": "观看量",
        "comments": "评论数", "likes": "点赞数", "favorites": "收藏数",
        "shares": "分享数",
    },
}

# 抖音完播率等字段是小数(0.0173 表示 1.73%),需要 ×100
PERCENT_AS_DECIMAL_FIELDS = {"finish_rate", "finish5_rate", "cover_ctr", "bounce2_rate"}


# ─── 数据库 ──────────────────────────────────────────────────────────
class DB:
    """SQLite 数据仓库封装。"""

    def __init__(self, path=None):
        self.path = str(path or DB_PATH)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._init_schema()

    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS videos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,          -- bilibili / douyin / xiaohongshu
                    video_key TEXT NOT NULL,         -- BV号 / 抖音作品ID / 笔记ID
                    title TEXT,
                    published_at TEXT,               -- ISO 时间
                    url TEXT,
                    series_tag TEXT,                 -- 系列标记(如 disney-3)
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    UNIQUE(platform, video_key)
                );
                CREATE TABLE IF NOT EXISTS metrics_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL,
                    snapshot_type TEXT NOT NULL,     -- T7 / cumulative / daily_curve
                    captured_at TEXT DEFAULT (datetime('now','localtime')),
                    views REAL, likes REAL, favorites REAL, comments REAL,
                    shares REAL, coins REAL, danmaku REAL,
                    finish_rate REAL, finish5_rate REAL, cover_ctr REAL,
                    bounce2_rate REAL, avg_duration REAL, followers_gain REAL,
                    tv_ratio REAL,
                    source TEXT,                     -- 数据来源: api / page / manual
                    raw_json TEXT,                   -- 原始抓取 JSON 存档
                    FOREIGN KEY(video_id) REFERENCES videos(id)
                );
                CREATE TABLE IF NOT EXISTS daily_series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL,
                    stat_date TEXT NOT NULL,          -- YYYY-MM-DD
                    daily_plays REAL,
                    cumulative_plays REAL,
                    UNIQUE(video_id, stat_date),
                    FOREIGN KEY(video_id) REFERENCES videos(id)
                );
                -- 通用趋势表:任意指标逐日(播放/点赞/评论/弹幕/投币/收藏/分享/涨粉…)
                CREATE TABLE IF NOT EXISTS metric_series (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL,
                    metric TEXT NOT NULL,             -- plays / likes / comments / coins / favorites / shares / danmaku / followers…
                    stat_date TEXT NOT NULL,          -- YYYY-MM-DD
                    daily_value REAL,                 -- 当日增量
                    cumulative_value REAL,            -- 当日累计
                    UNIQUE(video_id, metric, stat_date),
                    FOREIGN KEY(video_id) REFERENCES videos(id)
                );
                -- 画像快照表:观众画像/播放来源/互动构成(JSON,完整保留)
                CREATE TABLE IF NOT EXISTS audience_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id INTEGER NOT NULL,
                    captured_at TEXT DEFAULT (datetime('now','localtime')),
                    gender_json TEXT,                 -- 性别分布
                    age_json TEXT,                    -- 年龄分布
                    region_json TEXT,                 -- 地区分布
                    source_json TEXT,                 -- 播放来源
                    interest_json TEXT,               -- 兴趣标签
                    extra_json TEXT,                  -- 其他画像字段
                    FOREIGN KEY(video_id) REFERENCES videos(id)
                );
                -- 接口存档表:所有拦截到的 JSON 接口响应(全量原始数据,防遗漏)
                CREATE TABLE IF NOT EXISTS api_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    video_key TEXT,
                    url TEXT NOT NULL,
                    captured_at TEXT DEFAULT (datetime('now','localtime')),
                    body_json TEXT,                   -- 响应全文 JSON
                    UNIQUE(url, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_snap_video ON metrics_snapshots(video_id);
                CREATE INDEX IF NOT EXISTS idx_daily_video ON daily_series(video_id);
                CREATE INDEX IF NOT EXISTS idx_mseries_video ON metric_series(video_id);
                CREATE INDEX IF NOT EXISTS idx_aud_video ON audience_snapshots(video_id);
                CREATE INDEX IF NOT EXISTS idx_api_video ON api_archives(video_key);
                """
            )

    # ── videos ──
    def upsert_video(self, platform, video_key, title=None, published_at=None,
                     url=None, series_tag=None):
        with self._conn() as c:
            c.execute(
                """INSERT INTO videos (platform, video_key, title, published_at, url, series_tag)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(platform, video_key) DO UPDATE SET
                     title=excluded.title, published_at=excluded.published_at,
                     url=excluded.url, series_tag=excluded.series_tag""",
                (platform, video_key, title, published_at, url, series_tag),
            )
            row = c.execute(
                "SELECT id FROM videos WHERE platform=? AND video_key=?",
                (platform, video_key),
            ).fetchone()
            return row["id"]

    def get_video(self, platform=None, video_key=None, video_id=None):
        with self._conn() as c:
            if video_id:
                return c.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone()
            if platform and video_key:
                return c.execute(
                    "SELECT * FROM videos WHERE platform=? AND video_key=?",
                    (platform, video_key),
                ).fetchone()
            return None

    def list_videos(self, platform=None):
        with self._conn() as c:
            if platform:
                return c.execute(
                    "SELECT * FROM videos WHERE platform=? ORDER BY published_at DESC", (platform,)
                ).fetchall()
            return c.execute("SELECT * FROM videos ORDER BY published_at DESC").fetchall()

    # ── snapshots ──
    def add_snapshot(self, video_id, snapshot_type, metrics, source="page"):
        """metrics: dict of 标准字段(仅含非空值)"""
        keys = [
            "views", "likes", "favorites", "comments", "shares", "coins",
            "danmaku", "finish_rate", "finish5_rate", "cover_ctr",
            "bounce2_rate", "avg_duration", "followers_gain", "tv_ratio",
            "unfollow", "total_duration",
            "interact_rate", "interact_viewer_rate", "interact_fan_rate",
            "interact_star", "crash_rate", "crash_viewer_rate", "crash_fan_rate",
            "crash_star", "fan_play_rate", "viewer_play_rate",
            "homepage_visit", "fan_view_rate",
        ]
        cols, vals = ["video_id", "snapshot_type", "source"], [video_id, snapshot_type, source]
        for k in keys:
            if k in metrics and metrics[k] is not None:
                cols.append(k)
                vals.append(metrics[k])
        raw = {k: metrics.get(k) for k in metrics if k not in ("raw_json",)}
        with self._conn() as c:
            c.execute(
                f"INSERT INTO metrics_snapshots ({','.join(cols)}, raw_json) VALUES ({','.join('?'*len(cols))}, ?)",
                vals + [json.dumps(raw, ensure_ascii=False)],
            )
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]

    def get_snapshots(self, video_id, snapshot_type=None):
        with self._conn() as c:
            if snapshot_type:
                return c.execute(
                    "SELECT * FROM metrics_snapshots WHERE video_id=? AND snapshot_type=? ORDER BY captured_at",
                    (video_id, snapshot_type),
                ).fetchall()
            return c.execute(
                "SELECT * FROM metrics_snapshots WHERE video_id=? ORDER BY captured_at",
                (video_id,),
            ).fetchall()

    # ── daily_series ──
    def add_daily(self, video_id, stat_date, daily_plays, cumulative_plays=None):
        with self._conn() as c:
            c.execute(
                """INSERT INTO daily_series (video_id, stat_date, daily_plays, cumulative_plays)
                   VALUES (?,?,?,?)
                   ON CONFLICT(video_id, stat_date) DO UPDATE SET
                     daily_plays=excluded.daily_plays,
                     cumulative_plays=excluded.cumulative_plays""",
                (video_id, stat_date, daily_plays, cumulative_plays),
            )

    def get_daily(self, video_id):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM daily_series WHERE video_id=? ORDER BY stat_date", (video_id,)
            ).fetchall()

    # ── metric_series(通用趋势) ──
    def add_metric_series(self, video_id, metric, stat_date,
                          daily_value=None, cumulative_value=None):
        with self._conn() as c:
            c.execute(
                """INSERT INTO metric_series (video_id, metric, stat_date, daily_value, cumulative_value)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(video_id, metric, stat_date) DO UPDATE SET
                     daily_value=excluded.daily_value,
                     cumulative_value=excluded.cumulative_value""",
                (video_id, metric, stat_date, daily_value, cumulative_value),
            )

    def get_metric_series(self, video_id, metric=None):
        with self._conn() as c:
            if metric:
                return c.execute(
                    "SELECT * FROM metric_series WHERE video_id=? AND metric=? ORDER BY stat_date",
                    (video_id, metric),
                ).fetchall()
            return c.execute(
                "SELECT * FROM metric_series WHERE video_id=? ORDER BY metric, stat_date",
                (video_id,),
            ).fetchall()

    def bulk_add_metric_series(self, video_id, series: dict):
        """series: {metric: [(date, daily, cumulative), …]} 批量写入。"""
        for metric, rows in series.items():
            for date, daily, cumulative in rows:
                self.add_metric_series(video_id, metric, date, daily, cumulative)

    # ── audience_snapshots(画像) ──
    def add_audience(self, video_id, gender=None, age=None, region=None,
                     source=None, interest=None, extra=None):
        import json as _json
        with self._conn() as c:
            c.execute(
                """INSERT INTO audience_snapshots
                   (video_id, gender_json, age_json, region_json, source_json, interest_json, extra_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (video_id,
                 _json.dumps(gender, ensure_ascii=False) if gender else None,
                 _json.dumps(age, ensure_ascii=False) if age else None,
                 _json.dumps(region, ensure_ascii=False) if region else None,
                 _json.dumps(source, ensure_ascii=False) if source else None,
                 _json.dumps(interest, ensure_ascii=False) if interest else None,
                 _json.dumps(extra, ensure_ascii=False) if extra else None),
            )

    def get_audience(self, video_id):
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM audience_snapshots WHERE video_id=? ORDER BY captured_at",
                (video_id,),
            ).fetchall()

    # ── api_archives(接口全量存档) ──
    def add_api_archive(self, platform, video_key, url, body_json):
        with self._conn() as c:
            c.execute(
                "INSERT INTO api_archives (platform, video_key, url, body_json) VALUES (?,?,?,?)",
                (platform, video_key, url, body_json),
            )

    def list_api_archives(self, video_key=None, limit=100):
        with self._conn() as c:
            if video_key:
                return c.execute(
                    "SELECT * FROM api_archives WHERE video_key=? ORDER BY captured_at DESC LIMIT ?",
                    (video_key, limit),
                ).fetchall()
            return c.execute(
                "SELECT * FROM api_archives ORDER BY captured_at DESC LIMIT ?", (limit,)
            ).fetchall()


# ─── 工具函数 ────────────────────────────────────────────────────────
def normalize(raw: dict, platform: str) -> dict:
    """把平台原始字段 dict 映射为统一标准字段 dict,并做类型清洗。

    raw 的 key 可以是平台中文表头(来自 xlsx 导出)或英文接口字段。
    """
    fm = PLATFORM_FIELD_MAP.get(platform, {})
    # 同时支持中文表头映射 + 英文原名直通
    STRING_FIELDS = {"title", "published_at"}
    out = {}
    for std, src in fm.items():
        v = raw.get(src) if raw.get(src) is not None else raw.get(std)
        if v is None or v == "" or v == "-":
            continue
        out[std] = str(v).strip() if std in STRING_FIELDS else _clean(v, std, platform)
    # 英文直通字段(接口返回时的额外字段)
    for k in raw:
        if k in ("views", "likes", "favorites", "comments", "shares",
                 "coins", "danmaku", "finish_rate", "finish5_rate",
                 "cover_ctr", "bounce2_rate", "avg_duration",
                 "followers_gain", "tv_ratio") and k not in out:
            if raw[k] not in (None, "", "-"):
                out[k] = _clean(raw[k], k, platform)
    return out


def _clean(v, field, platform):
    """类型与单位清洗。"""
    if isinstance(v, str):
        v = v.replace(",", "").replace("万", "0000").strip()
        # 处理"1.2万"这类
        if "万" in v:
            v = v.replace("万", "")
            try:
                return float(v) * 10000
            except ValueError:
                return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # 抖音完播率等小数 → 百分数 (0.0173 → 1.73)
    if field in PERCENT_AS_DECIMAL_FIELDS and platform == "douyin" and f <= 1:
        return round(f * 100, 2)
    if field in PERCENT_AS_DECIMAL_FIELDS:
        return round(f, 2)
    return f


def is_t7_due(published_at: str, now=None) -> bool:
    """发布是否已满 7 天(含发布日)。用于判定是否需要抓 T+7 快照。"""
    if not published_at:
        return False
    try:
        dt = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    # 转本地时区(简化:直接按 ISO 字符串前 10 位算日期)
    try:
        pub_date = datetime.strptime(str(published_at)[:10], "%Y-%m-%d")
    except ValueError:
        return False
    now = now or datetime.now()
    return (now.date() - pub_date.date()).days >= 7


def t7_date(published_at: str) -> str:
    """返回 T+7 日期 (发布日 + 7 天)。"""
    pub_date = datetime.strptime(str(published_at)[:10], "%Y-%m-%d")
    return (pub_date + timedelta(days=7)).strftime("%Y-%m-%d")


def export_xlsx(db: DB, out_path=None):
    """把数据库全量导出为 xlsx(三平台各一张 sheet)。

    默认输出到 ~/AQi-fetcher/export/,文件名带时间戳(审计痕迹),
    供人工/定时拷入 AQi-channel 使用。
    """
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    cols = [
        "平台", "稿件ID", "标题", "发布时间", "URL", "系列",
        "快照类型", "抓取时间", "播放/观看", "点赞", "收藏", "评论",
        "分享", "投币", "弹幕", "完播率%", "5s完播%", "封面点击%",
        "2s跳出%", "平均时长", "涨粉", "TV占比%",
    ]
    for platform in ("bilibili", "douyin", "xiaohongshu"):
        ws = wb.create_sheet(platform)
        ws.append(cols)
        for v in db.list_videos(platform):
            for s in db.get_snapshots(v["id"]):
                ws.append([
                    platform, v["video_key"], v["title"], v["published_at"],
                    v["url"], v["series_tag"], s["snapshot_type"],
                    s["captured_at"], s["views"], s["likes"], s["favorites"],
                    s["comments"], s["shares"], s["coins"], s["danmaku"],
                    s["finish_rate"], s["finish5_rate"], s["cover_ctr"],
                    s["bounce2_rate"], s["avg_duration"], s["followers_gain"],
                    s["tv_ratio"],
                ])
    if out_path is None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = SNAPSHOT_DIR / f"sq_metrics_{stamp}.xlsx"
    wb.save(out_path)
    return out_path


def export_json(db: DB, out_path=None):
    data = []
    for v in db.list_videos():
        data.append({
            "video": dict(v),
            "snapshots": [dict(s) for s in db.get_snapshots(v["id"])],
            "daily": [dict(d) for d in db.get_daily(v["id"])],
        })
    if out_path is None:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = SNAPSHOT_DIR / f"sq_metrics_{stamp}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


if __name__ == "__main__":
    db = DB()
    print("DB path:", db.path)
    print("tables ok")
    print("is_t7_due 2026-08-01 →", is_t7_due("2026-08-01"))
    print("t7_date 2026-08-01 →", t7_date("2026-08-01"))
    print("normalize 抖音 →", normalize(
        {"作品名称": "测试", "发布时间": "2026-08-05 20:00:00", "播放量": "1234",
         "完播率": 0.0173, "封面点击率": "-"}, "douyin"))
