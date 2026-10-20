"""Run the fixed development comparison and save its JSON report."""

import argparse
from pathlib import Path

from src.evaluation_runner import run_evaluation, write_json_report


def print_report(report):
    dataset = report["dataset"]
    print(f"评测集：{dataset['version']}；题数={dataset['case_count']}，计分={dataset['scored_case_count']}，证据不足={dataset['unscored_insufficient_evidence_count']}")
    print("指标基于已标注目标证据；全部为开发题，不是独立测试集。")
    for method in report["methods"]:
        print("\n方案：", method["name"])
        for case in method["cases"]:
            ranks = [target["rank"] for target in case["target_ranks"]]
            print("题目：", case["case_id"], "查询：", case["query"])
            if not case["scored"]:
                print(case["skip_reason"])
                print("前 5 条来源：", [(hit["pdf_page_number"], hit["start_char"], hit["end_char"]) for hit in case["retrieved"][:5]])
                continue
            print("目标证据排名：", ranks)
            for k, row in case["metrics"].items():
                print(f"K={k}：目标覆盖率={row['target_coverage']:.4f}，目标RR={row['target_rr']:.4f}，目标nDCG={row['target_ndcg']:.4f}")
        print("方案汇总：")
        for k, summary in method["summary"].items():
            if not summary["scored_case_count"]:
                print(f"K={k}：没有可计分的题目")
                continue
            print(f"K={k}，计分题数={summary['scored_case_count']}：平均目标覆盖率={summary['mean_target_coverage']:.4f}，目标MRR={summary['target_mrr']:.4f}，平均目标nDCG={summary['mean_target_ndcg']:.4f}")


def main():
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="运行固定 BM25 开发评测并保存 JSON 报告")
    parser.add_argument("--cases", type=Path, default=Path("docs/eval_cases.json"))
    parser.add_argument("--manifest", type=Path, default=Path("docs/eval_manifest.json"))
    parser.add_argument("--output", type=Path, help="新报告的 JSON 路径；已存在则拒绝覆盖")
    parser.add_argument("--k", nargs="+", type=int, default=[5, 10, 20])
    args = parser.parse_args()

    def resolve(path):
        return path if path.is_absolute() else root / path

    try:
        report = run_evaluation(root, resolve(args.cases), resolve(args.manifest), args.k)
        output = resolve(args.output) if args.output else root / "reports" / f"evaluation-{report['run_id']}.json"
        write_json_report(report, output)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    print_report(report)
    print("\nJSON 报告已保存到：", output)


if __name__ == "__main__":
    main()
