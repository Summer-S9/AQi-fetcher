# -*- coding: utf-8 -*-
"""
SQ 小红书数据抓取器 (sf_xiaohongshu.py)
==============================================
用 Playwright 复用已保存登录态,从小红书创作服务平台抓取笔记**全维度数据**。

已验证接口(小红书创作服务平台 creator.xiaohongshu.com,2026-08-11):
  - /api/galaxy/v2/creator/note/user/posted?tab=0&page=N   笔记列表(滚动翻页,每页约11条)
      notes[].字段: id / display_title / time(发布时间) / type(video|image) /
        view_count 观看 / likes 点赞 / comments_count 评论 /
        collected_count 收藏 / shared_count 分享 / video_info.duration 视频时长 /
        images_list 封面 / sticky 置顶 / tab_status
  - /api/galaxy/creator/data/note_detail_new              单笔记数据总览(近7天/30天)
      data.seven / data.thirty 各含 9 项指标(view/like/collect/comment/danmaku/
      share/rise_fans/home_view/view_time_avg) + 逐日明细列表(view_list 等)
  - /api/galaxy/creator/datacenter/note/base?note_id=     单笔记基础数据(需页面签名)

注意:
  - 小红书页面请求带 x-s/x-t 签名(JS 闭包内生成),直接 fetch 会返回 -1,
    因此本抓取器采用「拦截页面自身请求」的方式(与 B站/抖音同一套路)。
  - 列表接口已覆盖用户手动表全部字段(观看/点赞/收藏/评论/分享/类型/时长)。
  - 单笔记详情接口 note_detail_new 页面加载时自动请求(默认最新笔记),
    指定笔记需在页面点击进入详情页(见 fetch_note_detail 增强,后续补充)。

用法:
  python sf_xiaohongshu.py --all --pages 20   # 全量抓取笔记列表(滚动翻页)
  python sf_xiaohongshu.py --id <笔记ID>       # 抓指定笔记(列表匹配)
  python sf_xiaohongshu.py --url <链接>        # 链接提取 ID
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from sf_core import DB, SESSION_DIR

MANAGE_URL = "https://creator.xiaohongshu.com/new/note-manager"
POSTED_PATH = "/api/galaxy/v2/creator/note/user/posted"
DETAIL_NEW_PATH = "/api/galaxy/creator/data/note_detail_new"
NOTE_BASE_PATH = "/api/galaxy/creator/datacenter/note/base"

# 需要全量存档的接口
ARCHIVE_ENDPOINTS = [
    "note/user/posted",
    "note_detail_new",
    "note/base",
    "note/analyze/audience",
    "note/audience/source",
]

# 笔记列表字段 → SQ 标准字段
XHS_NOTE_MAP = {
    "view_count": "views",
    "likes": "likes",
    "comments_count": "comments",
    "collected_count": "favorites",
    "shared_count": "shares",
}

# note/base 深度指标 → SQ 标准字段(rate 已是百分数,直接入库)
XHS_BASE_RATE_MAP = {
    "full_view_rate": "finish_rate",          # 完播率 %
    "finish5s_rate": "finish5_rate",          # 5s完播 %
    "exit_view2s_rate": "bounce2_rate",       # 2s跳出 %
    "cover_click_rate": "cover_ctr",          # 封面点击率 %
    "view_time_avg": "avg_duration",          # 平均观看时长(秒)
    "interaction_rate": "interact_rate",      # 互动率 %
    "impl_count": "exposure",                 # 曝光量
    "play60s_count": "play60s_count",         # 60s完播人数
    "rise_fans_count": "followers_gain",      # 涨粉
    "quote_count": "quote_count",             # 引用数
}
# note/base 粉丝维度(带 _with_fans 后缀)
XHS_BASE_FAN_RATE_MAP = {
    "full_view_rate_with_fans": "finish_rate_fan",
    "finish5s_rate_with_fans": "finish5_rate_fan",
    "exit_view2s_rate_with_fans": "bounce2_rate_fan",
    "cover_click_rate_with_fans": "cover_ctr_fan",
    "interaction_rate_with_fans": "interact_rate_fan",
    "view_rate_with_fans": "view_rate_fan",
    "like_rate_with_fans": "like_rate_fan",
    "collect_rate_with_fans": "collect_rate_fan",
    "comment_rate_with_fans": "comment_rate_fan",
    "share_rate_with_fans": "share_rate_fan",
    "danmaku_rate_with_fans": "danmaku_rate_fan",
    "view_time_avg_with_fans": "avg_duration_fan",
}

# note/base hour 逐小时列表 → metric_series 指标
XHS_HOUR_LIST_MAP = {
    "view_list": "hour_views",
    "like_list": "hour_likes",
    "collect_list": "hour_favorites",
    "comment_list": "hour_comments",
    "share_list": "hour_shares",
    "danmaku_list": "hour_danmaku",
    "finish_list": "hour_finish",
    "finish5s_list": "hour_finish5s",
    "rise_fans_list": "hour_followers",
    "play60s_list": "hour_play60s",
    "interact_list": "hour_interact",
    "view_time_list": "hour_view_time",
}

# 画像字段
XHS_AUDIENCE_MAP = {
    "gender": "gender",
    "age": "age",
    "city": "region",
    "interest": "interest",
}


def _extract_note_id(raw: str) -> str:
    """从链接/文本提取 24 位 hex 笔记 ID(小红书 note_id 是 24 位 hex)。"""
    m = re.search(r"[0-9a-f]{24}", raw)
    return m.group(0) if m else raw


def _ts_to_date(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(ts)


def _ts_to_hour(ts) -> str:
    """时间戳(毫秒)→ 'YYYY-MM-DD HH:MM'(保留小时粒度,避免同日覆盖)。"""
    try:
        return datetime.datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return str(ts)


class XhsFetcher:
    def __init__(self, db: DB, page, video_key: str):
        self.db = db
        self.page = page
        self.video_key = video_key
        self.archived = 0
        self.posted_pages = {}  # page -> body
        self.detail_responses = {}  # 深度接口响应: url_key -> (url, body)

    def _on_response(self, resp):
        try:
            url = resp.url
            if "creator.xiaohongshu.com" not in url:
                return
            if not any(ep in url for ep in ARCHIVE_ENDPOINTS):
                return
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            body = resp.text()
            if not body or body[0] not in "{[":
                return
            self.db.add_api_archive("xiaohongshu", self.video_key, url, body)
            self.archived += 1
            if "note/user/posted" in url:
                pg = url.split("page=")[-1].split("&")[0]
                self.posted_pages[pg] = body
            # 深度接口存档(key 用路径段)
            for ep in ("note/base", "note_detail_new", "audience/source/detail",
                       "audience/source", "audience/trend", "analyze/list"):
                if ep in url:
                    self.detail_responses[ep] = (url, body)
        except Exception:
            pass

    def scroll_to_load(self, max_rounds: int = 30):
        """滚动 .content 容器到底部,触发列表加载更多(页面自动带签名请求)。"""
        for i in range(max_rounds):
            before = set(self.posted_pages.keys())
            self.page.evaluate(
                """() => {
                    const c = document.querySelector('.content') || document.documentElement;
                    c.scrollTop = c.scrollHeight;
                    window.scrollTo(0, document.body.scrollHeight);
                }"""
            )
            self.page.wait_for_timeout(1800)
            after = set(self.posted_pages.keys())
            if i > 4 and after == before:
                break  # 连续没翻页,认为到底了

    def parse_note(self, note: dict) -> dict:
        """notes[] → 标准快照字段 + raw。"""
        out = {}
        for src, dst in XHS_NOTE_MAP.items():
            v = note.get(src)
            if v not in (None, ""):
                try:
                    out[dst] = float(v)
                except (ValueError, TypeError):
                    pass
        raw = {
            "type": note.get("type"),
            "duration": (note.get("video_info") or {}).get("duration") if note.get("video_info") else None,
            "sticky": note.get("sticky"),
            "tab_status": note.get("tab_status"),
            "xsec_token": note.get("xsec_token"),
            "xsec_source": note.get("xsec_source"),
            "permission_code": note.get("permission_code"),
            "cocreate": note.get("cocreate"),
            "cover_url": ((note.get("images_list") or [{}])[0] or {}).get("url"),
        }
        out["raw_xhs"] = json.dumps(raw, ensure_ascii=False)
        return out

    def parse_detail(self, detail: dict, note_id: str) -> dict:
        """note_detail_new → 标准快照字段(取 seven 近7天 + thirty 近30天)。
        9 项指标 + 逐日明细(7天/30天)。"""
        out = {}
        data = detail.get("data") or {}
        # 视频时长(从 noteInfo 或列表带过来)
        note_info = data.get("noteInfo") or {}
        seven = data.get("seven") or {}
        thirty = data.get("thirty") or {}

        # 核心指标:近30天总量(与手动表对照用 thirty 更接近全量)
        MAP = {
            "view_count": "views",
            "like_count": "likes",
            "collect_count": "favorites",
            "comment_count": "comments",
            "share_count": "shares",
            "rise_fans_count": "followers_gain",
            "home_view_count": "homepage_visit",
        }
        for src, dst in MAP.items():
            if thirty.get(src) not in (None, ""):
                try:
                    out[dst] = float(thirty[src])
                except (ValueError, TypeError):
                    pass
        # 平均观看时长(view_time_avg 单位疑似毫秒;仅保留 raw)
        if seven.get("view_time_avg") not in (None, ""):
            try:
                out["avg_duration"] = round(float(seven["view_time_avg"]) / 1000, 1)
            except (ValueError, TypeError):
                pass
        out["raw_xhs_detail"] = json.dumps({
            "note_id": note_id,
            "title": note_info.get("title"),
            "post_time": _ts_to_date(note_info.get("postTime")) if note_info.get("postTime") else None,
            "note_type": note_info.get("type"),
            "seven": seven,
            "thirty": thirty,
            "analyse_infos": data.get("analyse_infos"),
        }, ensure_ascii=False)
        return out

    def series_from_detail(self, detail: dict) -> dict:
        """note_detail_new → metric_series(逐日明细,7天/30天)。"""
        data = detail.get("data") or {}
        series = {}
        # 逐日列表:view_list / like_list / collect_list / comment_list /
        # share_list / rise_fans_list / home_view_list / view_time_list / danmaku_list
        LIST_MAP = {
            "view_list": "plays",
            "like_list": "likes",
            "collect_list": "favorites",
            "comment_list": "comments",
            "share_list": "shares",
            "rise_fans_list": "followers",
            "home_view_list": "homepage_visit",
            "danmaku_list": "danmaku",
        }
        for period in ("seven", "thirty"):
            p = data.get(period) or {}
            for key, metric in LIST_MAP.items():
                rows = p.get(key) or []
                if not rows:
                    continue
                tag = "" if period == "thirty" else "7d_"
                series[f"{tag}{metric}"] = [
                    (_ts_to_date(item.get("date")), item.get("count"), None)
                    for item in rows if item.get("date")
                ]
        return series

    # ── 深度数据解析 ──────────────────────────────────────────────
    def parse_note_base(self, note_id: str) -> dict:
        """note/base → 标准快照字段(完播/跳出/封面点击/互动等)。"""
        item = self.detail_responses.get("note/base")
        if not item:
            return {}
        try:
            d = json.loads(item[1])
        except json.JSONDecodeError:
            return {}
        data = d.get("data") or {}
        out = {}
        # 基础计数
        for src, dst in [("view_count", "views"), ("like_count", "likes"),
                         ("collect_count", "favorites"), ("comment_count", "comments"),
                         ("share_count", "shares"), ("danmaku_count", "danmaku")]:
            v = data.get(src)
            if v not in (None, "", -1):
                try:
                    out[dst] = float(v)
                except (ValueError, TypeError):
                    pass
        # rate 类(百分数直存; -1 = 数据冻结/不可用,跳过)
        for src, dst in XHS_BASE_RATE_MAP.items():
            v = data.get(src)
            if v not in (None, "", -1):
                try:
                    out[dst] = round(float(v), 2)
                except (ValueError, TypeError):
                    pass
        # 粉丝维度 rate
        for src, dst in XHS_BASE_FAN_RATE_MAP.items():
            v = data.get(src)
            if v not in (None, "", -1):
                try:
                    out[dst] = round(float(v), 2)
                except (ValueError, TypeError):
                    pass
        # analyse_infos → interact_rate(平台 vqa 互动质量指数)
        # 注意: interaction_rate 字段常为 0(口径不同),真实互动率在
        # analyse_infos[quota=vqa].count(如 4.95 = 互动率 4.95%)
        ai = data.get("analyse_infos") or []
        for item in ai:
            if item.get("quota") == "vqa" and item.get("count") is not None:
                out["interact_rate"] = round(float(item["count"]), 2)
                break
        # note_info(标题/标签/发布天数)
        ni = data.get("note_info") or {}
        out["raw_xhs_base"] = json.dumps({
            "note_id": note_id,
            "title": ni.get("desc"),
            # 保留完整时分(避免覆盖列表抓取的精确发布时间)
            "post_time": _ts_to_hour(ni.get("post_time")) if ni.get("post_time") else None,
            "note_type": ni.get("type"),
            "tags": ni.get("tags"),
            "note_post_days": data.get("note_post_days"),
            "hour": data.get("hour"),
            "analyse_infos": data.get("analyse_infos"),
        }, ensure_ascii=False)
        return out

    def series_from_note_base(self, note_id: str) -> dict:
        """note/base hour 逐小时曲线 → metric_series。

        注意:统计时间戳必须保留小时粒度(stat_date 用 'YYYY-MM-DD HH:MM'),
        否则 metric_series 的 UNIQUE(video_id, metric, stat_date) 会把同一天
        内多个小时点互相覆盖(71 条只剩 4 条)。
        """
        item = self.detail_responses.get("note/base")
        if not item:
            return {}
        try:
            d = json.loads(item[1])
        except json.JSONDecodeError:
            return {}
        hour = (d.get("data") or {}).get("hour") or {}
        series = {}
        for key, metric in XHS_HOUR_LIST_MAP.items():
            rows = hour.get(key) or []
            if not rows:
                continue
            series[metric] = [
                (_ts_to_hour(r.get("date")), r.get("count"), None)
                for r in rows if r.get("date")
            ]
        return series

    def series_from_trend(self, note_id: str, published_at: str) -> dict:
        """audience/trend → 发布后逐日相对指数曲线。

        trend_list: date = 发布后第 N 天(0=发布当天), count = 相对指数
        (发布当天=100,后续递减,表示播放热度衰减,非绝对观看数)。
        similar_trend_list: 同题材对比曲线(同类均值,同样语义)。
        """
        item = self.detail_responses.get("audience/trend")
        if not item:
            return {}
        try:
            d = json.loads(item[1])
        except json.JSONDecodeError:
            return {}
        data = d.get("data") or {}
        tl = data.get("trend_list") or []
        st = data.get("similar_trend_list") or []
        series = {}
        if tl:
            series["trend_heat_index"] = [
                (f"D+{r.get('date')}", r.get("count"), None)
                for r in tl if r.get("date") is not None
            ]
        if st:
            series["trend_similar_index"] = [
                (f"D+{r.get('date')}", r.get("count"), None)
                for r in st if r.get("date") is not None
            ]
        return series

    def parse_audience(self) -> dict:
        """audience/source/detail → 观众画像(gender/age/city/interest)。"""
        item = self.detail_responses.get("audience/source/detail")
        if not item:
            return {}
        try:
            d = json.loads(item[1])
        except json.JSONDecodeError:
            return {}
        data = d.get("data") or {}
        out = {}
        for src, dst in XHS_AUDIENCE_MAP.items():
            v = data.get(src)
            if v:
                out[dst] = v
        return out

    def parse_audience_source(self) -> dict:
        """audience/source → 观看来源分布。"""
        item = self.detail_responses.get("audience/source")
        if not item:
            return {}
        try:
            d = json.loads(item[1])
        except json.JSONDecodeError:
            return {}
        return (d.get("data") or {}).get("source") or []

    def parse_audience_trend(self) -> dict:
        """audience/trend → 完整结构(含 summary 分析建议)。"""
        item = self.detail_responses.get("audience/trend")
        if not item:
            return {}
        try:
            d = json.loads(item[1])
        except json.JSONDecodeError:
            return {}
        data = d.get("data") or {}
        return {
            "trend_list": data.get("trend_list") or [],
            "similar_trend_list": data.get("similar_trend_list") or [],
            "summary": data.get("summary"),
            "note_post_days": data.get("note_post_days"),
        }


def fetch_single_note(note_id: str, headless: bool = True, wait_ms: int = 9000):
    """只抓指定单篇笔记的列表数据(不进笔记管理列表页,不滚全部)。

    打开数据中心单篇详情页 statistics/note-detail?noteId=X,拦截
    note/base 响应:note_info(标题/时间/类型)+ data 层基础计数
    (view/like/collect/comment/share),存为 cumulative 快照。
    """
    state_path = SESSION_DIR / "xiaohongshu.json"
    if not state_path.exists():
        print("❌ 未找到小红书登录态,请先运行: python sf_login.py xiaohongshu")
        sys.exit(1)

    db = DB()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            storage_state=str(state_path),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        fetcher = XhsFetcher(db, page, note_id)
        page.on("response", fetcher._on_response)

        url = f"https://creator.xiaohongshu.com/statistics/note-detail?noteId={note_id}&t={int(datetime.datetime.now().timestamp())}"
        print(f"抓取单篇: {note_id}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(wait_ms)
        if "/login" in page.url:
            print("⚠️ 登录态已过期,请重新登录: python sf_login.py xiaohongshu")
            browser.close()
            sys.exit(1)

        # 从 note/base 响应解析
        item = fetcher.detail_responses.get("note/base")
        if not item:
            print(f"⚠️ {note_id}: 未获取到 note/base 数据(可能无权限/页面未加载)")
            browser.close()
            return False
        try:
            d = json.loads(item[1])
        except json.JSONDecodeError:
            print(f"⚠️ {note_id}: note/base 响应解析失败")
            browser.close()
            return False
        data = d.get("data") or {}
        ni = data.get("note_info") or {}

        metrics = {}
        for src, dst in [("view_count", "views"), ("like_count", "likes"),
                         ("collect_count", "favorites"), ("comment_count", "comments"),
                         ("share_count", "shares"), ("danmaku_count", "danmaku")]:
            v = data.get(src) if src in data else ni.get(src)
            if v not in (None, "", -1):
                try:
                    metrics[dst] = float(v)
                except (ValueError, TypeError):
                    pass
        if not metrics:
            print(f"⚠️ {note_id}: note/base 无有效计数数据")
            browser.close()
            return False

        video_id = db.upsert_video(
            platform="xiaohongshu",
            video_key=note_id,
            title=ni.get("desc"),
            published_at=_ts_to_hour(ni.get("post_time")) if ni.get("post_time") else None,
            url=f"https://www.xiaohongshu.com/explore/{note_id}",
        )
        db.add_snapshot(video_id, "cumulative", metrics, source="api")
        print(f"  ✅ {note_id} | {str(ni.get('desc'))[:22]:24s} | 观看 {metrics.get('views')} | 点赞 {metrics.get('likes')} | 收藏 {metrics.get('favorites')} | 评论 {metrics.get('comments')} | 分享 {metrics.get('shares')}")

        browser.close()
        return True


def fetch_notes(target_id: str = None, pages: int = None, headless: bool = True):
    """全量抓取笔记列表(滚动翻页),可选匹配指定笔记 ID。"""
    state_path = SESSION_DIR / "xiaohongshu.json"
    if not state_path.exists():
        print("❌ 未找到小红书登录态,请先运行: python sf_login.py xiaohongshu")
        sys.exit(1)

    db = DB()
    key = target_id or "all"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            storage_state=str(state_path),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        fetcher = XhsFetcher(db, page, key)
        page.on("response", fetcher._on_response)

        page.goto(MANAGE_URL + "?t=" + str(int(datetime.datetime.now().timestamp())),
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8000)
        if "/login" in page.url:
            print("⚠️ 登录态已过期,请重新登录: python sf_login.py xiaohongshu")
            browser.close()
            sys.exit(1)

        # 滚动翻页加载全部
        max_rounds = pages or 40
        fetcher.scroll_to_load(max_rounds=max_rounds)

        # 解析
        notes_all = []
        seen_ids = set()
        for pg in sorted(fetcher.posted_pages.keys(), key=lambda x: int(x)):
            body = fetcher.posted_pages[pg]
            try:
                d = json.loads(body)
            except json.JSONDecodeError:
                continue
            notes = (d.get("data") or {}).get("notes") or []
            for note in notes:
                nid = str(note.get("id"))
                if nid in seen_ids:
                    continue
                seen_ids.add(nid)
                notes_all.append(note)

        # 匹配指定 ID
        matched = [n for n in notes_all if target_id and str(n.get("id")) == target_id]
        if target_id:
            if not matched:
                print(f"⚠️ 未在笔记列表中找到 {target_id}")
                browser.close()
                sys.exit(1)
            notes_all = matched

        found = 0
        for note in notes_all:
            nid = str(note.get("id"))
            title = str(note.get("display_title") or "")[:60]
            pub_time = str(note.get("time") or "")
            note_type = note.get("type")
            duration = ((note.get("video_info") or {}).get("duration")) if note.get("video_info") else None
            metrics = fetcher.parse_note(note)

            video_id = db.upsert_video(
                platform="xiaohongshu",
                video_key=nid,
                title=title,
                published_at=pub_time,
                url=f"https://www.xiaohongshu.com/explore/{nid}",
            )
            db.add_snapshot(video_id, "cumulative", metrics, source="api")
            found += 1
            typ = "视频" if note_type == "video" else "图文"
            dur = f"{duration}s" if duration else "-"
            print(f"  ✅ {nid} | [{typ}] {title[:22]:24s} | 观看 {metrics.get('views')} | 点赞 {metrics.get('likes')} | 收藏 {metrics.get('favorites')} | 评论 {metrics.get('comments')} | 分享 {metrics.get('shares')} | {dur}")

        browser.close()

    if found == 0:
        print("⚠️ 未抓取到任何笔记。")
    else:
        print(f"\n✅ 小红书抓取完成: {found} 条笔记,接口存档 {fetcher.archived} 次")
        print(f"   数据库: {db.path}")
        return found


def _fetch_one_detail(db, page, note_id: str, wait_ms: int = 7000) -> bool:
    """单篇深度抓取(复用已打开的浏览器页面)。"""
    fetcher = XhsFetcher(db, page, note_id)
    # 避免 listener 堆叠:先移除之前注册的,再注册当前
    if getattr(page, "_xhs_prev_fetcher", None) is not None:
        try:
            page.remove_listener("response", page._xhs_prev_fetcher._on_response)
        except Exception:
            pass
    page._xhs_prev_fetcher = fetcher
    page.on("response", fetcher._on_response)

    url = f"https://creator.xiaohongshu.com/statistics/note-detail?noteId={note_id}&t={int(datetime.datetime.now().timestamp())}"
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(wait_ms)

    # 解析
    metrics = fetcher.parse_note_base(note_id)
    if not metrics:
        print(f"  ⚠️ {note_id}: 未解析到 note/base 数据")
        return False

    video_id = db.upsert_video(
        platform="xiaohongshu",
        video_key=note_id,
        title=json.loads(metrics.get("raw_xhs_base", "{}")).get("title"),
        published_at=json.loads(metrics.get("raw_xhs_base", "{}")).get("post_time"),
        url=f"https://www.xiaohongshu.com/explore/{note_id}",
    )
    db.add_snapshot(video_id, "deep", metrics, source="api")

    # 逐小时曲线 + 发布后逐日热度指数
    series = fetcher.series_from_note_base(note_id)
    trend = fetcher.parse_audience_trend()
    if trend:
        # 热度指数 + 同类对比 + summary 存档
        series.update(fetcher.series_from_trend(note_id, ""))
    if series:
        db.bulk_add_metric_series(video_id, series)

    # 画像
    audience = fetcher.parse_audience()
    source = fetcher.parse_audience_source()
    if audience or source:
        db.add_audience(
            video_id,
            gender=audience.get("gender"),
            age=audience.get("age"),
            region=audience.get("region"),
            interest=audience.get("interest"),
            extra={"source": source, "trend_summary": (trend or {}).get("summary")} if (source or (trend or {}).get("summary")) else None,
        )

    print(f"  ✅ {note_id} | 观看 {metrics.get('views')} | 完播 {metrics.get('finish_rate')}% | 5s完播 {metrics.get('finish5_rate')}% | 2s跳出 {metrics.get('bounce2_rate')}% | 封面点击 {metrics.get('cover_ctr')}% | 平均观看 {metrics.get('avg_duration')}s | 曝光 {metrics.get('exposure')} | 逐小时 {len(series)} 组 | 热度指数 {len((trend or {}).get('trend_list') or [])} 天 | 画像 {'✅' if audience else '—'}")
    return True


def fetch_note_detail(note_id: str, headless: bool = True, wait_ms: int = 12000):
    """抓取单笔记深度数据(独立浏览器,适合单篇)。"""
    state_path = SESSION_DIR / "xiaohongshu.json"
    if not state_path.exists():
        print("❌ 未找到小红书登录态,请先运行: python sf_login.py xiaohongshu")
        sys.exit(1)

    db = DB()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            storage_state=str(state_path),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        print(f"打开笔记详情: {note_id}")
        ok = _fetch_one_detail(db, page, note_id, wait_ms=wait_ms)
        if "/login" in page.url:
            print("⚠️ 登录态已过期,请重新登录: python sf_login.py xiaohongshu")
        browser.close()
        return ok


def fetch_all_details(note_ids, headless: bool = True, wait_ms: int = 7000,
                      skip_done: bool = True):
    """批量抓取深度数据(复用单个浏览器,大幅提速)。

    note_ids: 可迭代的笔记 ID 列表
    skip_done: 跳过已有深度快照的笔记(断点续抓)
    """
    state_path = SESSION_DIR / "xiaohongshu.json"
    if not state_path.exists():
        print("❌ 未找到小红书登录态,请先运行: python sf_login.py xiaohongshu")
        sys.exit(1)

    db = DB()
    ok = fail = skipped = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            storage_state=str(state_path),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        for nid in note_ids:
            if skip_done:
                v = db.get_video(platform="xiaohongshu", video_key=nid)
                if v:
                    done = db.get_snapshots(v["id"], snapshot_type="deep")
                    if done:
                        skipped += 1
                        continue
            try:
                if _fetch_one_detail(db, page, nid, wait_ms=wait_ms):
                    ok += 1
                else:
                    fail += 1
            except Exception as e:
                fail += 1
                print(f"  ⚠️ {nid} 抓取异常: {str(e)[:80]}")
            # 每篇小间隔,避免风控
            import time as _t
            _t.sleep(0.5)
        browser.close()

    print(f"\n✅ 深度抓取完成: 成功 {ok} / 失败 {fail} / 跳过(已有) {skipped}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SQ 小红书数据抓取器")
    ap.add_argument("--id", help="笔记 ID(24位hex)")
    ap.add_argument("--url", help="笔记链接(自动提取 ID)")
    ap.add_argument("--all", action="store_true", help="全量模式(抓取列表全部)")
    ap.add_argument("--pages", type=int, help="滚动翻页轮数(默认40)")
    ap.add_argument("--deep", action="store_true", help="深度模式(抓单笔记/全部笔记的深度数据)")
    ap.add_argument("--limit", type=int, help="深度模式最多抓前 N 篇(默认全部)")
    ap.add_argument("--wait", type=int, default=7000, help="深度模式页面等待毫秒(默认7000)")
    ap.add_argument("--show", action="store_true", help="有头调试模式")
    args = ap.parse_args()
    target = args.id or (_extract_note_id(args.url) if args.url else None)

    if args.deep:
        from sf_core import DB as _DB
        if target:
            fetch_note_detail(target, headless=not args.show, wait_ms=args.wait)
        else:
            # 从库中取全部小红书笔记 ID
            db = _DB()
            ids = [v["video_key"] for v in db.list_videos(platform="xiaohongshu")]
            if args.limit:
                ids = ids[:args.limit]
            print(f"深度抓取 {len(ids)} 篇笔记...")
            fetch_all_details(ids, headless=not args.show, wait_ms=args.wait)
    elif args.all:
        fetch_notes(target_id=None, pages=args.pages, headless=not args.show)
    elif target:
        # 指定单条:只抓这一条,不滚全部列表
        fetch_single_note(target, headless=not args.show)
    else:
        print("用法: python sf_xiaohongshu.py --id <ID> | --url <链接> | --all | --deep [--id] [--limit N]")
        sys.exit(1)
