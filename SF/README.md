# SF (Super Fetcher) v1.0 — 采集器代码

AQi-channel 的独立数据采集项目。**独立工作区**,与 AQi-channel 盲测项目物理隔离,
通过导出快照单向供给数据,避免盲测/复盘 agent 污染。
**项目总览请读上级 `../README.md`,行为规范请读 `../AGENTS.md`,版本历史见 `../VERSION_HISTORY.md`。**

## 工作区结构

```
~/AQi-fetcher/
├── SF/                    # SF v1.0 采集器代码(本目录)
│   ├── sf_core.py         # 核心库:DB 连接/标准化/快照/T7 判定/导出
│   ├── sf_fetch.py        # CLI 入口(login/fetch/status/export)
│   ├── sf_login.py        # 三平台登录(浏览器扫码,保存会话)
│   ├── sf_bilibili.py     # B站抓取器
│   ├── sf_douyin.py       # 抖音抓取器
│   ├── sf_xiaohongshu.py  # 小红书抓取器(列表+深度)
│   ├── xhs_audit.py       # 小红书全量校验(DB vs 原始存档)
│   ├── xhs_reparse.py     # 小红书重解析(从存档恢复)
│   ├── sf_push.py         # 导出快照并推送主项目(唯一出口)
│   ├── build-sf-report.py # 采集成果 Word 报告生成器
│   └── report_lib_v4.py   # Word 视觉库(复用主项目)
├── data/
│   ├── sq_metrics.db      # 主数据库(真实后台数据,只在本项目读写)
│   └── fetcher/sessions/  # 三平台登录会话
├── export/                # 快照输出目录(带时间戳)
├── README.md              # 项目总览
├── AGENTS.md              # agent 行为规范
└── VERSION_HISTORY.md     # 版本迭代报告
```

## 命名规范

- 采集器统一命名 **SF (Super Fetcher) v1.0**
- 模块前缀 `sf_`,历史名称 sq_fetch_* 已废弃
- 审计脚本保留 xhs_ 前缀(专项工具)

## 隔离原则(重要)

1. **AQi-channel 内不得存在 sq_metrics.db 实时库**,只能有导出快照。
2. **导出快照带时间戳**(如 `sq_metrics_20260812_2216.json`),文件名即审计痕迹。
3. **盲测/复盘 agent 视野内不允许出现真实后台数据**;主项目报告只能引用快照。
4. 如需更新主项目数据:在本项目跑 `sf_push.py` 导出 → 自动拷入 AQi-channel/data/fetcher/snapshots/ → 主项目使用快照。

## 用法(在 SF/ 目录内执行)

```bash
cd ~/AQi-fetcher/SF

# 登录(浏览器扫码)
python sf_fetch.py login xiaohongshu

# 抓取
python sf_fetch.py fetch bilibili --bv BV1XXXX
python sf_douyin.py --id <作品ID> | --all --pages N
python sf_xiaohongshu.py --all --pages 40
python sf_xiaohongshu.py --deep --limit 40

# 校验/重解析
python xhs_audit.py
python xhs_reparse.py

# 导出快照并推送主项目
python sf_push.py
```

## 数据流

```
平台后台 → SF 抓取 → sq_metrics.db(独立工作区)
                          ↓ export(带时间戳快照)
                    AQi-channel/data/fetcher/*.json/xlsx(主项目只读快照)
                          ↓
                    报告/复盘(仅用快照,不触实时库)
```
