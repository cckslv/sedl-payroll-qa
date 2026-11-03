# 示例企业 链路重构设计（RAG → 结构化问答）

> 写于 2026-11-02　｜　依据：现有代码实测 + 两个开源实现的范式对比

## 一、现有链路的 6 个结构性问题

| # | 问题 | 证据 |
|:--|:--|:--|
| 1 | **用向量检索取数字** | 8 个人、几个数值字段，本质是查表；向量相似度不保证数值正确性 |
| 2 | **整表切片，跨行切断** | `chunk_size=1000 / overlap=200` 会把一行员工数据切到两个 chunk 里，检索回来是"半张表" |
| 3 | **top_k=100** | 46~212 条 chunk 里取 100 条，等于把大半库塞进 prompt，噪声压过信号 |
| 4 | **答案写死** | Prompt 里 8 人工资表标"唯一真相，禁止修改"；`salary_calc.py:126-156` 用 `elif emp=="10000001": cum_total=35` 兜底 |
| 5 | **让 LLM 做算术** | 扣款公式有 5 个分支、涉及比例与取整，模型算不准 |
| 6 | **没有评测集** | 改完 prompt 无法判断变好还是变坏 |

第 4 条最要命：**写死的数据会把 bug 藏起来**。当前 8 个人永远对，换一批数据就全错，而且错得没有声音。

## 二、开源实现的共性（正规做法）

参考：

- [Howard-Qin-art/nl2sql-agent](https://github.com/Howard-Qin-art/nl2sql-agent) — 中文自然语言转 SQL，LangGraph 编排，带 60 题评测
- [jonkmatsumo/text2sql](https://github.com/jonkmatsumo/text2sql) — 生产级 Text2SQL，ANN schema linking + AST 策略 + 可回放

提炼出 6 条：

1. **schema linking 代替"全库塞入"** — 先选表选列（BM25 + 字面值命中 + 外键扩展 + 向量 → RRF → LLM 精排），只把命中的表结构拼进 prompt。
2. **数字走 SQL/代码，不走向量。** 腾讯云那篇文章说得很直接："当答案是确定性的、且存在于结构化数据中时，SQL 才是正确的检索路径"。
3. **护栏用 AST，不用正则。** `sqlglot` 解析后判断语句类型、涉及表、有无 LIMIT；连接层再加 `mode=ro` + `PRAGMA query_only=1` 双保险。
4. **执行失败要自愈**，把数据库报错原文回注给模型，要求最小改动重写。
5. **歧义要澄清**，且"该用规则的地方别指望模型"——没有量化标准的词（"最近""大客户"）用规则直接反问。
6. **必须有评测集**，主指标是**执行准确率**（跑 gold SQL 和模型 SQL，结果集规范化后比对），不是 SQL 字符串匹配（EM 恒为 0）。

还有一条实践结论值得抄：**表格类文档不要按 prose 切**，每行保留 document id / sheet / 列名 / 行号等元数据，检索时按元数据过滤而不是靠相似度。

## 三、目标架构（三层）

```
┌─ L3 编排层（LLM）─────────────────────────────┐
│  只做三件事：                                 │
│  1. 听懂问题 → 决定调哪个工具                 │
│  2. 从工具结果里挑该说的                       │
│  3. 用中文讲清楚（不做计算）                   │
└───────────────┬───────────────────────────────┘
                │ tool call
┌─ L2 计算层（确定性代码）──────────────────────┐
│  core/payroll.py                              │
│   · compute_deduction(basic, hours, prev, cur)│
│   · 参数来自 policy 表，不写死                 │
│   · 纯函数，可单测、可复现                     │
│  core/query.py                                │
│   · 只读 SQL，AST 白名单                       │
└───────────────┬───────────────────────────────┘
                │
┌─ L1 数据层（SQLite）──────────────────────────┐
│  employee / attendance / policy_params /       │
│  deduction_result / leave_balance              │
│  由 Excel 导入脚本一次性建好                   │
└───────────────────────────────────────────────┘
```

**RAG 的定位要改**：只用于非结构化文档（《病假扣款计算公式说明》《考勤制度》），**不参与任何数字回答**。这类文档量小（1 份 txt），直接塞进 prompt 比建向量库更快更准。

## 四、表结构草案

```sql
-- 员工主数据
CREATE TABLE employee (
  emp_id      TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  bu          TEXT,
  basic_salary REAL NOT NULL,
  schedule_hours REAL NOT NULL DEFAULT 168
);

-- 月度考勤事实（一个员工一个月一行）
CREATE TABLE attendance (
  emp_id     TEXT NOT NULL REFERENCES employee(emp_id),
  period     TEXT NOT NULL,          -- '2026-04'
  sick_hours REAL DEFAULT 0,
  special_sick_hours REAL DEFAULT 0,
  sick_days  REAL,                   -- 冗余，便于查询
  PRIMARY KEY (emp_id, period)
);

-- 政策参数（把 2510、21、0.25 这些从代码里挪出来）
CREATE TABLE policy_params (
  name  TEXT PRIMARY KEY,            -- min_wage / workdays_per_month / ...
  value REAL NOT NULL,
  note  TEXT
);

-- 计算结果（落库，可复现、可审计）
CREATE TABLE deduction_result (
  emp_id     TEXT,
  period     TEXT,
  branch     TEXT,                   -- A~F
  deduction  REAL,
  net_pay    REAL,
  computed_at TEXT,
  calc_version TEXT,                 -- 计算逻辑版本，便于回溯
  PRIMARY KEY (emp_id, period)
);
```

关键点：**数据进来，结果出去，都要落库**。这样任何一次回答都能追溯到具体行，而不是"模型说的"。

## 五、Langflow 里的拓扑

```
ChatInput
   ↓
Intent Router（LLM，结构化输出 → JSON: {tool, args}）
   ├─ tool=query   → SQL 查询组件（只读 + AST 校验）
   ├─ tool=calc    → 计算组件（调 core/payroll.py）
   ├─ tool=export  → 导出组件（生成 TSV/CSV）
   └─ tool=doc     → 文档直读（不做向量检索）
   ↓
Answer Composer（LLM，只负责把结构化结果讲成人话）
   ↓
ChatOutput
```

不建议在 Langflow 里实现护栏、自愈、投票、评测——这些放 Python 里，Langflow 只当门面。

## 六、评测（最小可行，先 15~20 题）

| 组 | 例 |
|:--|:--|
| 单值查询 | 张三 4 月病假扣款多少？ |
| 聚合 | 8 个人 4 月扣款合计？ |
| 排序 | 谁扣得最多？ |
| 条件过滤 | 实发低于 5000 的有谁？ |
| 分支判断 | 为什么刘一是 C 分支？ |
| 导出 | 导出所有人工资表 |
| 边界 | 病假 0 天的人扣款是多少？ |
| 歧义 | 最近情况怎么样？（应反问） |

指标：
- **执行准确率**（结果集比对）——主指标
- 数字精度（金额 2 位小数）
- 澄清召回率 / 误澄清率
- 平均延迟、token 消耗

每题带 gold 结果（不是 gold SQL），因为这里数据小，可以直接写期望值。

## 七、分阶段实施

| 阶段 | 内容 | 验收 |
|:--|:--|:--|
| P1 | Excel → SQLite 导入 + 表结构 | 8 人 4 月数据可 SQL 查到，数量对得上 |
| P2 | `core/payroll.py` 纯函数 + 参数表化 | 单测覆盖 A~F 六个分支，金额与交付包一致 |
| P3 | `core/query.py` 只读 SQL + AST 护栏 | 危险 SQL 被拒，正常查询可用 |
| P4 | 重连 Langflow（路由 + 三工具） | Playground 里 6 类问题都能答对 |
| P5 | 评测集 + 跑分 | 出第一份准确率报告，可重复跑 |

P1~P3 不用碰 Langflow，纯 Python 就能验收——这是刻意安排的：**先把确定的做对，再接编排**。
