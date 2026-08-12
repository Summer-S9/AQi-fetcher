# AQi-fetcher 版本历史 (VERSION_HISTORY)

> **规则**:任何迭代完成后,必须在**本文件顶部**追加一条版本记录(格式见模板)。
> 未更新本文件的迭代视为未完成(详见 `AGENTS.md` 版本迭代硬约束)。
> 版本号语义化:`x.y.z`(bug 修复 → z+1,功能新增 → y+1,重大重构 → x+1)。

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
