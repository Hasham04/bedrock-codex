"""
Terminal REST and WebSocket endpoints.

Handles terminal CWD, command execution, tab completion, and full PTY WebSocket.
"""

import asyncio
import json
import logging
import os
import posixpath
import shlex
import struct
import sys
import termios
import threading
from typing import Optional, Dict, Any, List, Tuple

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse

from backend import Backend, LocalBackend, SSHBackend
import web.state as _state

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/terminal-cwd")
async def terminal_cwd():
    """Return current working directory for the integrated terminal (project root)."""
    if _state._backend is None:
        return {"ok": False, "cwd": None}
    return {"ok": True, "cwd": _state._backend.working_directory}


def _terminal_cwd_ok(backend, requested_cwd: str) -> Tuple[bool, str]:
    """Validate requested cwd is project root, a subdir, or an ancestor (so cd .. works).
    Returns (ok, resolved_cwd)."""
    root = backend.working_directory
    if not requested_cwd or requested_cwd == ".":
        return True, root
    # SSH backend: allow root, subdirs, or ancestors of project root
    if getattr(backend, "_host", None) is not None:
        root_norm = root.rstrip("/")
        req_norm = requested_cwd.rstrip("/")
        if req_norm == root_norm or req_norm.startswith(root_norm + "/"):
            return True, requested_cwd
        if root_norm.startswith(req_norm + "/"):
            return True, requested_cwd
        return False, root
    # Local: allow under project root or ancestor of project root
    try:
        req_abs = os.path.abspath(os.path.join(root, requested_cwd)) if not os.path.isabs(requested_cwd) else requested_cwd
        req_real = os.path.realpath(req_abs)
        root_real = os.path.realpath(root)
        sep = os.sep
        root_prefix = root_real.rstrip(sep) + sep
        req_prefix = req_real.rstrip(sep) + sep
        if req_real == root_real or req_real.startswith(root_prefix):
            return True, req_real
        if root_real.startswith(req_prefix) or root_real == req_real:
            return True, req_real
    except Exception:
        pass
    return False, root


@router.post("/api/terminal-run")
async def terminal_run(request: Request):
    """Run a shell command in the given cwd (default project root). Returns stdout, stderr, returncode, cwd."""
    if _state._backend is None:
        return JSONResponse({"ok": False, "error": "No project open. Open a local folder or connect via SSH first."}, status_code=400)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Invalid request: {e!s}"}, status_code=400)
    command = (body.get("command") or "").strip()
    if not command:
        return JSONResponse({"ok": False, "error": "No command"}, status_code=400)
    requested_cwd = (body.get("cwd") or "").strip() or "."
    ok, cwd = _terminal_cwd_ok(_state._backend, requested_cwd)
    if not ok:
        return JSONResponse({"ok": False, "error": "Directory not under project root"}, status_code=400)
    timeout = min(int(body.get("timeout", 60)), 300)
    try:
        stdout, stderr, returncode = await asyncio.to_thread(
            _state._backend.run_command, command, cwd, timeout
        )
        return {
            "ok": True,
            "stdout": stdout or "",
            "stderr": stderr or "",
            "returncode": returncode,
            "cwd": cwd,
        }
    except Exception as e:
        err_msg = str(e).strip() or "Command failed"
        logger.exception("Terminal run failed")
        return JSONResponse({"ok": False, "error": err_msg}, status_code=500)


@router.post("/api/terminal-complete")
async def terminal_complete(request: Request):
    """Return tab-completion candidates for the terminal. Body: prefix, cwd, type ('path'|'command')."""
    if _state._backend is None:
        return JSONResponse({"ok": False, "error": "No project open."}, status_code=400)
    try:
        body = await request.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    prefix = (body.get("prefix") or "").strip()
    complete_type = (body.get("type") or "path").strip().lower() or "path"
    if complete_type not in ("path", "command"):
        complete_type = "path"
    requested_cwd = (body.get("cwd") or "").strip() or "."
    ok, cwd = _terminal_cwd_ok(_state._backend, requested_cwd)
    if not ok:
        return JSONResponse({"ok": False, "error": "Directory not under project root"}, status_code=400)

    try:
        if complete_type == "command":
            out, err, rc = await asyncio.to_thread(
                _state._backend.run_command, "bash -c 'compgen -c'", cwd, 5
            )
            candidates = [line for line in (out or "").splitlines() if line.strip()]
            if prefix:
                prefix_lower = prefix.lower()
                candidates = [c for c in candidates if c.lower().startswith(prefix_lower)]
            candidates = sorted(set(candidates))[:100]
            return {"ok": True, "completions": candidates, "prefix": prefix}
        else:
            # Path completion: list directory (cwd or parent of prefix), filter by prefix
            if "/" in prefix:
                dir_part = prefix.rsplit("/", 1)[0]
                filter_prefix = prefix.rsplit("/", 1)[1]
                list_path = posixpath.normpath(cwd.rstrip("/") + "/" + dir_part) if cwd else dir_part
            else:
                list_path = cwd
                filter_prefix = prefix
            try:
                entries = await asyncio.to_thread(_state._backend.list_dir, list_path)
            except Exception:
                entries = []
            completions = []
            for e in entries:
                name = e.get("name") or ""
                if not name or (filter_prefix and not name.startswith(filter_prefix)):
                    continue
                if e.get("type") == "directory":
                    completions.append(name + "/")
                else:
                    completions.append(name)
            completions = sorted(completions)[:100]
            return {"ok": True, "completions": completions, "prefix": prefix}
    except Exception as e:
        logger.exception("Terminal complete failed")
        return JSONResponse({"ok": False, "error": str(e).strip() or "Completion failed"}, status_code=500)


# ------------------------------------------------------------------
# Full terminal (PTY) WebSocket — local backend only
# ------------------------------------------------------------------

def _pty_shell(cwd: str) -> None:
    """Run in the child after pty.fork(): stdio is already the slave; chdir and exec shell."""
    os.setsid()
    os.chdir(cwd)
    shell = os.environ.get("SHELL", "/bin/bash")
    if not os.path.exists(shell):
        shell = "/bin/bash"
    os.execlp(shell, os.path.basename(shell), "-l")


def _pty_read_loop(master_fd: int, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """Thread: read from PTY master and put bytes into asyncio queue."""
    try:
        while True:
            try:
                data = os.read(master_fd, 4096)
            except (OSError, AttributeError):
                break
            if not data:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(queue.put_nowait, None)
    except Exception:
        pass


def _ssh_channel_read_loop(channel: Any, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> None:
    """Thread: read from paramiko Channel and put bytes into asyncio queue."""
    try:
        while channel.active:
            try:
                data = channel.recv(4096)
            except Exception:
                break
            if not data:
                break
            loop.call_soon_threadsafe(queue.put_nowait, data)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(queue.put_nowait, None)
    except Exception:
        pass


@router.websocket("/ws/terminal")
async def websocket_terminal(ws: WebSocket):
    """Full PTY terminal. Local: pty.fork(); SSH: invoke_shell. Binary = I/O; text JSON = resize [rows, cols]."""
    await ws.accept()

    async def _send_error_and_close(message: str) -> None:
        await ws.send_json({"type": "error", "message": message})
        await asyncio.sleep(0.05)
        await ws.close()

    if _state._backend is None:
        logger.warning("terminal ws: rejected (no project open)")
        await _send_error_and_close("No project open. Open a project first.")
        return

    is_local = isinstance(_state._backend, LocalBackend)
    is_ssh = isinstance(_state._backend, SSHBackend)
    if not is_local and not is_ssh:
        logger.warning("terminal ws: rejected (backend type %s)", type(_state._backend).__name__)
        await _send_error_and_close("Full terminal is not available for this backend.")
        return

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()
    master_fd: Optional[int] = None
    pid: Optional[int] = None
    ssh_channel: Optional[Any] = None

    if is_local:
        cwd = os.path.abspath(os.path.expanduser(str(_state._working_directory)))
        if not os.path.isdir(cwd):
            logger.warning("terminal ws: local project dir not found: %s", cwd)
            await _send_error_and_close(f"Project directory not found: {cwd}")
            return
        pty_available = False
        try:
            import pty
            pty_available = True
        except ImportError:
            pass
        if not pty_available or sys.platform == "win32":
            logger.warning("terminal ws: PTY not available (platform=%s)", sys.platform)
            await _send_error_and_close("PTY not available on this system (required for local terminal).")
            return
        import pty
        try:
            pid, master_fd = pty.fork()
        except OSError as e:
            logger.exception("terminal ws: pty.fork failed")
            await _send_error_and_close(f"Terminal failed to start: {e!s}")
            return
        if pid == 0:
            try:
                _pty_shell(cwd)
            except Exception:
                os._exit(1)
            os._exit(0)
        reader_thread = threading.Thread(
            target=_pty_read_loop,
            args=(master_fd, queue, loop),
            daemon=True,
        )
        reader_thread.start()
    else:
        # SSH: open interactive shell with PTY
        backend = _state._backend
        cwd = (backend.working_directory or "").rstrip("/") or "/"
        with backend._lock:
            backend._reconnect_if_needed()
            try:
                ssh_channel = backend._client.invoke_shell(term="xterm", width=80, height=24)
            except Exception as e:
                await _send_error_and_close(f"SSH shell failed: {e!s}")
                return
        ssh_channel.settimeout(0.5)
        # Start in project directory
        if cwd and cwd != "~":
            try:
                ssh_channel.send(f"cd {shlex.quote(cwd)}\r\n")
            except Exception:
                pass
        reader_thread = threading.Thread(
            target=_ssh_channel_read_loop,
            args=(ssh_channel, queue, loop),
            daemon=True,
        )
        reader_thread.start()

    async def send_output():
        """Batch PTY output: drain queue for a short window and send one combined message to reduce WebSocket chatter."""
        batch: list = []
        batch_size = 0
        max_batch_bytes = 8192
        flush_interval = 0.008  # 8ms

        async def flush():
            nonlocal batch, batch_size
            if not batch:
                return
            try:
                await ws.send_bytes(b"".join(batch))
            except Exception:
                pass
            batch = []
            batch_size = 0

        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=flush_interval)
            except asyncio.TimeoutError:
                await flush()
                continue
            if data is None:
                await flush()
                break
            batch.append(data)
            batch_size += len(data)
            while batch_size >= max_batch_bytes:
                await flush()
            # Drain any more available without waiting
            while True:
                try:
                    data = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if data is None:
                    await flush()
                    return
                batch.append(data)
                batch_size += len(data)
                if batch_size >= max_batch_bytes:
                    await flush()
                    break
        await flush()

    send_task = asyncio.create_task(send_output())

    def do_resize(rows: int, cols: int) -> None:
        if rows <= 0 or cols <= 0:
            return
        if is_local and master_fd is not None:
            try:
                if hasattr(termios, "tcsetwinsize"):
                    termios.tcsetwinsize(master_fd, (rows, cols))
                else:
                    import fcntl
                    buf = struct.pack("HHHH", rows, cols, 0, 0)
                    fcntl.ioctl(master_fd, termios.TIOCSWINSZ, buf)
            except (OSError, AttributeError, NameError):
                pass
        elif is_ssh and ssh_channel is not None:
            try:
                ssh_channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    try:
        logger.info("terminal ws: connected (backend=%s)", "local" if is_local else "ssh")
        await ws.send_json({"type": "ready"})
        while True:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=3600.0)
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "websocket.disconnect":
                break
            if msg.get("type") == "websocket.receive":
                text = msg.get("text")
                data = msg.get("bytes")
                if text is not None:
                    logger.info("terminal ws: received text len=%s (first 50 repr=%s)", len(text), repr(text[:50]))
                    try:
                        obj = json.loads(text)
                        if not isinstance(obj, dict):
                            logger.warning(
                                "terminal ws: received JSON text that is not a dict (type=%s, repr=%s, raw_len=%s)",
                                type(obj).__name__, repr(obj)[:200], len(text),
                            )
                        elif isinstance(obj.get("resize"), (list, tuple)) and len(obj["resize"]) >= 2:
                            rows, cols = int(obj["resize"][0]), int(obj["resize"][1])
                            logger.info("terminal ws: resize rows=%s cols=%s", rows, cols)
                            do_resize(rows, cols)
                            continue
                    except (json.JSONDecodeError, ValueError, TypeError) as e:
                        logger.debug("terminal ws: JSON parse failed for text (len=%s): %s", len(text), e)
                    try:
                        if is_local and master_fd is not None:
                            os.write(master_fd, text.encode("utf-8"))
                        elif is_ssh and ssh_channel is not None:
                            ssh_channel.send(text)
                    except (OSError, BrokenPipeError, Exception):
                        break
                elif data is not None:
                    logger.debug("terminal ws: received binary payload len=%s", len(data))
                    try:
                        if is_local and master_fd is not None:
                            os.write(master_fd, data)
                        elif is_ssh and ssh_channel is not None:
                            ssh_channel.send(data)
                    except (OSError, BrokenPipeError, Exception):
                        break
    except WebSocketDisconnect:
        logger.debug("terminal ws: client disconnected")
    finally:
        logger.info("terminal ws: closing")
        send_task.cancel()
        try:
            send_task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        if is_local and master_fd is not None:
            try:
                os.close(master_fd)
            except OSError:
                pass
            if pid is not None:
                try:
                    os.kill(pid, 9)
                except OSError:
                    pass
                try:
                    os.waitpid(pid, 0)
                except OSError:
                    pass
        if is_ssh and ssh_channel is not None:
            try:
                ssh_channel.close()
            except Exception:
                pass
