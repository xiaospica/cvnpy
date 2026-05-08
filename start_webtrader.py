"""Start vnpy_webtrader HTTP service only.

This script is intentionally small: it does not create a vn.py MainEngine and
does not load strategies. It only starts the FastAPI/uvicorn webtrader process
and connects it to an already-running WebTrader RPC server.

Default wiring:
  RPC req: tcp://127.0.0.1:2014
  RPC sub: tcp://127.0.0.1:4102
  HTTP:    http://127.0.0.1:8001

Examples:
  F:/Program_Home/vnpy/python.exe start_webtrader.py
  F:/Program_Home/vnpy/python.exe start_webtrader.py --port 18001 --req tcp://127.0.0.1:12014 --sub tcp://127.0.0.1:14102
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _port_is_free(host: str, port: int) -> bool:
    bind_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex((bind_host, port)) != 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start vnpy_webtrader HTTP uvicorn service only.",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("WEBTRADER_HTTP_HOST", "127.0.0.1"),
        help="HTTP bind host, default: WEBTRADER_HTTP_HOST or 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("WEBTRADER_HTTP_PORT", "8001")),
        help="HTTP port, default: WEBTRADER_HTTP_PORT or 8001.",
    )
    parser.add_argument(
        "--req",
        default=os.getenv("VNPY_WEB_REQ_ADDRESS", "tcp://127.0.0.1:2014"),
        help="RPC request address for vnpy_webtrader, default: tcp://127.0.0.1:2014.",
    )
    parser.add_argument(
        "--sub",
        default=os.getenv("VNPY_WEB_SUB_ADDRESS", "tcp://127.0.0.1:4102"),
        help="RPC subscribe address for vnpy_webtrader, default: tcp://127.0.0.1:4102.",
    )
    parser.add_argument(
        "--node-id",
        default=os.getenv("VNPY_NODE_ID", "local"),
        help="Node id exposed to mlearnweb, default: VNPY_NODE_ID or local.",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Pass --reload to uvicorn for local development.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()

    if not _port_is_free(args.host, args.port):
        print(
            f"[webtrader] HTTP port already in use: {args.host}:{args.port}",
            file=sys.stderr,
            flush=True,
        )
        return 2

    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "VNPY_WEB_REQ_ADDRESS": args.req,
        "VNPY_WEB_SUB_ADDRESS": args.sub,
        "VNPY_NODE_ID": args.node_id,
    }
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    cmd = [
        sys.executable,
        "-u",
        "-m",
        "uvicorn",
        "vnpy_webtrader.web:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]
    if args.reload:
        cmd.append("--reload")

    print(f"[webtrader] repo: {ROOT}", flush=True)
    print(f"[webtrader] RPC req={args.req} sub={args.sub}", flush=True)
    print(f"[webtrader] HTTP http://{args.host}:{args.port}", flush=True)
    print("[webtrader] stop with Ctrl+C", flush=True)

    try:
        return subprocess.call(cmd, cwd=str(ROOT), env=env)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
