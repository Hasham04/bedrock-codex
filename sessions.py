"""
Session persistence for Bedrock Codex.
Stores conversation history as JSON files so users can close the app
and resume where they left off, with support for multiple sessions per project.
"""

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_DIR = os.path.join(os.path.expanduser("~"), ".bedrock-codex", "sessions")

SESSION_VERSION = 1


@dataclass
class Session:
    """A persisted agent session."""
    session_id: str = ""
    version: int = SESSION_VERSION
    name: str = "default"
    working_directory: str = ""
    model_id: str = ""
    created_at: str = ""
    updated_at: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    token_usage: Dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    })
    extra_state: Dict[str, Any] = field(default_factory=dict)

    @property
    def message_count(self) -> int:
        """Count user messages in history."""
        return sum(1 for m in self.history if m.get("role") == "user"
                   and isinstance(m.get("content"), str))

    @property
    def total_tokens(self) -> int:
        return self.token_usage.get("input_tokens", 0) + self.token_usage.get("output_tokens", 0)


def _slugify(name: str) -> str:
    """Turn a session name into a safe filename component."""
    s = name.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")[:50]
    return s or "default"


def _is_ssh_path(wd: str) -> bool:
    """Check if a working directory looks like an SSH composite path (user@host:...)."""
    return "@" in wd and ":" in wd


def _parse_ssh_composite(wd: str) -> Optional[Dict[str, Any]]:
    """Parse user@host:port:directory composite path into SSH info."""
    if not _is_ssh_path(wd):
        return None
    try:
        user_host, port_s, directory = wd.split(":", 2)
        if "@" not in user_host:
            return None
        user, host = user_host.split("@", 1)
        port = int(port_s)
        return {
            "host": host.strip(),
            "user": user.strip(),
            "port": port,
            "key_path": "",
            "directory": directory.strip() or "/",
        }
    except Exception:
        return None


def _normalize_wd(working_directory: str) -> str:
    """Normalize a working directory for hashing/storage.
    SSH paths are kept as-is; local paths are made absolute."""
    if _is_ssh_path(working_directory):
        return working_directory
    return os.path.abspath(working_directory)


def _dir_hash(working_directory: str) -> str:
    """Deterministic short hash of a working directory path."""
    return hashlib.sha256(_normalize_wd(working_directory).encode()).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _auto_name(first_task: str) -> str:
    """Generate a session name from the first user task."""
    words = first_task.strip().split()[:6]
    name = " ".join(words)
    if len(first_task.strip().split()) > 6:
        name += "..."
    return name or "default"


class SessionStore:
    """
    Manages session files on disk.

    File layout:  {base_dir}/{dir_hash}_{slug}.json
    """

    def __init__(self, base_dir: str = DEFAULT_BASE_DIR):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    def save(self, session: Session) -> str:
        """Save a session to disk. Returns the file path."""
        if not session.session_id:
            session.session_id = self._make_id(session.working_directory, session.name)
        session.updated_at = _now_iso()
        if not session.created_at:
            session.created_at = session.updated_at

        path = self._path_for(session.session_id)
        data = asdict(session)

        tmp_path = path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
            logger.info(f"Session saved: {path}")
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise

        return path

    def load(self, session_id: str) -> Optional[Session]:
        """Load a session by ID."""
        path = self._path_for(session_id)
        if not os.path.exists(path):
            return None
        return self._read_file(path)

    def delete(self, session_id: str) -> bool:
        """Delete a session file. Returns True if deleted."""
        path = self._path_for(session_id)
        if os.path.exists(path):
            os.remove(path)
            logger.info(f"Session deleted: {path}")
            return True
        return False

    def list_sessions(self, working_directory: str) -> List[Session]:
        """List all sessions for a given working directory, newest first."""
        prefix = _dir_hash(_normalize_wd(working_directory)) + "_"
        sessions: List[Session] = []

        for fname in os.listdir(self.base_dir):
            if fname.startswith(prefix) and fname.endswith(".json"):
                full = os.path.join(self.base_dir, fname)
                sess = self._read_file(full)
                if sess:
                    sessions.append(sess)

        sessions.sort(key=lambda s: s.updated_at or "", reverse=True)
        return sessions

    def get_latest(self, working_directory: str) -> Optional[Session]:
        """Get the most recently updated session for a working directory."""
        sessions = self.list_sessions(working_directory)
        return sessions[0] if sessions else None

    def list_all_projects(self) -> List[Dict[str, Any]]:
        """List all known projects, grouped by working directory.
        Returns: [{path, name, session_count, message_count, total_tokens, updated_at,
                   is_ssh, ssh_info}]"""
        projects: Dict[str, Dict[str, Any]] = {}
        for fname in os.listdir(self.base_dir):
            if not fname.endswith(".json"):
                continue
            full = os.path.join(self.base_dir, fname)
            sess = self._read_file(full)
            if not sess or not sess.working_directory:
                continue
            wd = sess.working_directory
            is_ssh = _is_ssh_path(wd)
            parsed_ssh = _parse_ssh_composite(wd) if is_ssh else None
            if wd not in projects:
                if is_ssh:
                    # Parse display name from composite path: user@host:port:dir
                    parts = wd.split(":", 2)
                    display_dir = parts[2] if len(parts) >= 3 else parts[-1]
                    host_part = parts[0]  # user@host
                    proj_name = os.path.basename(display_dir.rstrip("/")) or display_dir
                    proj_name = f"{proj_name} ({host_part})"
                else:
                    proj_name = os.path.basename(wd) or wd
                projects[wd] = {
                    "path": wd,
                    "name": proj_name,
                    "session_count": 0,
                    "message_count": 0,
                    "total_tokens": 0,
                    "updated_at": "",
                    "session_name": "",
                    "is_ssh": is_ssh,
                    "ssh_info": parsed_ssh,
                }
            p = projects[wd]
            p["session_count"] += 1
            p["message_count"] += sess.message_count
            p["total_tokens"] += sess.total_tokens
            # Keep the most recent updated_at, session name, and ssh_info
            if sess.updated_at and (not p["updated_at"] or sess.updated_at > p["updated_at"]):
                p["updated_at"] = sess.updated_at
                p["session_name"] = sess.name
                # Extract SSH info from the latest session's extra_state
                if is_ssh:
                    merged = dict(parsed_ssh or {})
                    if sess.extra_state and isinstance(sess.extra_state.get("ssh_info"), dict):
                        merged.update({k: v for k, v in sess.extra_state.get("ssh_info", {}).items() if v not in (None, "")})
                    p["ssh_info"] = merged or None

        result = sorted(projects.values(), key=lambda x: x["updated_at"] or "", reverse=True)
        return result

    def find_by_name(self, working_directory: str, name: str) -> Optional[Session]:
        """Find a session by name (case-insensitive) for a working directory."""
        name_lower = name.lower().strip()
        for sess in self.list_sessions(working_directory):
            if sess.name.lower().strip() == name_lower:
                return sess
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def create_session(
        self,
        working_directory: str,
        model_id: str,
        name: str = "default",
    ) -> Session:
        """Create a new empty session (not yet saved)."""
        sid = self._make_id(working_directory, name)
        return Session(
            session_id=sid,
            name=name,
            working_directory=_normalize_wd(working_directory),
            model_id=model_id,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )

    def rename(self, session: Session, new_name: str) -> Session:
        """Rename a session (creates new file, deletes old)."""
        old_id = session.session_id
        old_path = self._path_for(old_id)

        session.name = new_name
        session.session_id = self._make_id(session.working_directory, new_name)

        self.save(session)

        # Remove old file if ID changed
        if old_id != session.session_id and os.path.exists(old_path):
            os.remove(old_path)

        return session

    def auto_name_session(self, session: Session, first_task: str) -> Session:
        """Auto-name a session based on the first user task, if it's still 'default'."""
        if session.name == "default":
            new_name = _auto_name(first_task)
            return self.rename(session, new_name)
        return session

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_id(self, working_directory: str, name: str) -> str:
        return f"{_dir_hash(working_directory)}_{_slugify(name)}"

    def _path_for(self, session_id: str) -> str:
        return os.path.join(self.base_dir, f"{session_id}.json")

    def _read_file(self, path: str) -> Optional[Session]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Session(
                session_id=data.get("session_id", ""),
                version=data.get("version", 1),
                name=data.get("name", "default"),
                working_directory=data.get("working_directory", ""),
                model_id=data.get("model_id", ""),
                created_at=data.get("created_at", ""),
                updated_at=data.get("updated_at", ""),
                history=data.get("history", []),
                token_usage=data.get("token_usage", {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                }),
                extra_state=data.get("extra_state", {}),
            )
        except Exception as e:
            logger.warning(f"Failed to read session {path}: {e}")
            return None
