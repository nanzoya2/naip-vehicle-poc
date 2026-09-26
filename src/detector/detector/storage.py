"""成果物の保存先。パスはバケット内パス（§11.2）で扱う。

Phase 1 はローカルディレクトリをバケットに見立てる（data/ = gs://naip-vehicle-analysis-{project_id}/）。
Phase 3 で GCS 実装を追加する。
"""
from pathlib import Path


class LocalStorage:
    def __init__(self, root: Path):
        self.root = Path(root)

    def uri(self, path: str) -> str:
        return str(self.root / path)

    def glob(self, pattern: str) -> list[str]:
        return sorted(str(p.relative_to(self.root).as_posix()) for p in self.root.glob(pattern))

    def read_text(self, path: str) -> str:
        return (self.root / path).read_text(encoding="utf-8")

    def write_bytes(self, path: str, data: bytes):
        dst = self.root / path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)

    def write_text(self, path: str, text: str):
        self.write_bytes(path, text.encode("utf-8"))
