"""Parse one immutable upload, preserving page numbers and text offsets."""

import hashlib
import json
import re
from importlib.metadata import version
from io import BytesIO
from pathlib import Path

from pydantic import ValidationError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from src.api_models import UploadResponse
from src.chunking import chunk_page
from src.document_upload import MAX_UPLOAD_BYTES
from src.extraction import extract_pages
from src.storage import write_jsonl


MAX_PARSE_PAGES = 300
MAX_EXTRACTED_CHARS = 2_000_000


class DocumentProcessingError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


def process_document(
    document_id: str,
    upload_directory: Path,
    parsed_directory: Path,
    chunk_size: int = 500,
):
    if not isinstance(document_id, str) or not re.fullmatch(r"[0-9a-f]{32}", document_id):
        raise DocumentProcessingError(422, "文档编号必须是 32 位小写十六进制字符串")
    if type(chunk_size) is not int or not 1 <= chunk_size <= 10_000:
        raise DocumentProcessingError(422, "chunk_size 必须是 1 到 10000 的整数")

    pdf_path = upload_directory / f"{document_id}.pdf"
    metadata_path = upload_directory / f"{document_id}.json"
    if pdf_path.is_symlink() or metadata_path.is_symlink():
        raise DocumentProcessingError(409, "上传记录不能指向符号链接")
    if not pdf_path.is_file() or not metadata_path.is_file():
        raise DocumentProcessingError(404, "文档不存在或上传记录不完整")

    try:
        metadata = UploadResponse.model_validate(
            json.loads(metadata_path.read_text(encoding="utf-8")),
        )
    except (ValidationError, ValueError, UnicodeError) as error:
        raise DocumentProcessingError(500, "上传记录损坏，无法解析") from error
    if metadata.document_id != document_id:
        raise DocumentProcessingError(409, "上传记录的文档编号不一致")

    output_directory = parsed_directory / document_id
    if output_directory.exists():
        raise DocumentProcessingError(409, "该文档已有解析结果，当前版本不覆盖旧结果")

    # Parse a bounded snapshot: the bytes hashed are exactly the bytes parsed.
    with pdf_path.open("rb") as source:
        data = source.read(MAX_UPLOAD_BYTES + 1)
    source_sha256 = hashlib.sha256(data).hexdigest()
    if len(data) != metadata.size_bytes or source_sha256 != metadata.sha256:
        raise DocumentProcessingError(409, "文件与上传时的大小或指纹不一致，请重新上传")
    if len(data) > MAX_UPLOAD_BYTES:
        raise DocumentProcessingError(413, "原始文件超过解析大小上限")

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise DocumentProcessingError(422, "当前版本暂不支持加密 PDF，请先解除加密")
        page_count = len(reader.pages)
        if not page_count:
            raise DocumentProcessingError(422, "PDF 没有可解析的页面")
        if page_count > MAX_PARSE_PAGES:
            raise DocumentProcessingError(413, "PDF 超过当前 300 页解析上限")
        pages = extract_pages(reader, metadata.document_name)
    except DocumentProcessingError:
        raise
    except (PdfReadError, ValueError, KeyError, TypeError) as error:
        raise DocumentProcessingError(422, "PDF 无法读取或提取文本，文件可能损坏") from error

    if sum(len(page["text"]) for page in pages) > MAX_EXTRACTED_CHARS:
        raise DocumentProcessingError(413, "提取文本超过当前 200 万字符上限")

    chunks = []
    for page in pages:
        page["document_id"] = document_id
        for chunk in chunk_page(page, chunk_size=chunk_size):
            chunk["document_id"] = document_id
            chunks.append(chunk)

    empty_page_numbers = [
        page["pdf_page_number"] for page in pages if not page["text"].strip()
    ]
    warnings = []
    if len(empty_page_numbers) == page_count:
        warnings.append("未提取到非空文本，可能是空白或扫描 PDF；当前版本不执行 OCR")

    report = {
        "document_id": document_id,
        "document_name": metadata.document_name,
        "source_sha256": source_sha256,
        "status": "parsed",
        "page_count": page_count,
        "nonempty_page_count": page_count - len(empty_page_numbers),
        "empty_page_numbers": empty_page_numbers,
        "chunk_count": len(chunks),
        "chunk_size": chunk_size,
        "parser_version": f"pypdf {version('pypdf')}",
        "warnings": warnings,
    }

    parsed_directory.mkdir(parents=True, exist_ok=True)
    try:
        output_directory.mkdir(exist_ok=False)
    except FileExistsError as error:
        raise DocumentProcessingError(409, "该文档已有解析结果，当前版本不覆盖旧结果") from error
    pages_path = output_directory / "pages.jsonl"
    chunks_path = output_directory / "chunks.jsonl"
    report_path = output_directory / "parse_report.json"
    try:
        write_jsonl(pages, pages_path)
        write_jsonl(chunks, chunks_path)
        # Written last: a report exists only after both data files were saved.
        with report_path.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(report, output, ensure_ascii=False)
            output.write("\n")
    except BaseException:
        # Only this attempt's new directory and known artifacts are cleaned up.
        for path in (report_path, chunks_path, pages_path):
            path.unlink(missing_ok=True)
        output_directory.rmdir()
        raise
    return report
