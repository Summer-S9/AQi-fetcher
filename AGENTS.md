# AGENTS.md — AQi-fetcher 项目 agent 行为规范

本文件是 AQi-fetcher(SF v1.2)项目的 agent 行为规范。所有在本工作区工作的
agent(Codex / DeepSeek / Claude / 其他)必须优先遵守。

## 基本要求

- 所有项目相关回复一律使用中文。
- 必须主动完成用户要求的完整任务,不得用空泛总结、占位内容、模板化废话代替实际工作。
- 用户要求"全部/所有/重构/完成"时,必须逐项处理并验证,不得只做示范。
- 无法完成时,必须说明具体阻塞点,不得给看似完成的答案。

## 开始任务前必读

| 任务类型 | 必读文件 |
|---|---|
| 任何项目任务 | `README.md`(项目总览)、`AGENTS.md`(本文件) |
| 理解采集器 | `SF/README.md`(模块说明)+ 各 `sf_*.py` 文件头注释 |
| 迭代/升级 | `VERSION_HISTORY.md`(历史版本)+ `README.md` 对应章节 |
| 小红书数据修复 | `SF/xhs_audit.py`、`SF/xhs_reparse.py` 文件头说明 |
| B站抓取/补抓/校验 | `SF/sf_bilibili.py`(`--list`/`--sync`)、`SF/bili_audit.py` 文件头说明 |

## 隔离铁律(最重要,不可违反)

1. **AQi-channel 内禁止存在 `sq_metrics.db` 实时库**——只允许导出快照。
2. **盲测/复盘 agent 禁止读取本工作区**或任何实时采集数据。
   - 本工作区是**数据生产者**,主项目是**数据消费者**,边界不可模糊。
   - 在 AQi-channel 做盲测/复盘/诊断时,即使知道本工作区存在,也不得读取。
3. 报告/复盘只能引用 `AQi-channel/data/fetcher/snapshots/` 内快照,并声明快照文件名。
4. 复盘必须用"揭晓时点之前"的快照,禁止用最新数据回填历史判断。
5. 更新主项目数据:在本项目跑 `python SF/sf_push.py --month YYYY-MM [--platform X]`(或 `--all`),
   主项目不直连平台接口。v1.2.0 起为按月×平台分目录交付,旧的无参数调用已失效。

## 数据安全

- `data/sq_metrics.db` 是真实后台数据,含创作者账号信息,不得外泄、不得复制到主项目。
- `data/assets/` 是原始接口响应全文(按抓取月份归档),含平台返回的 token 等字段,不得外泄、不得入库。
- `data/backup/` 是 DB 版本备份,含真实后台数据,同 `sq_metrics.db` 管理。
- `data/fetcher/sessions/*.json` 是登录会话,含 cookie,等同于账号凭证,不得外泄。
- 登录/抓取是低频手动触发,已加随机延迟;不得高频请求触发风控。
- 平台改版可能导致选择器失效,需按页面结构调整解析逻辑,并在 VERSION_HISTORY 记录。

## 不得偷懒的具体标准

1. 不得擅自省略固定栏目、关键字段、校验步骤。
2. 不得只写"提高采集效率""优化解析"这类空话,必须写具体到可执行的动作。
3. 资料不足时明确列出缺失字段,不得编造。
4. 输出前自检:文件是否生成/修改成功、路径是否存在、规则是否满足、有无旧口径残留。
5. 修改 DB schema / 字段映射 / 抓取策略后,必须校验存量数据是否受影响(需要重抓/重解析)。
6. 修改小红书相关代码后,建议跑 `xhs_audit.py` 确认无新增不一致。
7. **路径必须从 `sf_core` 统一取,禁止脚本内自行推算 ROOT。**
   必须 `from sf_core import ROOT/DB_PATH/SESSION_DIR/ASSET_DIR/SNAPSHOT_DIR`,
   不得写 `Path(__file__).resolve().parents[N]`。
   (教训:v1.2.2 前 `sf_login.py` 用 `parents[2]` 得到 `~/data/...`,
   与抓取器读取的 `~/AQi-fetcher/data/...` 不一致,导致"重新登录后抓取器
   仍用旧会话"。此类 bug 不报错、只静默失效,排查成本极高。)
8. 新增/修改任何"写文件到磁盘"的脚本后,必须验证目标路径**真实存在且正确**,
   不能只看代码跑通。

## 版本迭代硬约束(强制)

**任何迭代必须写版本报告。** 包括但不限于:

- 新增/修改抓取脚本、登录器、导出器、审计/重解析脚本
- 调整 DB schema、表结构、字段映射、标准化逻辑
- 改变抓取策略(翻页/签名/拦截/等待/断点续抓)
- 改变导出格式(JSON/XLSX/快照命名)
- 改变冻结判定、T+7 判定、数据时效逻辑
- 平台改版导致的选择器/接口适配
- 修复数据 bug(入库错误、精度丢失、语义误解等)

### 执行步骤

1. 改动前:查看 `VERSION_HISTORY.md` 最新版本号,确定新版本号(语义化 `x.y.z`,bug 修复 → z+1,功能新增 → y+1,重大重构 → x+1)。
2. 改动中:如涉及 DB schema 变更,先备份 `data/sq_metrics.db`。
3. 改动后:
   - 在 `VERSION_HISTORY.md` **顶部**追加一条完整版本记录(格式见下文)。
   - 更新 `README.md` 对应章节(能力/用法/注意点/表结构)。
   - 更新 `SF/README.md` 模块说明(如涉及)。
   - 运行必要的验证(导入测试/冒烟/审计/重解析)。
4. **未更新 VERSION_HISTORY.md 的迭代视为未完成。**

### VERSION_HISTORY.md 记录格式

```markdown
## v1.1.0 (2026-08-20)

### 变更内容
- [功能/修复/重构] 具体描述…

### 涉及文件
- `SF/xxx.py` …

### 存量数据影响
- [无影响 / 需重抓 / 需重解析 / 需迁移],具体操作…

### 验证结果
- [冒烟测试 / xhs_audit 结果 / 导出验证] …
```

## 常见操作备忘

```bash
# 冒烟测试核心库
python -c "import sys; sys.path.insert(0,'SF'); from sf_core import DB; db=DB(); print(db.path)"

# 会话探活(抓取前建议先跑;cookie 没过期 ≠ 服务端还认)
cd SF && python sf_check_session.py          # 三平台
cd SF && python sf_check_session.py bilibili # 单平台
cd SF && python sf_check_session.py --no-live  # 只看 cookie 时间,不开浏览器

# B站:发现新稿件 + 自动补抓(v1.3.0)
cd SF && python sf_bilibili.py --list                  # 列出后台全部稿件
cd SF && python sf_bilibili.py --sync --dry-run         # 列"未入库"清单(不抓)
cd SF && python sf_bilibili.py --sync --since 2026-08-01  # 补抓该日之后的新稿件

# B站全量校验(抓取后必跑;全量核对,不抽查)
cd SF && python bili_audit.py
cd SF && python bili_audit.py --csv out.csv            # 同时导出明细

# 小红书全量校验(改动后必跑)
cd SF && python xhs_audit.py

# 小红书重解析(修复入库错误后跑)
cd SF && python xhs_reparse.py

# 推送快照到主项目(v1.2.0 起按月×平台分目录交付)
cd SF && python sf_push.py --month 2026-09 --platform bilibili   # 指定月×平台
cd SF && python sf_push.py --month 2026-09                        # 整月(三平台)
cd SF && python sf_push.py --all                                  # 全部有数据的月份
```
