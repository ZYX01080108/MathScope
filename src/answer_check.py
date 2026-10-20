"""Check draft structure and literal citations; never claim semantic support."""

import re

from src.api_models import AnswerCheckIssue, AnswerCheckResponse, AnswerDraft, ContextResponse
from src.evidence_context import compute_context_id, serialize_context_evidence


INLINE_CITATION = re.compile(r"\[\s*E[^\]\r\n]*\]")


class AnswerCheckError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


def validate_evidence_context(packet):
    try:
        context = ContextResponse.model_validate(packet).model_dump()
    except (ValueError, TypeError) as error:
        raise AnswerCheckError(409, "证据包字段不合法") from error
    evidence = context["evidence"]
    expected_status = "ready" if evidence else "no_matches" if not context["retrieved_count"] else "budget_exceeded"
    if (
        context["context_id"] != compute_context_id(context)
        or context["included_count"] != len(evidence)
        or context["omitted_count"] != len(context["omitted"])
        or context["retrieved_count"] != len(evidence) + len(context["omitted"])
        or context["retrieved_count"] > context["k"]
        or context["status"] != expected_status
        or context["context_text"] != serialize_context_evidence(evidence)
        or context["used_chars"] != len(context["context_text"])
        or context["used_chars"] > context["max_context_chars"]
    ):
        raise AnswerCheckError(409, "证据包指纹、内容或数量不一致")
    for index, item in enumerate(evidence, start=1):
        if (
            item["citation_id"] != f"E{index}"
            or item["document_id"] != context["document_id"]
            or item["document_name"] != context["document_name"]
            or item["end_char"] - item["start_char"] != len(item["text"])
            or not item["text"].strip()
        ):
            raise AnswerCheckError(409, "证据编号、来源或字符区间不一致")
    return context


def check_answer_draft(packet, draft):
    context = validate_evidence_context(packet)
    registry = {item["citation_id"]: item for item in context["evidence"]}
    issues, checked = [], []

    def issue(code, detail, claim_index=None, citation_id=None):
        issues.append(AnswerCheckIssue(
            code=code, detail=detail, claim_index=claim_index, citation_id=citation_id,
        ).model_dump())

    candidate = None
    try:
        candidate = AnswerDraft.model_validate(draft)
    except (ValueError, TypeError):
        issue("invalid_draft", "回答结构不符合约定，请使用 claims/citations 或拒答 reason")

    if candidate is not None and candidate.decision == "refuse":
        if candidate.claims:
            issue("refusal_has_claims", "拒答草稿不能同时包含回答句子")
        if candidate.reason is None or not candidate.reason.strip():
            issue("missing_refusal_reason", "拒答草稿必须提供非空理由")
        elif INLINE_CITATION.search(candidate.reason):
            issue("inline_citation_not_allowed", "拒答理由不要插入未经登记的引用标记")
    elif candidate is not None:
        if not candidate.claims:
            issue("empty_answer", "回答草稿至少需要一条带引用的句子")
        if candidate.reason is not None:
            issue("answer_has_refusal_reason", "回答草稿不能同时携带拒答理由")
        if not registry:
            issue("no_evidence_for_answer", "当前证据包没有可引用片段，不能通过回答引用核验")
        for index, claim in enumerate(candidate.claims, start=1):
            if INLINE_CITATION.search(claim.text):
                issue("inline_citation_not_allowed", "编号必须放在 citations 中，不要手写在句子里", index)
            citations, seen = [], set()
            for use in claim.citations:
                key = (use.citation_id, use.quote)
                if key in seen:
                    issue("duplicate_citation", "同一条句子的相同引用重复出现", index, use.citation_id)
                    continue
                seen.add(key)
                evidence = registry.get(use.citation_id)
                if evidence is None:
                    issue("unknown_citation", "引用编号不属于当前证据包", index, use.citation_id)
                    continue
                offset = evidence["text"].find(use.quote)
                if offset < 0:
                    issue("quote_not_found", "引文不是对应证据中的连续原文，不能自动改写或纠正", index, use.citation_id)
                    continue
                citations.append({
                    "citation_id": use.citation_id, "quote": use.quote,
                    "document_id": evidence["document_id"], "document_name": evidence["document_name"],
                    "pdf_page_number": evidence["pdf_page_number"],
                    "evidence_start_char": evidence["start_char"], "evidence_end_char": evidence["end_char"],
                    "quote_start_char": evidence["start_char"] + offset,
                    "quote_end_char": evidence["start_char"] + offset + len(use.quote),
                })
            checked.append({"text": claim.text, "citations": citations})

    passed = not issues
    return AnswerCheckResponse(
        document_id=context["document_id"], source_sha256=context["source_sha256"],
        context_id=context["context_id"], query=context["query"],
        status="passed" if passed else "rejected",
        candidate_decision=candidate.decision if candidate is not None else None,
        checks_scope="format_and_literal_citations", semantics_verified=False,
        issues=issues, claims=checked if passed else [],
        refusal_reason=candidate.reason if passed and candidate.decision == "refuse" else None,
    ).model_dump()
