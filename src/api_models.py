from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500, strict=True)
    method: Literal["original", "normalized"] = "original"
    k: int = Field(default=5, ge=1, le=20, strict=True)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        value = value.strip()

        if not value:
            raise ValueError("问题不能为空")

        return value


class EvidenceChunk(BaseModel):
    document_name: str = Field(min_length=1, strict=True)
    pdf_page_number: int = Field(
        ge=1,
        strict=True,
        description="PDF 物理页号，从 1 开始，不是页面印刷页码",
    )
    start_char: int = Field(
        ge=0,
        strict=True,
        description="片段在该页原文中的起始字符位置，包含此位置",
    )
    end_char: int = Field(
        ge=0,
        strict=True,
        description="片段在该页原文中的结束字符位置，不包含此位置",
    )
    text: str = Field(strict=True)


class SearchResult(BaseModel):
    score: float = Field(
        strict=True,
        allow_inf_nan=False,
        description="BM25 排序分数，不是概率或置信度",
    )
    chunk: EvidenceChunk


class SearchResponse(BaseModel):
    query: str = Field(strict=True)
    method: Literal["original", "normalized"]
    k: int = Field(ge=1, le=20, strict=True)
    returned_count: int = Field(ge=0, strict=True)
    results: list[SearchResult]


class UploadResponse(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    document_name: str = Field(min_length=1, strict=True)
    size_bytes: int = Field(ge=1, strict=True)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    status: Literal["uploaded"] = Field(
        description="仅表示原始文件已保存，尚未解析或加入搜索索引",
    )


class ParseRequest(BaseModel):
    chunk_size: int = Field(
        default=500,
        ge=1,
        le=10_000,
        strict=True,
        description="按页分块的字符数上限，不是词数或 token 数",
    )


class ParseResponse(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    document_name: str = Field(min_length=1, strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    status: Literal["parsed"] = Field(description="页面和分块已保存，尚未加入搜索索引")
    page_count: int = Field(ge=1, strict=True)
    nonempty_page_count: int = Field(ge=0, strict=True)
    empty_page_numbers: list[int]
    chunk_count: int = Field(ge=0, strict=True)
    chunk_size: int = Field(ge=1, le=10_000, strict=True)
    parser_version: str
    warnings: list[str]


class DocumentEvidenceChunk(EvidenceChunk):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)


class DocumentSearchResult(SearchResult):
    chunk: DocumentEvidenceChunk


class DocumentSearchResponse(SearchResponse):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    document_name: str = Field(min_length=1, strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    indexed_chunk_count: int = Field(ge=0, strict=True)
    cache_hit: bool = Field(strict=True, description="本次是否复用已有的内存索引")
    results: list[DocumentSearchResult]


class ContextRequest(SearchRequest):
    max_context_chars: int = Field(
        default=6000, ge=128, le=20_000, strict=True,
        description="序列化后的证据上下文字符上限，含标签与来源，不是模型 token 上限",
    )


class ContextEvidence(DocumentEvidenceChunk):
    citation_id: str = Field(pattern=r"^E[1-9][0-9]*$", strict=True)
    retrieval_rank: int = Field(ge=1, le=20, strict=True)
    score: float = Field(strict=True, allow_inf_nan=False, description="原 BM25 分数，不是置信度")


class ContextOmission(BaseModel):
    retrieval_rank: int = Field(ge=1, le=20, strict=True)
    reason: Literal["duplicate", "budget_exceeded"]
    pdf_page_number: int = Field(ge=1, strict=True)
    start_char: int = Field(ge=0, strict=True)
    end_char: int = Field(ge=0, strict=True)


class ContextResponse(BaseModel):
    context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True, description="当前问题、参数及证据包的指纹，不是认证令牌")
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    document_name: str = Field(min_length=1, strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    query: str = Field(strict=True)
    method: Literal["original", "normalized"]
    k: int = Field(ge=1, le=20, strict=True)
    status: Literal["ready", "no_matches", "budget_exceeded"] = Field(
        description="ready 仅表示有片段被收录，不表示证据充分或答案正确",
    )
    retrieved_count: int = Field(ge=0, le=20, strict=True, description="本次 Top K 候选数，不是全库匹配总数")
    included_count: int = Field(ge=0, le=20, strict=True)
    omitted_count: int = Field(ge=0, le=20, strict=True)
    max_context_chars: int = Field(ge=128, le=20_000, strict=True)
    used_chars: int = Field(ge=0, strict=True)
    evidence: list[ContextEvidence]
    omitted: list[ContextOmission]
    context_text: str = Field(strict=True, description="证据 JSON 字符串；没有收录片段时为空，不包含模型指令")


class CitationUse(BaseModel):
    model_config = {"extra": "forbid"}
    citation_id: str = Field(pattern=r"^E[1-9][0-9]*$", strict=True)
    quote: str = Field(min_length=1, max_length=1000, strict=True)

    @field_validator("quote")
    @classmethod
    def nonblank_quote(cls, value):
        if not value.strip():
            raise ValueError("引文不能只有空白")
        return value


class AnswerClaim(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(min_length=1, max_length=2000, strict=True)
    citations: list[CitationUse] = Field(min_length=1, max_length=5)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value):
        if not value.strip():
            raise ValueError("回答句子不能只有空白")
        return value


class AnswerDraft(BaseModel):
    model_config = {"extra": "forbid"}
    decision: Literal["answer", "refuse"]
    claims: list[AnswerClaim] = Field(default_factory=list, max_length=20)
    reason: str | None = Field(default=None, max_length=1000, strict=True)


class AnswerCheckRequest(ContextRequest):
    model_config = {"extra": "forbid"}
    context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    draft: AnswerDraft


class AnswerCheckIssue(BaseModel):
    code: Literal[
        "invalid_draft", "empty_answer", "answer_has_refusal_reason", "missing_refusal_reason",
        "refusal_has_claims", "no_evidence_for_answer", "unknown_citation", "quote_not_found",
        "duplicate_citation", "inline_citation_not_allowed",
        "reviewed_statement_mismatch",
    ]
    detail: str
    claim_index: int | None = Field(default=None, ge=1, le=20, strict=True)
    citation_id: str | None = Field(default=None, pattern=r"^E[1-9][0-9]*$", strict=True)


class ResolvedCitation(BaseModel):
    citation_id: str = Field(pattern=r"^E[1-9][0-9]*$", strict=True)
    quote: str = Field(strict=True)
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    document_name: str = Field(strict=True)
    pdf_page_number: int = Field(ge=1, strict=True)
    evidence_start_char: int = Field(ge=0, strict=True)
    evidence_end_char: int = Field(ge=0, strict=True)
    quote_start_char: int = Field(ge=0, strict=True)
    quote_end_char: int = Field(ge=0, strict=True)


class CheckedClaim(BaseModel):
    text: str = Field(strict=True)
    citations: list[ResolvedCitation]


class AnswerCheckResponse(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    query: str
    status: Literal["passed", "rejected"]
    candidate_decision: Literal["answer", "refuse"] | None
    checks_scope: Literal["format_and_literal_citations"] = "format_and_literal_citations"
    semantics_verified: Literal[False] = False
    issues: list[AnswerCheckIssue]
    claims: list[CheckedClaim]
    refusal_reason: str | None


class FormulaQualityRequest(ContextRequest):
    model_config = {"extra": "forbid"}
    method: Literal["original", "normalized"] = "normalized"
    k: int = Field(default=10, ge=1, le=20, strict=True)
    max_context_chars: int = Field(default=6000, ge=128, le=6000, strict=True,
                                  description="本次模型接入最多 6000 个证据字符，不是 token 数")


class AnswerRequest(FormulaQualityRequest):
    allow_external: Literal[True] = Field(description="必填 true：同意将问题和检索片段发送到 DeepSeek；此调用可能收费")

    @field_validator("allow_external", mode="before")
    @classmethod
    def explicit_external_consent(cls, value):
        if value is not True:
            raise ValueError("allow_external 必须明确填写布尔值 true")
        return value


class ModelTokenUsage(BaseModel):
    prompt_tokens: int = Field(ge=0, strict=True)
    completion_tokens: int = Field(ge=0, strict=True)
    total_tokens: int = Field(ge=0, strict=True)


class ReviewedAnswerRequest(AnswerRequest):
    expected_reviewed_context_id: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$", strict=True,
        description="可选：绑定免费预览得到的核对证据版本；改变时在模型调用前返回 409",
    )


class GenerationInfo(BaseModel):
    provider: Literal["deepseek"]
    model: Literal["deepseek-flash"]
    model_called: bool = Field(strict=True)
    usage: ModelTokenUsage | None
    calls_used: int = Field(ge=0, le=100, strict=True)
    max_calls: int = Field(ge=1, le=100, strict=True,
                           description="每个服务实例的尝试次数上限；不是人民币预算，重启会重置")


class GenerationStatus(BaseModel):
    model_config = {"extra": "forbid"}
    provider: Literal["deepseek"]
    model: Literal["deepseek-flash"]
    status: Literal["disabled", "missing_key", "busy", "limit_reached", "ready"]
    calls_used: int = Field(ge=0, le=100, strict=True)
    max_calls: int = Field(ge=1, le=100, strict=True)
    credential_verified: Literal[False] = False


class FormulaPageQuality(BaseModel):
    model_config = {"extra": "forbid"}
    pdf_page_number: int = Field(ge=1, le=300, strict=True)
    status: Literal["risk_detected", "not_flagged", "unassessed"]
    short_horizontal_mark_count: int = Field(ge=0, le=20_000, strict=True)
    reasons: list[Literal["short_horizontal_vector_marks", "unsupported_graphics", "scan_limit", "page_unreadable", "not_checked"]]
    detail: str = Field(strict=True)


class FormulaQualityResponse(BaseModel):
    model_config = {"extra": "forbid"}
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    status: Literal["risk_detected", "not_flagged", "unassessed", "no_evidence"]
    checked_page_numbers: list[int] = Field(max_length=20)
    pages: list[FormulaPageQuality] = Field(max_length=20)
    blocks_generation: bool = Field(strict=True)
    scope: Literal["page_level_vector_marks"] = "page_level_vector_marks"
    text_modified: Literal[False] = False


class DocumentAnswerResponse(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    query: str
    context_status: Literal["ready", "no_matches", "budget_exceeded"]
    included_count: int = Field(ge=0, le=20, strict=True)
    status: Literal["answered", "refused", "rejected"] = Field(description="answered 仅表示生成完成且字面引用通过，不保证事实或推理正确")
    generation: GenerationInfo
    quality: FormulaQualityResponse
    check: AnswerCheckResponse


class FormulaReviewRecord(BaseModel):
    model_config = {"extra": "forbid"}
    review_id: str = Field(pattern=r"^FR[0-9]{3}$", strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    pdf_page_number: int = Field(ge=1, le=300, strict=True)
    raw_start_char: int = Field(ge=0, strict=True)
    raw_end_char: int = Field(ge=1, strict=True)
    raw_quote: str = Field(min_length=1, max_length=1000, strict=True)
    reviewed_statement: str = Field(min_length=1, max_length=1000, strict=True)
    formula_latex: str = Field(min_length=1, max_length=500, strict=True)
    review_method: Literal["assistant_visual_review"]
    reviewed_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", strict=True)
    scope: Literal["single_statement"]


class ReviewedFormulaEvidence(FormulaReviewRecord):
    citation_id: str = Field(pattern=r"^E[1-9][0-9]*$", strict=True)
    review_revision: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    source_kind: Literal["reviewed_formula_annotation"] = "reviewed_formula_annotation"


class ReviewedFormulaContext(BaseModel):
    model_config = {"extra": "forbid"}
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    document_name: str = Field(strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    raw_context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    reviewed_context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    query: str = Field(strict=True)
    status: Literal["ready", "no_evidence", "no_reviewed_evidence", "budget_exceeded"]
    included_count: int = Field(ge=0, le=8, strict=True)
    max_context_chars: int = Field(ge=128, le=6000, strict=True)
    used_chars: int = Field(ge=0, le=6000, strict=True)
    source_kind: Literal["reviewed_formula_annotation"] = "reviewed_formula_annotation"
    raw_text_modified: Literal[False] = False
    evidence: list[ReviewedFormulaEvidence] = Field(max_length=8)
    context_text: str = Field(strict=True)


class ReviewedFormulaCitation(ReviewedFormulaEvidence):
    quote: str = Field(strict=True, description="引自已核对注释，不是 PDF 的逐字提取文本")
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    document_name: str = Field(strict=True)


class ReviewedFormulaClaim(BaseModel):
    text: str = Field(strict=True)
    citations: list[ReviewedFormulaCitation]


class ReviewedFormulaCheck(BaseModel):
    status: Literal["passed", "rejected"]
    candidate_decision: Literal["answer", "refuse"] | None
    checks_scope: Literal["format_citations_and_reviewed_statement_selection"] = "format_citations_and_reviewed_statement_selection"
    semantics_verified: Literal[False] = False
    claims: list[ReviewedFormulaClaim]
    issues: list[AnswerCheckIssue]
    refusal_reason: str | None


class ReviewedFormulaAnswer(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    raw_context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    reviewed_context_id: str = Field(pattern=r"^[0-9a-f]{64}$", strict=True)
    query: str
    source_kind: Literal["reviewed_formula_annotation"] = "reviewed_formula_annotation"
    status: Literal["answered", "refused", "rejected"]
    included_count: int = Field(ge=0, le=8, strict=True)
    generation: GenerationInfo
    check: ReviewedFormulaCheck


class DocumentInfo(BaseModel):
    document_id: str = Field(pattern=r"^[0-9a-f]{32}$", strict=True)
    status: Literal["uploaded", "parsed", "incomplete", "invalid"]
    document_name: str | None = Field(default=None, min_length=1, strict=True)
    size_bytes: int | None = Field(default=None, ge=1, strict=True)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", strict=True)
    page_count: int | None = Field(default=None, ge=1, strict=True)
    nonempty_page_count: int | None = Field(default=None, ge=0, strict=True)
    chunk_count: int | None = Field(default=None, ge=0, strict=True)
    chunk_size: int | None = Field(default=None, ge=1, le=10_000, strict=True)
    empty_page_numbers: list[int] | None = None
    warnings: list[str] = Field(default_factory=list)
    detail: str


class DocumentListResponse(BaseModel):
    total: int = Field(ge=0, strict=True)
    offset: int = Field(ge=0, strict=True)
    limit: int = Field(ge=1, le=100, strict=True)
    returned_count: int = Field(ge=0, strict=True)
    documents: list[DocumentInfo]
