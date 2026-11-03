# -*- coding: utf-8 -*-
"""自然语言 → SQL → 只读执行 → 结果表述。

分层：
  build_prompt()  组装 schema + 业务口径 + 规则
  extract_sql()   从模型输出里抠出 SQL（兼容 ```sql 代码块 / 裸 SELECT）
  ask()           一次完整问答：生成 → 抽取 → 护栏执行 → 结果表格
  compose_answer() 把表格变成人话（可选；默认不调模型，避免数字被改写）

设计原则：**模型只负责写查询，数字一律由数据库产出**。
模型输出的 SQL 必须经过 core.query 的三道护栏才能执行，模型写错/写坏都跑不通。
"""
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .query import ALLOWED_TABLES, QueryResult, UnsafeQueryError, describe_schema, run_query

SCHEMA_NOTE = """
表结构：
{schema}

字段口径：
  employee        员工主数据：emp_id 工号、name 姓名、bu 事业部
  attendance      月度考勤（小时）：schedule_hours 应出勤、special_sick_hours 特殊病假、
                  sick_hours 病假、total_leave_hours 总请假
  salary_month    月度基本工资：period 形如 '2026-04'
  calc_reference  官方 Excel 算出的结果：month_sick_days 当月病假天数、
                  prev_total_days 考勤月前累计、total_sick_days 累计总天数、
                  deduction 病假扣款、net_pay 实发工资
  policy_params   政策参数（name 为英文键名，value 为数值）：
                  min_wage=最低工资、workdays_per_month=月计薪天数、
                  full_month_sick_threshold=累计病假门槛、long_sick_days=长病假天数、
                  paid_sick_days=免扣天数、rate_low=25% 比例、rate_high=50% 比例、
                  rate_full=100% 比例

规则：
  1. 只写 SQLite 的 SELECT 语句，不要 INSERT/UPDATE/DELETE，不要建表。
  2. 只能查上面这 5 张表。
  3. 涉及扣款/实发等结果数字，直接查 calc_reference，不要自己算。
  4. 需要人名时 JOIN employee 取 name。
  5. 结果默认返回全部匹配行，不要加 LIMIT（系统会自动补）。
  6. 查 policy_params 时 **必须用上表里的英文键名**（如 min_wage），
     不要用中文名称当 name 去匹配，表里存的是英文键。
  7. 如果同一取值在结果里会重复出现（例如按月统一的应出勤小时数），可用 SELECT DISTINCT 去重；
     但**去重必须保留问题所问的列**：问"每个人"就要返回姓名 name，不要拿 emp_id 顶上，
     也不要因为去重把 JOIN employee 省掉。
  8. 只有当缺少**关键**信息、无法确定查询范围时才返回 `CLARIFY: <问题>`
     （例如没说人名且问的是个人数据、没说月份且数据跨月）。
     能合理推断的不要反问：全表只有一个员工/一种取值时，直接查。
     问"合计/平均/几个人"这类全局问题时，不要反问是哪个人。
"""

SYSTEM_PROMPT = (
    "你是 SEDL 公司的薪酬数据查询助手。"
    "你的唯一工作是把中文问题翻译成一条 SQLite SELECT 语句。"
    "不要解释，不要输出 SQL 以外的内容。"
)

SQL_FENCE = re.compile(r"```(?:sql)?\s*(.+?)```", re.I | re.S)
CLARIFY = re.compile(r"^\s*CLARIFY[:：]\s*(.+)$", re.I | re.M)

# 写意图：动词 + 数据对象，双序匹配（"把员工表删了" 与 "删掉员工表" 都要命中）。
# 放在调模型之前拦：模型会把危险请求当歧义来反问，规则不会。
WRITE_INTENT = re.compile(
    r"(删\s*除|删\s*掉|删了|删|清\s*空|清\s*除|清掉|清|改\s*成|改掉|修\s*改|更\s*新|插\s*入|新\s*增|"
    r"写\s*入|覆\s*盖|调\s*到|设\s*成|设\s*置|移\s*除|重\s*置|去掉|录入|导入|"
    r"\bdrop\b|\bdelete\b|\bupdate\b|\binsert\b|\btruncate\b|\balter\b)"
    r"[^。？！\n]{0,12}?"
    r"(表|数据|记录|几行|库|字段|参数|工资|薪资|实发|底薪|员工|"
    r"employee|attendance|salary_month|calc_reference|policy_params|最低工资)"
    r"|"
    r"(表|数据|记录|库|字段|参数|工资|薪资|实发|底薪|员工|"
    r"employee|attendance|salary_month|calc_reference|policy_params)"
    r"[^。？！\n]{0,8}?"
    r"(删|清|清空|清除|清掉|改成|改掉|修改|覆盖|调到|设成|移除|重置|\bdrop\b|\bdelete\b|\bupdate\b)",
    re.I,
)

REFUSE_TEXT = (
    "本系统只支持只读查询，不会修改、删除或新增任何数据。\n"
    "如果你想查某人的扣款或实发工资，请直接问，例如：张三 4 月扣了多少？"
)


@dataclass
class Answer:
    question: str
    sql: str | None
    result: QueryResult | None
    text: str
    needs_clarify: bool = False
    error: str | None = None
    tables_used: list[str] = field(default_factory=list)


def build_prompt(db_path: str, question: str, examples: Iterable[tuple[str, str]] = ()) -> str:
    """组装发给模型的完整提示词。"""
    schema = describe_schema(db_path)
    parts = [SYSTEM_PROMPT, "", SCHEMA_NOTE.format(schema=schema)]
    ex = list(examples)
    if ex:
        parts.append("示例：")
        for q, sql in ex:
            parts.append(f"  问：{q}\n  SQL：{sql}")
    parts.append("")
    parts.append(f"问题：{question}")
    parts.append("SQL：")
    return "\n".join(parts)


FENCE = re.compile(r"```[ \t]*([A-Za-z0-9_+-]*)[ \t]*\r?\n(.*?)```", re.S)
SQL_TAGS = {"", "sql", "sqlite", "sqlite3", "sqlite-sql"}
CLARIFY_ANY = re.compile(r"CLARIFY[:：]\s*(.+)", re.I)


def _strip_sql_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"--[^\n]*", " ", text)


def _first_statement(text: str) -> str | None:
    """按 sqlite3.complete_statement 切出第一条完整语句。"""
    import sqlite3
    buf = []
    for line in text.splitlines():
        buf.append(line)
        candidate = "\n".join(buf).strip()
        if candidate and sqlite3.complete_statement(candidate + ";"):
            return candidate.rstrip(";").strip()
    candidate = "\n".join(buf).strip().rstrip(";").strip()
    return candidate or None


def extract_sql(model_output: str) -> str:
    """从模型输出里取出 SQL；若模型只给了澄清则抛 ClarifyNeeded。

    规则（都是踩过的坑）：
      · 只认 sql/sqlite/sqlite3 或无标签的代码块，图文块（```text）跳过
      · 多个候选时取**最后一个** —— 模型常在推理后给出最终 SQL
      · 先抽 SQL 再找 CLARIFY，避免「SQL + 澄清」混合输出丢掉可执行语句
    """
    text = model_output or ""

    candidates = []
    for tag, body in FENCE.findall(text):
        if tag.lower() in SQL_TAGS:
            candidates.append(body)
    if not candidates:
        candidates = [text]

    parsed = []
    for c in candidates:
        c = _strip_sql_comments(c)
        start = re.search(r"\b(select|with)\b", c, re.I)
        if not start:
            continue
        s = _first_statement(c[start.start():])
        if not s:
            continue
        s = re.sub(r"^\s*sql\s*[:：]\s*", "", s, flags=re.I).strip()
        if s.lower().startswith(("select", "with")):
            parsed.append(s.rstrip(";").strip())

    if parsed:
        return parsed[-1]

    m = CLARIFY_ANY.search(text)
    if m:
        raise ClarifyNeeded(m.group(1).strip())
    raise ValueError("模型输出里没有可执行的 SQL")


class ClarifyNeeded(Exception):
    """问题信息不足，需要反问用户。"""


def _format_table(r: QueryResult, max_rows: int = 20) -> str:
    if not r.rows:
        return "（没有匹配的记录）"
    lines = [" | ".join(r.columns), "-" * 40]
    for row in r.rows[:max_rows]:
        lines.append(" | ".join("" if v is None else str(v) for v in row))
    if len(r.rows) > max_rows:
        lines.append(f"... 共 {len(r.rows)} 行，只显示前 {max_rows} 行")
    return "\n".join(lines)


def ask(db_path: str, question: str,
        llm: Callable[[str], str],
        examples: Iterable[tuple[str, str]] = ()) -> Answer:
    """一次完整问答。llm 是「提示词 -> 文本」的可调用对象，便于替换与测试。"""
    # 写意图在调模型之前就拦掉
    if WRITE_INTENT.search(question or ""):
        return Answer(question=question, sql=None, result=None, text=REFUSE_TEXT,
                      error="只读系统，拒绝写操作")

    # 分析型问题（为什么 / 原因 / 差多少）走确定性归因，不让模型生成数字
    from .explain import explain, is_analysis_question
    if is_analysis_question(question):
        text, sql = explain(db_path, question)
        return Answer(question=question, sql=sql, result=None, text=text)

    prompt = build_prompt(db_path, question, examples)
    try:
        raw = llm(prompt)
    except Exception as e:  # 模型/网络错误
        return Answer(question=question, sql=None, result=None,
                      text=f"模型调用失败：{e}", error=str(e))

    try:
        sql = extract_sql(raw)
    except ClarifyNeeded as c:
        return Answer(question=question, sql=None, result=None,
                      text=f"需要确认：{c}", needs_clarify=True)
    except ValueError as e:
        return Answer(question=question, sql=None, result=None,
                      text=f"未能生成有效 SQL：{e}", error=str(e))

    try:
        r = run_query(db_path, sql)
    except UnsafeQueryError as e:
        return Answer(question=question, sql=sql, result=None,
                      text=f"查询被拒绝：{e}", error=str(e))
    except sqlite3.Error as e:
        return Answer(question=question, sql=sql, result=None,
                      text=f"查询出错：{e}", error=str(e))

    return Answer(question=question, sql=sql, result=r,
                  text=_format_table(r), tables_used=r.tables_used)


def compose_answer(answer: Answer, llm: Callable[[str], str] | None = None) -> str:
    """把问答结果整理成给用户看的中文。

    默认不调模型：直接把表格 + SQL 返回，数字绝不会被改写。
    传入 llm 时才让它润色，但要求它引用表格里的数字，不得新增。
    """
    if not answer.result:
        return answer.text
    if llm is None:
        return f"{answer.text}\n\n（SQL: {answer.sql}）"
    prompt = (
        "下面是一张查询结果表，请用一句中文把结论说清楚。"
        "要求：只能引用表里的数字，不得计算或推测表中没有的数；"
        "如果表为空，就说没查到。\n\n"
        f"表：\n{answer.text}\n"
    )
    try:
        return llm(prompt).strip()
    except Exception:
        return answer.text
