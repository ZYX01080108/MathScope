"""Read-only document inventory; report status is not a BM25 readiness claim."""

import hashlib
import json
import re
from pathlib import Path

from src.api_models import DocumentInfo, ParseResponse, UploadResponse
from src.document_upload import MAX_UPLOAD_BYTES


DOCUMENT_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class DocumentCatalogError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


def _read_bounded(path: Path, limit: int):
    if path.is_symlink():
        raise ValueError("不接受符号链接")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("文件超过检查上限")
    return data


def get_document_info(document_id, upload_directory: Path, parsed_directory: Path):
    if not isinstance(document_id, str) or not DOCUMENT_ID_PATTERN.fullmatch(document_id):
        raise DocumentCatalogError(422, "文档编号必须是 32 位小写十六进制字符串")
    pdf_path = upload_directory / f"{document_id}.pdf"
    metadata_path = upload_directory / f"{document_id}.json"
    directory = parsed_directory / document_id
    paths = (pdf_path, metadata_path, directory)
    if not any(path.exists() or path.is_symlink() for path in paths):
        raise DocumentCatalogError(404, "文档不存在")

    info = DocumentInfo(document_id=document_id, status="invalid", detail="上传记录缺失或损坏")
    try:
        metadata = UploadResponse.model_validate(json.loads(_read_bounded(metadata_path, 64 * 1024)))
        if metadata.document_id != document_id:
            raise ValueError("文档编号不一致")
        info.document_name = metadata.document_name
        info.size_bytes = metadata.size_bytes
        info.sha256 = metadata.sha256
        source = _read_bounded(pdf_path, MAX_UPLOAD_BYTES)
        if len(source) != metadata.size_bytes or hashlib.sha256(source).hexdigest() != metadata.sha256:
            info.detail = "原文件大小或指纹与上传记录不一致"
            return info.model_dump()
    except (OSError, ValueError, UnicodeError, TypeError):
        return info.model_dump()

    if directory.is_symlink() or directory.resolve().parent != parsed_directory.resolve():
        info.detail = "解析数据路径不合法"
        return info.model_dump()
    if not directory.exists():
        info.status = "uploaded"
        info.detail = "原文件已保存，尚未解析"
        return info.model_dump()
    if not directory.is_dir():
        info.detail = "解析目录异常"
        return info.model_dump()

    artifacts = [directory / name for name in ("parse_report.json", "pages.jsonl", "chunks.jsonl")]
    if any(path.is_symlink() for path in artifacts):
        info.detail = "解析数据不能指向符号链接"
        return info.model_dump()
    if not all(path.is_file() for path in artifacts):
        info.status = "incomplete"
        info.detail = "解析目录存在，但报告、页面或分块文件不完整"
        return info.model_dump()
    try:
        report = ParseResponse.model_validate(json.loads(_read_bounded(artifacts[0], 64 * 1024)))
        if (
            report.document_id != document_id
            or report.document_name != metadata.document_name
            or report.source_sha256 != metadata.sha256
            or report.nonempty_page_count > report.page_count
            or report.empty_page_numbers != sorted(set(report.empty_page_numbers))
            or any(number < 1 or number > report.page_count for number in report.empty_page_numbers)
            or len(report.empty_page_numbers) != report.page_count - report.nonempty_page_count
        ):
            raise ValueError("报告与上传记录不一致")
    except (OSError, ValueError, UnicodeError, TypeError):
        info.detail = "解析报告损坏或与上传记录不一致"
        return info.model_dump()

    info.status = "parsed"
    info.detail = "解析报告和数据文件已保存；搜索时仍会校验全部分块来源"
    info.page_count = report.page_count
    info.nonempty_page_count = report.nonempty_page_count
    info.chunk_count = report.chunk_count
    info.chunk_size = report.chunk_size
    info.empty_page_numbers = report.empty_page_numbers
    info.warnings = report.warnings
    return info.model_dump()


def list_documents(upload_directory: Path, parsed_directory: Path, offset=0, limit=20):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise DocumentCatalogError(422, "offset 必须非负，limit 必须是 1 到 100 的整数")
    identifiers = set()
    if upload_directory.exists():
        for path in upload_directory.iterdir():
            if path.suffix in (".pdf", ".json") and DOCUMENT_ID_PATTERN.fullmatch(path.stem):
                identifiers.add(path.stem)
    if parsed_directory.exists():
        for path in parsed_directory.iterdir():
            if DOCUMENT_ID_PATTERN.fullmatch(path.name):
                identifiers.add(path.name)
    ordered_ids = sorted(identifiers)
    documents = []
    for document_id in ordered_ids[offset:offset + limit]:
        try:
            documents.append(get_document_info(document_id, upload_directory, parsed_directory))
        except DocumentCatalogError as error:
            if error.status_code != 404:
                raise
            # A file may disappear between directory enumeration and inspection.
            documents.append(DocumentInfo(
                document_id=document_id, status="invalid", detail="文档在本次查询过程中已不存在",
            ).model_dump())
    return {
        "total": len(ordered_ids), "offset": offset, "limit": limit,
        "returned_count": len(documents), "documents": documents,
    }
