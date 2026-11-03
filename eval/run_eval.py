# -*- coding: utf-8 -*-
"""P5 评测：跑 20 题，算执行准确率。

指标口径（与 nl2sql-agent 等实现保持一致）：
  主指标 = **执行准确率**：把模型 SQL 和 gold SQL 都执行一遍，
  结果集规范化后比较是否等价。不比 SQL 文本（EM 恒为 0，没有意义）。
  比较时忽略行序（ORDER BY 已由 gold 决定内容，次序不影响"答对没答对"），
  数值按 2 位小数比较，None 统一成空。

用法：
  python eval/run_eval.py            # 全量
  python eval/run_eval.py --limit 5  # 先跑前 5 题
"""
import argparse
import io
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from core.llm import chat  # noqa: E402
from core.nl2sql import ask  # noqa: E402
from core.query import run_query  # noqa: E402

DB = os.path.join(BASE, "data", "sedl.db")
QUESTIONS = os.path.join(BASE, "eval", "questions.jsonl")
OUT_MD = os.path.join(BASE, "eval", "report.md")
OUT_JSON = os.path.join(BASE, "eval", "report.json")

EXAMPLES = [
    ("张三的4月病假扣款是多少",
     "SELECT c.deduction FROM calc_reference c JOIN employee e ON e.emp_id=c.emp_id "
     "WHERE e.name='张三' AND c.period='2026-04'"),
    ("谁扣款最多",
     "SELECT e.name FROM calc_reference c JOIN employee e ON e.emp_id=c.emp_id "
     "WHERE c.period='2026-04' ORDER BY c.deduction DESC LIMIT 1"),
]


def norm_cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v).strip()


def norm_rows(rows):
    """行内数值统一到 2 位小数，整体排序后比较（忽略行序）。"""
    return sorted(tuple(norm_cell(c) for c in row) for row in rows)


def matches_loose(gold_rows, got_rows):
    """宽松口径：行数一致，且 gold 每行能在结果里按前 N 列匹配上。

    用来区分"答错了"和"答对了但多给了一两列"——后者在人工复核里算答对，
    这也是 nl2sql-agent 等项目通行的做法（他们的错题 81% 是列形状差异）。
    """
    if len(gold_rows) != len(got_rows):
        return False
    pool = list(got_rows)
    for g in gold_rows:
        n = len(g)
        for i, cand in enumerate(pool):
            if tuple(cand[:n]) == tuple(g):
                pool.pop(i)
                break
        else:
            return False
    return True


def load_questions(limit=None):
    items = []
    with io.open(QUESTIONS, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items[:limit] if limit else items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    qs = load_questions(args.limit or None)
    results = []
    t0 = time.time()

    for q in qs:
        gold = run_query(DB, q["gold_sql"])
        gold_rows = norm_rows(gold.rows)
        gold_cols = len(gold.columns)

        start = time.time()
        a = ask(DB, q["question"], chat, EXAMPLES)
        elapsed = time.time() - start

        if a.result is None:
            status = "ERROR"
            got_rows = None
            detail = a.error or a.text[:120]
        else:
            got_rows = norm_rows(a.result.rows)
            if got_rows == gold_rows:
                status = "PASS"
                detail = ""
            elif matches_loose(gold_rows, got_rows):
                status = "PASS_LOOSE"
                detail = f"多给列：gold {gold_cols} 列 / got {len(a.result.columns)} 列"
            else:
                status = "FAIL"
                detail = ""

        results.append({
            "id": q["id"], "kind": q["kind"], "question": q["question"],
            "status": status, "elapsed": round(elapsed, 2),
            "sql": a.sql, "error": a.error,
            "gold_rows": [list(r) for r in gold.rows],
            "got_rows": [list(r) for r in (a.result.rows if a.result else [])],
            "detail": detail,
        })
        print(f"{q['id']:<5} {status:<6} {elapsed:5.1f}s  {q['question']}")

    total = len(results)
    passed = sum(1 for r in results if r["status"] == "PASS")
    loose = sum(1 for r in results if r["status"] == "PASS_LOOSE")
    failed = sum(1 for r in results if r["status"] == "FAIL")
    errors = sum(1 for r in results if r["status"] == "ERROR")
    acc = passed / total if total else 0
    acc_loose = (passed + loose) / total if total else 0

    summary = {
        "model": "deepseek-chat", "total": total, "pass": passed,
        "pass_loose": loose, "fail": failed, "error": errors,
        "accuracy_strict": round(acc, 4),
        "accuracy_loose": round(acc_loose, 4),
        "elapsed_total": round(time.time() - t0, 1),
        "tag": args.tag,
    }

    lines = [f"# 评测报告 {args.tag}".strip(), "",
             f"- 模型：deepseek-chat",
             f"- 题数：{total}",
             f"- **执行准确率（严格）：{acc:.1%}**（{passed}/{total}）",
             f"- **执行准确率（宽松，容忍多给列）：{acc_loose:.1%}**（{passed + loose}/{total}）",
             f"- 多给列：{loose}　答错：{failed}　出错：{errors}",
             f"- 总耗时：{summary['elapsed_total']}s", "",
             "| 题号 | 类型 | 结果 | 耗时 | 问题 |", "|:--|:--|:--|--:|:--|"]
    for r in results:
        lines.append(f"| {r['id']} | {r['kind']} | {r['status']} | {r['elapsed']}s | {r['question']} |")
    lines.append("")
    lines.append("## 未通过明细")
    for r in results:
        if r["status"] == "PASS":
            continue
        lines.append(f"### {r['id']} {r['question']}（{r['status']}）")
        lines.append(f"- 模型 SQL：`{r['sql']}`")
        lines.append(f"- gold：`{[q for q in qs if q['id'] == r['id']][0]['gold_sql']}`")
        lines.append(f"- gold 结果：{r['gold_rows']}")
        lines.append(f"- 模型结果：{r['got_rows']}")
        if r["detail"]:
            lines.append(f"- 备注：{r['detail']}")
        lines.append("")

    io.open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines))
    io.open(OUT_JSON, "w", encoding="utf-8").write(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2))
    print(f"\n执行准确率 {acc:.1%} ({passed}/{total})  报告 -> eval/report.md")


if __name__ == "__main__":
    main()
