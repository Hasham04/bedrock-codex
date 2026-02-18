""".gitignore-aware filtering helpers."""

import os
import logging
from typing import Dict, Optional, Set

import pathspec

from backend import Backend

logger = logging.getLogger(__name__)

_ALWAYS_SKIP_DIRS: Set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".tox", ".eggs",
    "dist", "build", ".next", ".nuxt", ".cache",
    "coverage", ".coverage", "htmlcov", ".bedrock-codex",
}

_ALWAYS_SKIP_EXTENSIONS: Set[str] = {
    ".pyc", ".pyo", ".so", ".dylib", ".o", ".a", ".class",
    ".min.js", ".min.css", ".map", ".lock",
}

_gitignore_cache: Dict[str, Optional[pathspec.PathSpec]] = {}


def _load_gitignore(working_directory: str, backend: Optional[Backend] = None) -> Optional[pathspec.PathSpec]:
    """Load and cache .gitignore patterns for a project root.

    Works with both local and SSH backends. If backend is provided and is an
    SSHBackend, reads .gitignore over SSH. Otherwise reads from local filesystem.

    Returns a PathSpec matcher or None if no .gitignore exists.
    """
    if working_directory in _gitignore_cache:
        return _gitignore_cache[working_directory]

    spec = None
    try:
        if backend is not None and getattr(backend, "_host", None) is not None:
            # SSH: read .gitignore via backend
            try:
                content = backend.read_file(".gitignore")
                if content:
                    spec = pathspec.PathSpec.from_lines("gitwildmatch", content.splitlines())
            except Exception:
                pass
        else:
            # Local filesystem
            gitignore_path = os.path.join(working_directory, ".gitignore")
            if os.path.isfile(gitignore_path):
                with open(gitignore_path, "r", encoding="utf-8", errors="replace") as f:
                    spec = pathspec.PathSpec.from_lines("gitwildmatch", f)
    except Exception as e:
        logger.debug(f"Failed to parse .gitignore: {e}")

    _gitignore_cache[working_directory] = spec
    return spec


def _is_ignored(rel_path: str, name: str, is_dir: bool,
                gitignore_spec: Optional[pathspec.PathSpec]) -> bool:
    """Check if a path should be ignored based on .gitignore + hardcoded skips."""
    if name in _ALWAYS_SKIP_DIRS and is_dir:
        return True
    if not is_dir:
        _, ext = os.path.splitext(name)
        if ext in _ALWAYS_SKIP_EXTENSIONS:
            return True
    if gitignore_spec:
        check_path = rel_path + "/" if is_dir else rel_path
        if gitignore_spec.match_file(check_path):
            return True
    return False


def invalidate_gitignore_cache(working_directory: Optional[str] = None) -> None:
    """Clear cached .gitignore specs. Call when .gitignore changes."""
    if working_directory:
        _gitignore_cache.pop(working_directory, None)
    else:
        _gitignore_cache.clear()
