# -*- coding: utf-8 -*-
"""core.payroll 单元测试：六个分支 + 与官方 Excel 结果的逐人比对。"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.payroll import Policy, compute_deduction  # noqa: E402

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sedl.db")


# ---------- 六个分支的手算用例 ----------
# 日薪 = 月薪 / 21（应出勤 168h）

def test_branch_A_full_deduction():
    """累计>22 且 过往>=22：(月薪-2510)/21*当月天数"""
    r = compute_deduction(basic_salary=5000, schedule_hours=168,
                          prev_cum_days=59, cum_total_days=80, month_sick_days=21)
    assert r.branch == "A"
    assert r.deduction == 2490.0
    assert r.net_pay == 2510.0


def test_branch_C_short_gap():
    """累计>22、过往<=22、差 1~5 天：(月薪-2510)/21*(累计-22) + 日薪*(21-过往)*25%"""
    r = compute_deduction(basic_salary=3000, schedule_hours=168,
                          prev_cum_days=21, cum_total_days=35, month_sick_days=14)
    assert r.branch == "C"
    # (3000-2510)/21*13 + 3000/21*0*0.25 = 303.33 -> 取整 303
    assert r.deduction == 303.0
    assert r.net_pay == 2697.0


def test_branch_C_keeps_cents_when_requested():
    """同一个数，按 2 位小数口径应得 303.33"""
    r = compute_deduction(basic_salary=3000, schedule_hours=168,
                          prev_cum_days=21, cum_total_days=35, month_sick_days=14,
                          rounding="cent")
    assert r.deduction == 303.33
    assert r.net_pay == 2696.67


def test_branch_B_gap_over_five():
    """累计>22、过往<=22、差>=6：分段 25% + 50%"""
    r = compute_deduction(basic_salary=8000, schedule_hours=168,
                          prev_cum_days=10, cum_total_days=30, month_sick_days=20)
    assert r.branch == "B"
    expected = ((8000 - 2510) / 21 * 8) + (8000 / 21 * 4 * 0.25) + (8000 / 21 * 7 * 0.5)
    assert r.deduction == round(expected)


def test_branch_D_long_leave():
    """累计<=22 且 当月>5：前 4 天 25%，超出部分 50%"""
    r = compute_deduction(basic_salary=8000, schedule_hours=168,
                          prev_cum_days=0, cum_total_days=20, month_sick_days=20)
    assert r.branch == "D"
    # 8000/21*4*0.25 + 8000/21*15*0.5 = 380.95 + 2857.14 = 3238.09 -> 3238
    assert r.deduction == 3238.0


def test_branch_E_short_leave():
    """累计<=22 且 当月>1：超出 1 天的部分按 25%"""
    r = compute_deduction(basic_salary=15000, schedule_hours=168,
                          prev_cum_days=4, cum_total_days=6, month_sick_days=2)
    assert r.branch == "E"
    assert r.deduction == 179.0   # 15000/21*1*0.25 = 178.57 -> 179


def test_branch_F_no_deduction():
    """当月病假 <=1 天且累计未超：不扣"""
    r = compute_deduction(basic_salary=10000, schedule_hours=168,
                          prev_cum_days=0, cum_total_days=1, month_sick_days=1)
    assert r.branch == "F"
    assert r.deduction == 0.0
    assert r.net_pay == 10000.0


def test_net_pay_floor():
    """扣到低于最低工资时，实发按 2510 兜底"""
    r = compute_deduction(basic_salary=2600, schedule_hours=168,
                          prev_cum_days=59, cum_total_days=80, month_sick_days=21)
    assert r.net_pay == 2510.0


def test_invalid_input_rejected():
    with pytest.raises(ValueError):
        compute_deduction(basic_salary=3000, schedule_hours=0,
                          prev_cum_days=0, cum_total_days=0, month_sick_days=0)


def test_partial_month_uses_actual_schedule_hours():
    """应出勤 128h 的月份，日薪应按 16 天算，而不是固定 21 天"""
    r = compute_deduction(basic_salary=3000, schedule_hours=128,
                          prev_cum_days=21, cum_total_days=35, month_sick_days=14)
    assert r.workdays == 16.0
    assert r.daily_rate == pytest.approx(187.5)


# ---------- 与官方 Excel 结果逐人比对 ----------
@pytest.mark.skipif(not os.path.exists(DB), reason="sedl.db 不存在，先跑 scripts/import_excel.py")
def test_against_official_excel_reference():
    """8 个人 2026-04 的扣款/实发，必须与官方计算过程表完全一致（取整口径）"""
    con = sqlite3.connect(DB)
    rows = con.execute("""
        SELECT a.emp_id, e.name, s.basic_salary, a.schedule_hours,
               c.prev_total_days, c.total_sick_days, c.month_sick_days,
               c.deduction, c.net_pay
        FROM attendance a
        JOIN employee e ON e.emp_id = a.emp_id
        JOIN salary_month s ON s.emp_id = a.emp_id AND s.period = a.period
        JOIN calc_reference c ON c.emp_id = a.emp_id AND c.period = a.period
        WHERE a.period = '2026-04'
        ORDER BY a.emp_id
    """).fetchall()
    con.close()
    assert len(rows) == 8, f"应有 8 条基准数据，实际 {len(rows)}"

    mismatches = []
    for eid, name, basic, sch, prev, total, mdays, exp_ded, exp_net in rows:
        r = compute_deduction(
            basic_salary=basic, schedule_hours=sch,
            prev_cum_days=prev, cum_total_days=total, month_sick_days=mdays)
        if abs(r.deduction - exp_ded) > 0.005 or abs(r.net_pay - exp_net) > 0.005:
            mismatches.append(
                f"{eid} {name}: 计算 {r.deduction}/{r.net_pay} "
                f"vs Excel {exp_ded}/{exp_net} (分支 {r.branch})")
    assert not mismatches, "与官方结果不一致：\n" + "\n".join(mismatches)
