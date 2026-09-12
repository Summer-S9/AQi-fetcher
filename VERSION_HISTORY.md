# AQi-fetcher 版本历史 (VERSION_HISTORY)

> **规则**:任何迭代完成后,必须在**本文件顶部**追加一条版本记录(格式见模板)。
> 未更新本文件的迭代视为未完成(详见 `AGENTS.md` 版本迭代硬约束)。
> 版本号语义化:`x.y.z`(bug 修复 → z+1,功能新增 → y+1,重大重构 → x+1)。

---

## v1.4.0 (2026-09-12)

### 变更内容
- [功能] **B站历史基线合并**(打通主项目 xlsx 与 SF 之间从未连通的两半数据):
  - 数据源:`AQi-channel/data/B站后台视频数据.xlsx`(人工从创作中心导出,231 条,
    2021-12-31 ~ 2026-07-31,7 项聚合指标,**无 BV 号**);已留档至 `data/imports/`。
  - 匹配策略两级:① 发布时间(北京时间,秒级)精确匹配 → 227/231(98.3%);
    ② 标题相似度兜底(difflib ≥ 0.85 且发布时间须在 ±3 天内)→ 救回 1 条
    (`BV1Kx4y1m77C`,标题「！」后被平台改成「❗」)。
  - 数据源自身问题 3 类,均已处理并在报告中单列:
    - 重复行 1 条(同一视频被记两遍,标题多出 "sI",时间/播放完全相同)→ 去重保留 1 条;
    - 已删除稿件 3 条(后台已无对应 BV)→ 以合成 key `xlsx-{YYYYMMDDHHMMSS}` 入库;
    - 时间口径差异 1 条(`BV1Kx4y1m77C` xlsx 记 2024-03-30 00:00:00,真实为
      2024-03-29 16:00:00)→ 已用 B站公开接口 `pubdate=1711699200` 独立裁决,采用后台权威时间。
  - 结果:写入 **230 条稿件 / 230 条快照**;B站由 6 条 → **235 条**
    (2021:1 / 2022:28 / 2023:46 / 2024:61 / 2025:65 / 2026:34)。
- [功能] **数据质量标记**:导入快照统一 `source='import_xlsx'`,与 API 抓取的 `api` 严格区分。
  实测该来源指标为创作中心**显示值**,字段精度如下(消费方必读):
  - ≥1万 的数值精度只到千位 —— 202 个 ≥1万 播放值 100% 能被 1000 整除、0 反例;
    点赞 51/51、收藏 35/35、投币 2/2 同规律。即存在 ±500 量级取整误差。
  - <1万 的数值为精确值。
  - 仅 2026-08-01 单一快照,无趋势/画像/完播率/互动率/涨粉。
  - 交叉校验:对同时有两侧数据的 `BV1rnaJzpEFv` 做量级比对,xlsx(8-01) vs
    API(8-11) —— 弹幕 316/316、评论 120/120、分享 144/144 完全一致,
    播放 132,000 vs 131,942(差 0.04%),证明该来源数据可信。
- [功能] 新增 `SF/sf_import_bili_history.py`:合并导入器,支持 `--dry-run`、
  `--list-json`(复用后台列表缓存)、`--xlsx`(指定数据源);**幂等**——重复执行会先清除
  本平台既有 `import_xlsx` 快照再重建,不会重复,且**不触碰** `api` 数据。
- [功能] 新增 `SF/bili_import_audit.py`:导入数据全量核对器,走**反向链路**
  (DB video_key → 后台列表反查发布时间 → 回到 xlsx 原行)以刻意与导入脚本解耦,
  避免自证循环。
- [修复] `sf_bilibili.py` `_ts_to_date()` 时间精度缺陷:原只返回 `%Y-%m-%d`,
  丢失时分秒,导致 `--list` 输出无法与外部导出列表精确配对(按时间匹配命中率 0%)。
  现改为显式按 UTC+8 换算,新增 `full=True` 返回 `YYYY-MM-DD HH:MM:SS`,
  并输出新字段 `published_at_full`;`--list` 文本打印改用完整时间。
  注:`published_at` 仍保持日期口径,不改变既有入库语义。
- [功能] `sf_core.py` `add_snapshot()` 新增 `captured_at` 参数:导入历史数据时显式指定
  「数据实际时点」,否则交付物 manifest 的 `data_through` 会误报为导入时刻,
  破坏「复盘只用揭晓时点前快照」的审计前提。

### 涉及文件
- `SF/sf_import_bili_history.py`(新增)
- `SF/bili_import_audit.py`(新增)
- `SF/sf_bilibili.py`(时间精度修复 + `published_at_full`)
- `SF/sf_core.py`(`add_snapshot` 支持 `captured_at`)
- `data/imports/`(数据源留档 + 配对报告)
- `README.md`、`SF/README.md`、`AGENTS.md`

### 存量数据影响
- **无破坏性影响**。仅新增数据:稿件 +229 条、快照 +230 条(全部 `import_xlsx`)。
- 已有 `api` 抓取数据(18 条快照)**零改动**,已核对完好。
- `sf_import_bili_history.py` 幂等,可安全重跑;`--dry-run` 不写库。
- 回退:`data/backup/sq_metrics_v1.3.0_20260912_pre_bili_import.db`(导入前快照)。

### 验证结果
- `bili_import_audit.py` 全量核对 **2305 项 → 0 不一致**
  (230 条记录 × (7 指标 + source + captured_at) + 覆盖/唯一性/重复行检查)。
- 时间口径差异 1 条已用 B站公开接口独立裁决(见上),登记为已知例外。
- 交付物导出验证(2021-12 / 2024-03 / 2025-09 / 2026-06 四个月):
  `data_through` 正确(导入数据=2026-08-01,API 数据=2026-08-11)、
  `source` 标记正确、`raw_json` 已剔除。
- 唯一性:videos 235 条 / 唯一 video_key 235 / 重复 0 / 空 key 0。
- 全库:稿件 220 → 449、快照 576 → 806、趋势 25951(不变)、画像 47、资产 990(不变)。

---

## v1.3.0 (2026-09-12)

### 变更内容
- [功能] **B站稿件自动发现与补抓**(解决"抓取器只能按 BV 单抓、无法发现新视频"的能力缺口):
  - `sf_bilibili.py --list`:调用创作中心稿件列表接口 `/x/web/archives`(自动分页),
    列出后台**全部**稿件,按发布时间倒序;`--list-out` 可落盘 JSON。
    实测后台 284 条稿件与接口 `page.count` 完全一致。
  - `sf_bilibili.py --sync`:一条命令完成「列出全部稿件 → 与 DB 比对 → 抓未入库的」。
    支持 `--since YYYY-MM-DD`(只抓该日之后发布)、`--limit N`、`--bv-only`(定点补抓)、
    `--include-nonpublic`、`--dry-run`(只列清单)。默认跳过非「开放浏览」稿件。
    每条之间随机延迟 8–15s,避免触发风控。
- [新增] **`bili_audit.py` B站数据全量校验器**(对标 `xhs_audit.py`,全量核对不抽查):
  逐条把 DB 与抓取时存档的接口原文(`data/assets/`)比对,覆盖 7 类共 54 项:
  ① 稿件身份(title/published_at) ② 核心指标 9 项 ③ 完播率/平均时长/段退出曲线
  ④ 互动率/3s跳出率/播放来源 10 项 ⑤ TV 占比重算 ⑥ 趋势日期集合(8 维度逐日 +
  小时级 + 近30天长尾,逐日期比对) ⑦ 画像字段完整性。支持 `--bv`、`--csv` 明细导出。
- [修复] **B站趋势指标命名不统一(存量数据问题)**:v1.0 时期抓取的 `BV1rnaJzpEFv`
  把逐日播放/转粉趋势写成 `daily_plays` / `daily_followers`,而现行代码统一用
  `plays` / `followers`,导致该稿件的数据挂在旧名下:
  - 同一份交付物里出现两套命名(其余 5 条稿件均为新命名);
  - 任何按 `plays` 查询的消费方(主项目/报告/审计)读不到这条稿件的数据。
  新增 `sf_migrate_metric_names.py` 就地改名(不删除,保住当初抓到的
  2025-09-01~2025-09-30 那段日期区间——趋势接口只给固定窗口,无法事后补抓历史)。
  改名 60 行,冲突 0 行。
- [校准] `bili_audit.py` 的 `avg_duration` 核对判据修正:B站 `archive_diagnose/overview`
  的 `stat.play_avg_duration` 字段**实测恒为 0**(全部 6 条稿件验证),真实平均播放时长
  只在 `v2/archive/analyze/graph` 的 `duration_info.avg_play_time_int`。故该字段的核对
  移到 graph 一节,避免误报。
- [数据] **补抓停滞期 B站新稿件 5 条**(2026-08-08 ~ 2026-08-27),每条 29 个接口存档、
  8 维度趋势全绿、完播与播放分析接口均 ok,失败 0 条。

### 涉及文件
- `SF/sf_bilibili.py`(新增 `list_archives()` / `sync_missing()` / `--list` / `--sync` 等)
- `SF/bili_audit.py`(新增,全量校验器)
- `SF/sf_migrate_metric_names.py`(新增,一次性指标改名)

### 存量数据影响
- [需重解析/已迁移] `BV1rnaJzpEFv` 的 `daily_plays`/`daily_followers` 共 60 行就地改名为
  `plays`/`followers`;改名前后行数一致、无冲突、无数据丢失。
- [仅记录,未处理] `retention_20s`(17 行,仅 `BV1rnaJzpEFv` 有,现行代码已不再产出)
  予以保留:它是当时由 graph 数据派生的留存率,删除会丢信息;消费方应优先看
  `quit_curve_20s`(现行标准口径)。
- [新增数据] B站稿件 1 → 6 条;`metrics_snapshots` +5;`metric_series` B站部分 +3648;
  `audience_snapshots` +5;`api_archives` +145。
- 备份:`data/backup/sq_metrics_v1.2.2_20260912_pre_bili_sync.db`(迁移前,4.8MB)、
  `data/backup/sq_metrics_pre_migrate_metric_names.db`(指标改名:前)。

### 验证结果
- `py_compile` 全部通过。
- **`bili_audit.py` 全量核对 6 条稿件 / 324 项 → 0 不一致**(明细 CSV:
  `/tmp/bili_audit_20260912.csv`)。
- 首次核对曾报 3 处不一致,逐一定性:
  - `avg_duration`(旧稿 DB=155 / overview 原文=0)→ 判据错,DB 正确(graph 取值),已校准脚本;
  - `plays` / `followers` 缺 30 个日期 → 确认为真问题(旧命名),已通过改名修复。
- `--list` 实测返回 284 条 = 接口 `page.count`,一致。
- `--sync --since 2026-08-01` 实测:先 dry-run 确认清单为 5 条,再正式抓取,成功 5 / 失败 0。
- 交付验证:`sf_push.py --month 2026-08 --platform bilibili` →
  `AQi-channel/data/fetcher/snapshots/2026_08_bilibili/`
  (manifest: 视频 5 · 快照 5 · 趋势 3648 · 画像 5 · 截止 2026-09-12 11:09:48;
  `snapshots.json` 含 `raw_json` 的行数 = 0;videos/snapshots/trends/audience 的
  `video_id` 关联全部可映射)。

---

## v1.2.2 (2026-09-12)

### 变更内容
- [修复] **`sf_login.py` 会话保存路径错误(严重)**:该文件自行推算
  `ROOT = Path(__file__).resolve().parents[2]` → 得到 `/Users/summer`,
  于是 `SESSION_DIR` 指向 `/Users/summer/data/fetcher/sessions`(非项目目录),
  而所有抓取器(`sf_bilibili.py` / `sf_douyin.py` / `sf_xiaohongshu.py`)读取的是
  `sf_core.SESSION_DIR` = `~/AQi-fetcher/data/fetcher/sessions`。
  **后果**:重新登录后会话被存到错误位置,**抓取器仍继续使用旧(可能已过期)会话**,
  表现为"明明刚扫过码却仍提示登录态过期",且会污染项目外目录。
  修复方式:改为 `from sf_core import SESSION_DIR`,路径统一由核心库提供,
  禁止各脚本自行推算 ROOT。
- [新增] **`sf_check_session.py` 会话探活检查器**:三级判定(① cookie 有效期 →
  ② 带登录态真实打开创作后台看是否被重定向到登录页 → ③ 调需登录接口验证),
  因为 cookie 未过期 ≠ 服务端仍认。支持 `python sf_check_session.py`(全平台)/
  `<platform>`(单平台)/ `--no-live`(只看 cookie 时间)/ `--show`(有头)。
- [清理] `sf_login.py` 中与现登录判据矛盾的过期注释:原注释写"小红书必须出现
  web_session cookie 作为登录硬标志",而 `LOGIN_RULES` 实际使用的是 creator 专用
  cookie(`access-token-creator.xiaohongshu.com` 等),web_session 在 creator 平台
  根本不下发 → 已更正,避免后续 agent 被误导改错判据。
- [记录] 遗留待处理(本轮未改,涉及产出路径语义需用户确认):
  - `SF/build-sf-report.py` 默认 `--out` 指向 `/Users/summer/UP/`(不存在),
    不带 `--out` 运行会失败,应为 `~/AQi-channel/UP/`。
  - `SF/report_lib_v4.py` 的 `OUT` / `SCRIPT_PATH` 为未被引用的死常量,
    且指向不存在的 `/Users/summer/UP/`、`/Users/summer/scripts/`,建议删除或修正。

### 涉及文件
- `SF/sf_login.py`(路径修复 + 注释清理)
- `SF/sf_check_session.py`(新增)

### 存量数据影响
- [无影响] 未改动 DB schema / 字段映射 / 抓取解析逻辑,存量数据与快照均不受影响。
- [安全核查] `/Users/summer/data/` 目录**不存在**,说明该 bug 从未实际写入过错误位置,
  项目内 3 个会话文件均完好,无需迁移。

### 验证结果
- `py_compile sf_login.py sf_check_session.py` 通过。
- 路径一致性验证:`sf_login.SESSION_DIR == sf_core.SESSION_DIR` → `True`,
  且该目录存在;全项目 `parents[2]` 残留已排查(仅剩报告脚本死常量,见上)。
- **三平台会话实测(2026-09-12,真实打开创作后台)**:

  | 平台 | cookie 有效期 | 真实后台访问 | 结论 |
  |---|---|---|---|
  | bilibili | SESSDATA 至 2027-02-07(剩 148 天) | 正常进入 `member.bilibili.com/platform/home`,`nav` 接口 `isLogin=True`(账号 阿七AQi_) | ✅ 有效 |
  | douyin | sessionid 至 2026-10-10(剩 28 天) | 正常进入 `creator.douyin.com/creator-micro/content/manage`,作品 284 条 | ✅ 有效 |
  | xiaohongshu | `access-token-creator` + `galaxy_creator_session_id` 已于 2026-09-10 过期 | 被重定向至 `/login?redirectReason=401` | ❌ 需重新登录 |

---

## v1.2.1 (2026-09-12)

### 变更内容
- [修复] `.gitignore` 缺口补全:v1.2.0 新增的 `data/assets/`(原始响应全文,含平台返回
  token 等字段,约 20MB)与 `data/backup/`(DB 版本备份,含真实后台数据)未被忽略,
  存在误 `git add` 入库的泄露风险 → 已加入忽略规则。
- [适配] `AGENTS.md` 同步至 v1.2 口径(修正 v1.2.0 迭代时的文档遗漏):
  - 版本号 `SF v1.0` → `SF v1.2`
  - 隔离铁律第 5 条 + 常见操作备忘的 `sf_push.py` 命令补齐
    `--month YYYY-MM [--platform X] / --all`(旧的无参数调用已失效)
  - 数据安全章节补充 `data/assets/`、`data/backup/` 的管理要求

### 涉及文件
- `.gitignore`、`AGENTS.md`

### 存量数据影响
- 无影响(仅忽略规则与文档,不涉及数据、schema 或抓取行为)。

### 验证结果
- `git check-ignore -v` 确认 `data/assets/`、`data/backup/`、`data/sq_metrics.db` 均被忽略
- `git status` 复核:`data/` 已不再出现在未跟踪列表(仅剩代码/文档改动 + `.workbuddy/`、`SF/sf_migrate_assets.py`)

---

## v1.2.0 (2026-08-13)

### 变更内容
- [重构] **原始响应剥离为独立资产**:`api_archives.body_json` 全文不再入库,改为落盘
  `data/assets/{YYYY_MM}_{platform}/raw_{stamp}_{video_key}[_hash].json`(按**抓取月份**归档),
  本表只存元数据 + `asset_path` 指针。同一视频同秒多接口时文件名追加 URL 短 hash 防覆盖。
- [功能] **按月×平台分目录交付**:新增 `export_monthly()`,产出
  `export/{YYYY_MM}_{platform}/`(按**发布月份**),含 `manifest.json` 清单 +
  `videos/snapshots/trends/audience` 四个数据文件。
  - 快照去重:每视频只输出「最新一条 cumulative/T7」+「全部 deep」,剔除 raw_json
  - `--split-video` 时另生成 `videos/{video_key}.json` 单视频文件(agent 按需直读)
  - `since` 增量语义:仅过滤快照 captured_at ≥ since,趋势/画像给完整档案
- [功能] `sf_push.py` 重构:`--month YYYY-MM / --platform / --split-video / --since / --all / --to`;
  交付物按 `{YYYY_MM}_{platform}/` 拷入主项目,不再生成全量单文件(全量归档走 `sf_fetch.py export`)。
- [适配] `xhs_audit.py` / `xhs_reparse.py` 改为经 `asset_path` 读资产文件(`load_asset`),
  不再依赖 `body_json` 列。
- [新增] `sf_migrate_assets.py`:一次性存量迁移(落盘→回填指针→校验→清空 body_json)。

### 涉及文件
- `SF/sf_core.py`(schema 加 asset_path / write_asset / load_asset / export_monthly / add_api_archive 改造)
- `SF/sf_push.py`(重写为按月×平台推送)
- `SF/xhs_audit.py`、`SF/xhs_reparse.py`(读资产文件)
- `SF/sf_migrate_assets.py`(新增,一次性迁移)
- `README.md`、`SF/README.md`(同步更新)

### 存量数据影响
- 需迁移(已完成):api_archives 890 条 body_json → 890 个资产文件,指针回填,body_json 置空。
  DB 22.7MB → 4.06MB(VACUUM 后,约 -82%)。
- 备份:`data/backup/sq_metrics_v1.1.0_20260813.db`(可回退)。

### 验证结果
- 迁移校验:890/890 落盘成功,抽查 20/20 内容一致
- `xhs_audit.py` 全量 211 篇跑通(资产链路 OK;2 处差异为 v1.1.0 实测新抓数据 vs 旧列表页存档的正常新旧差)
- `xhs_reparse.py --video` 单篇跑通(修复逐小时 852 / 热度指数 215)
- 按月导出三平台结构验证通过;`--split-video`(单视频文件含 video/snapshots/trends/audience、
  无 raw_json)、`--since` 增量过滤(4 条)验证通过
- 冒烟测试:核心库导入 OK,三平台数据完整

---

## v1.1.0 (2026-08-12)

### 变更内容
- [功能] 新增 `fetch_single_note()`:小红书**指定单条抓取**。
  - 行为变更:此前 `--id <笔记ID>` 会先滚动抓全量列表再筛选(慢且多余);
    现在指定单条直接打开 `statistics/note-detail?noteId=X` 抓取 note/base,只取这一条。
  - 数据来源:note/base 的 note_info(标题/时间/类型)+ data 层基础计数(view/like/collect/comment/share)。
  - 快照行为不变:每次抓取 INSERT 新行,历史保留,最新数据追加在旧记录下方。
- [适配] `sf_fetch.py` CLI:`--id/--url` 分支改为调用 `fetch_single_note`;`--all` 仍走全量列表。

### 涉及文件
- `SF/sf_xiaohongshu.py`(新增 fetch_single_note + CLI 分支)

### 存量数据影响
- 无影响:已有快照不变;只是指定单条时的抓取路径更高效。

### 验证结果
- 实测 `--id 6a66e7b9000000000503907c`:只抓「细思极恐」单条,10 秒完成;
  快照追加为新一行(23:43,观看 169,057),历史 3 条 cumulative 保留。

---

## v1.0.0 (2026-08-12)

### 变更内容
- [重构] 采集项目从 AQi-channel 拆分至独立工作区 `~/AQi-fetcher/`,更名 **SF (Super Fetcher) v1.0**。
- [重构] 全部采集器更名 `sq_fetch_*` → `sf_*`(sf_core / sf_fetch / sf_login / sf_bilibili / sf_douyin / sf_xiaohongshu)。
- [功能] 新增 `sf_push.py` 导出推送脚本:导出带时间戳快照(JSON/XLSX)到主项目 `data/fetcher/snapshots/`,作为主项目唯一数据源。
- [功能] 新增项目文档体系:README.md(总览)、AGENTS.md(行为规范)、VERSION_HISTORY.md(本文件)。
- [适配] 路径 ROOT 从 `parents[2]` 改为 `parents[1]`(SF/ 为一级目录);`build-sf-report.py` metric 名修正(plays→daily_plays)。

### 涉及文件
- 新增: `~/AQi-fetcher/SF/sf_push.py`、`~/AQi-fetcher/README.md`、`~/AQi-fetcher/AGENTS.md`、`~/AQi-fetcher/VERSION_HISTORY.md`
- 迁移: `sq_fetch_*.py` → `sf_*.py`、`xhs_audit.py`、`xhs_reparse.py`、`build-sf-report.py`、`report_lib_v4.py`
- 数据: `sq_metrics.db`(215 稿件 / 358 快照 / 861 接口存档 / 22303 趋势)+ 三平台登录会话

### 存量数据影响
- 无影响:数据完整迁移,校验通过(bilibili 1 / douyin 3 / xiaohongshu 211)。

### 验证结果
- 冒烟测试通过:SF 核心库可导入,三平台数据完整;主项目无采集残留,快照推送成功。
