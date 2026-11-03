# -*- coding: utf-8 -*-
"""命令行问答：python scripts/ask.py "张三 4 月病假扣了多少" [--show-sql]

需要环境变量 DEEPSEEK_API_KEY（没有就只做本地解析，不发请求）。
"""
import argparse
import io
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.nl2sql import ask, compose_answer  # noqa: E402

DB = os.path.join(BASE, "data", "sedl.db")
ENV_FILE = os.path.join(BASE, ".env")
API = "https://api.deepseek.com/chat/completions"
MODEL = "deepseek-chat"


def _load_env_file():
    """从 sedl/.env 读取配置（不依赖 python-dotenv）。

    .env 已在仓库 .gitignore 里，不会被提交。
    """
    if not os.path.exists(ENV_FILE):
        return
    with io.open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _api_key():
    _load_env_file()
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key and os.name == "nt":
        # 也接受 setx 写入的用户级环境变量（当前进程可能还没继承到）
        import subprocess
        try:
            out = subprocess.run(
                ["reg", "query", "HKCU\\Environment", "/v", "DEEPSEEK_API_KEY"],
                capture_output=True, text=True, timeout=5)
            for part in out.stdout.split():
                if part.startswith("sk-"):
                    key = part.strip()
                    break
        except Exception:
            pass
    return key

EXAMPLES = [
    ("张三 4 月病假扣了多少",
     "SELECT e.name, c.deduction, c.net_pay FROM calc_reference c "
     "JOIN employee e ON e.emp_id=c.emp_id WHERE e.name='张三' AND c.period='2026-04'"),
    ("谁扣款最多",
     "SELECT e.name, c.deduction FROM calc_reference c "
     "JOIN employee e ON e.emp_id=c.emp_id ORDER BY c.deduction DESC"),
]


def deepseek(prompt: str) -> str:
    key = _api_key()
    if not key:
        raise RuntimeError(
            "未设置 DEEPSEEK_API_KEY。两种方式：\n"
            f"  1) 在 {ENV_FILE} 写一行 DEEPSEEK_API_KEY=sk-xxx\n"
            "  2) PowerShell 执行 setx DEEPSEEK_API_KEY \"sk-xxx\" 后重开终端")
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    })
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--show-sql", action="store_true")
    ap.add_argument("--raw", action="store_true", help="只输出模型原始返回，用于调试提示词")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if args.raw:
        from core.nl2sql import build_prompt
        print(deepseek(build_prompt(DB, args.question, EXAMPLES)))
        return

    a = ask(DB, args.question, deepseek, EXAMPLES)
    print(compose_answer(a))
    if args.show_sql and a.sql:
        print("\nSQL:", a.sql)


if __name__ == "__main__":
    main()
