"""
Enterprise-grade semantic codebase index for Bedrock Codex.

Cursor-style: chunk code by semantic units (functions, classes), embed via Bedrock
Cohere Embed, store in a vector index. Incremental updates by file content hash.
Retrieval returns only relevant chunks so the agent never loads whole large files.
"""

import ast
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Chunk text max length for embedding (Cohere ~512 tokens, ~1500 chars safe)
CHUNK_TEXT_MAX = 1500
# Files/dirs to skip (same spirit as .cursorignore)
INDEX_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".bedrock-codex"}
INDEX_SKIP_SUFFIXES = {".min.js", ".min.css", ".lock", ".pyc", ".map", ".sum", ".mod"}
INDEX_SKIP_EXTENSIONS = {".pyc", ".pyo", ".so", ".dylib", ".o", ".a", ".bin"}


@dataclass
class CodeChunk:
    """A single semantic chunk of code with location and optional embedding."""
    path: str
    start_line: int
    end_line: int
    kind: str  # "function", "class", "module", "block"
    name: str
    text: str
    embedding: Optional[List[float]] = None

    def to_search_snippet(self, max_lines: int = 25) -> str:
        lines = self.text.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
        return f"{self.path}:{self.start_line}-{self.end_line} [{self.kind}] {self.name}\n" + "\n".join(lines)


def _file_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _chunk_python(content: str) -> List[Tuple[int, int, str, str]]:
    """Return (start_line_1idx, end_line_1idx, kind, name) for Python."""
    chunks = []
    try:
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = node.lineno
                end = node.end_lineno or node.lineno
                kind = "class" if isinstance(node, ast.ClassDef) else "function"
                chunks.append((start, end, kind, node.name))
    except SyntaxError:
        pass
    if not chunks:
        # Fallback: line-based windows
        lines = content.splitlines()
        for i in range(0, len(lines), 40):
            chunks.append((i + 1, min(i + 40, len(lines)), "block", ""))
    return chunks


def _chunk_js_ts(content: str) -> List[Tuple[int, int, str, str]]:
    """Heuristic chunks for JS/TS: function/class/method blocks."""
    chunks = []
    lines = content.splitlines()
    # Match function, class, export function, etc.
    pattern = re.compile(
        r"^\s*(export\s+)?(async\s+)?(function\s+(\w+)|(?:(\w+)\s*\([^)]*\)\s*=>)|class\s+(\w+))",
        re.MULTILINE,
    )
    for m in pattern.finditer(content):
        start = content[: m.start()].count("\n") + 1
        name = (m.group(4) or m.group(6) or m.group(5) or "anonymous").strip()
        kind = "class" if "class" in (m.group(0) or "") else "function"
        # Approximate end: next same-indent or +50 lines
        segment = content[m.start() :]
        end_line = start
        for i, line in enumerate(segment.splitlines()[:80], start=start):
            end_line = i
            if i > start and line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                break
        chunks.append((start, end_line, kind, name))
    if not chunks:
        for i in range(0, len(lines), 40):
            chunks.append((i + 1, min(i + 40, len(lines)), "block", ""))
    return chunks


def chunk_file(path: str, content: str) -> List[CodeChunk]:
    """Split file into semantic chunks. Path is relative to workspace."""
    ext = Path(path).suffix.lower()
    if ext == ".py":
        ranges = _chunk_python(content)
    elif ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        ranges = _chunk_js_ts(content)
    else:
        # Generic: 40-line windows
        lines = content.splitlines()
        ranges = []
        for i in range(0, len(lines), 40):
            ranges.append((i + 1, min(i + 40, len(lines)), "block", ""))
    lines = content.splitlines()
    out = []
    for start, end, kind, name in ranges:
        segment = "\n".join(lines[max(0, start - 1) : end])
        if not segment.strip():
            continue
        text = segment[:CHUNK_TEXT_MAX] + ("..." if len(segment) > CHUNK_TEXT_MAX else "")
        out.append(
            CodeChunk(
                path=path,
                start_line=start,
                end_line=end,
                kind=kind,
                name=name or f"lines_{start}_{end}",
                text=text,
            )
        )
    return out


def _should_index(path: str) -> bool:
    rel = path.replace("\\", "/")
    parts = rel.split("/")
    if any(p in INDEX_SKIP_DIRS for p in parts):
        return False
    if any(rel.endswith(s) for s in INDEX_SKIP_SUFFIXES):
        return False
    return True


class CodebaseIndex:
    """
    In-memory vector index over code chunks. Persists chunks and embeddings to disk
    for incremental updates (only re-embed changed files by content hash).
    """

    def __init__(
        self,
        working_directory: str,
        index_dir: Optional[str] = None,
        embed_fn: Optional[Any] = None,
    ):
        self.working_directory = os.path.normpath(working_directory)
        self.index_dir = index_dir or os.path.join(self.working_directory, ".bedrock-codex", "index")
        self.embed_fn = embed_fn
        self.chunks: List[CodeChunk] = []
        self.file_hashes: Dict[str, str] = {}
        self._embeddings_array: Optional[Any] = None

    def _load_metadata(self) -> None:
        meta_path = os.path.join(self.index_dir, "meta.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.file_hashes = data.get("file_hashes", {})
            except Exception as e:
                logger.debug("Index meta load failed: %s", e)

    def _save_metadata(self) -> None:
        os.makedirs(self.index_dir, exist_ok=True)
        meta_path = os.path.join(self.index_dir, "meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({"file_hashes": self.file_hashes}, f, indent=0)

    def _list_indexable_files(self, backend: Any) -> List[str]:
        """List relative paths of indexable files under working_directory."""
        out = []
        try:
            for root, dirs, files in os.walk(self.working_directory):
                dirs[:] = [d for d in dirs if d not in INDEX_SKIP_DIRS and not d.startswith(".")]
                rel_root = os.path.relpath(root, self.working_directory)
                if rel_root.startswith(".bedrock-codex") or ".." in rel_root:
                    dirs.clear()
                    continue
                for f in files:
                    if f.startswith("."):
                        continue
                    rel = os.path.join(rel_root, f).replace("\\", "/")
                    if not _should_index(rel):
                        continue
                    ext = os.path.splitext(f)[1].lower()
                    if f".{ext}" in INDEX_SKIP_EXTENSIONS or ext in ("", ".md", ".txt"):
                        continue
                    out.append(rel)
        except Exception as e:
            logger.warning("List indexable files failed: %s", e)
        return out

    def build(
        self,
        backend: Any,
        force_reindex: bool = False,
        on_progress: Optional[Any] = None,
    ) -> int:
        """
        Build or update the index. Only re-chunks and re-embeds files whose content hash changed.
        Returns number of chunks indexed.
        """
        import numpy as np
        # Indexing only for local backend (SSH would require remote walk/read)
        if backend is None or getattr(backend, "_host", None) is not None:
            logger.debug("Codebase index build skipped (remote or no backend)")
            return len(self.chunks)
        self._load_metadata()
        try:
            files = self._list_indexable_files(backend)
        except Exception:
            files = []
        if not files:
            logger.info("No indexable files found")
            return 0
        to_index: List[Tuple[str, str]] = []
        for rel in files:
            try:
                content = backend.read_file(rel)
            except Exception:
                continue
            h = _file_content_hash(content)
            if force_reindex or self.file_hashes.get(rel) != h:
                to_index.append((rel, content))
            self.file_hashes[rel] = h
        # Drop chunks for files we're re-indexing
        reindex_paths = {p for p, _ in to_index}
        self.chunks = [c for c in self.chunks if c.path not in reindex_paths]
        # Chunk and embed new/changed files
        all_new_chunks: List[CodeChunk] = []
        for i, (rel, content) in enumerate(to_index):
            if on_progress:
                on_progress(i + 1, len(to_index), rel)
            for c in chunk_file(rel, content):
                all_new_chunks.append(c)
        if not all_new_chunks or not self.embed_fn:
            self._save_metadata()
            return len(self.chunks)
        texts = [c.text for c in all_new_chunks]
        try:
            embeddings = self.embed_fn(texts, input_type="search_document")
        except Exception as e:
            logger.warning("Embedding failed: %s", e)
            embeddings = []
        for i, c in enumerate(all_new_chunks):
            if i < len(embeddings):
                c.embedding = embeddings[i]
        self.chunks.extend(all_new_chunks)
        self._embeddings_array = None
        self._save_metadata()
        # Persist chunks to disk for large repos
        chunks_path = os.path.join(self.index_dir, "chunks.json")
        os.makedirs(self.index_dir, exist_ok=True)
        serialized = []
        for c in self.chunks:
            serialized.append({
                "path": c.path,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "kind": c.kind,
                "name": c.name,
                "text": c.text,
                "embedding": c.embedding,
            })
        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump(serialized, f, indent=0)
        logger.info("Codebase index: %d chunks (%d files)", len(self.chunks), len(self.file_hashes))
        return len(self.chunks)

    def load_from_disk(self) -> bool:
        """Load chunks (and optionally embeddings) from disk."""
        chunks_path = os.path.join(self.index_dir, "chunks.json")
        if not os.path.isfile(chunks_path):
            return False
        try:
            with open(chunks_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.chunks = []
            for d in data:
                self.chunks.append(
                    CodeChunk(
                        path=d["path"],
                        start_line=d["start_line"],
                        end_line=d["end_line"],
                        kind=d["kind"],
                        name=d["name"],
                        text=d["text"],
                        embedding=d.get("embedding"),
                    )
                )
            self._load_metadata()
            self._embeddings_array = None
            logger.info("Loaded index: %d chunks", len(self.chunks))
            return True
        except Exception as e:
            logger.warning("Load index failed: %s", e)
            return False

    def retrieve(self, query: str, top_k: int = 10) -> List[CodeChunk]:
        """Semantic search: return top_k chunks most relevant to query."""
        import numpy as np
        if not self.chunks or not self.embed_fn:
            return []
        chunks_with_emb = [c for c in self.chunks if c.embedding]
        if not chunks_with_emb:
            return []
        try:
            query_emb = self.embed_fn([query], input_type="search_query")[0]
        except Exception as e:
            logger.warning("Query embed failed: %s", e)
            return []
        matrix = np.array([c.embedding for c in chunks_with_emb], dtype=np.float32)
        q = np.array(query_emb, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        q_norm = np.linalg.norm(q)
        if q_norm < 1e-9 or np.any(norms < 1e-9):
            return []
        sim = (matrix @ q) / (norms.ravel() * q_norm)
        top_indices = np.argsort(sim)[::-1][:top_k]
        return [chunks_with_emb[i] for i in top_indices]


_global_embed_fn: Optional[Any] = None


def set_embed_fn(fn: Optional[Any]) -> None:
    """Set the global embed function (e.g. BedrockService.embed_texts). Called by agent at task start."""
    global _global_embed_fn
    _global_embed_fn = fn


def get_embed_fn() -> Optional[Any]:
    return _global_embed_fn


def get_index(working_directory: str, embed_fn: Optional[Any] = None) -> CodebaseIndex:
    """Get or create the codebase index for this workspace."""
    index = CodebaseIndex(
        working_directory=working_directory,
        embed_fn=embed_fn if embed_fn is not None else _global_embed_fn,
    )
    index.load_from_disk()
    return index
