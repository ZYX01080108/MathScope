"""Return the validated byte snapshot, never reopen a path after validation."""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from src.api_models import UploadResponse
from src.document_upload import MAX_UPLOAD_BYTES


MAX_METADATA_BYTES = 64 * 1024


class DocumentFileError(ValueError):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


@dataclass(frozen=True)
class PdfSnapshot:
    content: bytes
    sha256: str


def _read_snapshot(path, root, limit, oversize_status):
    if path.is_symlink() or path.resolve().parent != root:
        raise DocumentFileError(409, "原文件或上传记录路径不合法")
    if not path.is_file():
        raise DocumentFileError(404, "原文件不存在或上传记录不完整")
    try:
        with path.open("rb") as stream:
            content = stream.read(limit + 1)
    except FileNotFoundError as error:
        raise DocumentFileError(404, "原文件或上传记录在读取时已不存在") from error
    if len(content) > limit:
        raise DocumentFileError(oversize_status, "原文件或上传记录超过读取上限")
    return content


def get_document_file(document_id, upload_directory: Path, expected_sha256=None):
    if not isinstance(document_id, str) or not re.fullmatch(r"[0-9a-f]{32}", document_id):
        raise DocumentFileError(422, "文档编号必须是 32 位小写十六进制字符串")
    if expected_sha256 is not None and (
        not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
    ):
        raise DocumentFileError(422, "文件指纹必须是 64 位小写十六进制字符串")
    root = upload_directory.resolve()
    metadata_bytes = _read_snapshot(upload_directory / f"{document_id}.json", root, MAX_METADATA_BYTES, 409)
    try:
        metadata = UploadResponse.model_validate(json.loads(metadata_bytes))
    except (ValueError, UnicodeError, TypeError) as error:
        raise DocumentFileError(409, "上传记录损坏，无法确认原文件来源") from error
    if metadata.document_id != document_id:
        raise DocumentFileError(409, "上传记录与文档编号不一致")
    content = _read_snapshot(upload_directory / f"{document_id}.pdf", root, MAX_UPLOAD_BYTES, 413)
    actual_sha256 = hashlib.sha256(content).hexdigest()
    if (
        not content.startswith(b"%PDF-")
        or len(content) != metadata.size_bytes
        or actual_sha256 != metadata.sha256
    ):
        raise DocumentFileError(409, "原文件格式、大小或指纹与上传记录不一致")
    if expected_sha256 is not None and expected_sha256 != actual_sha256:
        raise DocumentFileError(409, "原文件与证据所指版本不同，请重新检索并核对来源")
    return PdfSnapshot(content=content, sha256=actual_sha256)
