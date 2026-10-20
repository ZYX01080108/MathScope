"""Load the optional lecture experiment without blocking document services."""

import json
import logging
from pathlib import Path

from src.api_models import EvidenceChunk
from src.bm25_retrieval import BM25Retriever
from src.tokenization import tokenize, tokenize_normalized


MAX_BASELINE_BYTES = 64 * 1024 * 1024
MAX_BASELINE_CHUNKS = 100_000
logger = logging.getLogger(__name__)


def _unavailable(reason, error=None):
    detail = f"{reason}；可上传并解析文档后，使用 /documents/{{document_id}}/search"
    if error is None:
        logger.info("%s", detail)
    else:
        logger.warning("%s (%s)", detail, type(error).__name__)
    return {}, detail


def load_optional_baseline(chunks_path: Path):
    try:
        with chunks_path.open("rb") as source:
            data = source.read(MAX_BASELINE_BYTES + 1)
        if len(data) > MAX_BASELINE_BYTES:
            raise ValueError("默认基线文件超过加载上限")
        chunks = []
        for line in data.decode("utf-8").splitlines():
            if not line.strip():
                continue
            if len(chunks) >= MAX_BASELINE_CHUNKS:
                raise ValueError("默认基线块数超过加载上限")
            record = json.loads(line)
            evidence = EvidenceChunk.model_validate(record)
            if (
                evidence.end_char < evidence.start_char
                or evidence.end_char - evidence.start_char != len(evidence.text)
            ):
                raise ValueError("默认基线字符区间与文本长度不一致")
            chunks.append(record)

        if not chunks:
            return _unavailable("默认 lecture 基线分块为空，/search 尚未就绪")
        retrievers = {
            "original": BM25Retriever(chunks, tokenizer=tokenize),
            "normalized": BM25Retriever(chunks, tokenizer=tokenize_normalized),
        }
        if any(retriever.bm25 is None for retriever in retrievers.values()):
            return _unavailable("默认 lecture 基线没有可检索的词，/search 尚未就绪")
        return retrievers, None
    except FileNotFoundError:
        return _unavailable("默认 lecture 基线分块尚未准备，/search 尚未就绪")
    except (OSError, ValueError, UnicodeError, TypeError, KeyError) as error:
        return _unavailable("默认 lecture 基线读取、格式或来源字段检查失败，/search 尚未就绪", error)
