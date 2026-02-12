"""
Backend abstraction for file and command operations.
Supports local filesystem (default) and SSH remote via paramiko.
"""

import logging
import os
import pathlib
import subprocess
import threading
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger(__name__)


class Backend(ABC):
    """Abstract backend for file system and command operations."""

    @property
    @abstractmethod
    def working_directory(self) -> str:
        """Return the working directory path."""

    @abstractmethod
    def list_dir(self, path: str) -> List[Dict[str, Any]]:
        """List entries in a directory. Returns list of {name, type, ext?, size?}."""

    @abstractmethod
    def read_file(self, path: str) -> str:
        """Read file content as text."""

    @abstractmethod
    def write_file(self, path: str, content: str) -> None:
        """Write content to a file (create dirs as needed)."""

    @abstractmethod
    def file_exists(self, path: str) -> bool:
        """Check if a file exists."""

    @abstractmethod
    def is_dir(self, path: str) -> bool:
        """Check if a path is a directory."""

    @abstractmethod
    def is_file(self, path: str) -> bool:
        """Check if a path is a file."""

    @abstractmethod
    def file_size(self, path: str) -> int:
        """Get file size in bytes."""

    @abstractmethod
    def remove_file(self, path: str) -> None:
        """Delete a file."""

    @abstractmethod
    def run_command(self, command: str, cwd: str, timeout: int = 30) -> Tuple[str, str, int]:
        """Run a shell command. Returns (stdout, stderr, returncode)."""

    def cancel_running_command(self) -> bool:
        """Kill the currently running command, if any. Returns True if killed."""
        return False

    @abstractmethod
    def search(self, pattern: str, path: str, include: Optional[str] = None,
               cwd: str = ".") -> str:
        """Search for a regex pattern. Returns matching lines."""

    @abstractmethod
    def glob_find(self, pattern: str, cwd: str) -> List[str]:
        """Find files matching a glob pattern. Returns relative paths."""

    def resolve_path(self, path: str) -> str:
        """Resolve a path relative to the working directory."""
        if os.path.isabs(path):
            return path
        return os.path.normpath(os.path.join(self.working_directory, path))


# ============================================================
# Local Backend
# ============================================================

# Cache ripgrep availability
_HAS_RIPGREP: Optional[bool] = None

def _has_ripgrep() -> bool:
    global _HAS_RIPGREP
    if _HAS_RIPGREP is None:
        try:
            subprocess.run(["rg", "--version"], capture_output=True, check=True)
            _HAS_RIPGREP = True
        except (subprocess.CalledProcessError, FileNotFoundError):
            _HAS_RIPGREP = False
    return _HAS_RIPGREP


class LocalBackend(Backend):
    """Backend that operates on the local filesystem."""

    def __init__(self, working_directory: str = "."):
        self._working_directory = os.path.abspath(working_directory)
        self._active_process: Optional[subprocess.Popen] = None

    @property
    def working_directory(self) -> str:
        return self._working_directory

    def list_dir(self, path: str) -> List[Dict[str, Any]]:
        full = self.resolve_path(path) if path else self._working_directory
        entries = []
        for name in sorted(os.listdir(full)):
            child = os.path.join(full, name)
            if os.path.isdir(child):
                entries.append({"name": name, "type": "directory"})
            elif os.path.isfile(child):
                _, ext = os.path.splitext(name)
                entries.append({
                    "name": name, "type": "file",
                    "ext": ext.lstrip("."),
                    "size": os.path.getsize(child),
                })
        return entries

    def read_file(self, path: str) -> str:
        full = self.resolve_path(path)
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    def write_file(self, path: str, content: str) -> None:
        full = self.resolve_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)

    def file_exists(self, path: str) -> bool:
        return os.path.exists(self.resolve_path(path))

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(self.resolve_path(path))

    def is_file(self, path: str) -> bool:
        return os.path.isfile(self.resolve_path(path))

    def file_size(self, path: str) -> int:
        return os.path.getsize(self.resolve_path(path))

    def remove_file(self, path: str) -> None:
        os.remove(self.resolve_path(path))

    def run_command(self, command: str, cwd: str, timeout: int = 30) -> Tuple[str, str, int]:
        full_cwd = self.resolve_path(cwd) if cwd != "." else self._working_directory
        proc = subprocess.Popen(
            command, shell=True, cwd=full_cwd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            preexec_fn=os.setsid,  # create process group for clean kill
        )
        # Track the process so it can be killed on cancel
        self._active_process = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_process(proc)
            stdout, stderr = proc.communicate(timeout=5)
            return stdout or "", f"Command timed out after {timeout}s\n{stderr or ''}", -1
        finally:
            self._active_process = None
        return stdout or "", stderr or "", proc.returncode

    def cancel_running_command(self) -> bool:
        """Kill the currently running subprocess, if any. Returns True if killed."""
        proc = getattr(self, "_active_process", None)
        if proc and proc.poll() is None:
            self._kill_process(proc)
            return True
        return False

    @staticmethod
    def _kill_process(proc: subprocess.Popen) -> None:
        """Kill a process and its entire process group."""
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

    def search(self, pattern: str, path: str, include: Optional[str] = None,
               cwd: str = ".") -> str:
        search_path = self.resolve_path(path) if path else self._working_directory
        full_cwd = self.resolve_path(cwd) if cwd != "." else self._working_directory

        if _has_ripgrep():
            cmd = ["rg", "--line-number", "--no-heading", "--color=never", "-m", "100"]
            if include:
                cmd.extend(["--glob", include])
            cmd.extend([pattern, search_path])
        else:
            cmd = ["grep", "-rn", "--color=never"]
            if include:
                cmd.extend(["--include", include])
            cmd.extend([pattern, search_path])

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, cwd=full_cwd)
        return result.stdout.strip() if result.stdout else ""

    def glob_find(self, pattern: str, cwd: str) -> List[str]:
        base = pathlib.Path(self.resolve_path(cwd) if cwd != "." else self._working_directory)
        skip = {"__pycache__", ".pyc", "node_modules", ".git", "venv", ".venv"}
        matches = []
        for p in sorted(base.glob(pattern)):
            rel = str(p.relative_to(base))
            parts = set(pathlib.PurePath(rel).parts)
            if not parts & skip:
                matches.append(rel)
        return matches


# ============================================================
# SSH Backend
# ============================================================

class SSHBackend(Backend):
    """Backend that operates on a remote machine via SSH (paramiko)."""

    def __init__(self, host: str, working_directory: str,
                 user: Optional[str] = None, key_path: Optional[str] = None,
                 port: int = 22):
        try:
            import paramiko
        except ImportError:
            raise ImportError(
                "paramiko is required for SSH support. Install with: pip install paramiko"
            )

        self._host = host
        self._user = user
        self._port = port
        self._key_path = key_path
        self._working_directory = working_directory
        # Reentrant lock — serialises all SFTP/exec calls so concurrent
        # threads (agent tool execution + REST API) never corrupt the
        # shared paramiko session.
        self._lock = threading.RLock()

        # Connect
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: Dict[str, Any] = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": 15,
        }
        if key_path:
            connect_kwargs["key_filename"] = os.path.expanduser(key_path)

        logger.info(f"SSH connecting to {user}@{host}:{port}...")
        self._client.connect(**connect_kwargs)
        self._sftp = self._client.open_sftp()

        # Set keepalive
        transport = self._client.get_transport()
        if transport:
            transport.set_keepalive(30)

        self._active_channel = None  # track running command for cancel

        logger.info(f"SSH connected to {user}@{host}:{port}, dir: {working_directory}")

    @property
    def working_directory(self) -> str:
        return self._working_directory

    def _remote_path(self, path: str) -> str:
        """Resolve a relative path to absolute on the remote."""
        if os.path.isabs(path):
            return path
        # Use posixpath for remote (always Linux)
        import posixpath
        return posixpath.normpath(posixpath.join(self._working_directory, path))

    def _reconnect_if_needed(self):
        """Reconnect SSH if the connection dropped.  Caller MUST hold self._lock."""
        try:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                # Also check SFTP is alive with a quick operation
                try:
                    self._sftp.normalize(".")
                    return  # Connection is healthy
                except Exception:
                    logger.warning("SFTP channel broken, reopening...")
                    try:
                        self._sftp.close()
                    except Exception:
                        pass
                    self._sftp = self._client.open_sftp()
                    return
        except Exception:
            pass  # Fall through to full reconnect

        logger.warning("SSH connection lost, reconnecting...")
        connect_kwargs: Dict[str, Any] = {
            "hostname": self._host, "port": self._port, "username": self._user,
            "timeout": 15,
        }
        if self._key_path:
            connect_kwargs["key_filename"] = os.path.expanduser(self._key_path)
        self._client.connect(**connect_kwargs)
        self._sftp = self._client.open_sftp()
        transport = self._client.get_transport()
        if transport:
            transport.set_keepalive(30)
        logger.info("SSH reconnected.")

    def _exec(self, cmd: str, timeout: int = 30) -> Tuple[str, str, int]:
        """Execute a command on the remote host."""
        with self._lock:
            self._reconnect_if_needed()
            _, stdout_ch, stderr_ch = self._client.exec_command(cmd, timeout=timeout)
        channel = stdout_ch.channel
        self._active_channel = channel  # track for cancel
        # Read OUTSIDE the lock — exec channels are independent of SFTP,
        # so we only need the lock to open the channel safely.
        channel.settimeout(timeout)
        stderr_ch.channel.settimeout(timeout)
        try:
            stdout = stdout_ch.read().decode("utf-8", errors="replace")
        except Exception:
            stdout = ""
        try:
            stderr = stderr_ch.read().decode("utf-8", errors="replace")
        except Exception:
            stderr = ""
        try:
            rc = channel.recv_exit_status()
        except Exception:
            rc = -1
        self._active_channel = None
        return stdout, stderr, rc

    def cancel_running_command(self) -> bool:
        """Kill the currently running SSH command, if any."""
        ch = getattr(self, "_active_channel", None)
        if ch is not None:
            try:
                ch.close()
            except Exception:
                pass
            self._active_channel = None
            return True
        return False

    def list_dir(self, path: str) -> List[Dict[str, Any]]:
        import stat as stat_mod
        remote = self._remote_path(path) if path else self._working_directory
        entries = []
        with self._lock:
            self._reconnect_if_needed()
            try:
                attrs = self._sftp.listdir_attr(remote)
            except Exception as e:
                logger.error(f"SSH list_dir failed for {remote!r}: {e}")
                attrs = None

        if attrs is None:
            # Fallback: use ls command (goes through _exec which has its own lock)
            stdout, stderr, rc = self._exec(f"ls -1pa {remote!r}", timeout=10)
            if rc != 0 or not stdout.strip():
                logger.error(f"SSH ls fallback also failed: {stderr}")
                return []
            for line in stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                if line.endswith("/"):
                    name = line.rstrip("/")
                    if name:
                        entries.append({"name": name, "type": "directory"})
                else:
                    ext = os.path.splitext(line)[1].lstrip(".")
                    entries.append({"name": line, "type": "file", "ext": ext, "size": 0})
            return sorted(entries, key=lambda e: e["name"])

        for attr in sorted(attrs, key=lambda a: a.filename):
            name = attr.filename
            if stat_mod.S_ISDIR(attr.st_mode or 0):
                entries.append({"name": name, "type": "directory"})
            else:
                ext = os.path.splitext(name)[1].lstrip(".")
                entries.append({"name": name, "type": "file", "ext": ext, "size": attr.st_size or 0})
        return entries

    def read_file(self, path: str) -> str:
        with self._lock:
            self._reconnect_if_needed()
            remote = self._remote_path(path)
            with self._sftp.open(remote, "r") as f:
                return f.read().decode("utf-8", errors="replace")

    def write_file(self, path: str, content: str) -> None:
        import posixpath
        remote = self._remote_path(path)
        parent = posixpath.dirname(remote)
        # mkdir -p goes through _exec (has its own lock acquisition)
        self._exec(f"mkdir -p {parent!r}")
        with self._lock:
            self._reconnect_if_needed()
            with self._sftp.open(remote, "w") as f:
                f.write(content.encode("utf-8"))

    def file_exists(self, path: str) -> bool:
        with self._lock:
            self._reconnect_if_needed()
            try:
                self._sftp.stat(self._remote_path(path))
                return True
            except FileNotFoundError:
                return False

    def is_dir(self, path: str) -> bool:
        import stat as stat_mod
        with self._lock:
            self._reconnect_if_needed()
            try:
                attr = self._sftp.stat(self._remote_path(path))
                return stat_mod.S_ISDIR(attr.st_mode or 0)
            except FileNotFoundError:
                return False

    def is_file(self, path: str) -> bool:
        import stat as stat_mod
        with self._lock:
            self._reconnect_if_needed()
            try:
                attr = self._sftp.stat(self._remote_path(path))
                return stat_mod.S_ISREG(attr.st_mode or 0)
            except FileNotFoundError:
                return False

    def file_size(self, path: str) -> int:
        with self._lock:
            self._reconnect_if_needed()
            attr = self._sftp.stat(self._remote_path(path))
            return attr.st_size or 0

    def remove_file(self, path: str) -> None:
        with self._lock:
            self._reconnect_if_needed()
            self._sftp.remove(self._remote_path(path))

    def run_command(self, command: str, cwd: str, timeout: int = 30) -> Tuple[str, str, int]:
        full_cwd = self._remote_path(cwd) if cwd != "." else self._working_directory
        cmd = f"cd {full_cwd!r} && {command}"
        return self._exec(cmd, timeout=timeout)

    def search(self, pattern: str, path: str, include: Optional[str] = None,
               cwd: str = ".") -> str:
        search_path = self._remote_path(path) if path else self._working_directory
        # Try ripgrep first, fall back to grep
        if include:
            rg_cmd = f"rg --line-number --no-heading --color=never -m 100 --glob {include!r} {pattern!r} {search_path!r} 2>/dev/null || grep -rn --color=never --include={include!r} {pattern!r} {search_path!r} 2>/dev/null"
        else:
            rg_cmd = f"rg --line-number --no-heading --color=never -m 100 {pattern!r} {search_path!r} 2>/dev/null || grep -rn --color=never {pattern!r} {search_path!r} 2>/dev/null"
        stdout, _, _ = self._exec(rg_cmd, timeout=15)
        return stdout.strip()

    def glob_find(self, pattern: str, cwd: str) -> List[str]:
        full_cwd = self._remote_path(cwd) if cwd != "." else self._working_directory
        # Use find on remote
        stdout, _, _ = self._exec(
            f"cd {full_cwd!r} && find . -path './{pattern}' -not -path '*/node_modules/*' "
            f"-not -path '*/__pycache__/*' -not -path '*/.git/*' 2>/dev/null | head -200 | sort",
            timeout=15,
        )
        if not stdout.strip():
            return []
        return [line.lstrip("./") for line in stdout.strip().split("\n") if line.strip()]

    def close(self):
        """Close the SSH connection."""
        with self._lock:
            try:
                self._sftp.close()
                self._client.close()
            except Exception:
                pass
