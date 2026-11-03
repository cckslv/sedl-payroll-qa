# -*- coding: utf-8 -*-
"""DeepSeek 调用封装：密钥从环境变量 / sedl/.env / 用户级注册表读取，不写进代码。"""
import io
import json
import os
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(BASE_DIR, ".env")
API = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-chat"


def load_api_key() -> str:
    """依次尝试：环境变量 → .env 文件 → Windows 用户级环境变量。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if key:
        return key

    if os.path.exists(ENV_FILE):
        with io.open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY") and "=" in line:
                    return line.split("=", 1)[1].strip().strip('"').strip("'")

    if os.name == "nt":
        import subprocess
        try:
            out = subprocess.run(
                ["reg", "query", "HKCU\\Environment", "/v", "DEEPSEEK_API_KEY"],
                capture_output=True, text=True, timeout=5)
            for part in out.stdout.split():
                if part.startswith("sk-"):
                    return part.strip()
        except Exception:
            pass
    return ""


def chat(prompt: str, *, model: str = DEFAULT_MODEL, temperature: float = 0.0,
         timeout: int = 60) -> str:
    key = load_api_key()
    if not key:
        raise RuntimeError(
            "未设置 DEEPSEEK_API_KEY。两种方式：\n"
            f"  1) 在 {ENV_FILE} 写一行 DEEPSEEK_API_KEY=sk-xxx\n"
            '  2) PowerShell 执行 setx DEEPSEEK_API_KEY "sk-xxx" 后重开终端')
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"DeepSeek API {e.code}: {detail}") from e
    return data["choices"][0]["message"]["content"]
