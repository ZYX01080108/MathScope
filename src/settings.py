"""Explicit per-application configuration; reading settings never creates files."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"


def _resolve_path(value, name):
    if isinstance(value, str):
        value = value.strip()
        if not value:
            raise ValueError(f"{name} 不能为空")
    elif not isinstance(value, Path):
        raise ValueError(f"{name} 必须是路径字符串或 Path")
    path = Path(value)
    if not path.is_absolute():
        if path.anchor:
            raise ValueError(f"{name} 不能使用驱动器相对或根相对路径，请填写完整路径")
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True)
class Settings:
    data_dir: Path = DEFAULT_DATA_DIR
    baseline_chunks_path: Path | None = None
    index_cache_size: int = 4

    def __post_init__(self):
        data_dir = _resolve_path(self.data_dir, "MATHSCOPE_DATA_DIR")
        if data_dir == Path(data_dir.anchor):
            raise ValueError("数据目录不能是磁盘或文件系统根目录，请使用专用目录")
        if data_dir.exists() and not data_dir.is_dir():
            raise ValueError("MATHSCOPE_DATA_DIR 必须指向目录而不是文件")
        baseline = self.baseline_chunks_path
        if baseline is None:
            baseline = data_dir / "parsed" / "lecture.chunks.jsonl"
        baseline = _resolve_path(baseline, "MATHSCOPE_BASELINE_CHUNKS")
        if baseline.exists() and not baseline.is_file():
            raise ValueError("MATHSCOPE_BASELINE_CHUNKS 必须指向文件而不是目录")
        if type(self.index_cache_size) is not int or not 1 <= self.index_cache_size <= 64:
            raise ValueError("MATHSCOPE_INDEX_CACHE_SIZE 必须是 1 到 64 的整数")
        object.__setattr__(self, "data_dir", data_dir)
        object.__setattr__(self, "baseline_chunks_path", baseline)

    @property
    def upload_directory(self):
        return self.data_dir / "raw" / "uploads"

    @property
    def parsed_directory(self):
        return self.data_dir / "parsed" / "uploads"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        values = os.environ if environ is None else environ
        cache_size = values.get("MATHSCOPE_INDEX_CACHE_SIZE", "4")
        if not isinstance(cache_size, str) or not cache_size.strip():
            raise ValueError("MATHSCOPE_INDEX_CACHE_SIZE 必须是非空的整数字符串")
        try:
            cache_size = int(cache_size)
        except ValueError as error:
            raise ValueError("MATHSCOPE_INDEX_CACHE_SIZE 必须是整数字符串") from error
        return cls(
            data_dir=values.get("MATHSCOPE_DATA_DIR", DEFAULT_DATA_DIR),
            baseline_chunks_path=values.get("MATHSCOPE_BASELINE_CHUNKS"),
            index_cache_size=cache_size,
        )
