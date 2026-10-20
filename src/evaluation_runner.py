"""Validate fixed evaluation inputs and produce traceable retrieval reports."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from statistics import mean
from time import perf_counter
from uuid import uuid4

from src.bm25_retrieval import BM25Retriever
from src.evaluate import evidence_key
from src.metrics import ndcg_at_k, recall_at_k, reciprocal_rank_at_k, validate_k
from src.tokenization import tokenize, tokenize_normalized


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def evidence_reference(record):
    fields = ("document_name", "pdf_page_number", "start_char", "end_char")
    return {field: record[field] for field in fields}


def _validated_evidence_key(record):
    name = record["document_name"]
    page = record["pdf_page_number"]
    start, end = record["start_char"], record["end_char"]
    if (
        not isinstance(name, str) or not name
        or type(page) is not int or page < 1
        or type(start) is not int or type(end) is not int
        or not 0 <= start < end
    ):
        raise ValueError("证据标识中的文档名、页号或字符区间不合法")
    return evidence_key(record)


def _load_jsonl(path):
    with path.open("r", encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def _validate_sources(pages, chunks):
    page_index = {}
    for page in pages:
        number = page["pdf_page_number"]
        if type(number) is not int or number < 1 or not isinstance(page["text"], str):
            raise ValueError("页面记录的页号或文本不合法")
        key = (page["document_name"], number)
        if key in page_index:
            raise ValueError(f"重复页面记录：{key}")
        page_index[key] = page

    chunk_index = {}
    for chunk in chunks:
        key = _validated_evidence_key(chunk)
        if key in chunk_index:
            raise ValueError(f"重复块标识：{key}")
        page = page_index.get(key[:2])
        start, end = chunk["start_char"], chunk["end_char"]
        if page is None:
            raise ValueError(f"块无法定位到源页：{key}")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(page["text"]):
            raise ValueError(f"块的字符区间不合法：{key}")
        if chunk["text"] != page["text"][start:end]:
            raise ValueError(f"块文本与原页不一致：{key}")
        chunk_index[key] = chunk
    return chunk_index


def _validate_cases(cases, chunk_index):
    if not cases:
        raise ValueError("评测集不能为空")
    seen = set()
    for case in cases:
        case_id = case["case_id"]
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"题目编号为空或重复：{case_id}")
        seen.add(case_id)
        if not isinstance(case["query"], str) or not case["query"].strip():
            raise ValueError(f"{case_id} 的查询为空")
        behavior = case["expected_behavior"]
        targets = case["required_evidence"]
        if behavior not in ("answer", "insufficient_evidence"):
            raise ValueError(f"{case_id} 的 expected_behavior 不合法")
        if behavior == "answer" and not targets:
            raise ValueError(f"{case_id} 标为可回答，却没有目标证据")
        if behavior == "insufficient_evidence" and targets:
            raise ValueError(f"{case_id} 的干扰证据不能放进 required_evidence")
        if len({_validated_evidence_key(item) for item in targets}) != len(targets):
            raise ValueError(f"{case_id} 重复标注同一个目标块")
        for item in targets + case.get("distractor_evidence", []):
            chunk = chunk_index.get(_validated_evidence_key(item))
            if chunk is None:
                raise ValueError(f"{case_id} 的标注引用了不存在的块：{evidence_key(item)}")
            quote = item.get("evidence_quote")
            if quote is not None and (not quote or quote not in chunk["text"]):
                raise ValueError(f"{case_id} 的证据摘录不在对应块内")


def load_evaluation_inputs(project_root, cases_path, manifest_path):
    root = Path(project_root).resolve()
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "1.0":
        raise ValueError("不支持的评测清单版本")
    if manifest.get("annotation_status") != "partial_target_evidence":
        raise ValueError("当前评测器只支持部分目标证据标注")
    artifact_info = {}
    for name in ("pdf", "pages", "chunks"):
        spec = manifest["artifacts"][name]
        path = (root / spec["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"数据路径超出了项目目录：{name}")
        actual_hash = file_sha256(path)
        if actual_hash != spec["sha256"].lower():
            raise ValueError(f"{name} 文件指纹不一致；数据已变化，请重新核对标注和清单，勿直接沿用旧证据")
        artifact_info[name] = {"path": spec["path"], "sha256": actual_hash}

    pages = _load_jsonl(root / artifact_info["pages"]["path"])
    chunks = _load_jsonl(root / artifact_info["chunks"]["path"])
    if len(pages) != manifest["expected_page_count"] or len(chunks) != manifest["expected_chunk_count"]:
        raise ValueError("页数或块数与固定清单不一致")
    chunk_index = _validate_sources(pages, chunks)
    cases = json.loads(Path(cases_path).read_text(encoding="utf-8"))
    _validate_cases(cases, chunk_index)
    return cases, chunks, manifest, artifact_info


def _runtime_metadata(root):
    packages = {}
    for package in ("pypdf", "jieba", "rank-bm25", "numpy"):
        try:
            packages[package] = version(package)
        except PackageNotFoundError:
            packages[package] = None
    code_files = sorted((root / "src").glob("*.py"))
    if (root / "requirements.txt").exists():
        code_files.append(root / "requirements.txt")
    fingerprints = {path.relative_to(root).as_posix(): file_sha256(path) for path in code_files}
    git = {"commit": None, "dirty": None}
    prefix = ["git", "-c", f"safe.directory={root.as_posix()}", "-C", str(root)]
    try:
        head = subprocess.run(prefix + ["rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
        status = subprocess.run(prefix + ["status", "--porcelain"], capture_output=True, text=True, timeout=5)
        if head.returncode == 0:
            git["commit"] = head.stdout.strip()
        if status.returncode == 0:
            git["dirty"] = bool(status.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"python": platform.python_version(), "packages": packages, "code_sha256": fingerprints, "git": git}


def run_evaluation(project_root, cases_path=None, manifest_path=None, k_values=(5, 10, 20)):
    root = Path(project_root).resolve()
    cases_path = Path(cases_path) if cases_path is not None else root / "docs/eval_cases.json"
    manifest_path = Path(manifest_path) if manifest_path is not None else root / "docs/eval_manifest.json"
    if not k_values or len(set(k_values)) != len(k_values):
        raise ValueError("K 列表不能为空或重复")
    for k in k_values:
        validate_k(k)
    if 5 not in k_values:
        raise ValueError("本开发基线必须保留主要检查点 K=5")
    cases, chunks, manifest, artifacts = load_evaluation_inputs(root, cases_path, manifest_path)
    answerable = sum(case["expected_behavior"] == "answer" for case in cases)
    report = {
        "schema_version": "1.0", "run_id": uuid4().hex,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset": {"version": manifest["dataset_version"], "split": manifest["split"], "annotation_status": manifest["annotation_status"], "case_count": len(cases), "scored_case_count": answerable, "unscored_insufficient_evidence_count": len(cases) - answerable, "chunk_count": len(chunks), "metric_scope": manifest["metric_scope"], "manifest": manifest},
        "input_fingerprints": {"artifacts": artifacts, "cases_sha256": file_sha256(cases_path), "manifest_sha256": file_sha256(manifest_path)},
        "runtime": _runtime_metadata(root),
        "config": {"k_values": list(k_values), "primary_k": 5, "aggregation": "macro_average_over_answer_cases", "relevance": "binary_partial_target_evidence", "candidate_rule": "at_least_one_shared_token", "tie_break": "original_chunk_file_order", "automatic_refusal_evaluated": False},
        "timing_note": "单次运行的建索引和查询 wall-clock 耗时；未控制冷暖缓存，不是 P50/P95 或性能提升结论。",
        "methods": [],
    }
    methods = [("bm25_original", "原始 BM25", tokenize, []), ("bm25_term_normalized", "术语统一 BM25", tokenize_normalized, [{"from": "有序数域", "to": "有序域"}])]
    for method_id, name, tokenizer, rules in methods:
        start = perf_counter()
        retriever = BM25Retriever(chunks, tokenizer=tokenizer)
        index_ms = (perf_counter() - start) * 1000
        method = {"id": method_id, "name": name, "tokenizer": tokenizer.__name__, "normalization_rules": rules, "bm25_parameters": {"k1": getattr(retriever.bm25, "k1", 1.5), "b": getattr(retriever.bm25, "b", 0.75), "epsilon": getattr(retriever.bm25, "epsilon", 0.25)}, "index_build_ms": index_ms, "cases": [], "summary": {}}
        metric_rows = {str(k): [] for k in k_values}
        for case in cases:
            start = perf_counter()
            hits = retriever.search(case["query"], k=max(1, len(chunks)))
            query_ms = (perf_counter() - start) * 1000
            keys = [evidence_key(hit["chunk"]) for hit in hits]
            if len(set(keys)) != len(keys):
                raise ValueError("检索器返回了重复块")
            positions = {key: rank for rank, key in enumerate(keys, start=1)}
            targets = case["required_evidence"]
            target_keys = {evidence_key(target) for target in targets}
            result = {"case_id": case["case_id"], "query": case["query"], "expected_behavior": case["expected_behavior"], "answer_points": case.get("answer_points", []), "annotation_note": case.get("annotation_note", "见 eval-draft.md"), "required_evidence": targets, "distractor_evidence": case.get("distractor_evidence", []), "query_ms": query_ms, "returned_candidate_count": len(hits), "retrieved": [{"rank": rank, "score": hit["score"], **evidence_reference(hit["chunk"])} for rank, hit in enumerate(hits, start=1)], "target_ranks": [{**evidence_reference(target), "rank": positions.get(evidence_key(target))} for target in targets], "scored": case["expected_behavior"] == "answer", "metrics": {}}
            if not result["scored"]:
                result["skip_reason"] = "人工标为证据不足；没有回答层，不评判拒答是否正确。"
                result["metrics"] = {str(k): None for k in k_values}
            else:
                for k in k_values:
                    found = set(keys[:k]) & target_keys
                    row = {"target_coverage": recall_at_k(keys, target_keys, k), "target_rr": reciprocal_rank_at_k(keys, target_keys, k), "target_ndcg": ndcg_at_k(keys, target_keys, k), "matched_target_count": len(found), "target_count": len(target_keys), "missing_targets": [evidence_reference(target) for target in targets if evidence_key(target) not in found]}
                    result["metrics"][str(k)] = row
                    metric_rows[str(k)].append(row)
            method["cases"].append(result)
        for k, rows in metric_rows.items():
            method["summary"][k] = {"scored_case_count": len(rows), "unscored_case_count": len(cases) - len(rows), "mean_target_coverage": mean(row["target_coverage"] for row in rows) if rows else None, "target_mrr": mean(row["target_rr"] for row in rows) if rows else None, "mean_target_ndcg": mean(row["target_ndcg"] for row in rows) if rows else None}
        report["methods"].append(method)
    return report


def write_json_report(report, output_path):
    """Use exclusive creation so a new run never overwrites an earlier result."""
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8", newline="\n") as file:
        file.write(serialized)
    return output_path
