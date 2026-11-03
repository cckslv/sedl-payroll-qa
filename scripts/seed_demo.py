# -*- coding: utf-8 -*-
"""生成演示数据库（全部为合成数据，不含任何真实员工信息）。

用途：让仓库克隆下来就能跑测试与评测，不依赖任何外部 Excel 或客户数据。

用法:
  python scripts/seed_demo.py
"""
import os
import sqlite3

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(BASE, "data", "sedl.db")

SCHEMA = """
DROP TABLE IF EXISTS employee;
DROP TABLE IF EXISTS attendance;
DROP TABLE IF EXISTS salary_month;
DROP TABLE IF EXISTS calc_reference;
DROP TABLE IF EXISTS policy_params;

CREATE TABLE employee (
  emp_id TEXT PRIMARY KEY,
  name   TEXT NOT NULL,
  bu     TEXT
);

CREATE TABLE attendance (
  emp_id  TEXT NOT NULL REFERENCES employee(emp_id),
  period  TEXT NOT NULL,
  schedule_hours      REAL,
  special_sick_hours  REAL,
  sick_hours          REAL,
  total_leave_hours   REAL,
  PRIMARY KEY (emp_id, period)
);

CREATE TABLE salary_month (
  emp_id TEXT NOT NULL REFERENCES employee(emp_id),
  period TEXT NOT NULL,
  basic_salary REAL NOT NULL,
  PRIMARY KEY (emp_id, period)
);

CREATE TABLE calc_reference (
  emp_id TEXT NOT NULL,
  period TEXT NOT NULL,
  month_special_days REAL,
  month_regular_days REAL,
  month_sick_days    REAL,
  prev_total_days    REAL,
  total_sick_days    REAL,
  deduction REAL,
  net_pay   REAL,
  source_file TEXT,
  PRIMARY KEY (emp_id, period)
);

CREATE TABLE policy_params (
  name  TEXT PRIMARY KEY,
  value REAL NOT NULL,
  unit  TEXT,
  note  TEXT
);
"""

# emp_id, name, bu, 月薪, 应出勤h, 特殊病假h, 病假h, 当月病假天数, 前累计, 累计, 扣款, 实发
EMPLOYEES = [
    ("10000001", "刘一", 3000, 168, 8, 104, 14, 21, 35, 303, 2697),
    ("10000002", "陈二", 5000, 168, 0, 168, 21, 59, 80, 2490, 2510),
    ("10000003", "张三", 8000, 168, 8, 152, 20, 0, 20, 3238, 4762),
    ("10000004", "李四", 4000, 168, 0, 168, 21, 59, 80, 1490, 2510),
    ("10000005", "王五", 6000, 168, 0, 168, 21, 59, 80, 3490, 2510),
    ("10000006", "赵六", 20000, 168, 8, 16, 3, 37, 40, 2499, 17501),
    ("10000007", "周七", 15000, 168, 8, 8, 2, 4, 6, 179, 14821),
    ("10000008", "钱八", 16000, 168, 8, 16, 3, 1, 4, 381, 15619),
]

BU = "DEMO"
# 各月应出勤小时（演示用，2 月为短月）
SCHEDULE = {"2026-01": 168, "2026-02": 128, "2026-03": 176, "2026-04": 168}

POLICY = [
    ("min_wage", 2510, "CNY/月", "最低工资标准，也是实发下限"),
    ("workdays_per_month", 21, "天", "计薪工作日数（日薪=月薪/21）"),
    ("full_month_sick_threshold", 22, "天", "累计病假超过此天数进入 A/B/C 分支"),
    ("long_sick_days", 5, "天", "D 分支中超过 5 天的部分按 rate_high"),
    ("paid_sick_days", 1, "天", "E 分支中 1 天以内不扣"),
    ("rate_low", 0.25, "比例", "前段病假扣款比例"),
    ("rate_high", 0.50, "比例", "超长病假扣款比例"),
    ("rate_full", 1.0, "比例", "A 分支全额扣"),
]


def main():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    con = sqlite3.connect(DB)
    con.executescript(SCHEMA)
    cur = con.cursor()

    for emp_id, name, basic, sch, sp, sk, mdays, prev, total, ded, net in EMPLOYEES:
        cur.execute("INSERT INTO employee VALUES (?,?,?)", (emp_id, name, BU))
        cur.execute("INSERT INTO salary_month VALUES (?,?,?)", (emp_id, "2026-04", basic))
        cur.execute("INSERT INTO attendance VALUES (?,?,?,?,?,?)",
                    (emp_id, "2026-04", sch, sp, sk, sp + sk))
        cur.execute("INSERT INTO calc_reference VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (emp_id, "2026-04", sp / 8.0, sk / 8.0, mdays, prev, total,
                     float(ded), float(net), "synthetic_demo"))

    # 其余月份只补考勤（无薪资/结果数据），用于跨月查询与行数测试
    for period, sch in SCHEDULE.items():
        if period == "2026-04":
            continue
        for emp_id, name, *_ in EMPLOYEES:
            cur.execute("INSERT INTO attendance VALUES (?,?,?,?,?,?)",
                        (emp_id, period, sch, 0, 0, 0))
            cur.execute("INSERT OR IGNORE INTO salary_month VALUES (?,?,?)",
                        (emp_id, period, 0))

    cur.executemany("INSERT INTO policy_params VALUES (?,?,?,?)", POLICY)
    con.commit()

    print(f"已生成 {DB}")
    for t in ("employee", "attendance", "salary_month", "calc_reference", "policy_params"):
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t:<16} {n} 行")
    total = cur.execute("SELECT SUM(deduction) FROM calc_reference").fetchone()[0]
    print(f"  病假扣款合计：{total}")
    con.close()


if __name__ == "__main__":
    main()
