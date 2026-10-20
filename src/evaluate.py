import json
from pathlib import Path

from src.search import search_chunks


def evidence_key(record):
    return (
        record["document_name"],
        record["pdf_page_number"],
        record["start_char"],
        record["end_char"],
    )
def compare_evidence(expected_records, retrieved_records):
    expected = {evidence_key(item) for item in expected_records}
    retrieved = {evidence_key(item) for item in retrieved_records}

    matched = expected & retrieved
    missing = expected - retrieved

    return matched, missing
def main():
    project_root = Path(__file__).resolve().parent.parent
    cases_path = project_root / "docs" / "eval_cases.json"
    chunks_path = project_root / "data" / "parsed" / "lecture.chunks.jsonl"

    with cases_path.open("r", encoding="utf-8") as file:
        cases = json.load(file)

    with chunks_path.open("r", encoding="utf-8") as file:
        chunks = [json.loads(line) for line in file]

    k = 5

    for case in cases:
        matches = search_chunks(chunks, case["query"])
        displayed = matches[:k]

        print()
        print("题目：", case["case_id"])
        print("查询：", case["query"])
        print("全部匹配数：", len(matches))
        print("本次检查前几条：", k)

        for rank, chunk in enumerate(displayed, start=1):
            print(f"  第 {rank} 条：", evidence_key(chunk))

        if case["expected_behavior"] == "insufficient_evidence":
            print("人工预期：证据不足。")
            print("本题不计算目标证据命中比例，也未自动验证拒答。")
            continue

        matched, missing = compare_evidence(
            case["required_evidence"],
            displayed,
        )

        target_count = len({
            evidence_key(item)
            for item in case["required_evidence"]
        })

        print("目标证据命中数：", f"{len(matched)}/{target_count}")
        print("缺失的目标证据：", sorted(missing))

if __name__ == "__main__":
    main()

