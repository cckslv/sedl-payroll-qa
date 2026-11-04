from lfx.custom.custom_component.component import Component
from lfx.io import MessageTextInput, Output
from lfx.schema.message import Message

import os
import sys

# 仓库位置：优先环境变量，其次本机默认路径。
# 组件的职责只有「转发」——提示词、护栏、归因全部在仓库 core 里。
# 这里刻意不保留第二份实现：两份规则一定会漂移，而漂移是不可测试的。
REPO_DIR = os.environ.get("SEDL_REPO") or r"D:\AIProjects\ai-agent\sedl"
DB_PATH = os.environ.get("SEDL_DB_PATH") or os.path.join(REPO_DIR, "data", "sedl.db")


class SEDLAgent(Component):
    display_name = "SEDL Agent"
    description = "中文问薪酬数据：走仓库 core（模型只写 SQL，数字由数据库产出，三层护栏）"
    icon = "bot"
    name = "SEDLAgent"

    inputs = [
        MessageTextInput(
            name="input_value",
            display_name="问题",
            info="用中文问，例如：张三 4 月病假扣了多少？为什么赵六比刘一高？",
            value="",
            tool_mode=True,
        ),
    ]

    outputs = [
        Output(display_name="回答", name="output", method="build_output"),
    ]

    def build_output(self) -> Message:
        q = (self.input_value or "").strip()
        if not q:
            return Message(text="请输入问题，例如：张三 4 月病假扣了多少？")

        if not os.path.isdir(REPO_DIR):
            return Message(
                text="找不到项目目录：%s\n"
                     "请设置环境变量 SEDL_REPO 指向包含 core/ 的仓库目录。" % REPO_DIR)

        if REPO_DIR not in sys.path:
            sys.path.insert(0, REPO_DIR)

        try:
            from core.nl2sql import ask
            from core.llm import chat
        except Exception as e:
            return Message(text="无法加载 core（%s）：%s" % (REPO_DIR, e))

        if not os.path.exists(DB_PATH):
            return Message(
                text="数据库不存在：%s\n"
                     "先运行 scripts/import_excel.py 生成，或用环境变量 SEDL_DB_PATH 指定。" % DB_PATH)

        try:
            a = ask(DB_PATH, q, chat)
        except Exception as e:
            return Message(text="处理失败：%s" % e)

        # 归因类问题没有 SQL，只有解释；查数类带上 SQL 方便复核
        if a.sql:
            return Message(text="%s\n\nSQL: %s" % (a.text, a.sql))
        return Message(text=a.text)
