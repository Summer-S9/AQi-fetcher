# AQi-fetcher — SF (Super Fetcher) v1.2 数据采集项目

AQi-channel 的**独立数据采集工作区**。从 B站 / 抖音 / 小红书创作后台自动抓取
真实数据,统一入库,并通过**按月×平台分目录的快照**单向供给 AQi-channel 盲测项目使用。

> **本工作区与 AQi-channel 物理隔离**。实时数据库、登录会话、接口存档都在这里;
> AQi-channel 内只有快照文件。任何 agent 在 AQi-channel 做盲测/复盘时
> **禁止读取本工作区**,防止真实数据污染模型迭代。

---

## 1. 项目定位

| 项 | 说明 |
|---|---|
| 全称 | SF (Super Fetcher) v1.2 |
| 工作区 | `~/AQi-fetcher/` |
| 服务对象 | AQi-channel(盲测/复盘/报告) |
| 数据源 | B站创作中心 / 抖音创作者中心 / 小红书创作服务平台 |
| 产出 | SQLite 实时库 + 原始响应资产(assets/) + 按月交付快照(export/) |
| 核心原则 | **隔离**:主项目只见快照,不见实时库;交付按「发布月份×平台」分目录 |

## 2. 目录结构

```
~/AQi-fetcher/
├── SF/                         # SF v1.2 采集器代码
│   ├── sf_core.py              # 核心库:DB 连接/字段标准化/T+7 判定/资产读写/按月导出
│   ├── sf_fetch.py             # CLI 入口(login/fetch/status/export 本地归档)
│   ├── sf_login.py             # 三平台登录(浏览器扫码,保存会话)
│   ├── sf_check_session.py     # 会话探活(三级: cookie 时间→真实打开后台→调接口)
│   ├── sf_bilibili.py          # B站抓取器(单抓 --bv / 列表 --list / 补抓 --sync)
│   ├── sf_douyin.py            # 抖音抓取器
│   ├── sf_xiaohongshu.py       # 小红书抓取器(列表+深度)
│   ├── bili_audit.py           # B站全量校验(DB vs 接口原文,54 项/条)
│   ├── xhs_audit.py            # 小红书全量校验(DB vs 资产文件)
│   ├── xhs_reparse.py          # 小红书重解析(从资产文件恢复)
│   ├── sf_push.py              # 按月×平台导出并推送主项目(唯一出口)
│   ├── sf_migrate_assets.py    # 一次性存量迁移(资产落盘,仅 v1.2.0 用)
│   ├── sf_migrate_metric_names.py # 一次性指标命名归一(仅 v1.3.0 用)
│   ├── build-sf-report.py      # 采集成果 Word 报告生成器
│   └── report_lib_v4.py        # Word 视觉库(复用主项目)
├── data/
│   ├── sq_metrics.db           # 主数据库(SQLite,真实后台数据,已剥离原始全文)
│   ├── assets/                 # ★ 独立资产:原始接口响应全文,按 {YYYY_MM}_{platform}/ 归档
│   ├── backup/                 # DB 版本备份(迭代 schema 前生成)
│   └── fetcher/sessions/       # 三平台登录会话(bilibili/douyin/xiaohongshu.json)
├── export/                     # 交付物:{YYYY_MM}_{platform}/ 分目录(manifest + 4 数据文件)
├── README.md                   # 本文件
├── AGENTS.md                   # 项目内 agent 行为规范(必读)
└── VERSION_HISTORY.md          # 版本迭代报告(迭代后必更)
```

## 3. 数据流(单向,资产与交付分离)

```
平台创作后台
   │  (浏览器会话,Playwright 拦截接口)
   ▼
SF 抓取器 ─┬─► 原始响应全文 ─► data/assets/{YYYY_MM}_{platform}/   ← 独立资产(按抓取月)
           └─► 标准化数据 ──► sq_metrics.db(实时库,结构化)
                                  │
                                  │  sf_push.py --month YYYY-MM [--platform X]
                                  ▼
                          export/{YYYY_MM}_{platform}/              ← 交付物(按发布月)
                          ├─ manifest.json   (清单/行数/数据截止时间)
                          ├─ videos.json     ├─ snapshots.json
                          ├─ trends.json     └─ audience.json
                                  │  拷入(推送脚本自动)
                                  ▼
        AQi-channel/data/fetcher/snapshots/{YYYY_MM}_{platform}/  ← 主项目唯一数据源
                                  │
                                  ▼
                       盲测/复盘/报告(只读快照,按需取当月)
```

> **月份双轨制**:交付数据按「发布月份」归目录(一个视频的完整档案);
> 原始资产按「抓取月份」归档(当时快照)。同一视频 9 月发布、10 月深抓 →
> 数据永远在 `2026_09_xxx/`,资产分属 `2026_09_xxx/` 与 `2026_10_xxx/`。

## 4. 数据库结构 (data/sq_metrics.db)

| 表 | 用途 |
|---|---|
| `videos` | 稿件主表(platform / video_key / title / published_at / url / series_tag) |
| `metrics_snapshots` | 指标快照(snapshot_type: cumulative / deep / T7;播放/点赞/收藏/评论/分享/投币/弹幕/完播率/5s完播/封面点击/2s跳出/平均时长/涨粉/互动率…) |
| `metric_series` | **通用趋势表**(任意指标逐日/逐小时:播放/点赞/评论/收藏/分享/涨粉…,每日增量) |
| `audience_snapshots` | 观众画像(性别/年龄/城市/兴趣/来源,JSON 完整保留) |
| `api_archives` | **接口存档索引**(v1.2.0 起原文剥离为资产文件,本表存元数据 + `asset_path` 指针) |
| `daily_series` | 旧版逐日播放表(兼容保留) |

**抓取策略(全量原则)**:拦截页面所有 JSON 接口 → ①原文全部落盘
`data/assets/`(独立资产,防遗漏可回溯) ②可解析的结构化入库(快照/趋势/画像)。
平台改字段也不丢——原文在资产文件里。

## 5. 三平台能力

### B站 (sf_bilibili.py)
- 核心统计:播放/点赞/收藏/评论/分享/投币/弹幕/涨粉/取关/平均时长
- 播放端分布:TV/移动/PC 占比(电视污染判定数据)
- 逐日趋势:8 维度 × 30 天 + 小时级 48h + 近 30 天长尾
- 完播/跳出(analyze/graph):完播率/同题材均值/留存率曲线(逐 20s,含同类对比)
- 互动率/3s跳出(play_analyze):互动率、3s 跳出率(万分比÷100)、星级
- 段退出曲线(viewer_quit):逐 20s 原始计数(不自算留存率,用官方 3s 跳出率)
- 观众画像:性别/年龄/地区/内容类型/粉丝占比 + 同题材对比 10 条
- 发布后 30 日:逐日播放 30 天 + 48 小时级 + 长尾(搜索复利)

### 抖音 (sf_douyin.py)
- 来源:`/janus/douyin/creator/pc/work_list`(作品列表 items[].metrics 全维度)
- 覆盖:播放/点赞/评论/分享/收藏/弹幕/不喜欢 + 完播率/5s完播/2s跳出率/封面点击率/平均观看秒数/平均播放进度 + 主页访问/吸粉/取关/粉丝观看占比
- 注意:①rate 为小数(0.151=15.1%)入库 ×100 ②aweme_id 19 位超 JS 精度,必须返回原始文本由 Python 解析 ③登录需扫码+二次验证 ④**冻结机制:详细数据发布 3 个月后停止更新**,旧作品入库自动标注 `metrics_frozen_at`

### 小红书 (sf_xiaohongshu.py)
- 列表:`api/galaxy/v2/creator/note/user/posted?tab=0&page=N`(滚动翻页,每页约 11 条)
- 深度(打开 `statistics/note-detail?noteId=X` 拦截):完播率/5s完播/2s跳出/封面点击/平均观看/曝光/涨粉/互动率 + 逐小时 71 点曲线(14 维度)+ 发布后热度指数(200+ 天,含同类对比)+ 观众画像 + 观看来源 + 平台诊断评级(analyse_infos)
- 注意:①页面请求带 x-s/x-t 签名,直接 fetch 返回 -1,必须拦截页面自身请求 ②**冻结机制:深度数据约 1 年后冻结**,旧笔记 rate 返回 -1 ③URL 参数名是 `noteId`(不是 note_id)④共创笔记 note/base 无权限

## 6. 使用指南

### 环境
- Python: `/Users/summer/.workbuddy/binaries/python/envs/default/bin/python`
- Playwright + Chromium 已安装(登录有头,抓取无头)

### 登录(每平台一次,扫码)
```bash
cd ~/AQi-fetcher/SF
python sf_fetch.py login bilibili      # 或 douyin / xiaohongshu
```
弹出浏览器扫码,登录后自动保存会话到 `data/fetcher/sessions/{platform}.json`。

### 会话探活(抓取前建议先跑)
```bash
python sf_check_session.py              # 三个平台都查
python sf_check_session.py bilibili     # 只查一个
python sf_check_session.py --no-live    # 只看 cookie 有效期,不开浏览器
```
**cookie 未过期 ≠ 服务端仍认**(平台可提前吊销)。本工具会带登录态真实打开创作后台,
检测是否被重定向到登录页,并调用需登录接口验证,给出三平台有效/失效结论。

### 抓取
```bash
# B站(按 BV 或链接)
python sf_fetch.py fetch bilibili --bv BV1XXXX
python sf_fetch.py fetch bilibili --url "https://www.bilibili.com/video/BV1XXXX"

# B站:发现新稿件 + 自动补抓(v1.3.0)
python sf_bilibili.py --list                       # 列出后台全部稿件(284 条)
python sf_bilibili.py --list --list-out x.json     # 列表落盘
python sf_bilibili.py --sync --dry-run             # 只列"未入库"清单,不抓
python sf_bilibili.py --sync --since 2026-08-01    # 补抓该日之后发布的未入库稿件
python sf_bilibili.py --sync --bv-only BV1xxx,BV1yyy  # 定点补抓

# 抖音(按作品ID或链接 / 全量)
python sf_douyin.py --id <作品ID> --url <链接>
python sf_douyin.py --all --pages N

# 小红书(列表全量 / 指定 / 深度)
python sf_xiaohongshu.py --all --pages 40          # 全量列表
python sf_xiaohongshu.py --id <笔记ID>              # 单篇列表
python sf_xiaohongshu.py --deep --limit 40          # 深度批量(完播/画像/趋势)
python sf_xiaohongshu.py --deep --id <笔记ID>       # 单篇深度
```

### 校验与修复
```bash
python bili_audit.py         # B站全量校验:DB vs 接口原文,54 项/条(全量不抽查)
python bili_audit.py --csv out.csv   # 同时导出明细
python xhs_audit.py          # 小红书全量校验:DB 每条记录 vs 原始存档,输出不一致
python xhs_reparse.py        # 小红书重解析:从 api_archives 资产恢复被覆盖/错误的数据
```

### 查看与导出
```bash
python sf_fetch.py status              # 库内稿件清单
python sf_fetch.py export --xlsx       # 导出 xlsx
python sf_fetch.py export --json       # 导出 JSON
```

### 推送快照到主项目(唯一出口,v1.2.0 按月×平台)
```bash
# 交付指定月×平台 → AQi-channel/data/fetcher/snapshots/{YYYY_MM}_{platform}/
python sf_push.py --month 2026-09 --platform bilibili
python sf_push.py --month 2026-09                       # 整个月(三平台)
python sf_push.py --month 2026-09 --platform bilibili --split-video  # 单视频文件
python sf_push.py --month 2026-09 --since 2026-10-01    # 增量:只含其后新快照
python sf_push.py --all                                 # 所有有数据的月份×平台
python sf_push.py --to DIR                              # 指定目标目录

# 全量单文件归档(本地用,不推主项目)
python sf_fetch.py export [--json|--xlsx]
```

### 采集成果报告(Word)
```bash
python build-sf-report.py --bv BV1XXXX [--out 输出路径]
```

## 7. 隔离铁律(所有 agent 必须遵守)

1. **AQi-channel 内禁止存在 `sq_metrics.db` 实时库**——只允许导出快照。
2. **盲测/复盘 agent 禁止读取本工作区**或任何实时采集数据。
3. 报告/复盘只能引用 `AQi-channel/data/fetcher/snapshots/` 内快照,并声明快照文件名。
4. 复盘必须用"揭晓时点之前"的快照,禁止用最新数据回填历史判断。
5. 更新主项目数据:在本项目跑 `sf_push.py`,主项目不直连任何平台接口。

## 8. 命名规范

- 采集器统一命名 **SF (Super Fetcher) v1.2**
- 模块前缀 `sf_`;历史名称 `sq_fetch_*` 已废弃(2026-08-12 拆分时更名)
- 小红书专项工具保留 `xhs_` 前缀(xhs_audit / xhs_reparse)

## 9. 迭代规范

对 AQi-fetcher 的任何迭代(新增/修改脚本、调整 DB schema、改字段映射、
改抓取策略、改导出格式、改冻结判定等),完成后**必须**:

1. 在 `VERSION_HISTORY.md` 顶部追加一条版本记录(格式见该文件模板)。
2. 更新 `README.md` 中对应能力/用法/注意点(如有变化)。
3. 修改 DB schema 或字段映射时,同步更新第 4 节表结构说明。
4. 涉及抓取行为变更,注明对存量数据的影响(是否需要重抓/重解析)。
