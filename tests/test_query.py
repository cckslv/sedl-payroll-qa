# -*- coding: utf-8 -*-
"""core.query 护栏测试：正常查询放行，危险语句必须被拦。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.query import (ALLOWED_TABLES, UnsafeQueryError, get_schema,  # noqa: E402
                        run_query)

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sedl.db")
pytestmark = pytest.mark.skipif(not os.path.exists(DB), reason="sedl.db 不存在，先跑 scripts/import_excel.py")


# ---------- 放行 ----------
def test_simple_select():
    r = run_query(DB, "SELECT name, deduction FROM calc_reference c "
                      "JOIN employee e ON e.emp_id = c.emp_id ORDER BY deduction DESC LIMIT 3")
    assert len(r.rows) == 3
    assert r.columns == ["name", "deduction"]
    assert not r.truncated


def test_aggregate_query():
    r = run_query(DB, "SELECT SUM(deduction) AS total FROM calc_reference")
    assert r.rows[0][0] == pytest.approx(14070.0)


def test_with_cte():
    r = run_query(DB, "WITH t AS (SELECT emp_id, deduction FROM calc_reference) "
                      "SELECT COUNT(*) FROM t")
    assert r.rows[0][0] == 8


def test_auto_limit_appended():
    r = run_query(DB, "SELECT * FROM attendance")
    assert "LIMIT" in r.executed_sql.upper()
    assert r.truncated is True
    assert len(r.rows) == 32


def test_literal_keyword_not_blocked():
    """字面量里含 delete 这个词不该被拦"""
    r = run_query(DB, "SELECT COUNT(*) FROM employee WHERE 'delete' = 'delete'")
    assert r.rows[0][0] == 8


def test_schema_helper():
    s = get_schema(DB)
    assert set(s) == set(ALLOWED_TABLES)
    assert "deduction" in s["calc_reference"]


# ---------- 拦截 ----------
@pytest.mark.parametrize("sql", [
    "DELETE FROM employee",
    "UPDATE employee SET name='x'",
    "INSERT INTO employee VALUES ('9','x','y')",
    "DROP TABLE employee",
    "CREATE TABLE evil (a int)",
    "ALTER TABLE employee ADD COLUMN z int",
    "ATTACH DATABASE 'C:/tmp/x.db' AS x",
    "PRAGMA writable_schema=1",
    "VACUUM",
])
def test_dangerous_statements_rejected(sql):
    with pytest.raises(UnsafeQueryError):
        run_query(DB, sql)


def test_multi_statement_rejected():
    with pytest.raises(UnsafeQueryError):
        run_query(DB, "SELECT 1; DROP TABLE employee")


def test_system_table_rejected():
    with pytest.raises(UnsafeQueryError):
        run_query(DB, "SELECT name FROM sqlite_master")


def test_cte_delete_rejected():
    """WITH 开头也不能夹带写操作"""
    with pytest.raises(UnsafeQueryError):
        run_query(DB, "WITH t AS (SELECT 1) DELETE FROM employee")


def test_unknown_table_rejected():
    with pytest.raises(UnsafeQueryError):
        run_query(DB, "SELECT * FROM secret_table")


def test_empty_sql_rejected():
    with pytest.raises(UnsafeQueryError):
        run_query(DB, "   ")


# ---------- 只读连接验证 ----------
def test_connection_is_read_only():
    """即使绕过上层校验，连接层也必须写不进去"""
    import sqlite3
    from core.query import connect
    con = connect(DB)
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO employee VALUES ('9','x','y')")
            con.commit()
    finally:
        con.close()
