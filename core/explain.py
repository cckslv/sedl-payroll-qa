# -*- coding: utf-8 -*-
"""分析型问题：工资差异的确定性归因。

为什么不让模型写解释：
  扣款由五分支公式决定，走哪个分支、每段扣多少，都能从数据反推。
  让模型"讲原因"等于把已经确定的因果重新交给概率模型复述，是倒退。
  这里的每一句话、每一个数字，都能用 SQL 复核。

支持三种度量：实发工资 / 基本工资 / 病假扣款。
问天数、出勤、加班等本模块没有的度量时，明确回一句"不支持"，不硬套实发口径。
"""
import re
import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from .payroll import Policy, compute_deduction

ANALYSIS_HINTS = re.compile(
    r"(为什么|原因|怎么会|差多少|相差|差几|高出|低出|拉开|归因|怎么算的|算出来)", re.I)

METRIC_NET = "net_pay"
METRIC_BASIC = "basic_salary"
METRIC_DED = "deduction"

METRIC_CN = {METRIC_NET: "实发工资", METRIC_BASIC: "基本工资", METRIC_DED: "病假扣款"}

ROW_SQL = """
SELECT e.name, s.basic_salary, a.schedule_hours,
       c.month_sick_days, c.prev_total_days, c.total_sick_days,
       c.deduction, c.net_pay
FROM calc_reference c
JOIN employee e ON e.emp_id = c.emp_id
JOIN salary_month s ON s.emp_id = c.emp_id AND s.period = c.period
JOIN attendance a ON a.emp_id = c.emp_id AND a.period = c.period
WHERE c.period = ?
ORDER BY e.name
"""

BRANCH_DOC = {
    "A": "累计病假 > 22 天且考勤月前已满 22 天 → 按 (月薪−2510)/计薪天数×当月病假天数 全额扣。",
    "B": "累计 > 22 天、考勤月前不足 22 天且相差 ≥ 6 天 → (月薪−2510)/计薪天数×(累计−22) + 日薪×4×25% + 日薪×(22−前累计−5)×50%。",
    "C": "累计 > 22 天、考勤月前不足 22 天且相差 1~5 天 → (月薪−2510)/计薪天数×(累计−22) + 日薪×(21−前累计)×25%。",
    "D": "累计 ≤ 22 天且当月病假 > 5 天 → 日薪×4×25% + 日薪×(当月−5)×50%。",
    "E": "累计 ≤ 22 天且当月病假 1~5 天 → 日薪×(当月−1)×25%。",
    "F": "不满足以上任一分支 → 不扣款。",
}


def is_analysis_question(question: str) -> bool:
    return bool(ANALYSIS_HINTS.search(question or ""))


def pick_metric(question: str):
    """返回度量键；识别不出返回 None（由调用方转成澄清）。"""
    q = question or ""
    # 明确是别的度量（天数/出勤/加班）→ 本模块不支持，别硬套实发
    if re.search(r"(天数|小时|出勤|加班|请假|迟到|早退)", q):
        return None
    if re.search(r"(病假扣款|扣款|扣了多少|扣得最多|扣得多|扣款最高|扣款最低)", q):
        return METRIC_DED
    if re.search(r"(基本工资|月薪|底薪|基本薪资)", q):
        return METRIC_BASIC
    if re.search(r"(实发|到手|实际到手|税后)", q):
        return METRIC_NET
    # 兜底：提到"工资/薪资"或比较高低，默认按实发解释
    return METRIC_NET


def parse_period(question: str) -> str | None:
    """从问题里解析年份-月份；解析不到返回 None。"""
    q = question or ""
    m = re.search(r"(20\d{2})\s*[-/年]\s*(\d{1,2})", q)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"(\d{1,2})\s*月", q)
    if m:
        return f"2026-{int(m.group(1)):02d}"
    return None


def _rows(db_path: str, period: str):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = ["name", "basic_salary", "schedule_hours", "month_sick_days",
                "prev_total_days", "total_sick_days", "deduction", "net_pay"]
        return [dict(zip(cols, r)) for r in con.execute(ROW_SQL, (period,)).fetchall()]
    finally:
        con.close()


def available_periods(db_path: str) -> list[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT DISTINCT period FROM calc_reference ORDER BY period").fetchall()]
    finally:
        con.close()


def _branch_of(row: dict, policy: Policy):
    r = compute_deduction(
        basic_salary=row["basic_salary"], schedule_hours=row["schedule_hours"],
        prev_cum_days=row["prev_total_days"], cum_total_days=row["total_sick_days"],
        month_sick_days=row["month_sick_days"], policy=policy, rounding="yuan")
    return r.branch, r.daily_rate


def _fmt(v: float) -> str:
    """金额两位小数（千分位）。用 Decimal 四舍五入，与 payroll 口径一致。

    不能用 f"{v:,.2f}"：那是二进制浮点 + 银行家舍入，2.675 会变成 2.67。
    """
    d = Decimal(repr(float(v))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{d:,.2f}"


def _fmt_short(v: float) -> str:
    """天数类：整数不带小数位。"""
    d = Decimal(repr(float(v))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{d:,.2f}"
    return s[:-3] if s.endswith(".00") else s


def _named(question: str, rows: list[dict]) -> list[dict]:
    """问题里点名的人（按出现顺序）。"""
    q = question or ""
    hits = []
    for r in rows:
        idx = q.find(r["name"])
        if idx >= 0:
            hits.append((idx, r))
    return [r for _, r in sorted(hits, key=lambda x: x[0])]


def explain(db_path: str, question: str) -> tuple[str, str | None]:
    """返回 (解释文本, 用到的 SQL)。SQL 里的 ? 已替换为实际 period。"""
    metric = pick_metric(question)
    if metric is None:
        return ("这个问题涉及工资口径之外的度量（如天数、出勤、加班），"
                "归因分析目前只支持「实发工资 / 基本工资 / 病假扣款」三类。"
                "可以直接问具体数值，例如：张三 4 月病假几天？", None)

    periods = available_periods(db_path)
    if not periods:
        return "数据库里没有可分析的月度数据。", None

    period = parse_period(question) or periods[-1]
    if period not in periods:
        return (f"{period} 没有官方基准数据，无法做归因。"
                f"可用月份：{'、'.join(periods)}。", None)

    rows = _rows(db_path, period)
    if not rows:
        return f"{period} 没有可分析的数据。", None

    sql = ROW_SQL.replace("?", f"'{period}'").strip()
    cn = METRIC_CN[metric]
    values = [r[metric] for r in rows]
    hi, lo = max(values), min(values)

    if abs(hi - lo) < 0.005:
        return f"{period} 全部 {len(rows)} 人的{cn}都是 {_fmt(hi)} 元，没有高低差异。", sql

    tops = [r for r in rows if abs(r[metric] - hi) < 0.005]
    bots = [r for r in rows if abs(r[metric] - lo) < 0.005]

    # 问题里点名了两个人 → 只比这两人，不被全局最高/最低带跑
    named = _named(question, rows)
    if len(named) >= 2:
        a, b = named[0], named[1]
        if a[metric] < b[metric]:
            a, b = b, a
        tops, bots = [a], [b]
        hi, lo = a[metric], b[metric]

    def names(rs):
        body = "、".join(r["name"] for r in rs)
        return f"{body}（并列 {len(rs)} 人）" if len(rs) > 1 else body

    policy = Policy()
    lines = [
        f"{period} {cn}最高的是 {names(tops)}，{_fmt(hi)} 元；"
        f"最低的是 {names(bots)}，{_fmt(lo)} 元；相差 {_fmt(hi - lo)} 元。",
        "",
    ]

    # 逐对拆解：每个最低者都单独说明，避免"只解释其中一人"
    for top in tops:
        for bot in bots:
            lines.extend(_explain_pair(top, bot, metric, policy))
            lines.append("")

    return "\n".join(lines).strip() + "\n", sql


def _explain_pair(top: dict, bot: dict, metric: str, policy: Policy) -> list[str]:
    """解释一对高低选手的差异来源。"""
    t, b = top, bot
    out = [f"【{t['name']} vs {b['name']}】"]

    basic_gap = t["basic_salary"] - b["basic_salary"]
    if metric == METRIC_BASIC:
        out.append(f"- 基本工资：{_fmt(t['basic_salary'])} vs {_fmt(b['basic_salary'])}，"
                   f"相差 {_fmt(basic_gap)} 元，这就是全部差异来源。")
        return out

    if abs(basic_gap) > 0.005:
        direction = "" if basic_gap >= 0 else "（对方更高）"
        out.append(f"- 基本工资：{_fmt(t['basic_salary'])} vs {_fmt(b['basic_salary'])}，"
                   f"相差 {_fmt(abs(basic_gap))} 元{direction}。")
    else:
        out.append(f"- 基本工资：两人相同，均为 {_fmt(t['basic_salary'])} 元，差异不来自底薪。")

    tb, t_daily = _branch_of(t, policy)
    bb, b_daily = _branch_of(b, policy)
    ded_gap = t["deduction"] - b["deduction"]

    if metric == METRIC_DED:
        out.append(f"- 病假扣款：{_fmt(t['deduction'])} 元（{tb} 分支） vs "
                   f"{_fmt(b['deduction'])} 元（{bb} 分支），相差 {_fmt(ded_gap)} 元。")
    else:
        if abs(ded_gap) < 0.005:
            judge = "两人扣款相同，差距完全由基本工资决定。"
        elif ded_gap > 0:
            judge = f"扣款把差距缩小了 {_fmt(ded_gap)} 元（高薪方扣得更多）。"
        else:
            judge = f"扣款把差距拉大了 {_fmt(-ded_gap)} 元（低薪方反而扣得更多）。"
        out.append(f"- 病假扣款：{_fmt(t['deduction'])} 元（{tb} 分支） vs "
                   f"{_fmt(b['deduction'])} 元（{bb} 分支），相差 {_fmt(ded_gap)} 元。{judge}")

    out.append(f"  · {t['name']}：日薪 {_fmt(t_daily)} 元，当月病假 {_fmt_short(t['month_sick_days'])} 天，"
               f"考勤月前累计 {_fmt_short(t['prev_total_days'])} 天，累计 {_fmt_short(t['total_sick_days'])} 天 → {tb} 分支")
    out.append(f"  · {b['name']}：日薪 {_fmt(b_daily)} 元，当月病假 {_fmt_short(b['month_sick_days'])} 天，"
               f"考勤月前累计 {_fmt_short(b['prev_total_days'])} 天，累计 {_fmt_short(b['total_sick_days'])} 天 → {bb} 分支")
    out.append(f"  · {tb} 规则：{BRANCH_DOC.get(tb, '')}")
    if bb != tb:
        out.append(f"  · {bb} 规则：{BRANCH_DOC.get(bb, '')}")

    if metric == METRIC_NET:
        sign = "−" if ded_gap > 0 else "+"
        out.append(f"  ＝ 差距 {_fmt(t['net_pay'] - b['net_pay'])} 元 = 基本工资差 {_fmt(basic_gap)} 元 "
                   f"{sign} 扣款差 {_fmt(abs(ded_gap))} 元")
    return out
