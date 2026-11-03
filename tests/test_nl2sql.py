# -*- coding: utf-8 -*-
"""core.nl2sql 测试：用假模型，不联网。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.nl2sql import (ClarifyNeeded, ask, build_prompt, compose_answer,  # noqa: E402
                         extract_sql)

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sedl.db")
pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="sedl.db 不存在")


def fake_llm(reply):
    calls = []

    def _llm(prompt):
        calls.append(prompt)
        return reply
    _llm.calls = calls
    return _llm


# ---------- 提示词 ----------
def test_prompt_contains_schema_and_rules():
    p = build_prompt(DB, "张三扣了多少")
    assert "calc_reference" in p
    assert "deduction" in p
    assert "张三扣了多少" in p
    assert "只写 SQLite 的 SELECT" in p


def test_prompt_includes_examples():
    p = build_prompt(DB, "问题", examples=[("示例问题", "SELECT 1")])
    assert "示例问题" in p and "SELECT 1" in p


# ---------- SQL 抽取 ----------
def test_extract_from_code_fence():
    assert extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1"


def test_extract_bare_select():
    assert extract_sql("好的，SQL 是：SELECT name FROM employee") == "SELECT name FROM employee"


def test_extract_with_cte():
    out = extract_sql("WITH t AS (SELECT 1) SELECT * FROM t")
    assert out.startswith("WITH")


def test_extract_strips_trailing_semicolon():
    assert not extract_sql("SELECT 1;").endswith(";")


def test_clarify_detected():
    with pytest.raises(ClarifyNeeded):
        extract_sql("CLARIFY: 请问要看哪个月份？")


def test_empty_output_rejected():
    with pytest.raises(ValueError):
        extract_sql("抱歉，我不能回答")


# ---------- 端到端 ----------
def test_ask_end_to_end():
    sql = ("SELECT e.name, c.deduction, c.net_pay FROM calc_reference c "
           "JOIN employee e ON e.emp_id=c.emp_id WHERE e.name='张三'")
    a = ask(DB, "张三扣了多少", fake_llm(sql))
    assert a.error is None
    assert a.result is not None
    assert a.result.rows == [("张三", 3238.0, 4762.0)]
    assert "3238" in a.text


def test_ask_blocks_unsafe_sql():
    """模型写 DROP 也执行不了"""
    a = ask(DB, "删掉所有人", fake_llm("```sql\nDROP TABLE employee\n```"))
    assert a.result is None
    assert a.error is not None


def test_ask_blocks_unknown_table():
    a = ask(DB, "查密码", fake_llm("SELECT * FROM users"))
    assert a.result is None and a.error


def test_ask_clarify_flow():
    a = ask(DB, "工资多少", fake_llm("CLARIFY: 请问是哪位员工、哪个月份？"))
    assert a.needs_clarify is True
    assert "张三" not in a.text


def test_ask_handles_llm_failure():
    def boom(_):
        raise RuntimeError("网络断了")
    a = ask(DB, "张三扣多少", boom)
    assert a.error and "网络断了" in a.text


def test_ask_empty_result():
    a = ask(DB, "查不存在的人", fake_llm(
        "SELECT * FROM employee WHERE name='不存在'"))
    assert a.result is not None
    assert a.result.rows == []
    assert "没有匹配" in a.text


# ---------- 结果表述 ----------
def test_compose_without_llm_keeps_sql():
    sql = "SELECT COUNT(*) AS n FROM employee"
    a = ask(DB, "多少人", fake_llm(sql))
    out = compose_answer(a)
    assert "8" in out
    assert "SQL" in out


def test_compose_with_llm_uses_table():
    sql = "SELECT SUM(deduction) AS total FROM calc_reference"
    a = ask(DB, "总扣款", fake_llm(sql))
    out = compose_answer(a, llm=fake_llm("所有员工病假扣款合计 14070 元。"))
    assert "14070" in out

# ---------- 写意图拦截（规则层，调模型之前）----------
@pytest.mark.parametrize("q", [
    "帮我把员工表删了",
    "把张三的工资改成 99999",
    "删库跑路",
    "把最低工资参数调到 3000",
    "把数据全清了",
    "帮我删了张三的记录",
])
def test_write_intent_blocked_without_llm(q):
    def boom(_):
        raise AssertionError("写意图不应调用模型")
    a = ask(DB, q, boom)
    assert a.error is not None
    assert "只读" in a.text
    assert a.result is None


def test_normal_question_not_blocked():
    """正常的问句不能被写意图规则误杀"""
    from core.nl2sql import WRITE_INTENT
    assert not WRITE_INTENT.search("张三扣了多少")
    assert not WRITE_INTENT.search("谁扣款最多")


# ---------- 抽取鲁棒性 ----------
def test_extract_prefers_last_fence():
    """多个代码块时取最后一个（模型常在推理后给出最终 SQL）"""
    out = "```sql\nSELECT 1\n```\n再想想\n```sql\nSELECT name FROM employee\n```"
    assert extract_sql(out) == "SELECT name FROM employee"


def test_extract_skips_text_fence():
    out = "```text\n先查员工表\n```\n```sql\nSELECT COUNT(*) FROM employee\n```"
    assert extract_sql(out) == "SELECT COUNT(*) FROM employee"


def test_extract_handles_leading_comment():
    out = "-- 查询员工\nSELECT name FROM employee"
    assert extract_sql(out).startswith("SELECT")


def test_extract_sql_plus_clarify_keeps_sql():
    """SQL 与 CLARIFY 同时出现时，优先执行 SQL"""
    out = "```sql\nSELECT name FROM employee\n```\nCLARIFY: 是否要按月份分组？"
    assert extract_sql(out).startswith("SELECT")