# -*- coding: utf-8 -*-
"""
SQ 平台登录器 (sf_login.py)
==================================
打开有头浏览器,引导用户扫码登录各平台创作后台,保存登录态 storage_state。
登录态保存到 data/fetcher/sessions/{platform}.json,抓取器复用。

用法:
  python sf_login.py <platform>     # platform: bilibili / douyin / xiaohongshu
"""

import sys

from playwright.sync_api import sync_playwright

# 路径统一从核心库取——禁止在本文件自行推算 ROOT。
# (历史 bug:此处曾用 parents[2] 得到 ~/data/fetcher/sessions,与抓取器读取的
#  ~/AQi-fetcher/data/fetcher/sessions 不一致,导致重新登录后会话存错位置、
#  抓取器仍用旧会话。2026-09-12 修复,见 VERSION_HISTORY v1.2.2)
from sf_core import SESSION_DIR

# 各平台创作中心登录地址
PLATFORM_LOGIN_URL = {
    "bilibili": "https://member.bilibili.com/platform/home",
    "douyin": "https://creator.douyin.com/creator-micro/content/manage",
    "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish",
}

# 登录成功标志(页面出现这些元素/URL 即认为已登录)
LOGIN_CHECK = {
    "bilibili": ("member.bilibili.com", ".stat-info, .nav-user"),
    "douyin": ("creator.douyin.com", ".semi-input, [class*=nav-user]"),
    "xiaohongshu": ("creator.xiaohongshu.com", "[class*=user]"),
}

# 各平台登录成功判定:
#   text_keys: 页面正文出现任一关键词(排除"扫码登录"字样)
#   cookie_keys: cookie 中出现任一键名(更可靠,尤其小红书)
LOGIN_RULES = {
    "bilibili": {
        "text_keys": ("内容管理", "作品管理", "创作中心", "数据概览", "视频管理"),
        "cookie_keys": ("SESSDATA", "DedeUserID"),
    },
    "douyin": {
        "text_keys": ("内容管理", "作品管理", "创作中心", "数据概览"),
        "cookie_keys": ("sessionid", "sessionid_ss"),
    },
    "xiaohongshu": {
        "text_keys": ("创作服务", "数据中心", "笔记管理", "发布笔记", "作品管理", "数据概览", "创作中心"),
        # 注意(2026-08-11 实测确认):
        #   - a1 / webId / gid 是设备标识 cookie(未登录也会生成),不能当登录标志!
        #   - web_session 是 www 普通网页版的登录 cookie,创作者平台(creator)不下发!
        #   - creator 平台真实登录标志: access-token-creator.xiaohongshu.com /
        #     x-user-id-creator.xiaohongshu.com / galaxy_creator_session_id
        #   - 登录成功后 URL 从 /login 跳转到 /new/home 或 /publish/...
        "cookie_keys": ("access-token-creator.xiaohongshu.com", "x-user-id-creator.xiaohongshu.com"),
    },
}


def login(platform: str, headless: bool = False, timeout_ms: int = 300_000):
    if platform not in PLATFORM_LOGIN_URL:
        print(f"未知平台: {platform} (可选: {list(PLATFORM_LOGIN_URL)})")
        sys.exit(1)

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    state_path = SESSION_DIR / f"{platform}.json"

    url = PLATFORM_LOGIN_URL[platform]
    url_hint, _ = LOGIN_CHECK[platform]

    print(f"== 登录 {platform} ==")
    print(f"浏览器即将打开: {url}")
    print("请在窗口中扫码/登录,登录成功后本脚本自动保存会话。")
    print(f"等待上限: {timeout_ms // 1000} 秒 (可 Ctrl+C 中断)\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        # 持久化上下文:登录后状态自动保存
        ctx = browser.new_context(
            storage_state=str(state_path) if state_path.exists() else None,
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)

        # 等待登录完成:轮询检查 cookie 登录标志 + URL 离开登录页
        logged_in = False
        rule = LOGIN_RULES[platform]

        # 小红书注意:未登录时 publish 页会先渲染(带"发布笔记"等文字),1~2秒后才
        # 重定向到 /login。因此页面文字判据不可用,必须:
        #   1) 出现 creator 专用登录 cookie(硬标志,见 LOGIN_RULES["xiaohongshu"]
        #      —— 是 access-token-creator.xiaohongshu.com / x-user-id-creator...
        #      **不是** www 版的 web_session,creator 平台根本不下发它)
        #   2) URL 离开 /login
        #   3) 事后实测(重新打开创作中心确认不跳登录页)
        for _ in range(timeout_ms // 3000):
            page.wait_for_timeout(3000)
            try:
                # 1) cookie 登录标志(最可靠)
                cookies = ctx.cookies()
                cookie_names = {c["name"] for c in cookies}
                has_cookie = any(k in cookie_names for k in rule["cookie_keys"])
                # 2) URL 判据
                cur = page.url
                url_ok = False
                if platform == "xiaohongshu":
                    # 必须同时有 web_session + 不在 /login
                    url_ok = has_cookie and "/login" not in cur and "creator.xiaohongshu.com" in cur
                elif platform == "douyin":
                    url_ok = has_cookie and "/passport" not in cur and "creator.douyin.com" in cur
                else:  # bilibili
                    txt = page.evaluate("() => document.body.innerText") or ""
                    url_ok = (has_cookie or (any(k in txt for k in rule["text_keys"]) and "扫码登录" not in txt))
                if url_ok:
                    logged_in = True
                    break
            except Exception:
                pass

        # 超时后交互式确认:用户可能其实已登录(如小红书页面文本特殊)
        if not logged_in:
            print("\n⚠️ 自动检测未判定为登录完成。")
            try:
                ans = input("如果你已经扫码/登录完成,输入 y 强制保存会话;否则直接回车退出: ").strip().lower()
                if ans in ("y", "yes", "是"):
                    logged_in = True
            except EOFError:
                pass

        # 事后实测验证:用当前上下文重新打开创作中心,确认不跳转登录页
        verified = False
        if logged_in:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                page.wait_for_timeout(4000)
                cur = page.url
                if platform == "xiaohongshu":
                    verified = "/login" not in cur and "creator.xiaohongshu.com" in cur
                elif platform == "douyin":
                    verified = "/passport" not in cur and "creator.douyin.com" in cur
                else:
                    verified = True
            except Exception:
                verified = False

        if logged_in and verified:
            ctx.storage_state(path=str(state_path))
            print(f"\n✅ 登录成功,会话已保存: {state_path}")
            print("之后抓取数据无需再登录(过期后重新运行本命令即可)。")
        elif logged_in and not verified:
            print("\n⚠️ 检测到登录标志但实测仍跳转登录页,会话未保存。")
            print("请重新运行登录命令,并确保扫码后页面停留在创作中心。")
        else:
            print("\n⚠️ 未保存会话。请重新运行登录命令完成扫码。")
            print("部分平台(如小红书)可能需手动操作,请确认页面已进入创作中心。")

        browser.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python sf_login.py <bilibili|douyin|xiaohongshu>")
        sys.exit(1)
    login(sys.argv[1])
