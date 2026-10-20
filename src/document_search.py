"""Document-scoped BM25 search with checked artifacts and a bounded cache."""

import hashlib
import json
import re
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from pydantic import ValidationError

from src.api_models import DocumentEvidenceChunk, ParseResponse, UploadResponse
from src.bm25_retrieval import BM25Retriever
from src.document_upload import MAX_UPLOAD_BYTES
from src.tokenization import tokenize, tokenize_normalized


MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_CHUNKS_TO_INDEX = 100_000


class DocumentSearchError(ValueError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code


def _read_bounded(path: Path, limit: int):
    if path.is_symlink():
        raise DocumentSearchError(409, "文档数据不能指向符号链接")
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise DocumentSearchError(413, "文档数据超过当前检索加载上限")
    return data


def _jsonl_records(data: bytes, max_records: int):
    records = []
    for line in data.decode("utf-8").splitlines():
        if line.strip():
            if len(records) >= max_records:
                raise DocumentSearchError(413, "解析记录数超过当前检索上限")
            records.append(json.loads(line))
    return records


def _validate_artifacts(document_id, metadata, report, pages_data, chunks_data):
    try:
        pages = _jsonl_records(pages_data, max_records=300)
        chunks = _jsonl_records(chunks_data, max_records=MAX_CHUNKS_TO_INDEX)
        if len(pages) != report.page_count or len(chunks) != report.chunk_count:
            raise ValueError("记录数量与解析报告不一致")
        for page_number, page in enumerate(pages, start=1):
            if (
                not isinstance(page, dict)
                or type(page.get("pdf_page_number")) is not int
                or page["pdf_page_number"] != page_number
                or page.get("document_id") != document_id
                or page.get("document_name") != metadata.document_name
                or not isinstance(page.get("text"), str)
            ):
                raise ValueError("页面来源或物理页号不一致")
        empty_pages = [page["pdf_page_number"] for page in pages if not page["text"].strip()]
        if (
            report.empty_page_numbers != empty_pages
            or report.nonempty_page_count != len(pages) - len(empty_pages)
        ):
            raise ValueError("空文本页记录不一致")

        expected_offsets = (
            (page["pdf_page_number"], start, min(start + report.chunk_size, len(page["text"])))
            for page in pages
            for start in range(0, len(page["text"]), report.chunk_size)
        )
        validated_chunks = []
        for chunk in chunks:
            record = DocumentEvidenceChunk.model_validate(chunk)
            position = (record.pdf_page_number, record.start_char, record.end_char)
            if (
                record.document_id != document_id
                or record.document_name != metadata.document_name
                or position != next(expected_offsets, None)
            ):
                raise ValueError("分块来源、顺序或字符位置不一致")
            page_text = pages[record.pdf_page_number - 1]["text"]
            if record.text != page_text[record.start_char:record.end_char]:
                raise ValueError("分块不等于页面原文切片")
            validated_chunks.append(record.model_dump())
        if next(expected_offsets, None) is not None:
            raise ValueError("分块缺少原文内容")
        return validated_chunks
    except DocumentSearchError:
        raise
    except (ValueError, UnicodeError, TypeError, KeyError, IndexError) as error:
        raise DocumentSearchError(409, "解析数据与报告或来源位置不一致，请检查后重新上传解析") from error


class DocumentSearchService:
    def __init__(self, upload_directory: Path, parsed_directory: Path, max_cached_indexes=4):
        if type(max_cached_indexes) is not int or max_cached_indexes < 1:
            raise ValueError("缓存数量必须是正整数")
        self.upload_directory = upload_directory
        self.parsed_directory = parsed_directory
        self.max_cached_indexes = max_cached_indexes
        self.tokenizers = {"original": tokenize, "normalized": tokenize_normalized}
        self._cache = OrderedDict()
        self._lock = RLock()

    def clear(self):
        with self._lock:
            self._cache.clear()

    def _snapshot(self, document_id):
        pdf_path = self.upload_directory / f"{document_id}.pdf"
        metadata_path = self.upload_directory / f"{document_id}.json"
        if not pdf_path.is_file() or not metadata_path.is_file():
            raise DocumentSearchError(404, "上传文档不存在或记录不完整")
        directory = self.parsed_directory / document_id
        if directory.resolve().parent != self.parsed_directory.resolve():
            raise DocumentSearchError(409, "解析数据路径不合法")
        report_path = directory / "parse_report.json"
        pages_path = directory / "pages.jsonl"
        chunks_path = directory / "chunks.jsonl"
        if not all(path.is_file() for path in (report_path, pages_path, chunks_path)):
            raise DocumentSearchError(409, "文档尚未完成解析，请先调用该文档的 parse 接口")

        pdf_data = _read_bounded(pdf_path, MAX_UPLOAD_BYTES)
        metadata_data = _read_bounded(metadata_path, 64 * 1024)
        report_data = _read_bounded(report_path, 64 * 1024)
        pages_data = _read_bounded(pages_path, MAX_ARTIFACT_BYTES)
        chunks_data = _read_bounded(chunks_path, MAX_ARTIFACT_BYTES)
        try:
            metadata = UploadResponse.model_validate(json.loads(metadata_data))
            report = ParseResponse.model_validate(json.loads(report_data))
        except (ValidationError, ValueError, UnicodeError, TypeError) as error:
            raise DocumentSearchError(409, "上传记录或解析报告损坏，请检查后重新上传解析") from error
        source_sha256 = hashlib.sha256(pdf_data).hexdigest()
        if (
            metadata.document_id != document_id
            or report.document_id != document_id
            or metadata.document_name != report.document_name
            or metadata.size_bytes != len(pdf_data)
            or source_sha256 != metadata.sha256
            or source_sha256 != report.source_sha256
        ):
            raise DocumentSearchError(409, "文件、上传记录与解析报告不属于同一版本")
        fingerprint = tuple(
            hashlib.sha256(data).hexdigest()
            for data in (pdf_data, metadata_data, report_data, pages_data, chunks_data)
        )
        return fingerprint, metadata, report, pages_data, chunks_data

    def search(self, document_id, query, method="original", k=5):
        if not isinstance(document_id, str) or not re.fullmatch(r"[0-9a-f]{32}", document_id):
            raise DocumentSearchError(422, "文档编号必须是 32 位小写十六进制字符串")
        if method not in self.tokenizers:
            raise DocumentSearchError(422, "不支持的检索方案")
        if type(k) is not int or not 1 <= k <= 20:
            raise DocumentSearchError(422, "k 必须是 1 到 20 的整数")
        if not self.tokenizers[method](query):
            raise DocumentSearchError(422, "查询必须包含可检索的词")

        key = (document_id, method)
        with self._lock:
            fingerprint, metadata, report, pages_data, chunks_data = self._snapshot(document_id)
            cached = self._cache.get(key)
            cache_hit = cached is not None and cached[0] == fingerprint
            if cache_hit:
                retriever = cached[1]
                self._cache.move_to_end(key)
            else:
                self._cache.pop(key, None)
                chunks = _validate_artifacts(document_id, metadata, report, pages_data, chunks_data)
                retriever = BM25Retriever(chunks, tokenizer=self.tokenizers[method])
                self._cache[key] = (fingerprint, retriever)
                while len(self._cache) > self.max_cached_indexes:
                    self._cache.popitem(last=False)

        results = retriever.search(query, k=k)
        return {
            "document_id": document_id,
            "document_name": metadata.document_name,
            "source_sha256": report.source_sha256,
            "query": query,
            "method": method,
            "k": k,
            "returned_count": len(results),
            "indexed_chunk_count": report.chunk_count,
            "cache_hit": cache_hit,
            "results": results,
        }
