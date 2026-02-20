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
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


def _index_cache_root() -> str:
    """Root directory for index cache (e.g. SSH project indexes stored locally)."""
    return os.path.join(os.path.expanduser("~"), ".bedrock-codex")


def _project_key_for_ssh(working_directory: str) -> str:
    """Stable cache key for an SSH project (normalized path hash)."""
    from sessions import _normalize_wd
    normalized = _normalize_wd(working_directory)
    return "ssh-" + hashlib.sha256(normalized.encode()).hexdigest()[:16]


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


def _chunk_java(content: str) -> List[Tuple[int, int, str, str]]:
    """Regex-based Java chunking: classes, interfaces, enums, records, methods."""
    chunks = []
    lines = content.splitlines()

    # Match top-level and nested type declarations
    type_pattern = re.compile(
        r"^\s*(?:(?:public|protected|private|abstract|static|final|sealed|non-sealed)\s+)*"
        r"(?:class|interface|enum|record|@interface)\s+(\w+)",
        re.MULTILINE,
    )
    # Match method signatures
    method_pattern = re.compile(
        r"^\s*(?:(?:public|protected|private|abstract|static|final|synchronized|native|default)\s+)*"
        r"(?:<[^>]+>\s+)?(?:\w[\w.<>,\[\]\s]*?)\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+[\w,.\s]+)?\s*\{",
        re.MULTILINE,
    )

    def _find_block_end(start_idx: int) -> int:
        """Find closing brace for a block starting at start_idx (0-based line index)."""
        depth = 0
        for i in range(start_idx, min(start_idx + 500, len(lines))):
            line = lines[i]
            # Rough brace counting (ignores strings/comments, good enough for chunking)
            depth += line.count("{") - line.count("}")
            if depth <= 0 and i > start_idx:
                return i + 1
        return min(start_idx + 100, len(lines))

    # Collect type-level chunks
    for m in type_pattern.finditer(content):
        start = content[:m.start()].count("\n")
        name = m.group(1)
        kind_match = re.search(r"(class|interface|enum|record|@interface)", m.group(0))
        kind = kind_match.group(1) if kind_match else "class"
        end = _find_block_end(start)
        chunks.append((start + 1, end, kind, name))

    # Collect method-level chunks
    for m in method_pattern.finditer(content):
        start = content[:m.start()].count("\n")
        name = m.group(1)
        end = _find_block_end(start)
        # Only add if not entirely contained in a type chunk
        chunks.append((start + 1, end, "method", name))

    # Deduplicate and merge overlapping
    if chunks:
        chunks.sort(key=lambda c: (c[0], -c[1]))
        deduped = [chunks[0]]
        for c in chunks[1:]:
            prev = deduped[-1]
            # Skip if fully contained in previous
            if c[0] >= prev[0] and c[1] <= prev[1] and c[2] == prev[2]:
                continue
            deduped.append(c)
        chunks = deduped

    if not chunks:
        for i in range(0, len(lines), 40):
            chunks.append((i + 1, min(i + 40, len(lines)), "block", ""))

    return chunks


# ============================================================
# Import tracking
# ============================================================

def extract_imports(path: str, content: str) -> List[str]:
    """Extract import statements from a file. Returns a list of imported module/class names.

    Supports Python (import X, from X import Y) and Java (import com.example.X).
    """
    ext = Path(path).suffix.lower()
    imports: List[str] = []

    if ext == ".py":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name)
                elif isinstance(node, ast.ImportFrom):
                    module = node.module or ""
                    for alias in node.names:
                        imports.append(f"{module}.{alias.name}" if module else alias.name)
        except SyntaxError:
            # Fallback: regex
            for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+)?import\s+([\w., ]+)", content, re.MULTILINE):
                from_mod = m.group(1) or ""
                names = [n.strip().split(" as ")[0] for n in m.group(2).split(",")]
                for n in names:
                    imports.append(f"{from_mod}.{n}" if from_mod else n)

    elif ext == ".java":
        for m in re.finditer(r"^\s*import\s+(?:static\s+)?([\w.]+)\s*;", content, re.MULTILINE):
            imports.append(m.group(1))

    elif ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        # ES6: import X from 'Y' / import { X } from 'Y' / require('Y')
        for m in re.finditer(r"(?:import\s+.*?from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))", content):
            imports.append(m.group(1) or m.group(2))

    return imports


def build_import_graph(file_imports: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """Build a reverse import graph: for each module/file, list files that import it.

    file_imports: {file_path: [imported_module_names]}
    Returns: {module_or_stem: [file_paths_that_import_it]}
    """
    reverse: Dict[str, List[str]] = {}
    for fpath, imps in file_imports.items():
        for imp in imps:
            # Normalize: take last component as a short name
            short = imp.rsplit(".", 1)[-1] if "." in imp else imp
            reverse.setdefault(imp, []).append(fpath)
            if short != imp:
                reverse.setdefault(short, []).append(fpath)
    return reverse


def get_dependency_neighborhood(
    file_path: str,
    file_imports: Dict[str, List[str]],
    reverse_imports: Dict[str, List[str]],
    max_neighbors: int = 8,
) -> List[str]:
    """Get 1-hop dependency neighborhood: files that `file_path` imports + files that import `file_path`.

    Returns up to max_neighbors file paths (excluding self).
    """
    neighbors: set = set()

    # Forward: files this file imports
    direct_imports = file_imports.get(file_path, [])
    stem = Path(file_path).stem

    for imp in direct_imports:
        # Try to find a matching file by import name
        short = imp.rsplit(".", 1)[-1] if "." in imp else imp
        for candidate_path in file_imports:
            cand_stem = Path(candidate_path).stem
            if cand_stem == short or cand_stem == imp:
                neighbors.add(candidate_path)

    # Reverse: files that import this file
    for key in [stem, file_path]:
        for importer in reverse_imports.get(key, []):
            if importer != file_path:
                neighbors.add(importer)

    neighbors.discard(file_path)
    return sorted(neighbors)[:max_neighbors]


def chunk_file(path: str, content: str) -> List[CodeChunk]:
    """Split file into semantic chunks. Path is relative to workspace."""
    ext = Path(path).suffix.lower()
    if ext == ".py":
        ranges = _chunk_python(content)
    elif ext in (".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs"):
        ranges = _chunk_js_ts(content)
    elif ext == ".java":
        ranges = _chunk_java(content)
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


def _should_index(path: str, gitignore_spec=None) -> bool:
    rel = path.replace("\\", "/")
    parts = rel.split("/")
    if any(p in INDEX_SKIP_DIRS for p in parts):
        return False
    if any(rel.endswith(s) for s in INDEX_SKIP_SUFFIXES):
        return False
    if gitignore_spec and gitignore_spec.match_file(rel):
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
        self.file_mtimes: Dict[str, float] = {}  # track file modification times
        self._embeddings_array: Optional[Any] = None
        self._dirty_paths: Set[str] = set()
        # Import tracking
        self.file_imports: Dict[str, List[str]] = {}
        self.reverse_imports: Dict[str, List[str]] = {}

    def notify_file_changed(self, rel_path: str) -> None:
        """Mark a file as dirty so the next retrieval refreshes it."""
        self._dirty_paths.add(rel_path)

    def _load_metadata(self) -> None:
        meta_path = os.path.join(self.index_dir, "meta.json")
        if os.path.isfile(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.file_hashes = data.get("file_hashes", {})
                self.file_mtimes = data.get("file_mtimes", {})
                self.file_imports = data.get("file_imports", {})
                if self.file_imports:
                    self.reverse_imports = build_import_graph(self.file_imports)
            except Exception as e:
                logger.debug("Index meta load failed: %s", e)

    def _save_metadata(self) -> None:
        os.makedirs(self.index_dir, exist_ok=True)
        meta_path = os.path.join(self.index_dir, "meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "file_hashes": self.file_hashes,
                "file_mtimes": self.file_mtimes,
                "file_imports": self.file_imports,
            }, f, indent=0)

    def _get_stale_files(self, backend: Any) -> List[str]:
        """Get list of files that have been modified since last indexing."""
        stale_files = []
        try:
            files = self._list_indexable_files(backend)
            for rel_path in files:
                try:
                    # Get current file mtime via backend
                    if hasattr(backend, 'stat'):
                        stat = backend.stat(rel_path)
                        current_mtime = stat.mtime if hasattr(stat, 'mtime') else stat.st_mtime
                    else:
                        # Fallback for backends without stat method
                        import os.path
                        abs_path = os.path.join(self.working_directory, rel_path)
                        current_mtime = os.path.getmtime(abs_path)
                    
                    stored_mtime = self.file_mtimes.get(rel_path, 0)
                    if current_mtime > stored_mtime:
                        stale_files.append(rel_path)
                except Exception:
                    # If we can't get mtime, treat as stale to be safe
                    stale_files.append(rel_path)
        except Exception as e:
            logger.debug("Error detecting stale files: %s", e)
        
        return stale_files

    def _list_indexable_files_remote(self, backend: Any) -> List[str]:
        """List relative paths of indexable files via backend (SSH). BFS over list_dir."""
        out: List[str] = []
        try:
            queue: List[str] = ["."]
            while queue:
                rel_dir = queue.pop(0)
                entries = backend.list_dir(rel_dir)
                for e in entries:
                    name = e.get("name", "")
                    if not name or name.startswith("."):
                        continue
                    typ = e.get("type", "file")
                    if typ == "directory":
                        if name in INDEX_SKIP_DIRS:
                            continue
                        sub = (rel_dir + "/" + name) if rel_dir != "." else name
                        queue.append(sub)
                        continue
                    if typ != "file":
                        continue
                    rel = (rel_dir + "/" + name) if rel_dir != "." else name
                    rel = rel.replace("\\", "/")
                    if rel.startswith(".bedrock-codex/") or "/.bedrock-codex/" in rel:
                        continue
                    if not _should_index(rel):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if f".{ext}" in INDEX_SKIP_EXTENSIONS or ext in ("", ".md", ".txt"):
                        continue
                    out.append(rel)
        except Exception as e:
            logger.warning("List indexable files (remote) failed: %s", e)
        return out

    def _list_indexable_files(self, backend: Any) -> List[str]:
        """List relative paths of indexable files under working_directory (local or via backend)."""
        if backend is not None and getattr(backend, "_host", None) is not None:
            return self._list_indexable_files_remote(backend)

        # Load .gitignore for filtering
        gi = None
        try:
            from tools import _load_gitignore
            gi = _load_gitignore(self.working_directory)
        except Exception:
            pass

        out = []
        try:
            for root, dirs, files in os.walk(self.working_directory):
                dirs[:] = [d for d in dirs if d not in INDEX_SKIP_DIRS and not d.startswith(".")]
                rel_root = os.path.relpath(root, self.working_directory)
                if rel_root.startswith(".bedrock-codex") or ".." in rel_root:
                    dirs.clear()
                    continue
                # Filter dirs by gitignore
                if gi:
                    dirs[:] = [d for d in dirs if not gi.match_file(
                        (os.path.join(rel_root, d) if rel_root != "." else d).replace("\\", "/") + "/"
                    )]
                for f in files:
                    if f.startswith("."):
                        continue
                    rel = os.path.join(rel_root, f).replace("\\", "/")
                    if not _should_index(rel, gi):
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
        if backend is None:
            logger.debug("Codebase index build skipped (no backend)")
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
            # Track file modification time
            try:
                if hasattr(backend, 'stat'):
                    stat = backend.stat(rel)
                    mtime = stat.mtime if hasattr(stat, 'mtime') else stat.st_mtime
                else:
                    import os.path
                    abs_path = os.path.join(self.working_directory, rel)
                    mtime = os.path.getmtime(abs_path)
                self.file_mtimes[rel] = mtime
            except Exception:
                # If we can't get mtime, store current time as fallback
                import time
                self.file_mtimes[rel] = time.time()
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
            # Extract imports
            try:
                imps = extract_imports(rel, content)
                if imps:
                    self.file_imports[rel] = imps
            except Exception:
                pass
        # Also extract imports from files that didn't change (already indexed)
        for rel in files:
            if rel not in reindex_paths and rel not in self.file_imports:
                try:
                    content = backend.read_file(rel)
                    imps = extract_imports(rel, content)
                    if imps:
                        self.file_imports[rel] = imps
                except Exception:
                    pass
        # Build reverse import graph
        self.reverse_imports = build_import_graph(self.file_imports)
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

    def retrieve_with_refresh(self, query: str, top_k: int = 10, backend: Optional[Any] = None) -> List[CodeChunk]:
        """Semantic search with staleness check: refresh dirty/stale files before retrieval."""
        paths_to_refresh: set = set(self._dirty_paths)

        # Also check mtime-based staleness when backend is available
        if backend is not None:
            try:
                stale_files = self._get_stale_files(backend)
                paths_to_refresh.update(stale_files)
            except Exception as e:
                logger.debug("Error detecting stale files: %s", e)

        # Cap to avoid long delays
        refresh_list = list(paths_to_refresh)[:50]

        if refresh_list:
            logger.debug("Refreshing %d files in index", len(refresh_list))
            refresh_set = set(refresh_list)
            # Remove old chunks for these files
            self.chunks = [c for c in self.chunks if c.path not in refresh_set]
            self._embeddings_array = None

            new_chunks: List[CodeChunk] = []
            for rel_path in refresh_list:
                try:
                    if backend is not None:
                        content = backend.read_file(rel_path)
                    else:
                        abs_path = os.path.join(self.working_directory, rel_path)
                        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read()
                    h = _file_content_hash(content)
                    self.file_hashes[rel_path] = h
                    self.file_mtimes[rel_path] = time.time()
                    for chunk in chunk_file(rel_path, content):
                        new_chunks.append(chunk)
                except Exception as e:
                    logger.debug("Failed to refresh file %s: %s", rel_path, e)

            # Embed new chunks so they appear in search results
            if new_chunks and self.embed_fn:
                try:
                    texts = [c.text for c in new_chunks]
                    embeddings = self.embed_fn(texts, input_type="search_document")
                    for i, c in enumerate(new_chunks):
                        if i < len(embeddings):
                            c.embedding = embeddings[i]
                except Exception as e:
                    logger.warning("Embedding refresh failed: %s", e)

            self.chunks.extend(new_chunks)
            self._dirty_paths.clear()
            self._save_metadata()

        return self.retrieve(query, top_k)


_global_embed_fn: Optional[Any] = None


def set_embed_fn(fn: Optional[Any]) -> None:
    """Set the global embed function (e.g. BedrockService.embed_texts). Called by agent at task start."""
    global _global_embed_fn
    _global_embed_fn = fn


def get_embed_fn() -> Optional[Any]:
    return _global_embed_fn


_index_cache: Dict[str, "CodebaseIndex"] = {}


def get_index(
    working_directory: str,
    embed_fn: Optional[Any] = None,
    backend: Optional[Any] = None,
) -> CodebaseIndex:
    """Get or create the codebase index for this workspace.
    Cached by normalized working directory so all callers share one instance.
    """
    norm_wd = os.path.normpath(os.path.abspath(working_directory))
    cached = _index_cache.get(norm_wd)
    if cached is not None:
        # Update embed_fn if a newer one is provided
        if embed_fn is not None:
            cached.embed_fn = embed_fn
        return cached

    index_dir: Optional[str] = None
    if backend is not None and getattr(backend, "_host", None) is not None:
        cache_root = os.path.join(_index_cache_root(), "indexes")
        project_key = _project_key_for_ssh(working_directory)
        index_dir = os.path.join(cache_root, project_key)
    index = CodebaseIndex(
        working_directory=working_directory,
        index_dir=index_dir,
        embed_fn=embed_fn if embed_fn is not None else _global_embed_fn,
    )
    index.load_from_disk()
    _index_cache[norm_wd] = index
    return index


def notify_file_changed_global(rel_path: str, working_directory: str = ".") -> None:
    """Notify all known index instances that a file has changed.
    Called by file mutation tools (write_file, edit_file, symbol_edit).
    """
    norm_wd = os.path.normpath(os.path.abspath(working_directory))
    cached = _index_cache.get(norm_wd)
    if cached is not None:
        cached.notify_file_changed(rel_path)
    # Also notify the background index if it exists
    try:
        import web.state as _ws
        bg_idx = getattr(_ws, "_bg_codebase_index", None)
        if bg_idx is not None and bg_idx is not cached:
            bg_idx.notify_file_changed(rel_path)
    except Exception:
        pass
