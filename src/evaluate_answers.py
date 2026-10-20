"""CLI for preview-first, explicitly opt-in HTTP behavior evaluation."""

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from src.answer_evaluation import AnswerEvaluationError, load_suite, run_answer_evaluation


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def validate_local_url(value):
    try:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid characters")
        parts = urlsplit(value)
        if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1")
            or parts.username is not None or parts.password is not None or parts.path not in ("", "/")
            or parts.query or parts.fragment or parts.port is None or not 1 <= parts.port <= 65535):
            raise ValueError("not an explicit local origin")
        return value.rstrip("/")
    except ValueError:
        raise argparse.ArgumentTypeError("只允许带明确端口的本机 HTTP 根地址，例如 http://127.0.0.1:8000；不允许远程地址/路径/凭证") from None


def print_report(report):
    print("模式：", "真实行为评测（可能收费）" if report["mode"] == "live" else "免费预检（没有调用模型）")
    for row in report["cases"]:
        preview = row["preview"]
        reviews = [item["review_id"] for item in preview["evidence"]] if preview else None
        observed = row["score"]["observed_status"] if row["score"] else "未评测"
        match = row["score"]["behavior_match"] if row["score"] else None
        print(f"{row['case_id']}：预期={row['expected_status']}，可用核对记录={reviews}，实际={observed}，匹配={match}")
    summary = report["summary"]
    print("状态：", report["status"])
    print("可能调用模型的题数：", summary["planned_model_branch_cases"])
    if summary["fully_executed"]:
        print("开发集行为匹配率：", summary["behavior_match_rate"])
        print("模型分支匹配率：", summary["model_branch_match_rate"])
        print("本地无证据分支匹配率：", summary["local_branch_match_rate"])
    else:
        print("尚未完整执行回答评测，不给出回答/拒答分数；预检成功不代表模型答对。")
    if report["error"]:
        print("已停止：", report["error"]["code"], report["error"]["detail"])
    print("这些是围绕单条核对结论的开发案例，不是独立测试集或通用数学准确率。")
    print("问题相关性、拒答理由和审阅内容正确性仍需人工复核。")


def main(argv=None):
    parser = argparse.ArgumentParser(description="默认免费预检；显式开启后评测已核对公式的回答/拒答行为")
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--base-url", type=validate_local_url, default="http://127.0.0.1:8000")
    parser.add_argument("--cases", type=Path, default=Path("docs/reviewed_answer_cases.json"))
    parser.add_argument("--output", type=Path, help="必须是新路径；不覆盖已有报告")
    parser.add_argument("--live", action="store_true", help="开启真实回答请求，可能收费")
    parser.add_argument("--allow-paid", action="store_true", help="明确同意本轮可能产生 API 费用")
    parser.add_argument("--max-model-calls", type=int, default=0, help="本轮最多预留 1～3 次可能收费的模型尝试；不是人民币限额")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9a-f]{32}", args.document_id):
        parser.error("document-id 必须是真实文档的 32 位小写十六进制编号")
    if (not 0 <= args.max_model_calls <= 3 or args.live and (not args.allow_paid or args.max_model_calls < 1)
        or not args.live and (args.allow_paid or args.max_model_calls != 0)):
        parser.error("真实模式须同时填写 --live --allow-paid --max-model-calls 1至3；默认免费预检不加这些参数")
    def resolve(path):
        return path if path.is_absolute() else PROJECT_ROOT / path
    try:
        suite = load_suite(resolve(args.cases))
        output = resolve(args.output) if args.output else PROJECT_ROOT / "reports" / f"answer-evaluation-{uuid4().hex}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation occurs before any requests; snapshots only update
        # this invocation's owned report, never an existing user's file.
        with output.open("x", encoding="utf-8", newline="\n") as target:
            def checkpoint(report):
                serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
                target.seek(0)
                target.write(serialized)
                target.truncate()
                target.flush()
                os.fsync(target.fileno())
            with httpx.Client(base_url=args.base_url, follow_redirects=False, trust_env=False,
                              timeout=httpx.Timeout(45, connect=5, write=10, pool=5)) as client:
                report = run_answer_evaluation(client, args.document_id, suite, live=args.live,
                    allow_paid=args.allow_paid, max_model_calls=args.max_model_calls, checkpoint=checkpoint)
    except (AnswerEvaluationError, OSError) as error:
        parser.exit(2, f"无法完成评测：{error}\n若已经发出回答请求，请先核对账单，不要自动重跑整轮。\n")
    print_report(report)
    print("报告已保存到：", output)
    return 0 if report["status"] in ("preview_completed", "completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
