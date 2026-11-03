# -*- coding: utf-8 -*-
"""SEDL 只读查询层：三道护栏 + 自动限流。

护栏设计（越靠后越硬）：
  1. 语句类型：只允许 SELECT / WITH 开头，禁多语句、DDL、DML、ATTACH、PRAGMA。
  2. **SQLite 授权回调（authorizer）**：SQLite 在「准备语句」阶段会逐个动作回调我们，
     我们只放行白名单表的读取，其余（写、建表、ATTACH、PRAGMA…）一律 DENY。
     这是官方机制，比自己用正则解析 SQL 可靠得多 —— 试过从 EXPLAIN 字节码里捞表名，
     不行：OpenRead 的 p4 是列数/索引信息，压根不带表名。
  3. 连接层：数据库以 `mode=ro` 打开并置 `PRAGMA query_only=1`，
     就算前两道全被绕过，写操作也会被 SQLite 物理拒绝。

另外：
  - 自动补 LIMIT（默认 200），防止误查全库刷屏。
  - set_progress_handler 做执行超时，避免慢查询卡死。
"""
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Sequence

ALLOWED_TABLES = frozenset({
    "employee",
    "attendance",
    "salary_month",
    "calc_reference",
    "policy_params",
})

ALLOWED_PREFIX = ("select", "with")

FORBIDDEN_KEYWORDS = (
    "insert", "update", "delete", "drop", "alter", "create", "replace",
    "attach", "detach", "pragma", "vacuum", "reindex", "analyze",
    "load_extension", "writable_schema",
)

# SQLite 授权回调的动作码（Python 只导出了 OK/DENY/IGNORE，动作码要自己写）
SQLITE_DELETE = 9
SQLITE_INSERT = 18
SQLITE_PRAGMA = 19
SQLITE_READ = 20
SQLITE_UPDATE = 23
SQLITE_ATTACH = 24
SQLITE_DETACH = 25
WRITE_ACTIONS = frozenset({
    1, 2, 3, 4, 5, 6, 7, 8,        # CREATE_*（表/索引/视图/触发器，含 TEMP）
    SQLITE_DELETE, 18, 23,          # DELETE / INSERT / UPDATE
    10, 11, 12, 13, 14, 15, 16, 17, # DROP_*
    24, 25, 26, 27, 28,             # ATTACH / DETACH / ALTER / REINDEX / ANALYZE
    29, 30,                         # CREATE_VTABLE / DROP_VTABLE
})

# 允许通过这些 PRAGMA（只读的元数据查询）
ALLOWED_PRAGMAS = frozenset({"table_info", "table_xinfo", "index_list", "database_list"})

DEFAULT_LIMIT = 200
DEFAULT_TIMEOUT_SEC = 5.0


class UnsafeQueryError(ValueError):
    """SQL 未通过护栏。"""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool
    executed_sql: str
    tables_used: list[str]


# ---------------- 第一道：文本层 ----------------
def _strip_comments(sql: str) -> str:
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    return sql


def _strip_literals(sql: str) -> str:
    """去掉字符串字面量内容，避免 'delete' 这种值被当关键字误杀。"""
    out = re.sub(r"'[^']*'", "''", sql)
    out = re.sub(r'"[^"]*"', '""', out)
    return out


def _ensure_single_statement(sql: str) -> None:
    body = _strip_literals(_strip_comments(sql)).strip().rstrip(";").strip()
    if not body:
        raise UnsafeQueryError("SQL 为空")
    if ";" in body:
        raise UnsafeQueryError("只允许执行单条语句")


def _ensure_prefix(sql: str) -> None:
    head = _strip_comments(sql).strip().lower()
    if not head.startswith(ALLOWED_PREFIX):
        raise UnsafeQueryError(f"只允许 SELECT / WITH 开头，收到: {head[:40]!r}")
    lowered = _strip_literals(_strip_comments(sql)).lower()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{kw}\b", lowered):
            raise UnsafeQueryError(f"语句包含禁止的关键字: {kw}")


def _has_limit(sql: str) -> bool:
    return re.search(r"\blimit\b", _strip_literals(_strip_comments(sql)), re.I) is not None


# ---------------- 第二道：授权回调 ----------------
def _make_authorizer(seen_tables: list[str]):
    def authorizer(action, arg1, arg2, dbname, source):
        if action == SQLITE_READ:
            table = (arg1 or "").lower()
            if table not in ALLOWED_TABLES:
                seen_tables.append(f"DENY:{arg1}")
                return sqlite3.SQLITE_DENY
            if table not in seen_tables:
                seen_tables.append(table)
            return sqlite3.SQLITE_OK
        if action == SQLITE_PRAGMA:
            if (arg1 or "").lower() in ALLOWED_PRAGMAS:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY
        if action in WRITE_ACTIONS:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


# ---------------- 第三道：只读连接 ----------------
def connect(db_path: str, with_authorizer: bool = False,
            seen_tables: list[str] | None = None) -> sqlite3.Connection:
    """以只读方式打开数据库（物理层面写不进去）。"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(db_path)
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)
    con.execute("PRAGMA query_only=1")     # 必须在装 authorizer 之前
    con.row_factory = sqlite3.Row
    if with_authorizer:
        con.set_authorizer(_make_authorizer(seen_tables if seen_tables is not None else []))
    return con


# ---------------- 对外 API ----------------
def run_query(db_path: str, sql: str, *,
              limit: int = DEFAULT_LIMIT,
              timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> QueryResult:
    """校验并执行一条只读查询。"""
    _ensure_single_statement(sql)
    _ensure_prefix(sql)

    executed = sql.strip().rstrip(";").strip()
    truncated = False
    if not _has_limit(executed):
        executed = f"{executed}\nLIMIT {limit}"
        truncated = True

    seen: list[str] = []
    con = connect(db_path, with_authorizer=True, seen_tables=seen)
    try:
        # 超时保护：每 1000 条虚拟机指令回调一次
        deadline = [time.monotonic() + timeout_sec]
        con.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline[0] else 0, 1000)
        try:
            cur = con.execute(executed)
            rows = [tuple(r) for r in cur.fetchall()]
            columns = [d[0] for d in (cur.description or [])]
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "interrupted" in msg:
                raise UnsafeQueryError(f"查询超时（>{timeout_sec}s）") from e
            if "no such table" in msg or "no such column" in msg:
                raise UnsafeQueryError(f"不存在的表或列: {e}") from e
            raise
        except sqlite3.DatabaseError as e:
            msg = str(e).lower()
            if "not authorized" in msg:
                denied = [t for t in seen if t.startswith("DENY:")]
                raise UnsafeQueryError(
                    f"语句被拒绝（越权访问）: {denied or e}") from e
            raise UnsafeQueryError(f"SQL 执行失败: {e}") from e
        finally:
            con.set_progress_handler(None, 0)

        tables = [t for t in seen if not t.startswith("DENY:")]
        return QueryResult(columns=columns, rows=rows, truncated=truncated,
                           executed_sql=executed, tables_used=tables)
    finally:
        con.close()


def get_schema(db_path: str) -> dict[str, list[str]]:
    """返回白名单表的列名，供 schema linking / 提示词使用。"""
    con = connect(db_path)
    try:
        out = {}
        for t in sorted(ALLOWED_TABLES):
            cols = con.execute(f"PRAGMA table_info({t})").fetchall()
            out[t] = [c["name"] for c in cols]
        return out
    finally:
        con.close()


def describe_schema(db_path: str) -> str:
    """给 LLM 看的 schema 文本（只含白名单表）。"""
    schema = get_schema(db_path)
    lines = []
    for table, cols in schema.items():
        lines.append(f"{table}({', '.join(cols)})")
    return "\n".join(lines)
