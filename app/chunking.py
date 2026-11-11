"""Document ingestion + chunking (markdown files, split by heading then size)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Chunk:
    source: str      # file name
    chunk_id: str    # "<file>#<n>"
    heading: str
    text: str


def chunk_markdown(source: str, text: str, max_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading, buf = "", []

    def flush() -> None:
        if buf:
            chunks.append(Chunk(source, f"{source}#{len(chunks)}", heading, " ".join(buf)))
            buf.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()
        elif line:
            if buf and sum(len(x) + 1 for x in buf) + len(line) > max_chars:
                flush()
            buf.append(line)
    flush()
    return chunks


def load_chunks(docs_dir: Path, max_chars: int) -> list[Chunk]:
    chunks: list[Chunk] = []
    for path in sorted(Path(docs_dir).glob("*.md")):
        chunks.extend(chunk_markdown(path.name, path.read_text(encoding="utf-8"), max_chars))
    if not chunks:
        raise RuntimeError(f"no .md documents found in {docs_dir}")
    return chunks
