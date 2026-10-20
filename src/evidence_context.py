"""Pack checked retrieval into numbered evidence, without an LLM or file writes."""

import hashlib
import json

from src.api_models import ContextEvidence, ContextOmission, ContextResponse, DocumentSearchResponse


class EvidenceContextError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


def serialize_context_evidence(evidence):
    # Long machine identifiers and scores live in the reference registry, not
    # in the model-facing evidence text. JSON quoting preserves literal content.
    records = [
        {key: item[key] for key in (
            "citation_id", "document_name", "pdf_page_number", "start_char", "end_char", "text",
        )}
        for item in evidence
    ]
    return json.dumps(records, ensure_ascii=False, indent=2) if records else ""


def compute_context_id(packet):
    # An integrity/version identifier, NOT a signature or authentication token.
    fields = {key: value for key, value in packet.items() if key != "context_id"}
    serialized = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_evidence_context(search_response, max_context_chars=6000):
    if type(max_context_chars) is not int or not 128 <= max_context_chars <= 20_000:
        raise EvidenceContextError(422, "上下文预算必须是 128 到 20000 的整数字符数")
    try:
        search = DocumentSearchResponse.model_validate(search_response)
    except (ValueError, TypeError) as error:
        raise EvidenceContextError(409, "检索结果字段不合法，无法建立证据来源") from error
    if search.returned_count != len(search.results) or search.returned_count > search.k:
        raise EvidenceContextError(409, "检索返回数量不一致")

    # Validate every candidate before selecting any, including omitted ones.
    seen = {}
    candidates = []
    for rank, hit in enumerate(search.results, start=1):
        chunk = hit.chunk
        if (
            chunk.document_id != search.document_id
            or chunk.document_name != search.document_name
            or chunk.end_char - chunk.start_char != len(chunk.text)
            or not chunk.text.strip()
        ):
            raise EvidenceContextError(409, "检索片段的文档来源、字符区间或原文不一致")
        key = (chunk.document_id, chunk.pdf_page_number, chunk.start_char, chunk.end_char)
        duplicate = key in seen
        if duplicate and seen[key] != chunk.text:
            raise EvidenceContextError(409, "同一来源位置出现不同原文")
        seen[key] = chunk.text
        candidates.append((rank, hit, duplicate))

    evidence, omitted = [], []
    context_text = ""
    for rank, hit, duplicate in candidates:
        chunk = hit.chunk
        record = ContextEvidence(
            **chunk.model_dump(), citation_id=f"E{len(evidence) + 1}",
            retrieval_rank=rank, score=hit.score,
        ).model_dump()
        trial_text = serialize_context_evidence([*evidence, record]) if not duplicate else ""
        reason = "duplicate" if duplicate else "budget_exceeded" if len(trial_text) > max_context_chars else None
        if reason is not None:
            omitted.append(ContextOmission(
                retrieval_rank=rank, reason=reason,
                pdf_page_number=chunk.pdf_page_number,
                start_char=chunk.start_char, end_char=chunk.end_char,
            ).model_dump())
            continue
        evidence.append(record)
        context_text = trial_text

    status = "ready" if evidence else "no_matches" if not search.results else "budget_exceeded"
    fields = dict(
        document_id=search.document_id, document_name=search.document_name,
        source_sha256=search.source_sha256, query=search.query, method=search.method, k=search.k,
        status=status, retrieved_count=len(search.results), included_count=len(evidence),
        omitted_count=len(omitted), max_context_chars=max_context_chars,
        used_chars=len(context_text), evidence=evidence, omitted=omitted, context_text=context_text,
    )
    return ContextResponse(context_id=compute_context_id(fields), **fields).model_dump()
