"""Conservative page-level graphics heuristic, NOT formula restoration or OCR."""

import math
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.answer_check import AnswerCheckError, validate_evidence_context
from src.api_models import FormulaPageQuality, FormulaQualityResponse
from src.document_files import get_document_file


MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_PAGE_OPERATIONS = 20_000
UNSUPPORTED_GRAPHICS = {b"Do", b"sh", b"BI", b"ID", b"EI", b"INLINE IMAGE", b"W", b"W*", b"c", b"v", b"y"}


def _numbers(args, count):
    if len(args) != count:
        raise ValueError("unexpected operands")
    values = [float(value) for value in args]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("non-finite operands")
    return values


def _short_line(start, end, width):
    return (0 <= width <= 1 and abs(end[1] - start[1]) <= 0.05
            and 2 <= abs(end[0] - start[0]) <= 80)


def _page_result(page_number, count=0, reason=None):
    status = "risk_detected" if count else "unassessed" if reason else "not_flagged"
    reasons = (["short_horizontal_vector_marks"] if count else []) + ([reason] if reason else [])
    detail = ("检测到细短水平绘图痕迹，可能承载共轭横线或分式等；尚未恢复其含义。" if count
              else "当前规则无法完整检查该页，不能当成公式提取可靠。" if reason
              else "本规则未发现细短水平绘图痕迹；不代表公式完整或数学正确。")
    return FormulaPageQuality(pdf_page_number=page_number, status=status,
                              short_horizontal_mark_count=count, reasons=reasons, detail=detail).model_dump()


def assess_page_operations(operations, page_number):
    """Inspect simple stroked lines/filled bars, without assigning them to glyphs.

    Geometry is in content coordinates; only translation matrices are supported.
    Other transformations, XObjects, images, curves, clipping, or excessive input
    are explicitly unassessed rather than silently labelled clear.
    """
    if len(operations) > MAX_PAGE_OPERATIONS:
        return _page_result(page_number, reason="scan_limit")
    width, stack, path, rectangle = 1.0, [], [], None
    count, unknown = 0, None
    try:
        for args, operator in operations:
            if operator in UNSUPPORTED_GRAPHICS:
                unknown = "unsupported_graphics"
            if operator == b"q":
                stack.append(width)
            elif operator == b"Q":
                if not stack:
                    raise ValueError("unbalanced graphics state")
                width = stack.pop()
            elif operator == b"cm":
                a, b, c, d, _, _ = _numbers(args, 6)
                if (a, b, c, d) != (1, 0, 0, 1):
                    unknown = "unsupported_graphics"
            elif operator == b"w":
                width = _numbers(args, 1)[0]
                if width < 0:
                    raise ValueError("negative stroke width")
            elif operator == b"m":
                path = [tuple(_numbers(args, 2))]
                rectangle = None
            elif operator == b"l":
                path.append(tuple(_numbers(args, 2)))
            elif operator == b"re":
                rectangle = _numbers(args, 4)
                path = []
            elif operator in (b"S", b"s", b"f", b"f*", b"F", b"B", b"B*", b"b", b"b*", b"n"):
                if operator in (b"S", b"B", b"B*") and len(path) == 2 and _short_line(path[0], path[1], width):
                    count += 1
                if operator in (b"f", b"f*", b"F", b"B", b"B*", b"b", b"b*") and rectangle:
                    _, _, bar_width, bar_height = rectangle
                    if 2 <= abs(bar_width) <= 80 and 0 < abs(bar_height) <= 1:
                        count += 1
                path, rectangle = [], None
        if stack:
            raise ValueError("unbalanced graphics state")
    except (ValueError, TypeError, IndexError, OverflowError):
        unknown = "page_unreadable"
    return _page_result(page_number, count=count, reason=unknown)


def build_quality_report(packet, page_reports):
    context = validate_evidence_context(packet)
    pages = sorted({item["pdf_page_number"] for item in context["evidence"]})
    records = [FormulaPageQuality.model_validate(item).model_dump() for item in page_reports]
    if [item["pdf_page_number"] for item in records] != pages:
        raise AnswerCheckError(409, "公式质量检查页号与当前证据不一致")
    status = ("no_evidence" if not pages else "risk_detected" if any(item["status"] == "risk_detected" for item in records)
              else "unassessed" if any(item["status"] == "unassessed" for item in records) else "not_flagged")
    return FormulaQualityResponse(
        document_id=context["document_id"], source_sha256=context["source_sha256"], context_id=context["context_id"],
        status=status, checked_page_numbers=pages, pages=records,
        blocks_generation=status in ("risk_detected", "unassessed"),
        scope="page_level_vector_marks", text_modified=False,
    ).model_dump()


def validate_quality_report(packet, report):
    context = validate_evidence_context(packet)
    expected_pages = sorted({item["pdf_page_number"] for item in context["evidence"]})
    if report is None:
        # Internal callers cannot implicitly treat missing checks as a pass.
        return build_quality_report(context, [_page_result(page, reason="not_checked") for page in expected_pages])
    try:
        quality = FormulaQualityResponse.model_validate(report).model_dump()
        rebuilt = build_quality_report(context, quality["pages"])
        if quality != rebuilt:
            raise ValueError("inconsistent quality/source binding")
        for item in quality["pages"]:
            has_marks = item["short_horizontal_mark_count"] > 0
            if (has_marks != (item["status"] == "risk_detected")
                or has_marks != ("short_horizontal_vector_marks" in item["reasons"])
                or (item["status"] == "not_flagged" and item["reasons"])
                or (item["status"] == "unassessed" and not item["reasons"])):
                raise ValueError("inconsistent page assessment")
        return quality
    except (ValueError, TypeError):
        raise AnswerCheckError(409, "公式质量报告字段、来源或状态不一致") from None


def assess_document_formula_quality(packet, upload_directory):
    context = validate_evidence_context(packet)
    page_numbers = sorted({item["pdf_page_number"] for item in context["evidence"]})
    if not page_numbers:
        return build_quality_report(context, [])
    # Checked bytes from the same immutable source version, not an arbitrary path.
    snapshot = get_document_file(context["document_id"], upload_directory, expected_sha256=context["source_sha256"])
    try:
        reader = PdfReader(BytesIO(snapshot.content))
        if reader.is_encrypted or len(reader.pages) > 300 or any(page > len(reader.pages) for page in page_numbers):
            return build_quality_report(context, [_page_result(page, reason="page_unreadable") for page in page_numbers])
        reports = []
        for page_number in page_numbers:
            try:
                content = reader.pages[page_number - 1].get_contents()
                if content is None:
                    report = _page_result(page_number)
                elif len(content.get_data()) > MAX_CONTENT_BYTES:
                    report = _page_result(page_number, reason="scan_limit")
                else:
                    report = assess_page_operations(content.operations, page_number)
            except (PdfReadError, ValueError, TypeError, KeyError, IndexError, RecursionError):
                report = _page_result(page_number, reason="page_unreadable")
            reports.append(report)
    except (PdfReadError, ValueError, TypeError, KeyError, IndexError, RecursionError):
        reports = [_page_result(page, reason="page_unreadable") for page in page_numbers]
    return build_quality_report(context, reports)
