# SF (Super Fetcher) v1.2 — 采集器代码

AQi-channel 的独立数据采集项目。**独立工作区**,与 AQi-channel 盲测项目物理隔离,
通过按月×平台分目录的交付快照单向供给数据,避免盲测/复盘 agent 污染。
**项目总览请读上级 `../README.md`,行为规范请读 `../AGENTS.md`,版本历史见 `../VERSION_HISTORY.md`。**

## 工作区结构

```
~/AQi-fetcher/
├── SF/                    # SF v1.2 采集器代码(本目录)
│   ├── sf_core.py         # 核心库:DB/标准化/快照/T7/资产读写(write_asset/load_asset)/按月导出
│   ├── sf_fetch.py        # CLI 入口(login/fetch/status/export 本地归档)
│   ├── sf_login.py        # 三平台登录(浏览器扫码,保存会话)
│   ├── sf_check_session.py # 会话探活(三级: cookie 时间→真实打开后台→调接口)
│   ├── sf_bilibili.py     # B站抓取器(单抓 --bv / 列表 --list / 补抓 --sync)
│   ├── sf_douyin.py       # 抖音抓取器
│   ├── sf_xiaohongshu.py  # 小红书抓取器(列表+深度)
│   ├── bili_audit.py      # B站全量校验(DB vs 接口原文,54 项/条)
│   ├── xhs_audit.py       # 小红书全量校验(DB vs 资产文件)
│   ├── xhs_reparse.py     # 小红书重解析(从资产文件恢复)
│   ├── sf_push.py         # 按月×平台导出并推送主项目(唯一出口)
│   ├── sf_migrate_assets.py # 一次性存量迁移(资产落盘,仅 v1.2.0 用过)
│   ├── sf_migrate_metric_names.py # 一次性指标命名归一(仅 v1.3.0 用过)
│   ├── build-sf-report.py # 采集成果 Word 报告生成器
│   └── report_lib_v4.py   # Word 视觉库(复用主项目)
├── data/
│   ├── sq_metrics.db      # 主数据库(真实后台数据,已剥离原始全文,约 4MB)
│   ├── assets/            # 独立资产:原始接口响应全文,按 {YYYY_MM}_{platform}/ 归档
│   ├── backup/            # DB 版本备份
│   └── fetcher/sessions/  # 三平台登录会话
├── export/                # 交付物:{YYYY_MM}_{platform}/ 分目录
├── README.md              # 项目总览
├── AGENTS.md              # agent 行为规范
└── VERSION_HISTORY.md     # 版本迭代报告
```

## 命名规范

- 采集器统一命名 **SF (Super Fetcher) v1.2**
- 模块前缀 `sf_`,历史名称 sq_fetch_* 已废弃
- 审计脚本保留 xhs_ 前缀(专项工具)

## 隔离原则(重要)

1. **AQi-channel 内不得存在 sq_metrics.db 实时库**,只能有导出快照。
2. **交付快照按月×平台分目录**(如 `2026_09_bilibili/`),目录内带 `manifest.json` 清单,
   文件名/清单即审计痕迹(记录数据截止时间)。
3. **盲测/复盘 agent 视野内不允许出现真实后台数据**;主项目报告只能引用快照。
4. 如需更新主项目数据:在本项目跑 `sf_push.py --month YYYY-MM` 导出 →
   自动拷入 AQi-channel/data/fetcher/snapshots/{YYYY_MM}_{platform}/ → 主项目使用快照。

## 用法(在 SF/ 目录内执行)

```bash
cd ~/AQi-fetcher/SF

# 登录(浏览器扫码)
python sf_fetch.py login xiaohongshu

# 会话探活(抓取前先跑,确认登录态没失效)
python sf_check_session.py              # 三个平台都查
python sf_check_session.py bilibili     # 只查一个
python sf_check_session.py --no-live    # 只看 cookie 时间,不开浏览器

# 抓取
python sf_fetch.py fetch bilibili --bv BV1XXXX
python sf_bilibili.py --list                              # 列出后台全部稿件
python sf_bilibili.py --sync --dry-run                    # 列未入库清单(不抓)
python sf_bilibili.py --sync --since 2026-08-01           # 自动补抓新稿件
python sf_douyin.py --id <作品ID> | --all --pages N
python sf_xiaohongshu.py --all --pages 40
python sf_xiaohongshu.py --deep --limit 40

# 校验/重解析(读 data/assets/ 资产文件)
python bili_audit.py     # B站全量校验(全量不抽查)
python xhs_audit.py
python xhs_reparse.py

# 按月×平台交付推送主项目(唯一出口)
python sf_push.py --month 2026-09 --platform bilibili
python sf_push.py --month 2026-09 --platform bilibili --split-video
python sf_push.py --month 2026-09 --since 2026-10-01   # 增量
python sf_push.py --all                                  # 全部有数据的月份

# 全量单文件归档(本地用,不推主项目)
python sf_fetch.py export --json
```

## 数据流

```
平台后台 → SF 抓取 → ①原始响应 → data/assets/{YYYY_MM}_{platform}/ (独立资产)
                    ②标准化   → sq_metrics.db (实时库)
                                   ↓ sf_push.py --month (发布月份×平台)
                        export/{YYYY_MM}_{platform}/ (manifest+4 数据文件)
                                   ↓ 拷入
              AQi-channel/data/fetcher/snapshots/{YYYY_MM}_{platform}/ (主项目只读)
                                   ↓
                        报告/复盘(仅用快照,不触实时库)
```
