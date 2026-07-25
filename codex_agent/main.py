"""OpenAI Codex Python SDK entry point."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from openai_codex import ApprovalMode, Codex, CodexConfig, Sandbox

from eac.api import ApiError, NoOpenEvent, ValidationError
from eac.runtime import (
    AgentRun,
    ROOT,
    codex_output_schema,
    detach_submission_secret,
    finalize_run,
    prepare_run,
    print_result,
)

DEFAULT_PROMPT = (
    "本日の決算銘柄を分析し、決算サプライズの確度とリスクを比較して、"
    "自信のあるものだけで注文判断を出してください。"
)
CODEX_ENV_ALLOWLIST = {
    "CODEX_ACCESS_TOKEN",
    "CODEX_HOME",
    "CODEX_MODEL",
    "HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "NO_PROXY",
    "NODE_EXTRA_CA_CERTS",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SHELL",
    "SSL_CERT_FILE",
    "TMPDIR",
    "USER",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Codex SDKで決算銘柄を分析し、Cup提出JSONを作成します。"
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=DEFAULT_PROMPT,
        help="自由形式の投資戦略プロンプト",
    )
    return parser.parse_args()


def invoke_agent(prompt: str, schema: dict[str, object]) -> AgentRun:
    with tempfile.TemporaryDirectory(prefix="eac-codex-") as workspace:
        guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        Path(workspace, "AGENTS.md").write_text(guidance, encoding="utf-8")
        shutil.copyfile(
            ROOT / "eac" / "momonga_mcp.py",
            Path(workspace, "momonga_mcp.py"),
        )
        config = CodexConfig(
            cwd=workspace,
            env=_codex_environment(),
            config_overrides=_codex_config_overrides(workspace),
        )
        with _sanitized_process_environment(), Codex(config) as codex:
            thread = codex.thread_start(
                model=os.getenv("CODEX_MODEL", "gpt-5.6-luna"),
                cwd=workspace,
                approval_mode=ApprovalMode.deny_all,
                sandbox=Sandbox.read_only,
                ephemeral=True,
            )
            result = thread.run(
                prompt,
                effort="low",
                output_schema=codex_output_schema(schema),
            )
    if not result.final_response or not result.final_response.strip():
        raise RuntimeError("Codex SDKから最終応答を取得できませんでした。")
    return AgentRun(
        response=result.final_response,
        tool_calls=tuple(_codex_tool_calls(result.items)),
    )


def _codex_config_overrides(workspace: str | None = None) -> tuple[str, ...]:
    """Configure all research tools without mutating the user's Codex config."""

    overrides = [
        'web_search="live"',
        'mcp_servers.yfinance.command="uvx"',
        'mcp_servers.yfinance.args=["yfmcp"]',
        "mcp_servers.yfinance.startup_timeout_sec=120",
        "mcp_servers.yfinance.tool_timeout_sec=120",
    ]
    key = os.getenv("MOMONGA_SEARCH_API_KEY", "").strip()
    directory = os.getenv("MOMONGA_MCP_DIR", "").strip()
    if key or directory:
        if not key or not directory:
            raise ValueError(
                "Momonga Searchを使うには MOMONGA_SEARCH_API_KEY と "
                "MOMONGA_MCP_DIR の両方を設定してください。"
            )
        mcp_dir = Path(directory).expanduser().resolve()
        if not mcp_dir.is_dir():
            raise ValueError(f"MOMONGA_MCP_DIR が見つかりません: {mcp_dir}")
        launcher = str(
            (
                Path(workspace, "momonga_mcp.py")
                if workspace
                else ROOT / "eac" / "momonga_mcp.py"
            ).resolve()
        )
        overrides.extend(
            [
                'mcp_servers.momonga.command="uv"',
                "mcp_servers.momonga.args="
                f"{
                    json.dumps(
                        [
                            'run',
                            '--project',
                            str(ROOT),
                            'python',
                            launcher,
                            str(ROOT),
                        ]
                    )
                }",
                "mcp_servers.momonga.startup_timeout_sec=120",
                "mcp_servers.momonga.tool_timeout_sec=120",
            ]
        )
        if not Path(launcher).is_file():
            raise ValueError(f"Momongaランチャーが見つかりません: {launcher}")
    return tuple(overrides)


def _codex_tool_calls(items: list[object]) -> list[str]:
    calls: list[str] = []
    for item in items:
        root = getattr(item, "root", item)
        kind = type(root).__name__
        if kind == "WebSearchThreadItem":
            calls.append("web_search")
        elif kind == "McpToolCallThreadItem":
            calls.append(
                f"mcp__{getattr(root, 'server', 'unknown')}__"
                f"{getattr(root, 'tool', 'unknown')}"
            )
        elif kind == "DynamicToolCallThreadItem":
            calls.append(
                f"{getattr(root, 'namespace', 'dynamic')}__"
                f"{getattr(root, 'tool', 'unknown')}"
            )
    return calls


def _codex_environment() -> dict[str, str]:
    """Give the SDK only auth/runtime variables, not unrelated local secrets."""

    return _codex_environment_from(dict(os.environ))


@contextmanager
def _sanitized_process_environment():
    """Prevent the SDK's inherited environment from exposing unrelated secrets."""

    original = dict(os.environ)
    os.environ.clear()
    os.environ.update(_codex_environment_from(original))
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def _codex_environment_from(values: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in values.items() if key in CODEX_ENV_ALLOWLIST}


def main() -> int:
    args = parse_args()
    try:
        session = prepare_run("codex", args.prompt)
        detach_submission_secret()
        response = invoke_agent(session.prompt, session.schema)
        print_result(finalize_run(session, response))
        return 0
    except (ApiError, NoOpenEvent, ValidationError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
