from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message

import io
import json
import os
import re
import sqlite3
import urllib.request

REPO_DIR = os.environ.get("SEDL_REPO") or r"D:\AIProjects\ai-agent\sedl"
DB_PATH = os.environ.get("SEDL_DB_PATH") or os.path.join(REPO_DIR, "data", "sedl.db")
ENV_FILE = os.path.join(REPO_DIR, ".env")
API_URL = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"

ALLOWED_TABLES = {"employee", "attendance", "salary_month", "calc_reference", "policy_params"}
FORBIDDEN = ("insert", "update", "delete", "drop", "alter", "create", "replace",
             "attach", "detach", "pragma", "vacuum", "reindex", "analyze",
             "load_extension", "writable_schema")
WRITE_ACTIONS = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17,
                 18, 23, 24, 25, 26, 27, 28, 29, 30}
SQLITE_READ = 20
SQLITE_PRAGMA = 19
ALLOWED_PRAGMAS = {"table_info", "table_xinfo", "database_list"}

# 写意图：动词 + 数据对象。放在调模型之前拦，一次模型都不用调。
# 之所以用规则不用模型：模型对"要不要打扰用户"天然偏保守，危险请求会被它当成歧义来反问。
WRITE_INTENT = re.compile(
    r"(删\s*除|删\s*掉|清\s*空|清\s*除|改\s*成|修\s*改|更\s*新|插\s*入|新\s*增|写\s*入|覆\s*盖|"
    r"\bdrop\b|\bdelete\b|\bupdate\b|\binsert\b|\btruncate\b|\balter\b)"
    r"[^。？！\n]{0,12}?"
    r"(表|数据|记录|几行|库|employee|attendance|salary_month|calc_reference|policy_params|员工)"
    r"|"
    r"(表|数据|记录|库|employee|attendance|salary_month|calc_reference|policy_params)"
    r"[^。？！\n]{0,8}?"
    r"(删|清空|清除|改成|修改|覆盖|\bdrop\b|\bdelete\b|\bupdate\b)",
    re.I,
)

REFUSAL_HINT = re.compile(r"(无法执行|不能删除|不能修改|拒绝|只支持查询)", re.I)


class SEDLAgent(Component):
    display_name = "SEDL Agent"
    description = "自然语言问薪酬数据：模型只写 SQL，数字由 SQLite 产出。只读 + 三层护栏。"
    icon = "bot"
    name = "SEDLAgent"

    inputs = [
        MessageTextInput(
            name="input_value",
            display_name="问题",
            info="用中文问，例如：张三 4 月病假扣了多少？",
            value="",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(display_name="回答", name="output", method="build_output"),
    ]

    # ---------- 密钥 ----------
    def _api_key(self) -> str:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if key:
            return key
        if os.path.exists(ENV_FILE):
            with io.open(ENV_FILE, encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith("DEEPSEEK_API_KEY"):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
        if os.name == "nt":
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                    return (winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")[0] or "").strip()
            except OSError:
                pass
        return ""

    # ---------- schema ----------
    def _schema(self) -> str:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
        try:
            lines = []
            for t in sorted(ALLOWED_TABLES):
                cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % t)]
                lines.append("%s(%s)" % (t, ", ".join(cols)))
            return "\n".join(lines)
        finally:
            con.close()

    # ---------- 模型 ----------
    def _ask_model(self, question: str) -> str:
        key = self._api_key()
        if not key:
            raise RuntimeError("未设置 DEEPSEEK_API_KEY")

        prompt = (
            "你是 SEDL 公司薪酬数据查询助手，只做一件事：把中文问题翻译成一条 SQLite SELECT。\n"
            "不要解释，不要输出 SQL 以外内容。\n\n"
            "表结构：\n%s\n\n"
            "口径：calc_reference 是官方 Excel 算出的结果，含 deduction 病假扣款、net_pay 实发；"
            "employee 存姓名；period 形如 '2026-04'。\n"
            "policy_params 的 name 是英文键：min_wage 最低工资、workdays_per_month 月计薪天数、"
            "full_month_sick_threshold 累计病假门槛、long_sick_days 长病假天数、"
            "paid_sick_days 免扣天数、rate_low 0.25、rate_high 0.5、rate_full 1.0。\n\n"
            "规则：\n"
            "1. 只写 SELECT，只用上面这些表。\n"
            "2. 扣款/实发等结果数字直接查 calc_reference，不要自己算。\n"
            "3. 只 SELECT 问题需要的列，不要 SELECT *。\n"
            "4. 不要加 LIMIT。\n"
            "5. 问合计/平均/一共几个人/所有人这类全局问题时，不要反问是哪个人，直接查全表。\n"
            "6. 查 policy_params 时必须用上面的英文键名，不要用中文名称当 name 匹配。\n"
            "7. 同一取值会重复出现时（如按月统一的应出勤小时数）可用 SELECT DISTINCT，"
            "但去重必须保留问题所问的列：问\"每个人\"就返回姓名 name，不要用 emp_id 顶替。\n"
            "8. 只有当缺少关键信息、确实无法确定查询范围时才返回：CLARIFY: <要问的问题>。\n\n"
            "示例：\n"
            "问：张三 4 月扣了多少\n"
            "答：SELECT c.deduction FROM calc_reference c JOIN employee e "
            "ON e.emp_id=c.emp_id WHERE e.name='张三' AND c.period='2026-04'\n\n"
            "问：%s\n答：" % (self._schema(), question)
        )
        body = json.dumps({
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(API_URL, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        })
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]

    # ---------- 护栏 ----------
    def _clean_sql(self, raw: str) -> str:
        text = raw or ""
        m = re.search(r"```(?:sql)?\s*(.+?)```", text, re.I | re.S)
        if m:
            text = m.group(1)
        else:
            m = re.search(r"\b(select|with)\b", text, re.I)
            if not m:
                raise ValueError("模型没有给出 SQL")
            text = text[m.start():]
        sql = text.strip().strip("`").strip().rstrip(";").strip()
        if not sql.lower().startswith(("select", "with")):
            raise ValueError("不是查询语句")
        return sql

    def _check(self, sql: str) -> None:
        body = re.sub(r"'[^']*'", "''", sql)
        if ";" in body.strip().rstrip(";"):
            raise ValueError("只允许单条语句")
        low = body.lower()
        for kw in FORBIDDEN:
            if re.search(r"\b%s\b" % kw, low):
                raise ValueError("包含禁止的关键字: %s" % kw)

    def _execute(self, sql: str, limit: int = 200):
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA query_only=1")

            def auth(action, arg1, arg2, dbname, source):
                if action == SQLITE_READ:
                    return sqlite3.SQLITE_OK if (arg1 or "").lower() in ALLOWED_TABLES else sqlite3.SQLITE_DENY
                if action == SQLITE_PRAGMA:
                    return sqlite3.SQLITE_OK if (arg1 or "").lower() in ALLOWED_PRAGMAS else sqlite3.SQLITE_DENY
                if action in WRITE_ACTIONS:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK

            con.set_authorizer(auth)
            run = sql if re.search(r"\blimit\b", sql, re.I) else sql + "\nLIMIT %d" % limit
            try:
                cur = con.execute(run)
            except sqlite3.DatabaseError as e:
                raise ValueError("查询被拒绝或出错: %s" % e)
            cols = [d[0] for d in (cur.description or [])]
            rows = [tuple(r) for r in cur.fetchall()]
            return cols, rows
        finally:
            con.close()

    # ---------- 输出 ----------
    def build_output(self) -> Message:
        q = (self.input_value or "").strip()
        if not q:
            return Message(text="请输入问题，例如：张三 4 月病假扣了多少？")

        # 优先复用仓库里的 core（同一份提示词/护栏/归因），避免两份实现漂移
        result = self._via_core(q)
        if result is not None:
            return Message(text=result)

        if WRITE_INTENT.search(q):
            return Message(
                text="本系统只支持只读查询，不会修改、删除或新增任何数据。\n"
                     "如果你想查某人的扣款或实发工资，请直接问，例如：张三 4 月扣了多少？")

        try:
            raw = self._ask_model(q)
        except Exception as e:
            return Message(text="模型调用失败：%s" % e)

        m = re.search(r"CLARIFY[:：]\s*(.+)", raw or "", re.I)
        if m:
            return Message(text="需要确认：%s" % m.group(1).strip())

        try:
            sql = self._clean_sql(raw)
            self._check(sql)
            cols, rows = self._execute(sql)
        except Exception as e:
            if REFUSAL_HINT.search(raw or ""):
                return Message(
                    text="本系统只支持只读查询，不会修改、删除或新增任何数据。\n"
                         "如果你想查某人的扣款或实发工资，请直接问。")
            return Message(text="查询未通过：%s\n\n模型原文：%s" % (e, (raw or "")[:300]))

        if not rows:
            return Message(text="没有匹配的记录。\n\nSQL: %s" % sql)

        head = " | ".join(cols)
        body = "\n".join(" | ".join("" if v is None else str(v) for v in r) for r in rows[:20])
        more = "" if len(rows) <= 20 else "\n... 共 %d 行，只显示前 20 行" % len(rows)
        return Message(text="%s\n%s\n%s%s\n\nSQL: %s" % (head, "-" * 40, body, more, sql))

    # ---------- 复用仓库 core ----------
    def _via_core(self, question: str):
        """能导入仓库 core 就交给它处理（提示词/护栏/归因只有一份实现）。"""
        try:
            import sys
            if REPO_DIR not in sys.path:
                sys.path.insert(0, REPO_DIR)
            from core.nl2sql import ask as core_ask
            from core.llm import chat as core_chat

            if not os.path.exists(DB_PATH):
                return "数据库不存在：%s\n（可用环境变量 SEDL_DB_PATH 指定）" % DB_PATH

            a = core_ask(DB_PATH, question, core_chat)
            if a.sql:
                return "%s\n\nSQL: %s" % (a.text, a.sql)
            return a.text
        except Exception:
            return None   # 导入失败则回落到组件内置实现
