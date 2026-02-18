"""
CLI entry point for the Bedrock Codex web server.

Run:  python -m web [--port 8765] [--dir /path/to/project]
"""

import argparse
import logging
import os

import web.state as _state
from backend import LocalBackend


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description="Bedrock Codex — Web GUI")
    parser.add_argument("--port", type=int, default=8765, help="Server port (default: 8765)")
    parser.add_argument("--host", default="127.0.0.1", help="Server host (default: 127.0.0.1)")
    parser.add_argument("--dir", default=".", help="Working directory for the agent")
    parser.add_argument("--ssh", default=None, help="SSH remote: user@host (e.g. deploy@192.168.1.50)")
    parser.add_argument("--key", default=None, help="SSH private key path (default: ~/.ssh/id_rsa)")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH port (default: 22)")
    args = parser.parse_args()

    _state._working_directory = os.path.abspath(os.path.expanduser(args.dir))

    if args.ssh:
        # SSH remote mode — always explicit
        _state._explicit_dir = True
        from backend import SSHBackend
        parts = args.ssh.split("@", 1)
        if len(parts) == 2:
            user, host = parts
        else:
            user, host = None, parts[0]

        remote_dir = args.dir if args.dir != "." else "/home/" + (user or "root")

        try:
            _state._backend = SSHBackend(
                host=host,
                working_directory=remote_dir,
                user=user,
                key_path=args.key,
                port=args.ssh_port,
            )
            _state._working_directory = remote_dir
            print(f"\n  Bedrock Codex — Web GUI (SSH Remote)")
            print(f"  http://{args.host}:{args.port}")
            print(f"  Remote: {args.ssh}:{remote_dir}\n")
        except Exception as e:
            print(f"\n  SSH connection failed: {e}\n")
            raise SystemExit(1)
    else:
        # Local mode
        # If --dir was explicitly passed (not default "."), skip the welcome screen
        _state._explicit_dir = args.dir != "."

        if _state._explicit_dir and not os.path.isdir(_state._working_directory):
            print(f"\n  Error: directory not found: {_state._working_directory}")
            print(f"  Hint: use the full path, e.g. --dir ~/Desktop/my-project")
            print(f"        or run from inside the project with --dir .\n")
            raise SystemExit(1)

        _state._backend = LocalBackend(_state._working_directory)
        print(f"\n  Bedrock Codex — Web GUI")
        print(f"  http://{args.host}:{args.port}")
        if _state._explicit_dir:
            print(f"  Working directory: {_state._working_directory}")
        else:
            print(f"  Welcome screen enabled — select a project in the browser")
        print()

    # Ensure our app logs (e.g. terminal ws) are visible; uvicorn's log_level only affects its own loggers
    web_log = logging.getLogger("web")
    web_log.setLevel(logging.INFO)
    if not web_log.handlers:
        h = logging.StreamHandler()
        h.setLevel(logging.INFO)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [web] %(message)s"))
        web_log.addHandler(h)

    from web import app
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")