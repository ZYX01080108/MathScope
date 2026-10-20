"""Small development-set behavior evaluation; preview is deliberately not a score."""

import hashlib
import json
import re
from datetime import date
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field, field_validator

from src.api_models import FormulaReviewRecord, ReviewedFormulaAnswer, SearchRequest
from src.deepseek_client import strict_json_loads
from src.reviewed_formula import validate_reviewed_context


class AnswerEvaluationError(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


class EvaluationConfig(BaseModel):
    model_config = {"extra": "forbid"}
    method: Literal["original", "normalized"] = "normalized"
    k: int = Field(default=10, ge=1, le=20, strict=True)
    max_context_chars: int = Field(default=6000, ge=128, le=6000, strict=True)


class AnswerEvaluationCase(BaseModel):
    model_config = {"extra": "forbid"}
    case_id: str = Field(pattern=r"^RA[0-9]{3}$", strict=True)
    query: str = Field(min_length=1, max_length=500, strict=True)
    expected_status: Literal["answered", "refused"]
    available_review_ids: list[Annotated[str, Field(pattern=r"^FR[0-9]{3}$", strict=True)]] = Field(max_length=8)
    annotation_note: str = Field(min_length=1, max_length=1000, strict=True)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value):
        return SearchRequest(query=value).query


class AnswerEvaluationSuite(BaseModel):
    model_config = {"extra": "forbid"}
    schema_version: Literal["1.0"]
    dataset_id: str = Field(min_length=1, max_length=100, strict=True)
    split: Literal["development"]
    annotation_scope: Literal["single_reviewed_statement_behavior"]
    config: EvaluationConfig
    review_records: list[FormulaReviewRecord] = Field(min_length=1, max_length=8)
    cases: list[AnswerEvaluationCase] = Field(min_length=1, max_length=10)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def validate_suite(value):
    try:
        suite = AnswerEvaluationSuite.model_validate(value).model_dump()
        reviews = {item["review_id"]: item for item in suite["review_records"]}
        if len(reviews) != len(suite["review_records"]) or len({item["source_sha256"] for item in reviews.values()}) != 1:
            raise ValueError("duplicate reviews or mixed sources")
        for item in reviews.values():
            date.fromisoformat(item["reviewed_at"])
            if item["raw_end_char"] - item["raw_start_char"] != len(item["raw_quote"]):
                raise ValueError("invalid source range")
        ids = set()
        for case in suite["cases"]:
            available = case["available_review_ids"]
            if (case["case_id"] in ids or len(set(available)) != len(available)
                or not set(available) <= set(reviews)
                or case["expected_status"] == "answered" and not available):
                raise ValueError("invalid cases")
            ids.add(case["case_id"])
        return suite
    except (ValueError, TypeError):
        raise AnswerEvaluationError("INVALID_SUITE", "评测题、来源或标注不合法；请先核对固定开发集") from None


def load_suite(path):
    try:
        with Path(path).open("rb") as source:
            data = source.read(128 * 1024 + 1)
        if len(data) > 128 * 1024:
            raise ValueError("oversized suite")
        return validate_suite(strict_json_loads(data))
    except (OSError, ValueError, TypeError, RecursionError):
        raise AnswerEvaluationError("INVALID_SUITE", "无法读取固定回答评测集；不发起任何模型调用") from None


def _post_json(client, path, payload):
    try:
        with client.stream("POST", path, json=payload) as response:
            if response.status_code != 200:
                raise AnswerEvaluationError(f"HTTP_{response.status_code}",
                    f"本机接口返回 HTTP {response.status_code}；本次不重试，若已提交回答请求请核对账单")
            chunks, length = [], 0
            for chunk in response.iter_bytes():
                length += len(chunk)
                if length > 128 * 1024:
                    raise AnswerEvaluationError("RESPONSE_LIMIT", "本机响应超过当前读取上限；不重试")
                chunks.append(chunk)
        return strict_json_loads(b"".join(chunks))
    except AnswerEvaluationError:
        raise
    except httpx.TimeoutException:
        raise AnswerEvaluationError("HTTP_TIMEOUT", "本机接口超时；回答请求可能已计费，不自动重试") from None
    except httpx.HTTPError:
        raise AnswerEvaluationError("HTTP_CONNECTION", "本机连接异常；不自动重试") from None
    except (ValueError, TypeError, RecursionError):
        raise AnswerEvaluationError("INVALID_RESPONSE", "本机响应不是合法 JSON；不交付或记录原始异常内容") from None


def validate_preview(case, packet, suite, document_id):
    try:
        context = validate_reviewed_context(packet)
        expected = {item["review_id"]: item for item in suite["review_records"]}
        if (context["document_id"] != document_id or context["query"] != case["query"]
            or context["source_sha256"] != suite["review_records"][0]["source_sha256"]
            or sorted(item["review_id"] for item in context["evidence"]) != sorted(case["available_review_ids"])):
            raise ValueError("unexpected source or retrieval")
        for item in context["evidence"]:
            review = expected[item["review_id"]]
            if item["review_revision"] != canonical_hash(review) or any(item[key] != value for key, value in review.items()):
                raise ValueError("review changed")
        return context
    except (ValueError, TypeError, KeyError):
        raise AnswerEvaluationError("PREFLIGHT_MISMATCH", "预检的文档、检索结果或核对注释与标注不同；请重新核对，不进入收费评测") from None


def score_answer(case, packet, response):
    try:
        answer = ReviewedFormulaAnswer.model_validate(response).model_dump()
        if any(answer[key] != packet[key] for key in ("document_id", "source_sha256", "raw_context_id", "reviewed_context_id", "query", "included_count")):
            raise ValueError("answer binding mismatch")
        generation, check = answer["generation"], answer["check"]
        called = bool(packet["evidence"])
        if (generation["model_called"] != called or generation["calls_used"] > generation["max_calls"]
            or called and (generation["calls_used"] < 1 or generation["usage"] is None)
            or not called and generation["usage"] is not None):
            raise ValueError("generation state mismatch")
        usage = generation["usage"]
        if usage and usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            raise ValueError("usage mismatch")
        registry = {item["citation_id"]: item for item in packet["evidence"]}
        selected = []
        if answer["status"] == "answered":
            if (check["status"] != "passed" or check["candidate_decision"] != "answer"
                or check["issues"] or not check["claims"] or check["refusal_reason"] is not None):
                raise ValueError("invalid answered state")
            for claim in check["claims"]:
                if len(claim["citations"]) != 1:
                    raise ValueError("ambiguous reviewed claim")
                citation = claim["citations"][0]
                note = registry[citation["citation_id"]]
                if (claim["text"] != note["reviewed_statement"] or citation["quote"] != note["reviewed_statement"]
                    or citation["document_id"] != packet["document_id"] or citation["document_name"] != packet["document_name"]
                    or any(citation[key] != value for key, value in note.items())):
                    raise ValueError("invalid selected annotation")
                selected.append(note["review_id"])
            if len(set(selected)) != len(selected):
                raise ValueError("duplicate statements")
        elif answer["status"] == "refused":
            if (check["status"] != "passed" or check["candidate_decision"] != "refuse" or check["issues"]
                or check["claims"] or not check["refusal_reason"] or not check["refusal_reason"].strip()):
                raise ValueError("invalid refusal")
        elif check["status"] != "rejected" or check["claims"] or not check["issues"]:
            raise ValueError("invalid rejection")
        match = answer["status"] == case["expected_status"]
        if case["expected_status"] == "answered":
            match = match and sorted(selected) == sorted(case["available_review_ids"])
        return answer, {"observed_status": answer["status"], "behavior_match": match,
                        "selected_review_ids": selected, "manual_review_required": True}
    except (ValueError, TypeError, KeyError, IndexError):
        raise AnswerEvaluationError("ANSWER_CONTRACT_MISMATCH", "回答字段、来源、核对结论或用量不一致；停止后续请求，不把异常当成正确拒答") from None


def summarize(report):
    rows = report["cases"]
    complete = report["mode"] == "live" and all(row["execution_status"] == "completed" for row in rows)
    def rate(subset):
        return sum(row["score"]["behavior_match"] for row in subset) / len(subset) if complete and subset else None
    model_rows = [row for row in rows if row["preview"] and row["preview"]["included_count"] > 0]
    local_rows = [row for row in rows if row["preview"] and not row["preview"]["included_count"]]
    usage = {key: 0 for key in ("prompt_tokens", "completion_tokens", "total_tokens")}
    for row in rows:
        if row["response"] and row["response"]["generation"]["usage"]:
            for key in usage:
                usage[key] += row["response"]["generation"]["usage"][key]
    return {
        "dataset_case_count": len(rows), "preflight_completed_count": sum(row["preview"] is not None for row in rows),
        "planned_model_branch_cases": len(model_rows), "planned_local_branch_cases": len(local_rows),
        "completed_case_count": sum(row["execution_status"] == "completed" for row in rows),
        "fully_executed": complete, "behavior_match_rate": rate(rows),
        "model_branch_match_rate": rate(model_rows), "local_branch_match_rate": rate(local_rows),
        "answer_case_match_rate": rate([row for row in rows if row["expected_status"] == "answered"]),
        "refusal_case_match_rate": rate([row for row in rows if row["expected_status"] == "refused"]),
        "known_token_usage": usage if report["mode"] == "live" else None,
        "usage_is_complete_account_bill": False,
    }


def run_answer_evaluation(client, document_id, suite, *, live=False, allow_paid=False, max_model_calls=0, checkpoint=None):
    suite = validate_suite(suite)
    if not isinstance(document_id, str) or not re.fullmatch(r"[0-9a-f]{32}", document_id):
        raise AnswerEvaluationError("INVALID_DOCUMENT", "document-id 必须是真实文档的 32 位小写十六进制编号")
    if (type(live) is not bool or type(allow_paid) is not bool or type(max_model_calls) is not int
        or not 0 <= max_model_calls <= 3 or live and (not allow_paid or max_model_calls < 1 or checkpoint is None)
        or not live and (allow_paid or max_model_calls != 0)):
        raise AnswerEvaluationError("PAID_OPT_IN_REQUIRED", "真实评测须同时启用 live/allow-paid、指定 1～3 次模型尝试并保存报告；默认只免费预检")
    report = {
        "schema_version": "1.0", "run_id": uuid4().hex, "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": "live" if live else "preview", "status": "preflight_running",
        "document_id": document_id, "suite": suite, "suite_fingerprint": canonical_hash(suite),
        "scope": "small_development_set_expected_behavior_and_literal_review_binding",
        "automatic_mathematical_semantics_verified": False,
        "max_model_calls": max_model_calls, "model_call_reservations": 0,
        "next_case_id": None, "error": None, "summary": None,
        "cases": [{**case, "preview": None, "execution_status": "not_run", "response": None, "score": None, "error": None}
                  for case in suite["cases"]],
    }
    def save():
        report["summary"] = summarize(report)
        if checkpoint:
            checkpoint(report)
    base = f"/documents/{document_id}"
    try:
        save()
        for row in report["cases"]:
            report["next_case_id"] = row["case_id"]
            payload = {"query": row["query"], **suite["config"]}
            packet = _post_json(client, base + "/reviewed-context", payload)
            row["preview"] = validate_preview(row, packet, suite, document_id)
            save()
        report["status"] = "preflight_completed"
        report["next_case_id"] = None
        save()
        needed = sum(bool(row["preview"]["evidence"]) for row in report["cases"])
        if not live:
            report["status"] = "preview_completed"
        elif needed > max_model_calls:
            raise AnswerEvaluationError("MODEL_BUDGET_TOO_SMALL", "整轮可能调用模型的次数超过本次显式上限；未发送任何回答请求")
        else:
            report["status"] = "executing"
            for row in report["cases"]:
                report["next_case_id"] = row["case_id"]
                row["execution_status"] = "pending"
                packet = row["preview"]
                if packet["evidence"]:
                    # Reserve before sending, never refund on errors/timeouts.
                    report["model_call_reservations"] += 1
                save()  # Persistence failure here must prevent the request.
                payload = {"query": row["query"], **suite["config"], "allow_external": True,
                           "expected_reviewed_context_id": packet["reviewed_context_id"]}
                body = _post_json(client, base + "/answer-reviewed", payload)
                row["response"], row["score"] = score_answer(row, packet, body)
                row["execution_status"] = "completed"
                save()
            report["status"] = "completed"
        report["next_case_id"] = None
    except AnswerEvaluationError as error:
        report["error"] = {"code": error.code, "detail": str(error)}
        report["status"] = "execution_failed" if report["status"] == "executing" else "preflight_failed"
        for row in report["cases"]:
            if row["case_id"] == report["next_case_id"]:
                row["execution_status"] = "error"
                row["error"] = report["error"]
            elif row["execution_status"] == "not_run" and live:
                row["execution_status"] = "skipped_after_error"
    save()
    return report
