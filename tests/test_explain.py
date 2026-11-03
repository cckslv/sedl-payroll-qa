# -*- coding: utf-8 -*-
"""core.explain 归因测试：数字必须与库一致，方向不能反。"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.explain import explain, is_analysis_question  # noqa: E402
from core.nl2sql import ask  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sedl.db")
pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="sedl.db 不存在")


def test_detect_analysis_question():
    assert is_analysis_question("工资最高的人比工资最低的高多少？原因是什么？")
    assert is_analysis_question("为什么赵六比刘一高这么多")
    assert not is_analysis_question("张三扣了多少")
    assert not is_analysis_question("谁扣款最多")


def test_net_pay_gap_matches_db():
    """实发最高与最低的差额，必须等于库里算出来的"""
    con = sqlite3.connect(DB)
    hi, lo = con.execute(
        "SELECT MAX(net_pay), MIN(net_pay) FROM calc_reference WHERE period='2026-04'").fetchone()
    con.close()
    text, _ = explain(DB, "实发最高和最低差多少")
    gap = hi - lo
    assert f"{gap:,.2f}".rstrip("0").rstrip(".") in text


def test_basic_salary_gap():
    text, _ = explain(DB, "基本工资最高和最低相差多少")
    assert "20,000" in text and "3,000" in text and "17,000" in text


def test_deduction_shrinks_gap_direction():
    """扣款多的一方是高薪方时，扣款应缩小差距，文案不能说反"""
    text, _ = explain(DB, "实发最高和最低差多少")
    assert "缩小" in text
    assert "进一步拉大" not in text


def test_branch_attribution_present():
    text, _ = explain(DB, "为什么赵六比刘一高")
    # 两人分支不同：赵六 A、刘一 C
    assert "A 分支" in text and "C 分支" in text
    assert "规则：" in text


def test_tie_is_marked():
    text, _ = explain(DB, "实发最高和最低差多少")
    assert "并列" in text


def test_ask_routes_to_explain_without_llm():
    """归因路径不应调用模型：传一个会爆炸的 llm"""
    def boom(_):
        raise AssertionError("分析型问题不应该调用模型")

    a = ask(DB, "工资最高比最低高多少？原因是什么？", boom)
    assert a.error is None
    assert "相差" in a.text
    assert a.sql  # 仍返回用于复核的 SQL
