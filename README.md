# 薪酬数据智能问答（Text2SQL + 确定性计算）

> 用自然语言查询薪资数据：**模型只写 SQL，数字由数据库产出**。
> 面向「结构化数据不该用向量检索」这一判断的完整工程实现：三层护栏、确定性计算、可复现评测。

[![tests](https://img.shields.io/badge/tests-48%20passed-brightgreen)]()
[![eval](https://img.shields.io/badge/eval-90%25%20strict%20%2F%2095%25%20lenient-blue)]()
[![python](https://img.shields.io/badge/python-3.10%2B-blue)]()

---

## 为什么做这个

第一版用的是标准 RAG：Excel 切片 → 向量库 → 检索 top_k=100 → 塞进 Prompt → 让模型回答。

结果**金额全是模型"算"出来的**，不是查出来的：

- 一行员工数据会被 1000 字符的切片劈成两半，检索回来缺表头
- `top_k=100` 大于库容（46 条），等于每次全量塞入，检索形同虚设
- 最后只能把答案硬编码进 Prompt —— 于是换一批数据就全错，而且**错了没人知道**

**核心判断**：向量相似度回答的是"像不像"，而薪资问的是"对不对"。
`WHERE name='张三'` 是二值判断，`cosine=0.83` 什么都不保证。

于是重做成三层。

---

## 架构

```
┌─ 编排层（LLM）───────────────────────────────┐
│  只做两件事：听懂问题 → 生成一条 SQL          │
│  数字一律不经过模型                            │
└──────────────────┬───────────────────────────┘
                   │
┌─ 计算/查询层（确定性代码）────────────────────┐
│  core/payroll.py   五分支扣款公式（纯函数）    │
│  core/query.py     只读查询 + 三层护栏         │
│  core/nl2sql.py    NL → SQL → 执行 → 表述      │
└──────────────────┬───────────────────────────┘
                   │
┌─ 数据层（SQLite）─────────────────────────────┐
│  employee / attendance / salary_month /        │
│  calc_reference / policy_params                │
└────────────────────────────────────────────────┘
```

### 三层护栏

| 层 | 拦什么 | 怎么拦 |
|:--|:--|:--|
| 1. 文本层 | 非 SELECT、多语句、危险关键字 | 前缀白名单 + 剥掉字符串字面量后的关键字匹配 |
| 2. SQLite authorizer | 越权表、写操作、ATTACH、PRAGMA | SQLite 官方授权回调，prepare 阶段逐动作裁决 |
| 3. 连接层 | 任何写操作 | `mode=ro` + `PRAGMA query_only=1`，物理拒绝 |

危险意图（"把员工表删了"）在**调模型之前**用规则拦掉——实测模型会把它当成歧义来反问，规则不会。

---

## 结果

| 指标 | 数值 | 怎么得到的 |
|:--|:--|:--|
| 扣款计算正确性 | **8/8 与官方 Excel 完全一致** | `test_against_official_excel_reference` |
| 执行准确率（严格） | **100%**（20/20） | `eval/report.md`，单次运行 |
| 执行准确率（宽松） | **100%**（20/20） | 容忍多给列 |
| 单题延迟 | 1.3 s ～ 6 s | 20 题 25 s ~ 117 s，随网络波动 |
| 测试 | **48 项通过** | 11 扣款 + 21 护栏 + 16 问答 |

评测口径是**执行准确率**：跑 gold SQL 和模型 SQL，把结果集规范化后比对。
不比较 SQL 文本——别名、JOIN 顺序一变文本就不同，但答案是同一个。

> 样本只有 20 题，**100% 不代表泛化能力**，它只能防回归。
> 前两轮分别是 90% / 85%，修的是两处提示词问题（policy_params 用英文键名、去重不能顶替姓名列）。
> 评测集本身也出过错：有一题的 gold 写成 `SELECT DISTINCT`，问的却是"每个人"。已记录在 `eval/CHANGELOG.md`。

---

## 快速开始

```bash
git clone https://github.com/cckslv/sedl-payroll-qa.git
cd sedl-payroll-qa
pip install pytest

python scripts/seed_demo.py     # 生成合成演示库（无需任何外部文件）
python -m pytest tests -q       # 48 项测试
```

提问（需要 `DEEPSEEK_API_KEY`，或写在 `.env` 里）：

```bash
python scripts/ask.py "谁扣款最多" --show-sql
```

评测：

```bash
python eval/run_eval.py
```

接入 Langflow：把 `langflow/sedl_agent_component.py` 贴进一个 Custom Component 节点，
`ChatInput → 该组件 → ChatOutput` 即可（组件内自带护栏，不依赖外部依赖）。

---

## 目录

```
core/
  payroll.py    病假扣款五分支公式（纯函数，参数外置）
  query.py      只读查询 + 三层护栏 + schema 描述
  nl2sql.py     提示词组装、SQL 抽取、澄清判定、结果表述
  llm.py        DeepSeek 调用（密钥从环境变量/.env/注册表读取）
scripts/
  seed_demo.py  生成合成演示数据
  ask.py        命令行问答
eval/
  questions.jsonl  20 题评测集（gold SQL）
  run_eval.py      执行准确率
  CHANGELOG.md     评测集变更记录
tests/           48 项单元测试
langflow/        可直接粘贴进 Langflow 的自定义组件
docs/            架构与重构记录
```

---

## 数据说明

仓库内数据**全部为合成数据**（8 名虚构员工、4 个月考勤），由 `scripts/seed_demo.py` 生成。
不包含任何真实员工信息，也不含客户业务数据。

---

## 技术栈

Python 3.10+ · SQLite（stdlib）· DeepSeek API · Langflow 1.9（可选）· pytest

---

## License

MIT
