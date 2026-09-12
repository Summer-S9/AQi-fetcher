# -*- coding: utf-8 -*-
"""
SF 会话探活检查器 (sf_check_session.py)
========================================
检查各平台登录会话是否仍然有效——**不只看 cookie 过期时间,而是真实打开
创作后台验证**(cookie 有效期不等于服务端还认)。

判定链路(三级):
  1. cookie 层:解析 storage_state 中关键 cookie 的 expires,报告已过期/临期
  2. 页面层:带登录态打开创作后台,检测是否被重定向到登录页
  3. 接口层:调用需登录的接口,看返回码/登录标志(最权威)

用法:
  python sf_check_session.py                 # 检查全部平台
  python sf_check_session.py bilibili        # 只查 B站
  python sf_check_session.py --show          # 有头模式(可见浏览器)
  python sf_check_session.py --no-live       # 只查 cookie 时间,不打开浏览器
"""

import argparse
import json
import sys
import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from sf_core import SESSION_DIR

# 各平台检查配置
#   home:         创作后台入口
#   login_marks:  URL 中出现即判定"被踢回登录页"
#   probe:        需登录的接口(用页面 fetch 调用),返回 (url, 判定函数说明)
CHECKS = {
    "bilibili": {
        "home": "https://member.bilibili.com/platform/home",
        "login_marks": ("passport.bilibili.com", "/login"),
        "cookie_keys": ("SESSDATA", "DedeUserID", "bili_jct"),
        "probe": "https://api.bilibili.com/x/web-interface/nav",
    },
    "douyin": {
        "home": "https://creator.douyin.com/creator-micro/content/manage",
        "login_marks": ("/passport", "/login", "sso.douyin.com"),
        "cookie_keys": ("sessionid", "sessionid_ss", "passport_csrf_token"),
        "probe": "https://creator.douyin.com/creator-micro/content/manage",
    },
    "xiaohongshu": {
        "home": "https://creator.xiaohongshu.com/publish/publish",
        "login_marks": ("/login",),
        "cookie_keys": ("access-token-creator.xiaohongshu.com",
                        "x-user-id-creator.xiaohongshu.com",
                        "galaxy_creator_session_id"),
        "probe": None,
    },
}


def check_cookies(platform: str, state_path: Path) -> dict:
    """解析会话文件,cookie 层判定。"""
    out = {"exists": state_path.exists(), "file_mtime": None,
           "total": 0, "keys": [], "expired": [], "soon": [], "session_only": []}
    if not state_path.exists():
        return out
    st = state_path.stat()
    out["file_mtime"] = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    try:
        data = json.loads(state_path.read_text())
    except Exception as e:
        out["error"] = f"会话文件解析失败: {e}"
        return out

    cookies = data.get("cookies") or []
    out["total"] = len(cookies)
    now = datetime.datetime.now().timestamp()
    keys = CHECKS[platform]["cookie_keys"]

    # 只看主域名下的关键 cookie(忽略 .bilibili.cn/.biligame.com 等副本)
    for c in cookies:
        name = c.get("name")
        if name not in keys:
            continue
        dom = c.get("domain") or ""
        exp = c.get("expires")
        has_exp = isinstance(exp, (int, float)) and exp > 0
        exp_s = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d %H:%M") if has_exp else "会话级(关闭浏览器失效)"
        rec = {"name": name, "domain": dom, "expires": exp_s}
        if has_exp:
            left_days = (exp - now) / 86400
            rec["left_days"] = round(left_days, 1)
            if left_days < 0:
                out["expired"].append(rec)
            elif left_days < 7:
                out["soon"].append(rec)
        else:
            out["session_only"].append(rec)
        out["keys"].append(rec)
    return out


def check_live(platform: str, state_path: Path, headless: bool = True) -> dict:
    """带登录态真实打开后台,页面层 + 接口层判定。"""
    cfg = CHECKS[platform]
    out = {"opened": False, "final_url": None, "redirected_to_login": None,
           "page_ok": None, "probe_result": None, "user": None, "error": None}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(
            storage_state=str(state_path) if state_path.exists() else None,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        captured = {}

        # 拦截关键接口,确认是否真的拿到后台数据
        def on_response(resp):
            u = resp.url
            if platform == "bilibili" and "member.bilibili.com/x/web/data" in u:
                if "archive" in u or "overview" in u:
                    captured.setdefault("bili_data", u)
            if platform == "bilibili" and "archive/overview" in u:
                captured.setdefault("bili_overview", u)

        page.on("response", on_response)

        try:
            page.goto(cfg["home"], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(6000)
            out["opened"] = True
            out["final_url"] = page.url

            low = page.url
            hit = [m for m in cfg["login_marks"] if m in low]
            out["redirected_to_login"] = bool(hit)

            # 页面文字辅助判定
            try:
                txt = page.evaluate("() => document.body.innerText") or ""
                out["page_has_login_text"] = ("扫码登录" in txt or "登录后" in txt[:500])
                out["page_text_head"] = txt[:120].replace("\n", " ")
            except Exception:
                pass

            # ── 接口层:最权威 ──
            if platform == "bilibili":
                r = page.evaluate(
                    """async () => {
                        const r = await fetch('https://api.bilibili.com/x/web-interface/nav', {credentials:'include'});
                        const d = await r.json();
                        return {code: d.code, isLogin: (d.data||{}).isLogin, uname: (d.data||{}).uname, mid: (d.data||{}).mid};
                    }"""
                )
                out["probe_result"] = r
                out["user"] = r.get("uname")
                out["page_ok"] = bool(r.get("isLogin"))
            else:
                out["page_ok"] = not out["redirected_to_login"]
                if captured:
                    out["probe_result"] = captured

        except Exception as e:
            out["error"] = str(e)[:200]
        finally:
            browser.close()
    return out


def report(platform: str, headless: bool = True, live: bool = True):
    state_path = SESSION_DIR / f"{platform}.json"
    print("=" * 68)
    print(f"平台: {platform}    会话文件: {state_path}")
    print("=" * 68)

    ck = check_cookies(platform, state_path)
    if not ck["exists"]:
        print("❌ 会话文件不存在 — 需登录: python sf_login.py " + platform)
        return False
    print(f"文件修改时间: {ck['file_mtime']}   cookie 总数: {ck['total']}")

    print("\n[一级] cookie 有效期")
    if ck.get("error"):
        print("  " + ck["error"])
    if not ck["keys"]:
        print("  ⚠️ 未找到任何关键登录 cookie — 几乎可以确定会话无效")
    for r in ck["keys"]:
        flag = ""
        if r in ck["expired"]:
            flag = "  ❌ 已过期"
        elif r in ck["soon"]:
            flag = f"  ⚠️ 仅剩 {r.get('left_days')} 天"
        elif "left_days" in r:
            flag = f"  ✅ 剩 {r['left_days']} 天"
        print(f"  {r['name']:<40} {r['expires']}{flag}")
    for r in ck["session_only"]:
        print(f"  {r['name']:<40} {r['expires']}")

    cookie_verdict = None
    if ck["keys"] and not ck["expired"] and not ck["session_only"]:
        cookie_verdict = "cookie 未过期"
    elif ck["expired"]:
        cookie_verdict = "cookie 已过期"

    if not live:
        print(f"\n[结论] (仅 cookie 层) {cookie_verdict or '无法判定'}")
        return bool(cookie_verdict == "cookie 未过期")

    print("\n[二级+三级] 真实打开创作后台 + 调需登录接口")
    lv = check_live(platform, state_path, headless=headless)
    if lv["error"]:
        print(f"  ⚠️ 检查异常: {lv['error']}")
        return False
    print(f"  打开: {CHECKS[platform]['home']}")
    print(f"  最终 URL: {lv['final_url']}")
    if lv.get("page_text_head"):
        print(f"  页面开头: {lv['page_text_head'][:90]}")
    print(f"  被重定向到登录页: {'是 ❌' if lv['redirected_to_login'] else '否 ✅'}")
    if lv["probe_result"]:
        print(f"  接口探测: {lv['probe_result']}")
    if lv.get("user"):
        print(f"  当前账号: {lv['user']}")

    ok = bool(lv["page_ok"]) and not lv["redirected_to_login"]
    print()
    if ok:
        print(f"✅ [结论] {platform} 会话有效 — 可正常进入后台并取数")
    else:
        print(f"❌ [结论] {platform} 会话无效/已过期 — 需重新登录: python sf_login.py {platform}")
    return ok


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SF 会话探活检查(真实打开后台验证)")
    ap.add_argument("platform", nargs="?", help="bilibili / douyin / xiaohongshu,缺省检查全部")
    ap.add_argument("--show", action="store_true", help="有头模式")
    ap.add_argument("--no-live", action="store_true", help="只查 cookie 时间,不开浏览器")
    args = ap.parse_args()

    targets = [args.platform] if args.platform else list(CHECKS)
    results = {}
    for pf in targets:
        if pf not in CHECKS:
            print(f"未知平台: {pf}")
            continue
        results[pf] = report(pf, headless=not args.show, live=not args.no_live)
        print()

    print("=" * 68)
    print("汇总")
    for pf, ok in results.items():
        print(f"  {pf:<14} {'✅ 有效' if ok else '❌ 需重新登录'}")
