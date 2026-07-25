"""Launch Momonga Search MCP without exposing its API key to Codex."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import TextIO

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def _debug(message: str) -> None:
    path = os.getenv("MOMONGA_MCP_DEBUG_PATH", "").strip()
    if path:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(message + "\n")


def _rewrite_initialize_response(
    line: str,
    request_id: object,
    requested_version: str,
) -> str:
    """Negotiate the client's version around Momonga's fixed future version."""

    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return line
    if (
        not isinstance(payload, dict)
        or payload.get("id") != request_id
        or not isinstance(payload.get("result"), dict)
    ):
        return line
    payload["result"]["protocolVersion"] = requested_version
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"


def _copy_stream(source: TextIO, destination: TextIO) -> None:
    try:
        for line in source:
            destination.write(line)
            destination.flush()
    except (BrokenPipeError, OSError):
        pass


def main() -> None:
    _debug("launcher started")
    root = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else ROOT
    values = dotenv_values(root / ".env")
    key = str(values.get("MOMONGA_SEARCH_API_KEY", "")).strip()
    directory = str(values.get("MOMONGA_MCP_DIR", "")).strip()
    if not key or not directory:
        raise SystemExit(
            "MOMONGA_SEARCH_API_KEY と MOMONGA_MCP_DIR を .env に設定してください。"
        )

    mcp_dir = Path(directory).expanduser().resolve()
    if not mcp_dir.is_dir():
        raise SystemExit(f"MOMONGA_MCP_DIR が見つかりません: {mcp_dir}")

    first_line = sys.stdin.readline()
    if not first_line:
        raise SystemExit("MCP initialize要求を受信できませんでした。")
    try:
        initialize = json.loads(first_line)
        request_id = initialize["id"]
        requested_version = initialize["params"]["protocolVersion"]
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit("最初のMCP要求がinitializeではありません。") from error
    if not isinstance(requested_version, str) or not requested_version:
        raise SystemExit("MCP protocolVersionが不正です。")
    _debug(f"initialize request id={request_id!r} protocol={requested_version!r}")
    print(
        f"Momonga MCP compatibility negotiation: client={requested_version}",
        file=sys.stderr,
    )

    child_environment = dict(os.environ)
    child_environment["MOMONGA_SEARCH_API_KEY"] = key
    process = subprocess.Popen(
        [
            "uv",
            "--directory",
            str(mcp_dir),
            "run",
            "momonga-search-mcp",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=child_environment,
    )
    if process.stdin is None or process.stdout is None or process.stderr is None:
        process.terminate()
        raise SystemExit("Momonga MCPのstdioを作成できませんでした。")

    stderr_thread = threading.Thread(
        target=_copy_stream,
        args=(process.stderr, sys.stderr),
        daemon=True,
    )
    stderr_thread.start()

    try:
        process.stdin.write(first_line)
        process.stdin.flush()
        response_line = process.stdout.readline()
        if not response_line:
            raise RuntimeError("Momonga MCPがinitialize応答前に終了しました。")
        rewritten = _rewrite_initialize_response(
            response_line,
            request_id,
            requested_version,
        )
        try:
            response = json.loads(rewritten)
            result = response.get("result", {})
            _debug(
                "initialize response "
                f"protocol={result.get('protocolVersion')!r} "
                f"capabilities={sorted(result.get('capabilities', {}))!r}"
            )
        except (AttributeError, json.JSONDecodeError):
            _debug("initialize response could not be parsed")
        sys.stdout.write(rewritten)
        sys.stdout.flush()

        stdout_thread = threading.Thread(
            target=_copy_stream,
            args=(process.stdout, sys.stdout),
            daemon=True,
        )
        stdout_thread.start()
        _copy_stream(sys.stdin, process.stdin)
        process.stdin.close()
        return_code = process.wait()
        stdout_thread.join(timeout=1)
        if return_code:
            raise SystemExit(return_code)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
