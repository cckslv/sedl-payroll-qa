# -*- coding: utf-8 -*-
"""SEDL 病假扣款计算 —— 纯函数。

设计约束：
  1. 不读写文件、不联网、不看时间 —— 同样输入必然同样输出，可被单测固定。
  2. 所有政策参数从 Policy 传入，不写死在函数体里。
  3. 取整规则可控：Excel 官方公式是 ROUND(...,0) 取整到元（"yuan"），
     交付版 Prompt 用的是 2 位小数（"cent"）。口径待业务确认，先两种都支持。

公式来源：4_1_2_计算过程表 的 Excel 嵌套 IF，与原 salary_calc.py 一致。
"""
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

RoundMode = Literal["yuan", "cent"]

WORK_HOURS_PER_DAY = 8


@dataclass(frozen=True)
class Policy:
    """政策参数（对应 policy_params 表）。默认值取自 SEDL 现行规则。"""
    min_wage: float = 2510.0             # 最低工资，也是实发下限
    full_month_sick_threshold: float = 22.0   # 累计病假 > 此值进入 A/B/C 分支
    long_sick_days: float = 5.0          # D 分支：超过 5 天的部分按 rate_high
    paid_sick_days: float = 1.0          # E 分支：1 天以内不扣
    rate_low: float = 0.25
    rate_high: float = 0.50


@dataclass(frozen=True)
class DeductionResult:
    deduction: float      # 病假扣款
    net_pay: float        # 扣款后实发（不低于 min_wage）
    branch: str           # A~F，便于解释和排查
    workdays: float       # 应出勤天数 = schedule_hours / 8
    daily_rate: float     # 日薪 = basic_salary / workdays


def _round(value: float, mode: RoundMode) -> float:
    """Excel 的 ROUND 是四舍五入（half-up），Python 内置 round 是银行家舍入，必须区分。"""
    quant = Decimal("1") if mode == "yuan" else Decimal("0.01")
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def compute_deduction(
    *,
    basic_salary: float,
    schedule_hours: float,
    prev_cum_days: float,
    cum_total_days: float,
    month_sick_days: float,
    policy: Policy | None = None,
    rounding: RoundMode = "yuan",
) -> DeductionResult:
    """按 Excel 五分支公式计算病假扣款。

    参数
      basic_salary    基本工资
      schedule_hours  应出勤小时数（日薪 = 月薪 / (schedule_hours/8)）
      prev_cum_days   考勤月前累计病假总天数（Excel X 列）
      cum_total_days  累计病假总天数（Excel AB 列）
      month_sick_days 考勤月病假总天数（Excel U 列）
      rounding        "yuan" 取整到元（Excel 口径）/ "cent" 保留 2 位
    """
    p = policy or Policy()

    if schedule_hours <= 0:
        raise ValueError(f"应出勤小时数必须为正，收到 {schedule_hours!r}")
    if basic_salary < 0:
        raise ValueError(f"基本工资不能为负，收到 {basic_salary!r}")
    if cum_total_days < 0 or prev_cum_days < 0 or month_sick_days < 0:
        raise ValueError("病假天数不能为负")

    workdays = schedule_hours / WORK_HOURS_PER_DAY
    daily = basic_salary / workdays

    AB, X, U = cum_total_days, prev_cum_days, month_sick_days
    th = p.full_month_sick_threshold
    long_days = p.long_sick_days          # 5：长病假起算天数
    paid_days = p.paid_sick_days          # 1：免扣天数
    gap = th - X

    if AB > th and X >= th:
        branch = "A"
        raw = (basic_salary - p.min_wage) / workdays * U
    elif AB > th and X <= th and gap >= long_days + 1:
        # 相差 ≥ 6 天 → 分段：前段 25%，超长部分 50%
        branch = "B"
        raw = ((basic_salary - p.min_wage) / workdays * (AB - th)
               + daily * (long_days - 1) * p.rate_low
               + daily * (gap - long_days) * p.rate_high)
    elif AB > th and X <= th and 0 < gap < long_days + 1:
        # 客户的 Excel 是 IF(...,6>(22-X)>1,...)。Excel 不做链式比较，
        # 实际按 (6>(22-X))>1 求值，即「相差 < 6 天」，不是「1~5 天」。
        # 这里按公式的实际行为实现，保证与客户 Excel / 说明文档一致。
        branch = "C"
        raw = ((basic_salary - p.min_wage) / workdays * (AB - th)
               + daily * (th - 1 - X) * p.rate_low)
    elif AB <= th and U > long_days:
        branch = "D"
        raw = (daily * (long_days - 1) * p.rate_low
               + daily * (U - long_days) * p.rate_high)
    elif AB <= th and U > paid_days:
        branch = "E"
        raw = daily * (U - paid_days) * p.rate_low
    else:
        branch = "F"
        raw = 0.0

    deduction = _round(raw, rounding)
    net = _round(max(p.min_wage, basic_salary - deduction), rounding)

    return DeductionResult(
        deduction=deduction,
        net_pay=net,
        branch=branch,
        workdays=workdays,
        daily_rate=daily,
    )
