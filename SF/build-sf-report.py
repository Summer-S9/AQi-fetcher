# -*- coding: utf-8 -*-
"""
build-sq-fetch-report.py — B站数据采集成果展示报告生成器
============================================================
从 data/sq_metrics.db 读取指定 BV 的全量采集数据,生成一份 Word 展示文档,
让创作者直观看到 SF 采集到了什么(核心指标/完播/跳出曲线/逐日趋势/画像/接口存档)。

用法:
  python build-sq-fetch-report.py --bv BV1rnaJzpEFv [--out 输出路径]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from report_lib_v4 import (
    THEME, add_para, add_heading, add_subheading, add_table,
    add_callout, add_gray_note, add_report_header,
)
from sf_core import DB


def _num(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        if v == int(v):
            return f"{int(v):,}"
        return f"{v:,.2f}"
    return f"{int(v):,}" if isinstance(v, int) else str(v)


def _fmt_bucket_date(rows, day7_date=None):
    """逐日播放 → 摘要文本。"""
    if not rows:
        return "无数据", []
    total = sum(r["daily_value"] for r in rows)
    peak = max(rows, key=lambda r: r["daily_value"])
    first = rows[0]
    # T+7 累计(发布日起 7 天)
    t7 = sum(r["daily_value"] for r in rows[:7])
    lines = [
        f"共 {len(rows)} 天,累计增量 {_num(total)}",
        f"发布首日 {first['stat_date']}: +{_num(first['daily_value'])}",
        f"峰值 {peak['stat_date']}: +{_num(peak['daily_value'])}",
        f"T+7 累计(前 7 天): {_num(t7)}",
        f"末段日均(衰减后): {_num(sum(r['daily_value'] for r in rows[-3:]) // 3)}",
    ]
    return "\n".join(lines), rows


def build(bv: str, out_path: str):
    db = DB()
    v = db.get_video("bilibili", bv)
    if not v:
        print(f"❌ 数据库中无 {bv},请先运行 sf_bilibili.py --bv {bv}")
        sys.exit(1)

    s = db.get_snapshots(v["id"])
    snap = dict(s[-1]) if s else {}
    plays = db.get_metric_series(v["id"], "daily_plays")
    fans = db.get_metric_series(v["id"], "daily_followers")
    aud = db.get_audience(v["id"])
    aud_latest = aud[-1] if aud else None
    extra = json.loads(aud_latest["extra_json"]) if aud_latest and aud_latest["extra_json"] else {}
    graph = extra.get("graph", {})
    compare = extra.get("compare", [])
    archives = db.list_api_archives(bv)
    arch_urls = sorted(set(a["url"].replace("https://member.bilibili.com", "").split("?")[0] for a in archives))

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Inches(0.7)
    section.bottom_margin = Inches(0.7)
    section.left_margin = Inches(0.8)
    section.right_margin = Inches(0.8)

    add_report_header(
        doc,
        video_title=v["title"],
        meta=f"BV {bv} · 抓取时间 {snap.get('captured_at', '—') if hasattr(snap, 'get') else '—'} · 数据源: B站创作中心(接口抓取)",
        title="B站数据采集成果展示",
    )

    # ── 1. 稿件基本信息 ──
    add_heading(doc, "一、稿件基本信息")
    add_table(doc, [
        ["字段", "值"],
        ["视频标题", v["title"]],
        ["BV 号", bv],
        ["发布时间", v["published_at"]],
        ["视频链接", v["url"]],
        ["视频时长", f"{int(graph.get('danmu_info', {}).get('duration', 0) or 0)} 秒" if graph.get("danmu_info") else "—"],
    ], [2.0, 4.5])

    # ── 2. 核心指标总览 ──
    add_heading(doc, "二、核心指标总览")
    add_table(doc, [
        ["指标", "数值", "说明"],
        ["播放量", _num(snap.get("views")), "累计播放"],
        ["点赞", _num(snap.get("likes")), ""],
        ["收藏", _num(snap.get("favorites")), "收藏/点赞比: " + (f"{snap['favorites']/snap['likes']:.2f}" if snap.get("favorites") and snap.get("likes") else "—")],
        ["评论", _num(snap.get("comments")), ""],
        ["分享", _num(snap.get("shares")), ""],
        ["投币", _num(snap.get("coins")), ""],
        ["弹幕", _num(snap.get("danmaku")), ""],
        ["涨粉", _num(snap.get("followers_gain")), "净增粉丝"],
        ["取关", _num(snap.get("unfollow")), "手动采集表里没有这项"],
        ["播放端 TV 占比", (str(snap.get("tv_ratio")) + "%" if snap.get("tv_ratio") is not None else "—"), "SQ 污染判定(TV>85%)的关键指标"],
        ["互动率", (str(snap.get("interact_rate")) + "%" if snap.get("interact_rate") is not None else "—"),
         f"游客 {snap.get('interact_viewer_rate')}% · 粉丝 {snap.get('interact_fan_rate')}% · {snap.get('interact_star')}星"],
        ["播放来源", (f"游客 {snap.get('viewer_play_rate')}% · 粉丝 {snap.get('fan_play_rate')}%" if snap.get("viewer_play_rate") is not None else "—"),
         "粉丝播放占比低 = 主要靠推荐/搜索流量"],
    ], [2.2, 1.6, 2.7])

    # ── 3. 完播与观看表现 ──
    add_heading(doc, "三、完播与观看表现")
    qi = graph.get("quit_info") or {}
    di = graph.get("duration_info") or {}
    add_table(doc, [
        ["指标", "数值", "参照"],
        ["完播率", f"{snap.get('finish_rate')}%" if snap.get("finish_rate") is not None else "—",
         f"同题材均值 {graph.get('finish_rate_peer_avg')}%"],
        ["3 秒跳出率", f"{snap.get('crash_rate')}%" if snap.get("crash_rate") is not None else "—",
         f"{snap.get('crash_star')}星 · 游客 {snap.get('crash_viewer_rate')}% · 粉丝 {snap.get('crash_fan_rate')}%"],
        ["平均观看时长", f"{snap.get('avg_duration')} 秒" if snap.get("avg_duration") else "—",
         f"{di.get('avg_play_time', '—')}"],
        ["平均播放进度", f"{graph.get('avg_play_progress')} 秒" if graph.get("avg_play_progress") is not None else "—",
         f"视频 {di.get('quit_duration', '—')} 秒"],
        ["观看星级", f"{qi.get('pass_star')} 星" if qi.get("pass_star") is not None else "—",
         "B站 5 星制"],
    ], [2.2, 1.8, 2.5])

    # 段退出曲线(原始数据,不做百分比换算——B站官方 3s 跳出率与段退出分母口径不同)
    qc = graph.get("quit_curve") or []
    if qc:
        add_subheading(doc, "段退出曲线(每 20 秒段退出人数,原始数据)")
        rows = [["时间段", "段退出人数"]]
        for item in qc:
            dk = item.get("duration_key")
            rows.append([f"{dk}s", _num(item.get("num", 0))])
        add_table(doc, rows, [2.2, 2.2])
        add_gray_note(doc, "注: 段退出为原始计数(接口 viewer_quit);留存判定请用官方「3 秒跳出率」,自算留存率因分母口径不同会失真。")

    # ── 4. 播放端分布 ──
    add_heading(doc, "四、播放端分布")
    pp = json.loads(aud_latest["source_json"]) if aud_latest and aud_latest["source_json"] else {}
    if pp:
        total = sum(pp.get(k, 0) for k in ("new_ott", "new_mobile", "new_pc", "new_h5", "new_others"))
        add_table(doc, [
            ["播放端", "播放量", "占比"],
            ["TV/OTT", _num(pp.get("new_ott")), f"{pp.get('new_ott',0)/total*100:.1f}%" if total else "—"],
            ["移动端", _num(pp.get("new_mobile")), f"{pp.get('new_mobile',0)/total*100:.1f}%" if total else "—"],
            ["PC 端", _num(pp.get("new_pc")), f"{pp.get('new_pc',0)/total*100:.1f}%" if total else "—"],
            ["H5 端", _num(pp.get("new_h5")), f"{pp.get('new_h5',0)/total*100:.1f}%" if total else "—"],
            ["其他", _num(pp.get("new_others")), f"{pp.get('new_others',0)/total*100:.1f}%" if total else "—"],
        ], [2.2, 1.8, 1.5])
        add_gray_note(doc, "注: TV 占比 ≥85% 时,按 SQ 判定口径属 scale_contaminated(事件型 TV 推流),不计入 organic_core 判定。")

    # ── 5. 逐日趋势(完整明细) ──
    add_heading(doc, "五、逐日趋势(8 维度 × 30 天,完整逐日明细)")
    metric_names = [
        ("daily_plays", "播放"), ("daily_followers", "转粉"), ("likes", "点赞"), ("coins", "投币"),
        ("favorites", "收藏"), ("comments", "评论"), ("danmaku", "弹幕"), ("shares", "分享"),
    ]
    # 聚合为"日期 × 指标"大表(保留每一天原始增量)
    by_metric = {}
    for mkey, _ in metric_names:
        rows = db.get_metric_series(v["id"], mkey)
        if rows:
            by_metric[mkey] = {r["stat_date"]: r["daily_value"] for r in rows}
    if by_metric:
        dates = sorted(set().union(*[set(m.keys()) for m in by_metric.values()]))
        hdr = ["日期", "发布天数"] + [label for _, label in metric_names]
        rows = [hdr]
        for i, d in enumerate(dates, 1):
            rows.append([d, f"D{i}"] + [_num(by_metric[m].get(d)) for m, _ in metric_names])
        add_table(doc, rows, [1.5, 1.1] + [0.9] * len(metric_names))
        add_gray_note(doc, "说明: 每天数值为「当日新增增量」(原始数据);D1 = 发布当天。累计值由下游按需计算,不在此聚合。")

    # 发布后小时级明细(前 48 小时,完整)
    hour_metrics = [("hour_plays", "播放"), ("hour_likes", "点赞"), ("hour_comments", "评论"),
                    ("hour_danmaku", "弹幕"), ("hour_favorites", "收藏"), ("hour_coins", "投币"),
                    ("hour_shares", "分享")]
    by_hour = {}
    for mkey, _ in hour_metrics:
        rows = db.get_metric_series(v["id"], mkey)
        if rows:
            by_hour[mkey] = {r["stat_date"]: r["daily_value"] for r in rows}
    if by_hour:
        add_subheading(doc, "发布后小时级明细(前 48 小时,完整)")
        h_times = sorted(set().union(*[set(m.keys()) for m in by_hour.values()]))
        hdr = ["时间", "发布后"] + [label for _, label in hour_metrics]
        rows = [hdr]
        for i, t in enumerate(h_times, 1):
            rows.append([t, f"H{i}"] + [_num(by_hour[m].get(t)) for m, _ in hour_metrics])
        add_table(doc, rows, [1.6, 1.1] + [0.9] * len(hour_metrics))

    # 近 30 天长尾明细(完整)
    last30_metrics = [("plays_last30", "播放"), ("likes_last30", "点赞"), ("favorites_last30", "收藏"),
                      ("comments_last30", "评论"), ("danmaku_last30", "弹幕"), ("coins_last30", "投币"),
                      ("shares_last30", "分享"), ("followers_last30", "转粉")]
    by_l30 = {}
    for mkey, _ in last30_metrics:
        rows = db.get_metric_series(v["id"], mkey)
        if rows:
            by_l30[mkey] = {r["stat_date"]: r["daily_value"] for r in rows}
    if by_l30:
        add_subheading(doc, "近 30 天长尾明细(发布数月后,完整逐日)")
        l_dates = sorted(set().union(*[set(m.keys()) for m in by_l30.values()]))
        hdr = ["日期"] + [label for _, label in last30_metrics]
        rows = [hdr]
        for d in l_dates:
            rows.append([d] + [_num(by_l30[m].get(d)) for m, _ in last30_metrics])
        add_table(doc, rows, [1.5] + [1.0] * len(last30_metrics))

    # ── 6. 观众画像 ──
    add_heading(doc, "六、观众画像(近 30 天抽样)")
    if aud_latest:
        gender = json.loads(aud_latest["gender_json"]) if aud_latest["gender_json"] else {}
        age = json.loads(aud_latest["age_json"]) if aud_latest["age_json"] else {}
        region = json.loads(aud_latest["region_json"]) if aud_latest["region_json"] else []
        interest = json.loads(aud_latest["interest_json"]) if aud_latest["interest_json"] else []
        g_total = sum(gender.values()) or 1
        a_total = sum(age.values()) or 1
        age_labels = {"age_one": "1-17", "age_two": "18-24", "age_three": "25-34", "age_four": "35+"}
        add_table(doc, [
            ["维度", "分布"],
            ["性别", f"女 {gender.get('female',0)/g_total*100:.1f}% · 男 {gender.get('male',0)/g_total*100:.1f}%"],
            ["年龄", " · ".join(f"{age_labels.get(k, k)} {v/a_total*100:.1f}%" for k, v in age.items())],
        ], [1.6, 4.9])
        # 地区完整明细:展示占比(与 B站后台一致),count 为样本计数
        r_total = sum(r.get("count", 0) for r in region) or 1
        add_subheading(doc, "观众地区(完整,占比口径)")
        add_table(doc, [["地区", "占比", "样本计数"]] +
                  [[r.get("location", "?"), f"{r.get('count',0)/r_total*100:.1f}%", _num(r.get("count", 0))] for r in region],
                  [2.2, 1.4, 1.6])
        # 兴趣标签完整明细
        add_subheading(doc, "兴趣标签(完整,覆盖人次)")
        add_table(doc, [["标签", "覆盖人次"]] +
                  [[t.get("tag_name", "?"), _num(t.get("count", 0))] for t in interest],
                  [2.2, 1.6])
        add_gray_note(doc, "口径说明: 观众画像基于「近 30 天抽样样本」统计——地区/性别/年龄展示为占比(样本计数合计 ≠ 总播放量,与 B站后台一致);兴趣标签为覆盖人次(一人可命中多个标签,可超过总播放)。")
    ap = extra.get("audience_proportion")
    if ap:
        add_gray_note(doc, f"粉丝/游客占比: {json.dumps(ap, ensure_ascii=False)[:120]}")

    # ── 7. 弹幕互动 ──
    dm = graph.get("danmu_info") or {}
    dm_list = dm.get("list") or []
    if dm_list:
        add_heading(doc, "七、弹幕互动(投票题)")
        for item in dm_list[:3]:
            opts = " · ".join(f"{o.get('opt_title')} {o.get('opt_cnt')}票" for o in (item.get("options") or []))
            add_para(doc, f"【{item.get('title','')}】参与 {item.get('count',0)} 人 | {opts}", size=10.5, after=6)

    # ── 8. 同题材对比 ──
    add_heading(doc, "八、同题材对比(近 10 条同类稿件)")
    if compare:
        rows = [["标题", "播放", "点赞", "收藏"]]
        for c in compare[:10]:
            st = c.get("stat") or {}
            rows.append([
                (c.get("title") or "")[:22],
                _num(st.get("view")),
                _num(st.get("like")),
                _num(st.get("favorite")),
            ])
        add_table(doc, rows, [3.2, 1.2, 1.0, 1.1])
    else:
        add_para(doc, "无同题材对比数据", size=10.5)

    # ── 9. 接口存档 ──
    add_heading(doc, "九、接口存档(全量原始数据)")
    add_para(doc, f"共拦截存档 {len(archives)} 条接口响应原文,涉及 {len(arch_urls)} 个接口——平台返回的所有数据均已留存,未来加新字段也不丢。", size=10.5)
    add_table(doc, [["接口", ""]] + [[u, ""] for u in arch_urls], [6.5, 0.0])

    # ── 10. 与手动采集对比 ──
    add_heading(doc, "十、相比手动采集:多了什么 / 少了什么")
    add_table(doc, [
        ["", "说明"],
        ["✅ 多: 逐日播放/转粉 30 天", "之前 T+7、首周 vs 长尾分析全靠手动抄"],
        ["✅ 多: 播放端 TV/移动/PC 分布", "SQ 污染判定关键,之前每次手动抄"],
        ["✅ 多: 完播率/平均时长/平均进度/跳出曲线", "B站后台才有,手动表里没有"],
        ["✅ 多: 观众画像(性别/年龄/地区/兴趣)", "手动完全没采"],
        ["✅ 多: 取关数/同题材对比/接口原文存档", "手动表没有"],
        ["⚠️ 少: 暂无", "B站侧手动表 9 项已全部覆盖(超集)"],
    ], [2.8, 3.7])

    add_para(doc, "", after=4)
    add_para(doc, "—— 本报告由 SF 采集系统自动生成 ——", size=9, color=THEME["gray"], align=WD_ALIGN_PARAGRAPH.CENTER)

    doc.save(out_path)
    print(f"✅ 报告已生成: {out_path}")
    print(f"   稿件: {v['title']} | 快照 {len(s)} 条 | 接口存档 {len(archives)} 条")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--bv", default="BV1rnaJzpEFv")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[2] / "UP" / "_sq_fetch_采集成果展示.docx"))
    args = ap.parse_args()
    build(args.bv, args.out)
