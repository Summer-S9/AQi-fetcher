# -*- coding: utf-8 -*-
"""
SQ 抖音数据抓取器 (sf_douyin.py)
========================================
用 Playwright 复用已保存登录态,从抖音创作者中心抓取指定作品(或全量)的**全维度数据**。

已验证接口(抖音创作者中心 /creator-micro/content/manage):
  - /janus/douyin/creator/pc/work_list?status=0&count=12&max_cursor=X  作品列表(翻页)

items[].metrics 字段(全部覆盖用户手动表 + 更多):
  view_count 播放 / like_count 点赞 / comment_count 评论 / share_count 分享 /
  favorite_count 收藏 / danmaku_count 弹幕 / dislike_count 不喜欢 /
  completion_rate 完播率 / completion_rate_5s 5s完播率 / bounce_rate_2s 2s跳出率 /
  cover_click_rate 封面点击率 / avg_view_second 平均观看秒数 / avg_view_proportion 平均播放进度 /
  homepage_visit_count 主页访问量 / subscribe_count 吸粉 / unsubscribe_count 取关 /
  fan_view_proportion 粉丝观看占比 / 各 *_rate 比率

注意:抖音 rate 类字段均为小数(0.151041 = 15.1%),入库前 ×100 转百分数。

用法:
  python sf_douyin.py --id 7579889058541767987    # 抓指定作品(翻页匹配)
  python sf_douyin.py --url https://v.douyin.com/xxx  # 链接提取 ID
  python sf_douyin.py --all --pages 3             # 全量抓前 3 页(每页 12 条)
"""

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from sf_core import DB, SESSION_DIR

MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
WORK_LIST_PATH = "/janus/douyin/creator/pc/work_list"

# 抖音 metrics 字段 → SQ 标准字段(rate 类为小数,×100 转 %)
DOUYIN_METRIC_MAP = {
    "view_count": "views",
    "like_count": "likes",
    "favorite_count": "favorites",
    "comment_count": "comments",
    "share_count": "shares",
    "danmaku_count": "danmaku",
    "subscribe_count": "followers_gain",
    "unsubscribe_count": "unfollow",
}
DOUYIN_RATE_MAP = {
    "completion_rate": "finish_rate",          # 完播率
    "completion_rate_5s": "finish5_rate",      # 5s完播
    "bounce_rate_2s": "bounce2_rate",          # 2s跳出
    "cover_click_rate": "cover_ctr",           # 封面点击率
}
# 保留在 raw 的附加字段(非标准列)
DOUYIN_EXTRA_FIELDS = [
    "avg_view_second", "avg_view_proportion", "homepage_visit_count",
    "fan_view_proportion", "dislike_count", "download_count",
    "like_rate", "comment_rate", "share_rate", "favorite_rate",
    "subscribe_rate", "unsubscribe_rate", "dislike_rate",
]


def _extract_id(raw: str) -> str:
    m = re.search(r"(\d{15,20})", raw)
    return m.group(1) if m else raw


class WorkListFetcher:
    def __init__(self, db: DB, page, video_key: str):
        self.db = db
        self.page = page
        self.video_key = video_key
        self.archived = 0

    def _on_response(self, resp):
        try:
            url = resp.url
            if "work_list" not in url:
                return
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            body = resp.text()
            if body:
                self.db.add_api_archive("douyin", self.video_key, url, body)
                self.archived += 1
        except Exception:
            pass

    def fetch_page(self, max_cursor: int, count: int = 12) -> dict:
        """在页面上下文 fetch 一页 work_list。

        关键:返回原始文本而非 r.json()——JS 的 Number 无法精确表示 19 位
        aweme_id(>2^53),JS 端 parse 会丢精度;原始文本由 Python 解析保精度。
        """
        txt = self.page.evaluate(
            """async (o) => {
                const url = o.path + '?status=0&count=' + o.count + '&max_cursor=' + o.maxCursor +
                    '&scene=star_atlas&device_platform=android&aid=1128&t=' + Date.now();
                const r = await fetch(url);
                return await r.text();
            }""",
            {"path": WORK_LIST_PATH, "maxCursor": max_cursor, "count": count},
        )
        return json.loads(txt)

    def parse_metrics(self, item: dict) -> dict:
        """items[].metrics → 标准快照字段 + raw。"""
        m = item.get("metrics") or {}
        out = {}
        for src, dst in DOUYIN_METRIC_MAP.items():
            if m.get(src) not in (None, ""):
                try:
                    out[dst] = float(m[src])
                except (ValueError, TypeError):
                    pass
        for src, dst in DOUYIN_RATE_MAP.items():
            if m.get(src) not in (None, ""):
                try:
                    out[dst] = round(float(m[src]) * 100, 2)  # 小数 → %
                except (ValueError, TypeError):
                    pass
        raw = {}
        for f in DOUYIN_EXTRA_FIELDS:
            if m.get(f) not in (None, ""):
                try:
                    raw[f] = round(float(m[f]), 4)
                except (ValueError, TypeError):
                    raw[f] = m[f]
        # 平均观看秒数 → avg_duration
        if m.get("avg_view_second") not in (None, ""):
            try:
                out["avg_duration"] = round(float(m["avg_view_second"]), 1)
            except (ValueError, TypeError):
                pass
        # 主页访问量 / 粉丝观看占比(标准列)
        if m.get("homepage_visit_count") not in (None, ""):
            try:
                out["homepage_visit"] = float(m["homepage_visit_count"])
            except (ValueError, TypeError):
                pass
        if m.get("fan_view_proportion") not in (None, ""):
            try:
                out["fan_view_rate"] = round(float(m["fan_view_proportion"]) * 100, 2)  # → %
            except (ValueError, TypeError):
                pass
        out["raw_douyin"] = json.dumps({**raw, "aweme_id": item.get("id")}, ensure_ascii=False)
        return out


def fetch_videos(target_id: str = None, pages: int = None, headless: bool = True):
    state_path = SESSION_DIR / "douyin.json"
    if not state_path.exists():
        print("❌ 未找到抖音登录态,请先运行: python sf_login.py douyin")
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
        fetcher = WorkListFetcher(db, page, key)
        page.on("response", fetcher._on_response)

        page.goto(MANAGE_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6000)
        if "passport" in page.url:
            print("⚠️ 登录态已过期,请重新登录: python sf_login.py douyin")
            browser.close()
            sys.exit(1)

        max_pages = pages or 40
        found_total = 0
        cursor = 0
        fetched_ids = set()
        stop = False
        for page_i in range(max_pages):
            data = fetcher.fetch_page(cursor)
            items = data.get("items") or []
            if not items:
                break
            for item in items:
                aweme_id = str(item.get("id"))
                if aweme_id in fetched_ids:
                    continue
                fetched_ids.add(aweme_id)
                # 匹配指定 ID(指定模式)
                if target_id and aweme_id != target_id:
                    continue
                desc = str(item.get("description") or "")[:60]
                create_ts = item.get("create_time")
                create_date = datetime.datetime.fromtimestamp(int(create_ts)).strftime("%Y-%m-%d %H:%M") if create_ts else None
                metrics = fetcher.parse_metrics(item)

                # 抖音数据冻结机制:详细数据(完播/跳出/趋势/画像)发布 3 个月后停止更新,
                # 页面标注「数据更新至 {发布+3个月}」;播放/点赞等基础数据持续更新。
                frozen = None
                if create_ts:
                    pub = datetime.datetime.fromtimestamp(int(create_ts))
                    frozen = (pub + datetime.timedelta(days=90)).strftime("%Y-%m-%d")
                    if (datetime.datetime.now() - pub).days > 90:
                        raw = json.loads(metrics.get("raw_douyin", "{}"))
                        raw["metrics_frozen_at"] = frozen  # 详细数据冻结日(3个月时点)
                        metrics["raw_douyin"] = json.dumps(raw, ensure_ascii=False)

                video_id = db.upsert_video(
                    platform="douyin",
                    video_key=aweme_id,
                    title=desc,
                    published_at=create_date,
                    url=f"https://www.douyin.com/video/{aweme_id}",
                )
                db.add_snapshot(video_id, "cumulative", metrics, source="api")
                found_total += 1
                frozen_note = f" | ⚠️详细数据冻结于{frozen}" if frozen and (datetime.datetime.now() - datetime.datetime.fromtimestamp(int(create_ts))).days > 90 else ""
                print(f"  ✅ {aweme_id} | {desc[:24]:26s} | 播放 {metrics.get('views')} | 完播 {metrics.get('finish_rate')}% | 2s跳出 {metrics.get('bounce2_rate')}% | 吸粉 {metrics.get('followers_gain')}{frozen_note}")
                if target_id:
                    stop = True
                    break
            if stop:
                break
            has_more = data.get("has_more")
            cursor = data.get("max_cursor") or 0
            if not has_more:
                break
            page.wait_for_timeout(800)

        browser.close()

    if target_id and found_total == 0:
        print(f"⚠️ 未在作品列表中找到 {target_id}(可能已删除或不在前 {max_pages} 页)")
    else:
        print(f"\n✅ 抖音抓取完成: {found_total} 条作品,接口存档 {fetcher.archived} 次")
        print(f"   数据库: {db.path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SQ 抖音数据抓取器")
    ap.add_argument("--id", help="作品 ID(纯数字)")
    ap.add_argument("--url", help="作品链接(自动提取 ID)")
    ap.add_argument("--all", action="store_true", help="全量模式(抓取列表全部)")
    ap.add_argument("--pages", type=int, help="全量模式翻页数(默认 40)")
    ap.add_argument("--show", action="store_true", help="有头调试模式")
    args = ap.parse_args()
    target = args.id or (_extract_id(args.url) if args.url else None)
    if args.all:
        fetch_videos(target_id=None, pages=args.pages, headless=not args.show)
    elif target:
        fetch_videos(target_id=target, headless=not args.show)
    else:
        print("用法: python sf_douyin.py --id <作品ID> | --url <链接> | --all")
        sys.exit(1)
