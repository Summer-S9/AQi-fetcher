# -*- coding: utf-8 -*-
"""
SQ B站数据抓取器 (sf_bilibili.py)
========================================
用 Playwright 复用已保存登录态,从 B站创作中心抓取指定稿件**全量数据**。

已验证接口(B站创作中心 /york/data-center-web/dataCenter/archive/overview):
  - /x/web/data/v3/archive/view?aid=          稿件基础信息(标题/发布时间/时长/分区)
  - /x/web/data/archive_diagnose/overview?bvid=  核心统计+播放端分布+观众画像(地区/性别/年龄/类型/粉丝占比)
  - /x/web/data/archive_diagnose/trend?type=play&bvid=  逐日播放增量
  - /x/web/data/archive_diagnose/trend?type=fan&bvid=   逐日转粉增量
  - /x/web/data/archive_diagnose/play_analyze?bvid=     播放分析(时长分布/退出率等)
  - /x/web/data/archive_diagnose/compare?bvid=&size=10  同题材对比
  - /x/web/data/archive_diagnose/limit?bvid=            稿件限制状态

所有接口响应原文全部存入 api_archives 表(防遗漏),结构化解析入各表。

用法:
  python sf_bilibili.py --bv BV1XXXX
  python sf_bilibili.py --url https://www.bilibili.com/video/BV1XXXX
  python sf_bilibili.py --list                      # 列出后台全部稿件(发现新视频)
  python sf_bilibili.py --list --list-out x.json    # 列表落盘
  python sf_bilibili.py --sync                      # 自动补抓所有未入库稿件
  python sf_bilibili.py --sync --since 2026-08-01   # 只补抓该日期之后发布的
  python sf_bilibili.py --sync --dry-run            # 只列待抓清单,不抓
  python sf_bilibili.py --bv BV1XXXX --show   # 有头调试
"""

import argparse
import datetime
import json
import random
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from sf_core import DB, SESSION_DIR

# 稿件分析页(已验证:直接带 bvid 即加载目标稿件)
ARCHIVE_OVERVIEW_URL = "https://member.bilibili.com/york/data-center-web/dataCenter/archive/overview?tmid=&bvid={bvid}"
DATA_BASE = "https://member.bilibili.com/x/web/data"

# 需要全量存档的接口(能拉到的都存)
ARCHIVE_ENDPOINTS = [
    "/archive_diagnose/overview",
    "/archive_diagnose/trend",
    "/archive_diagnose/play_analyze",
    "/archive_diagnose/compare",
    "/archive_diagnose/limit",
    "/v2/archive/analyze/graph",
    "/v3/archive/view",
    "/archive/index",
]

# trend 接口支持的指标维度(全部逐日 30 天 + 小时级 + 近 30 天长尾)
TREND_TYPES = {
    "play": "plays",
    "fan": "followers",
    "like": "likes",
    "coin": "coins",
    "fav": "favorites",
    "comment": "comments",
    "dm": "danmaku",
    "share": "shares",
}

# 稿件列表接口(创作中心稿件管理页 /platform/upload-manager/article 实际调用)
# status 覆盖全部状态:审核中 / 已通过(pubed) / 未通过
ARCHIVES_URL = "/x/web/archives"
ARCHIVE_STATUS = "is_pubing,pubed,not_pubed"

# 稿件状态码 → 文字(state_desc 平台已给,此处留作兜底)
STATE_DESC = {
    0: "开放浏览", -1: "待审核", -2: "被打回", -4: "待审核(UP主删除)",
    -6: "审核中", -7: "审核中", -8: "审核中", -9: "转码失败", -10: "待审核",
    -11: "待审核", -13: "审核中", -14: "审核中", -15: "审核中",
    -16: "审核中", -17: "审核中", -18: "审核中", -19: "审核中",
    -20: "审核中", -21: "审核中", -22: "审核中", -23: "审核中",
    -24: "审核中", -25: "审核中", -30: "审核中", -40: "审核中", -100: "用户删除",
    -101: "审核中", -102: "审核中", 100: "开放浏览", 120: "被删除",
    200: "审核中", 250: "审核中", 300: "审核中", 400: "审核中",
}


def _pick_bvid(raw: str) -> str:
    m = re.search(r"BV[0-9A-Za-z]{10}", raw)
    return m.group(0) if m else raw


def _get_cid(bvid: str):
    """公开接口拿 cid(analyze/graph 需要)。无登录要求。"""
    import urllib.request
    try:
        with urllib.request.urlopen(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", timeout=15
        ) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        pages = (d.get("data") or {}).get("pages") or []
        return pages[0].get("cid") if pages else None
    except Exception:
        return None


def _ts_to_date(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(ts)


class APIInterceptor:
    """拦截 JSON 接口响应:全量存档 + 供解析。"""

    def __init__(self, db: DB, video_key: str, platform: str = "bilibili"):
        self.db = db
        self.video_key = video_key
        self.platform = platform
        self.responses = []  # [(url, json_obj)]

    def attach(self, page):
        page.on("response", self._on_response)

    def _on_response(self, resp):
        try:
            url = resp.url
            if "member.bilibili.com" not in url:
                return
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return
            body = resp.text()
            if not body:
                return
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                return
            # 只存档数据接口
            if any(ep in url for ep in ARCHIVE_ENDPOINTS):
                self.db.add_api_archive(self.platform, self.video_key, url, body)
            self.responses.append((url, obj))
        except Exception:
            pass

    def find(self, *fragments):
        for url, obj in self.responses:
            if all(f in url for f in fragments):
                return obj
        return None


# ─── 解析器 ──────────────────────────────────────────────────────────
def parse_view(interceptor) -> dict:
    """v3/archive/view → 基础信息。"""
    obj = interceptor.find("v3/archive/view") or interceptor.find("archive", "view")
    if not obj or obj.get("code") != 0:
        return {}
    d = obj.get("data") or {}
    out = {
        "title": d.get("title"),
        "published_at": _ts_to_date(d.get("pubtime")) if d.get("pubtime") else None,
        "duration": d.get("duration"),
        "cover": d.get("cover"),
        "tag": (d.get("tag") or "")[:200],
    }
    if d.get("view") is not None:
        out["views"] = float(d["view"])
    if d.get("like") is not None:
        out["likes"] = float(d["like"])
    if d.get("fans_incr") is not None:
        out["followers_gain"] = float(d["fans_incr"])
    return out


def parse_overview(interceptor) -> dict:
    """archive_diagnose/overview → 核心统计 + 播放端分布 + 观众画像。"""
    obj = interceptor.find("archive_diagnose/overview")
    if not obj or obj.get("code") != 0:
        return {}, {}
    d = obj.get("data") or {}
    stat = d.get("stat") or {}
    # 核心指标映射
    metrics = {}
    M = {
        "play": "views", "like": "likes", "dm": "danmaku", "comment": "comments",
        "share": "shares", "fav": "favorites", "coin": "coins",
        "fan": "followers_gain", "unfollow": "unfollow",
        "play_avg_duration": "avg_duration", "total_duration": "total_duration",
    }
    for k, v in stat.items():
        if k in M and v is not None:
            metrics[M[k]] = float(v)
    # 观众画像
    audience = {}
    for key in ("play_proportion", "viewer_area", "gender", "viewer_age",
                "viewer_ty", "audience_proportion"):
        if key in d and d[key]:
            audience[key] = d[key]
    return metrics, audience


def parse_trend(interceptor, metric: str) -> list:
    """archive_diagnose/trend?type=X → [(date, daily_inc), ...]"""
    obj = interceptor.find("archive_diagnose/trend", f"type={metric}")
    if not obj or obj.get("code") != 0:
        return []
    d = obj.get("data") or {}
    tend = d.get("tendency") or []
    rows = []
    for item in tend:
        if isinstance(item, dict) and item.get("date_key") is not None:
            date = _ts_to_date(item["date_key"])
            daily = item.get("total_inc") or item.get("inc") or 0
            rows.append((date, daily))
    return rows


def parse_hour_trend(interceptor, metric: str) -> list:
    """hour_tendency(发布后小时级)→ [(datetime, inc), ...]"""
    obj = interceptor.find("archive_diagnose/trend", f"type={metric}")
    if not obj or obj.get("code") != 0:
        return []
    d = obj.get("data") or {}
    tend = d.get("hour_tendency") or []
    rows = []
    for item in tend:
        if isinstance(item, dict) and item.get("date_key") is not None:
            try:
                dt = datetime.datetime.fromtimestamp(int(item["date_key"])).strftime("%m-%d %H:%M")
            except (ValueError, TypeError):
                dt = str(item["date_key"])
            rows.append((dt, item.get("total_inc") or 0))
    return rows


def parse_last30_trend(interceptor, metric: str) -> list:
    """last_30_day_tendency(近 30 天长尾)→ [(date, daily_inc), ...]"""
    obj = interceptor.find("archive_diagnose/trend", f"type={metric}")
    if not obj or obj.get("code") != 0:
        return []
    d = obj.get("data") or {}
    tend = d.get("last_30_day_tendency") or []
    rows = []
    for item in tend:
        if isinstance(item, dict) and item.get("date_key") is not None:
            rows.append((_ts_to_date(item["date_key"]), item.get("total_inc") or 0))
    return rows


def parse_play_analyze(interceptor) -> dict:
    """play_analyze → 播放分析(互动率/3s跳出率/播放来源/建议等)。

    注意:B站该接口所有 rate 均为万分比(如 926 = 9.26%,2337 = 23.37%)。
    """
    obj = interceptor.find("archive_diagnose/play_analyze")
    if not obj or obj.get("code") != 0:
        return {}
    d = obj.get("data") or {}
    out = dict(d)
    # 互动率(万分比 → %)
    gi = d.get("guest_interact") or {}
    for k in ("interact_rate", "interact_viewer_rate", "interact_fan_rate"):
        if gi.get(k) is not None:
            out[k] = round(float(gi[k]) / 100, 2)
    if gi.get("interact_star") is not None:
        out["interact_star"] = round(float(gi["interact_star"]) / 10, 1)
    # 3s 跳出率(crash_rate,万分比 → %)+ 星级
    for k in ("crash_rate", "crash_viewer_rate", "crash_fan_rate"):
        if gi.get(k) is not None:
            out[k] = round(float(gi[k]) / 100, 2)
    if gi.get("crash_star") is not None:
        out["crash_star"] = round(float(gi["crash_star"]) / 10, 1)
    # 播放来源:粉丝/游客占比(万分比 → %)
    va = d.get("viewer_assistant") or {}
    if va.get("play_fan_rate") is not None:
        out["fan_play_rate"] = round(float(va["play_fan_rate"]) / 100, 2)
    if va.get("play_viewer_rate") is not None:
        out["viewer_play_rate"] = round(float(va["play_viewer_rate"]) / 100, 2)
    return out


def parse_graph(interceptor) -> dict:
    """v2/archive/analyze/graph → 完播/跳出/弹幕分析(结构化)。"""
    obj = interceptor.find("v2/archive/analyze/graph")
    if not obj or obj.get("code") != 0:
        return {}
    d = obj.get("data") or {}
    out = {}
    qi = d.get("quit_info") or {}
    if qi.get("full_play_ratio") is not None:
        # 万分比 → 百分数(3733 → 37.33)
        out["finish_rate"] = round(float(qi["full_play_ratio"]) / 100, 2)
    if qi.get("full_play_ratio_avg") is not None:
        out["finish_rate_peer_avg"] = round(float(qi["full_play_ratio_avg"]) / 100, 2)
    if qi.get("avg_play_progress") is not None:
        out["avg_play_progress"] = float(qi["avg_play_progress"])  # 秒
    di = d.get("duration_info") or {}
    if di.get("avg_play_time_int") is not None:
        out["avg_duration"] = float(di["avg_play_time_int"])  # 秒
    # 逐段退出曲线(每 20s 一段;仅保留原始数据,不做留存率换算——
    # B站官方 3s 跳出率(crash_rate)与段退出分母口径不同,自算百分比会失真)
    vq = d.get("viewer_quit") or []
    out["quit_curve"] = vq
    out["danmu_info"] = d.get("danmu_info")
    return out


def parse_compare(interceptor) -> list:
    """compare → 同题材对比列表。"""
    obj = interceptor.find("archive_diagnose/compare")
    if not obj or obj.get("code") != 0:
        return []
    return (obj.get("data") or {}).get("list") or []


def parse_limit(interceptor) -> dict:
    obj = interceptor.find("archive_diagnose/limit")
    if not obj or obj.get("code") != 0:
        return {}
    return obj.get("data") or {}


# ─── 稿件列表(发现新视频的前提) ──────────────────────────────────────
def list_archives(headless: bool = True, ps: int = 50, status: str = ARCHIVE_STATUS,
                  pages: int = 0) -> list:
    """列出创作中心**全部**稿件(自动分页),按发布时间倒序返回。

    返回每项:
      {bvid, aid, title, ptime(时间戳), published_at(YYYY-MM-DD), state,
       state_desc, duration, typename, tag, desc, ctime, stat(若平台返回)}

    pages=0 表示抓到为空为止(全量)。
    """
    state_path = SESSION_DIR / "bilibili.json"
    if not state_path.exists():
        print("❌ 未找到登录态,请先运行: python sf_login.py bilibili")
        sys.exit(1)

    items = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            storage_state=str(state_path),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        page.goto("https://member.bilibili.com/platform/home",
                  wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)

        if "passport" in page.url:
            print("⚠️ 登录态已过期,请重新运行: python sf_login.py bilibili")
            browser.close()
            sys.exit(1)

        pn = 1
        total = None
        while True:
            try:
                r = page.evaluate(
                    """async ({pn, ps, status}) => {
                        const u = `/x/web/archives?status=${encodeURIComponent(status)}`
                                + `&pn=${pn}&ps=${ps}&coop=1&interactive=1&t=${Date.now()}`;
                        const resp = await fetch(u, {credentials: 'include'});
                        return await resp.json();
                    }""",
                    {"pn": pn, "ps": ps, "status": status},
                )
            except Exception as e:
                print(f"  第 {pn} 页取数异常: {str(e)[:80]}")
                break

            if not r or r.get("code") != 0:
                print(f"  第 {pn} 页返回非 0: code={r.get('code') if r else 'None'} "
                      f"msg={r.get('message') if r else ''}")
                break

            d = r.get("data") or {}
            page_info = d.get("page") or {}
            if total is None:
                total = page_info.get("count")
                print(f"后台稿件总数: {total}")

            batch = d.get("arc_audits") or []
            batch += d.get("archives") or []
            if not batch:
                break

            for it in batch:
                a = it.get("Archive") if isinstance(it, dict) else None
                if a is None:
                    continue
                ptime = a.get("ptime") or 0
                items.append({
                    "bvid": a.get("bvid"),
                    "aid": a.get("aid"),
                    "title": a.get("title"),
                    "ptime": ptime,
                    "published_at": _ts_to_date(ptime) if ptime else None,
                    "ctime": a.get("ctime"),
                    "state": a.get("state"),
                    "state_desc": a.get("state_desc") or STATE_DESC.get(a.get("state"), ""),
                    "duration": a.get("duration"),
                    "typename": it.get("typename") or a.get("typename"),
                    "parent_tname": it.get("parent_tname"),
                    "tag": a.get("tag"),
                    "desc": a.get("desc"),
                    "stat": it.get("stat"),
                })

            if pages and pn >= pages:
                break
            if total is not None and len(items) >= total:
                break
            if len(batch) < ps:
                break
            pn += 1
            page.wait_for_timeout(800)  # 低频,避免触发风控

        browser.close()

    items.sort(key=lambda x: (x.get("ptime") or 0), reverse=True)
    return items


# ─── 自动补抓:列出全部稿件 → 与 DB 比对 → 抓未入库的 ─────────────────
def sync_missing(since: str = None, limit: int = 0, headless: bool = True,
                 dry_run: bool = False, include_nonpublic: bool = False,
                 only: list = None) -> dict:
    """把后台「未入库」的稿件全量抓回项目。

    since          只抓发布日 >= since 的稿件(YYYY-MM-DD),缺省不限
    limit          最多抓几条(0 = 全部)
    dry_run        只列出待抓清单,不真抓
    include_nonpublic  是否包含非「开放浏览」稿件(默认只抓公开的)
    only           指定 bvid 列表,只抓这些(用于定点补抓)
    """
    archives = list_archives(headless=headless)
    db = DB()
    have = {v["video_key"] for v in db.list_videos(platform="bilibili")}

    todo, skipped_state, already = [], [], 0
    for a in archives:
        bv = a.get("bvid")
        if not bv:
            continue
        if bv in have:
            already += 1
            continue
        if only and bv not in only:
            continue
        if not include_nonpublic and a.get("state_desc") != "开放浏览":
            skipped_state.append(a)
            continue
        if since and (a.get("published_at") or "") < since:
            continue
        todo.append(a)

    todo.sort(key=lambda x: (x.get("ptime") or 0))  # 由旧到新,便于趋势连续
    if limit:
        todo = todo[:limit]

    print(f"\n后台上共 {len(archives)} 条稿件 | 已入库 {already} 条 | "
          f"非公开跳过 {len(skipped_state)} 条 | 本次待抓 {len(todo)} 条")
    if skipped_state:
        print("  跳过的非公开稿件:")
        for a in skipped_state[:10]:
            print(f"    {a['published_at']}  {a['bvid']}  [{a['state_desc']}]  {a['title'][:34]}")
        if len(skipped_state) > 10:
            print(f"    ... 其余 {len(skipped_state) - 10} 条")

    if not todo:
        print("✅ 没有需要补抓的稿件")
        return {"total": len(archives), "already": already, "todo": 0,
                "ok": [], "failed": [], "skipped_state": len(skipped_state)}

    if dry_run:
        print("\n[dry-run] 待抓清单:")
        for a in todo:
            print(f"  {a['published_at']}  {a['bvid']}  {a['title'][:46]}")
        return {"total": len(archives), "already": already, "todo": len(todo),
                "ok": [], "failed": [], "skipped_state": len(skipped_state)}

    ok, failed = [], []
    for i, a in enumerate(todo, 1):
        print(f"\n{'=' * 68}")
        print(f"[{i}/{len(todo)}] {a['published_at']}  {a['bvid']}  {a['title'][:40]}")
        print("=" * 68)
        try:
            fetch_video(a["bvid"], headless=headless)
            ok.append(a["bvid"])
        except SystemExit as e:
            print(f"❌ {a['bvid']} 抓取失败(exit={e.code})")
            failed.append(a["bvid"])
        except Exception as e:
            print(f"❌ {a['bvid']} 抓取异常: {str(e)[:100]}")
            failed.append(a["bvid"])
        # 低频:每条之间随机延迟,避免触发风控
        if i < len(todo):
            delay = random.uniform(8, 15)
            print(f"   … 等待 {delay:.0f}s 后继续")
            time.sleep(delay)

    print(f"\n{'=' * 68}")
    print(f"补抓完成: 成功 {len(ok)} 条 / 失败 {len(failed)} 条")
    if failed:
        print("失败列表:", ", ".join(failed))
    return {"total": len(archives), "already": already, "todo": len(todo),
            "ok": ok, "failed": failed, "skipped_state": len(skipped_state)}


# ─── 主流程 ──────────────────────────────────────────────────────────
def fetch_video(bv_or_url: str, headless: bool = True):
    state_path = SESSION_DIR / "bilibili.json"
    if not state_path.exists():
        print("❌ 未找到登录态,请先运行: python sf_login.py bilibili")
        sys.exit(1)

    bvid = _pick_bvid(bv_or_url)
    db = DB()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            storage_state=str(state_path),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        interceptor = APIInterceptor(db, bvid)
        interceptor.attach(page)

        url = ARCHIVE_OVERVIEW_URL.format(bvid=bvid)
        print(f"打开稿件分析页: {url[:100]}")
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(4000)

        # 等数据加载(轮询直到 overview 接口返回)
        for _ in range(8):
            page.wait_for_timeout(2500)
            if interceptor.find("archive_diagnose/overview"):
                break

        # 主动 fetch 完播/跳出分析接口(analyze/graph,参数为 cid,需先取 cid)
        try:
            graph_ok = page.evaluate(
                """async (bvid) => {
                    const v = await (await fetch('https://api.bilibili.com/x/web-interface/view?bvid=' + bvid)).json();
                    const cid = (v.data && v.data.pages && v.data.pages[0]) ? v.data.pages[0].cid : null;
                    if (!cid) return 'no-cid';
                    const r = await fetch('/x/web/data/v2/archive/analyze/graph?cid=' + cid + '&tmid=&t=' + Date.now());
                    const d = await r.json();
                    return d.code === 0 ? 'ok' : 'code-' + d.code;
                }""",
                bvid,
            )
            print(f"完播分析接口: {graph_ok}")
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"完播分析接口跳过: {str(e)[:60]}")

        # 主动 fetch 全部指标维度的逐日趋势(play/fan/like/coin/fav/comment/dm/share)
        try:
            trend_ok = page.evaluate(
                """async (bvid) => {
                    const types = ['play','fan','like','coin','fav','comment','dm','share'];
                    const out = [];
                    for (const t of types) {
                        const r = await fetch('/x/web/data/archive_diagnose/trend?type=' + t + '&bvid=' + bvid + '&t=' + Date.now());
                        const d = await r.json();
                        out.push(t + ':' + (d.code === 0 ? 'ok' : 'code-' + d.code));
                    }
                    return out.join(',');
                }""",
                bvid,
            )
            print(f"趋势接口(8 维度): {trend_ok}")
            page.wait_for_timeout(2500)
        except Exception as e:
            print(f"趋势接口跳过: {str(e)[:60]}")

        # 主动 fetch 播放分析(互动率/播放来源/建议)
        try:
            pa_ok = page.evaluate(
                "async (bvid) => { const r = await fetch('/x/web/data/archive_diagnose/play_analyze?bvid=' + bvid + '&t=' + Date.now()); const d = await r.json(); return d.code === 0 ? 'ok' : 'code-' + d.code; }",
                bvid,
            )
            print(f"播放分析接口: {pa_ok}")
            page.wait_for_timeout(1500)
        except Exception as e:
            print(f"播放分析接口跳过: {str(e)[:60]}")

        if "passport" in page.url or "login" in page.url:
            print("⚠️ 登录态已过期,请重新运行: python sf_login.py bilibili")
            browser.close()
            sys.exit(1)

        # ── 解析 ──
        base = parse_view(interceptor)
        metrics, audience = parse_overview(interceptor)
        play_analyze = parse_play_analyze(interceptor)
        graph = parse_graph(interceptor)
        compare = parse_compare(interceptor)
        limit = parse_limit(interceptor)

        # 8 维度趋势(逐日 30 天 / 小时级 / 近 30 天长尾)
        trends = {}      # metric -> [(date, daily)]
        hour_trends = {} # metric -> [(datetime, daily)]
        last30_trends = {}
        for ttype, metric in TREND_TYPES.items():
            td = parse_trend(interceptor, ttype)
            if td:
                trends[metric] = td
            ht = parse_hour_trend(interceptor, ttype)
            if ht:
                hour_trends[metric] = ht
            l30 = parse_last30_trend(interceptor, ttype)
            if l30:
                last30_trends[metric] = l30

        # 合并基础+核心+完播指标
        all_metrics = {**base, **metrics}
        for k in ("finish_rate", "finish_rate_peer_avg", "avg_play_progress", "avg_duration"):
            if k in graph and graph[k] is not None:
                all_metrics[k] = graph[k]
        # 互动率/3s跳出率/播放来源(play_analyze 结构化)
        for k in ("interact_rate", "interact_viewer_rate", "interact_fan_rate",
                  "crash_rate", "crash_viewer_rate", "crash_fan_rate",
                  "interact_star", "crash_star", "fan_play_rate", "viewer_play_rate"):
            if k in play_analyze and play_analyze[k] is not None:
                all_metrics[k] = play_analyze[k]

        # TV 占比(污染判定用)
        pp = (audience or {}).get("play_proportion") or {}
        if pp:
            ott = pp.get("new_ott") or 0
            mob = pp.get("new_mobile") or 0
            pc = pp.get("new_pc") or 0
            total = ott + mob + pc + (pp.get("new_h5") or 0) + (pp.get("new_others") or 0)
            if total:
                all_metrics["tv_ratio"] = round(ott / total * 100, 1)
        if not all_metrics.get("views") and not all_metrics.get("likes"):
            print("⚠️ 未解析到核心指标。")
            print(f"   已拦截接口 {len(interceptor.responses)} 个,原文已存档可核查。")
            browser.close()
            sys.exit(1)

        # ── 入库 ──
        video_id = db.upsert_video(
            platform="bilibili",
            video_key=bvid,
            title=base.get("title"),
            published_at=base.get("published_at"),
            url=f"https://www.bilibili.com/video/{bvid}",
        )
        db.add_snapshot(video_id, "cumulative", all_metrics, source="api")

        # 逐日趋势(8 维度)+ 小时级 + 近30天长尾
        series = {}
        for metric, rows in trends.items():
            series[metric] = [(d, v, None) for d, v in rows]
        for metric, rows in hour_trends.items():
            series[f"hour_{metric}"] = [(d, v, None) for d, v in rows]
        for metric, rows in last30_trends.items():
            series[f"{metric}_last30"] = [(d, v, None) for d, v in rows]
        if series:
            db.bulk_add_metric_series(video_id, series)

        # 逐段退出曲线(原始数据,存 metric_series 便于查询;不做百分比换算)
        qc = graph.get("quit_curve") or []
        if qc:
            db.bulk_add_metric_series(video_id, {
                "quit_curve_20s": [(f"P{item.get('duration_key')}s", item.get("num", 0), None) for item in qc]
            })

        # 画像
        if audience:
            db.add_audience(video_id,
                            gender=audience.get("gender"),
                            age=audience.get("viewer_age"),
                            region=audience.get("viewer_area"),
                            source=audience.get("play_proportion"),
                            interest=audience.get("viewer_ty"),
                            extra={"audience_proportion": audience.get("audience_proportion"),
                                   "play_analyze": play_analyze,
                                   "graph": graph,
                                   "compare": compare,
                                   "limit": limit})

        # ── 输出摘要 ──
        print(f"\n✅ 已抓取 {bvid}")
        print(f"   标题: {all_metrics.get('title')}")
        print(f"   播放: {all_metrics.get('views')} | 点赞: {all_metrics.get('likes')} | 收藏: {all_metrics.get('favorites')}")
        print(f"   评论: {all_metrics.get('comments')} | 分享: {all_metrics.get('shares')} | 投币: {all_metrics.get('coins')} | 弹幕: {all_metrics.get('danmaku')}")
        print(f"   涨粉: {all_metrics.get('followers_gain')} | 取关: {all_metrics.get('unfollow')} | 平均时长: {all_metrics.get('avg_duration')}s")
        if all_metrics.get("finish_rate") is not None:
            print(f"   完播率: {all_metrics.get('finish_rate')}% (同题材均 {all_metrics.get('finish_rate_peer_avg')}%) | 平均进度: {all_metrics.get('avg_play_progress')}s")
        if graph.get("quit_curve"):
            print(f"   段退出曲线: {len(graph['quit_curve'])} 段(每 20s) | 3s跳出率: {all_metrics.get('crash_rate')}% ({all_metrics.get('crash_star')}星) | 互动率: {all_metrics.get('interact_rate')}%")
        n_daily = len(trends.get("plays", []))
        n_hour = len(hour_trends.get("plays", []))
        n_l30 = len(last30_trends.get("plays", []))
        print(f"   趋势维度: {len(trends)} 项(播放/转粉/点赞/投币/收藏/评论/弹幕/分享)")
        print(f"   逐日 {n_daily} 天 | 小时级 {n_hour} 小时 | 近30天长尾 {n_l30} 天(每维度)")
        if audience.get("play_proportion"):
            pp = audience["play_proportion"]
            ott = pp.get("new_ott") or 0
            mob = pp.get("new_mobile") or 0
            pc = pp.get("new_pc") or 0
            total = ott + mob + pc + (pp.get("new_h5") or 0) + (pp.get("new_others") or 0)
            tv_ratio = round(ott / total * 100, 1) if total else 0
            print(f"   播放端: TV {ott} ({tv_ratio}%) | 移动 {mob} | PC {pc}")
        print(f"   同题材对比: {len(compare)} 条")
        print(f"   接口存档: {len(interceptor.responses)} 个(原文全量入 api_archives)")
        print(f"   数据库: {db.path}")

        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SQ B站数据抓取器(全量)")
    ap.add_argument("--bv", help="BV号")
    ap.add_argument("--url", help="视频链接")
    ap.add_argument("--list", action="store_true",
                    help="列出创作中心全部稿件(JSON,按发布时间倒序),不抓取")
    ap.add_argument("--list-out", help="--list 结果写入该 JSON 文件")
    ap.add_argument("--sync", action="store_true",
                    help="自动补抓:列出后台全部稿件→与 DB 比对→抓未入库的")
    ap.add_argument("--since", help="--sync 时只抓发布日 >= 该日期(YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, default=0, help="--sync 最多抓几条(0=全部)")
    ap.add_argument("--bv-only", help="--sync 时只抓指定 BV(逗号分隔),用于定点补抓")
    ap.add_argument("--include-nonpublic", action="store_true",
                    help="--sync 时连非「开放浏览」稿件一起抓")
    ap.add_argument("--dry-run", action="store_true", help="--sync 只列清单不抓")
    ap.add_argument("--show", action="store_true", help="有头模式(调试)")
    args = ap.parse_args()

    if args.sync:
        only = [s.strip() for s in args.bv_only.split(",")] if args.bv_only else None
        res = sync_missing(since=args.since, limit=args.limit, headless=not args.show,
                           dry_run=args.dry_run, include_nonpublic=args.include_nonpublic,
                           only=only)
        sys.exit(1 if res["failed"] else 0)

    if args.list:
        result = list_archives(headless=not args.show)
        print(f"\n共列出 {len(result)} 条稿件")
        if args.list_out:
            out = Path(args.list_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已写入: {out}")
        else:
            for it in result:
                print(f"  {it['published_at']}  {it['bvid']}  [{it['state_desc']}]  {it['title']}")
        sys.exit(0)

    target = args.url or args.bv
    if not target:
        print("用法: python sf_bilibili.py --bv BV1XXXX 或 --url <链接>")
        print("      python sf_bilibili.py --list [--list-out FILE]   # 列出全部稿件")
        sys.exit(1)
    fetch_video(target, headless=not args.show)
