"""
Verification and learning system for the coding agent.
Handles project rules loading, failure pattern tracking, and policy decisions.
"""

import json
import os
import time
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path

from .events import PolicyDecision
from config import app_config
from tools.schemas import NATIVE_BASH_NAME

logger = logging.getLogger(__name__)


class VerificationMixin:
    """Mixin providing verification, learning, and policy capabilities."""
    
    # Constants
    _PROJECT_RULES_MAX_CHARS = 8000
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Cache for failure patterns to avoid repeated file I/O
        self._failure_pattern_cache: Optional[List[Dict[str, Any]]] = None
    
    # ------------------------------------------------------------------
    # Project Rules
    # ------------------------------------------------------------------
    
    def _load_project_rules(self) -> str:
        """Load project rule files and return concatenated content for system prompt.
        Tries: .cursorrules, RULE.md, CLAUDE.md, .claude/CLAUDE.md,
        .cursor/RULE.md, .cursor/rules/*.mdc, .cursor/rules/*.md.
        Capped at _PROJECT_RULES_MAX_CHARS."""
        parts: List[str] = []
        total = 0

        def _add(path: str, label: str) -> None:
            nonlocal total
            if total >= self._PROJECT_RULES_MAX_CHARS:
                return
            full = os.path.join(self.working_directory, path)
            if os.path.isfile(full):
                try:
                    with open(full, "r", encoding="utf-8", errors="ignore") as f:
                        chunk = f.read().strip()
                    if chunk:
                        take = min(len(chunk), self._PROJECT_RULES_MAX_CHARS - total)
                        parts.append(f"=== {label} ===\n{chunk[:take]}")
                        total += take
                except Exception:
                    pass

        _add(".cursorrules", "cursorrules")
        _add("RULE.md", "RULE.md")
        _add("CLAUDE.md", "CLAUDE.md")
        _add(".claude/CLAUDE.md", ".claude/CLAUDE.md")
        _add(".cursor/RULE.md", ".cursor/RULE.md")
        
        # Check for .cursor/rules directory
        cursor_rules_dir = os.path.join(self.working_directory, ".cursor", "rules")
        if os.path.isdir(cursor_rules_dir):
            try:
                for fname in sorted(os.listdir(cursor_rules_dir)):
                    if fname.endswith((".md", ".mdc")) and total < self._PROJECT_RULES_MAX_CHARS:
                        _add(f".cursor/rules/{fname}", f".cursor/rules/{fname}")
            except Exception:
                pass

        return "\n\n".join(parts)
    
    # ------------------------------------------------------------------
    # Failure Pattern Learning
    # ------------------------------------------------------------------
    
    def _failure_memory_path(self) -> str:
        return os.path.join(self.working_directory, ".bedrock-codex", "learning", "failure_patterns.json")

    def _load_failure_patterns(self) -> List[Dict[str, Any]]:
        if self._failure_pattern_cache is not None:
            return self._failure_pattern_cache
        path = self._failure_memory_path()
        try:
            if not os.path.exists(path):
                self._failure_pattern_cache = []
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._failure_pattern_cache = data
            elif isinstance(data, dict):
                self._failure_pattern_cache = data.get("patterns", [])
            else:
                self._failure_pattern_cache = []
            return self._failure_pattern_cache
        except Exception as e:
            logger.warning(f"Failed to load failure patterns: {e}")
            self._failure_pattern_cache = []
            return []

    def _save_failure_patterns(self, rows: List[Dict[str, Any]]) -> None:
        path = self._failure_memory_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            data = {"patterns": rows, "last_updated": int(time.time())}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self._failure_pattern_cache = rows
        except Exception as e:
            logger.warning(f"Failed to save failure patterns: {e}")

    def _record_failure_pattern(self, kind: str, detail: str, context: Optional[Dict[str, Any]] = None) -> None:
        """Record a failure pattern for future learning."""
        rows = self._load_failure_patterns()
        detail_key = detail.strip()[:500]  # Normalize and truncate
        now = int(time.time())
        
        # Find existing pattern or create new one
        found = False
        for row in rows:
            if row.get("kind") == kind and row.get("detail") == detail_key:
                row["count"] = int(row.get("count", 1)) + 1
                row["last_seen"] = now
                row["last_context"] = context or {}
                found = True
                break
        
        if not found:
            rows.append({
                "kind": kind,
                "detail": detail_key,
                "count": 1,
                "first_seen": now,
                "last_seen": now,
                "last_context": context or {},
            })
        rows = sorted(rows, key=lambda r: (int(r.get("count", 1)), int(r.get("last_seen", 0))), reverse=True)[:200]
        self._save_failure_patterns(rows)

    def _failure_patterns_prompt(self) -> str:
        rows = self._load_failure_patterns()
        if not rows:
            return ""
        lines = []
        for row in rows[:8]:
            lines.append(
                f"- [{row.get('kind','failure')}] x{row.get('count',1)}: {str(row.get('detail',''))[:180]}"
            )
        header = (
            "Avoid repeating these known failure patterns:\n"
        )
        footer = (
            "\nIf you encounter one of these patterns, try an alternative approach "
            "rather than repeating the same failing operation."
        )
        return header + "\n".join(lines) + footer
    
    # ------------------------------------------------------------------
    # Policy Engine
    # ------------------------------------------------------------------
    
    def _policy_decision(self, tool_name: str, tool_input: Dict[str, Any]) -> PolicyDecision:
        """Policy engine: block or require approval for risky operations."""
        if tool_name == NATIVE_BASH_NAME:
            cmd = tool_input.get("command", "")
            # Patterns that could affect shared systems
            shared_impact_patterns = [
                "git push", "git pull", "git fetch", "git merge", "git rebase",
                "npm publish", "pip install --global", "sudo", "chmod +x", "docker push",
                "gcloud", "aws", "kubectl apply", "terraform apply", "ansible-playbook",
            ]
            # Highly destructive patterns
            destructive_patterns = [
                "rm -rf", "rm -fr", "rm -r", "rm -f", "rmdir", "> /dev/null",
                "dd if=", "mkfs.", "fdisk", "parted", "fsck",
                "iptables -F", "ufw --force", "systemctl stop", "service stop",
                "docker system prune", "docker volume rm", "docker network rm",
                "git reset --hard", "git clean -fd", "git checkout -- .",
                "DROP TABLE", "DROP DATABASE", "TRUNCATE", "DELETE FROM",
                "kubectl delete", "helm uninstall",
            ]
            if any(p in cmd for p in destructive_patterns):
                if app_config.block_destructive_commands:
                    return PolicyDecision(blocked=True, reason="Blocked destructive command by policy engine.")
                return PolicyDecision(require_approval=True, reason="Destructive command requires explicit approval.")
            if any(p in cmd for p in shared_impact_patterns):
                return PolicyDecision(require_approval=True, reason="Shared-impact command requires explicit approval.")

        return PolicyDecision()