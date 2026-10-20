"""Save uploaded bytes without parsing PDFs or changing search indexes."""

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import uuid4


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
READ_BLOCK_BYTES = 64 * 1024


class UploadValidationError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


def save_pdf_upload(
    stream: BinaryIO,
    filename: str | None,
    upload_directory: Path,
    max_bytes: int = MAX_UPLOAD_BYTES,
):
    if type(max_bytes) is not int or max_bytes < 5:
        raise ValueError("上传大小上限必须是至少 5 字节的整数")

    # The client name is display metadata only, never part of the storage path.
    document_name = PurePosixPath((filename or "").replace("\\", "/")).name
    if (
        not document_name
        or not document_name.casefold().endswith(".pdf")
        or any(ord(character) < 32 or ord(character) == 127 for character in document_name)
    ):
        raise UploadValidationError(415, "请选择文件名以 .pdf 结尾的 PDF 文件")

    stream.seek(0)
    block = stream.read(min(READ_BLOCK_BYTES, max_bytes + 1))
    if not block:
        raise UploadValidationError(400, "不能上传空文件")
    if len(block) > max_bytes:
        raise UploadValidationError(413, "文件超过上传大小上限（默认 10 MiB）")
    if not block.startswith(b"%PDF-"):
        raise UploadValidationError(415, "文件头不是 PDF 格式；改扩展名不能转成 PDF")

    document_id = uuid4().hex
    upload_directory.mkdir(parents=True, exist_ok=True)
    pdf_path = upload_directory / f"{document_id}.pdf"
    metadata_path = upload_directory / f"{document_id}.json"
    pdf_created = False
    metadata_created = False

    try:
        size_bytes = 0
        digest = hashlib.sha256()
        with pdf_path.open("xb") as output:
            pdf_created = True
            while block:
                size_bytes += len(block)
                if size_bytes > max_bytes:
                    raise UploadValidationError(413, "文件超过上传大小上限（默认 10 MiB）")
                output.write(block)
                digest.update(block)
                # At most one byte beyond the allowed size is needed to reject.
                block = stream.read(min(READ_BLOCK_BYTES, max_bytes - size_bytes + 1))

        metadata = {
            "document_id": document_id,
            "document_name": document_name,
            "size_bytes": size_bytes,
            "sha256": digest.hexdigest(),
            "status": "uploaded",
        }
        with metadata_path.open("x", encoding="utf-8", newline="\n") as output:
            metadata_created = True
            json.dump(metadata, output, ensure_ascii=False)
            output.write("\n")
        return metadata
    except BaseException:
        # Remove only files this attempt created; never remove a colliding file.
        for created, path in ((metadata_created, metadata_path), (pdf_created, pdf_path)):
            if created:
                path.unlink(missing_ok=True)
        raise
