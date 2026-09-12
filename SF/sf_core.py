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

import hashlib
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
# 独立资产目录:原始接口响应全文按 {YYYY_MM}_{platform}/ 归档(v1.2.0 起)
ASSET_DIR = ROOT / "data" / "assets"

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
                -- v1.2.0:响应全文剥离为独立资产文件(data/assets/),本表只存元数据+资产路径指针
                CREATE TABLE IF NOT EXISTS api_archives (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform TEXT NOT NULL,
                    video_key TEXT,
                    url TEXT NOT NULL,
                    captured_at TEXT DEFAULT (datetime('now','localtime')),
                    body_json TEXT,                   -- 响应全文 JSON(存量已迁移后置空)
                    asset_path TEXT,                  -- 资产文件相对路径(data/assets/ 下)
                    UNIQUE(url, captured_at)
                );
                CREATE INDEX IF NOT EXISTS idx_snap_video ON metrics_snapshots(video_id);
                CREATE INDEX IF NOT EXISTS idx_daily_video ON daily_series(video_id);
                CREATE INDEX IF NOT EXISTS idx_mseries_video ON metric_series(video_id);
                CREATE INDEX IF NOT EXISTS idx_aud_video ON audience_snapshots(video_id);
                CREATE INDEX IF NOT EXISTS idx_api_video ON api_archives(video_key);
                """
            )
            # v1.2.0:旧库 api_archives 无 asset_path 列时补充(ALTER 不破坏存量)
            cols = [r["name"] for r in c.execute("PRAGMA table_info(api_archives)")]
            if "asset_path" not in cols:
                c.execute("ALTER TABLE api_archives ADD COLUMN asset_path TEXT")
            # 资产目录就绪
        os.makedirs(ASSET_DIR, exist_ok=True)

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
    def add_snapshot(self, video_id, snapshot_type, metrics, source="page",
                     captured_at=None):
        """metrics: dict of 标准字段(仅含非空值)。

        captured_at: 快照时点(YYYY-MM-DD HH:MM:SS)。缺省取当前时间。
        导入历史数据时必须显式指定「数据实际时点」,否则交付物 manifest 的
        data_through 会误报为导入时刻,破坏「复盘只用揭晓时点前快照」的审计前提。
        """
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
        if captured_at:
            cols.append("captured_at")
            vals.append(captured_at)
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
        """接口响应落盘为独立资产文件(data/assets/),本表只存元数据+路径指针。

        v1.2.0 起 body_json 不再入库:响应全文按抓取月份归档为
        assets/{YYYY_MM}_{platform}/raw_{stamp}_{video_key}.json。
        """
        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rel = write_asset(platform, video_key, url, body_json, captured_at)
        with self._conn() as c:
            c.execute(
                "INSERT INTO api_archives (platform, video_key, url, captured_at, asset_path) VALUES (?,?,?,?,?)",
                (platform, video_key, url, captured_at, rel),
            )
            return c.execute("SELECT last_insert_rowid()").fetchone()[0]

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


# ─── 独立资产(原始接口响应全文,按 年_月_平台 归档) ────────────────
def asset_rel_path(platform: str, captured_at: str, video_key: str) -> str:
    """资产相对路径: {YYYY_MM}_{platform}/raw_{YYYYMMDD_HHMMSS}_{video_key}.json

    资产按「抓取月份」归档(当时快照),与交付数据按「发布月份」区分开。
    """
    ym = captured_at[:7].replace("-", "_")          # 2026-09 → 2026_09
    stamp = captured_at[:19].replace("-", "").replace(":", "").replace(" ", "_")  # 20260905_103000
    return f"{ym}_{platform}/raw_{stamp}_{video_key}.json"


def write_asset(platform: str, video_key: str, url: str, body_json: str,
                captured_at: str = None) -> str:
    """把接口响应全文写入 assets/ 目录,返回相对路径。

    同一视频同一秒可能拦截多个接口(不同 URL),文件名冲突时追加
    URL 短 hash 后缀区分,保证不互相覆盖。
    """
    captured_at = captured_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rel = asset_rel_path(platform, captured_at, video_key)
    p = ASSET_DIR / rel
    if p.exists():
        h = hashlib.md5(url.encode("utf-8")).hexdigest()[:6]
        ym = captured_at[:7].replace("-", "_")
        stamp = captured_at[:19].replace("-", "").replace(":", "").replace(" ", "_")
        rel = f"{ym}_{platform}/raw_{stamp}_{video_key}_{h}.json"
        p = ASSET_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body_json if isinstance(body_json, str) else json.dumps(body_json, ensure_ascii=False),
                 encoding="utf-8")
    return rel


def load_asset(rel_path: str):
    """从资产文件读原始响应,返回 JSON 对象;缺失/损坏返回 None。"""
    if not rel_path:
        return None
    p = ASSET_DIR / rel_path
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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


def export_monthly(db: DB, year_month: str, platform: str, split_video: bool = False,
                   since: str = None, out_dir=None):
    """按月×平台导出交付物(v1.2.0 推荐出口,替代全量单文件推送)。

    规则:
    - 视频归属月 = published_at 前 7 位(year_month,如 "2026-09")
    - 快照去重:每视频只输出「最新一条 cumulative/T7」+「全部 deep」,剔除 raw_json
    - since:增量语义,仅过滤快照 captured_at >= since;趋势/画像始终给完整档案
    - 产出目录 {out_dir}/{YYYY_MM}_{platform}/:
        manifest.json / videos.json / snapshots.json / trends.json / audience.json
      split_video=True 时另生成 videos/{video_key}.json 单视频文件(agent 按需直读)
    """
    ym = year_month
    ym_dir = ym.replace("-", "_")  # 目录名用下划线: 2026-07 → 2026_07(与资产目录一致)
    if out_dir is None:
        out_dir = SNAPSHOT_DIR / f"{ym_dir}_{platform}"
    else:
        out_dir = Path(out_dir) / f"{ym_dir}_{platform}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with db._conn() as c:
        vrows = c.execute(
            "SELECT * FROM videos WHERE platform=? AND substr(published_at,1,7)=? ORDER BY published_at DESC",
            (platform, ym)).fetchall()
    videos = [dict(v) for v in vrows]

    snap_rows, trend_rows, aud_rows = [], [], []
    data_through = None
    per_video = {}  # video_key -> {"video":..,"snapshots":..,"trends":..,"audience":..}

    for v in vrows:
        vid = v["id"]
        with db._conn() as c:
            cum = c.execute(
                """SELECT * FROM metrics_snapshots
                   WHERE video_id=? AND snapshot_type IN ('cumulative','T7')
                   ORDER BY captured_at DESC LIMIT 1""", (vid,)).fetchone()
            deeps = c.execute(
                """SELECT * FROM metrics_snapshots
                   WHERE video_id=? AND snapshot_type='deep' ORDER BY captured_at""",
                (vid,)).fetchall()
            trends = c.execute(
                "SELECT * FROM metric_series WHERE video_id=? ORDER BY metric, stat_date",
                (vid,)).fetchall()
            auds = c.execute(
                "SELECT * FROM audience_snapshots WHERE video_id=? ORDER BY captured_at",
                (vid,)).fetchall()

        snaps = ([dict(cum)] if cum else []) + [dict(s) for s in deeps]
        for s in snaps:
            s.pop("raw_json", None)  # 原始全文留在 assets/,不进交付物
            if since and s.get("captured_at", "") < since:
                continue
            if s.get("captured_at") and (data_through is None or s["captured_at"] > data_through):
                data_through = s["captured_at"]
            snap_rows.append(s)
        t_rows = [dict(t) for t in trends]
        a_rows = [dict(a) for a in auds]
        for a in a_rows:  # JSON 字段解析为对象,交付友好
            for k in ("gender_json", "age_json", "region_json", "source_json", "interest_json", "extra_json"):
                if a.get(k):
                    try:
                        a[k] = json.loads(a[k])
                    except (ValueError, TypeError):
                        pass
        trend_rows += t_rows
        aud_rows += a_rows
        per_video[v["video_key"]] = {
            "video": dict(v),
            "snapshots": [s for s in snaps if not (since and s.get("captured_at", "") < since)],
            "trends": t_rows,
            "audience": a_rows,
        }

    manifest = {
        "schema": "sf-monthly-v1",
        "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "month": ym,
        "platform": platform,
        "since": since,
        "video_count": len(videos),
        "files": {
            "videos.json": len(videos),
            "snapshots.json": len(snap_rows),
            "trends.json": len(trend_rows),
            "audience.json": len(aud_rows),
        },
        "data_through": data_through,
        "videos": [v["video_key"] for v in videos],
    }

    def dump(obj, name):
        (out_dir / name).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")

    dump(videos, "videos.json")
    dump(snap_rows, "snapshots.json")
    dump(trend_rows, "trends.json")
    dump(aud_rows, "audience.json")
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if split_video:
        vdir = out_dir / "videos"
        vdir.mkdir(exist_ok=True)
        for key, pv in per_video.items():
            (vdir / f"{key}.json").write_text(
                json.dumps(pv, ensure_ascii=False), encoding="utf-8")

    return out_dir


if __name__ == "__main__":
    db = DB()
    print("DB path:", db.path)
    print("tables ok")
    print("is_t7_due 2026-08-01 →", is_t7_due("2026-08-01"))
    print("t7_date 2026-08-01 →", t7_date("2026-08-01"))
    print("normalize 抖音 →", normalize(
        {"作品名称": "测试", "发布时间": "2026-08-05 20:00:00", "播放量": "1234",
         "完播率": 0.0173, "封面点击率": "-"}, "douyin"))
