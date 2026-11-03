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
  policy_params   政策参数：min_wage 最低工资、rate_low 0.25、rate_high 0.5 等

规则：
  1. 只写 SQLite 的 SELECT 语句，不要 INSERT/UPDATE/DELETE，不要建表。
  2. 只能查上面这 5 张表。
  3. 涉及扣款/实发等结果数字，直接查 calc_reference，不要自己算。
  4. 需要人名时 JOIN employee 取 name。
  5. 结果默认返回全部匹配行，不要加 LIMIT（系统会自动补）。
  6. 只有当缺少**关键**信息、无法确定查询范围时才返回 `CLARIFY: <问题>`
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


def extract_sql(model_output: str) -> str:
    """从模型输出里取出 SQL；若模型选择澄清则抛 ClarifyNeeded。"""
    m = CLARIFY.search(model_output or "")
    if m:
        raise ClarifyNeeded(m.group(1).strip())

    text = model_output or ""
    fences = SQL_FENCE.findall(text)
    if fences:
        text = fences[0]
    else:
        idx = re.search(r"\b(select|with)\b", text, re.I)
        if not idx:
            raise ValueError("模型输出里没有 SQL")
        text = text[idx.start():]
    sql = text.strip().strip("`").strip()
    sql = re.sub(r"^sql\s*:?\s*", "", sql, flags=re.I).strip()
    sql = sql.rstrip(";").strip()
    if not sql:
        raise ValueError("模型没有给出 SQL")
    if not sql.lower().startswith(("select", "with")):
        raise ValueError(f"抽取到的内容不是查询语句: {sql[:40]!r}")
    return sql


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
