"""Claude Agent SDK entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from eac.api import ApiError, NoOpenEvent, ValidationError
from eac.runtime import (
    AgentRun,
    ROOT,
    detach_submission_secret,
    finalize_run,
    load_system_prompt,
    prepare_run,
    print_result,
)

DEFAULT_PROMPT = (
    "本日の決算銘柄を分析し、決算サプライズの確度とリスクを比較して、"
    "自信のあるものだけで注文判断を出してください。"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Claude Agent SDKで決算銘柄を分析し、Cup提出JSONを作成します。"
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        default=DEFAULT_PROMPT,
        help="自由形式の投資戦略プロンプト",
    )
    return parser.parse_args()


def _mcp_config() -> tuple[dict[str, Any], list[str]]:
    servers: dict[str, Any] = {
        "yfinance": {
            "type": "stdio",
            "command": "uvx",
            "args": ["yfmcp"],
            "alwaysLoad": True,
        }
    }
    allowed_tools = [
        "ToolSearch",
        "WebSearch",
        "WebFetch",
        "mcp__yfinance__*",
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
        servers["momonga"] = {
            "type": "stdio",
            "command": "uv",
            "args": [
                "--directory",
                str(mcp_dir),
                "run",
                "momonga-search-mcp",
            ],
            "env": {"MOMONGA_SEARCH_API_KEY": key},
        }
        allowed_tools.append("mcp__momonga__*")
    return servers, allowed_tools


async def invoke_agent(prompt: str) -> AgentRun:
    servers, allowed_tools = _mcp_config()
    options = ClaudeAgentOptions(
        model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5"),
        system_prompt=load_system_prompt(),
        tools=["ToolSearch", "WebSearch", "WebFetch"],
        mcp_servers=servers,
        strict_mcp_config=True,
        allowed_tools=allowed_tools,
        permission_mode="bypassPermissions",
        max_turns=int(os.getenv("CLAUDE_MAX_TURNS", "30")),
        cwd=ROOT,
        setting_sources=[],
        env={"MCP_TIMEOUT": "120000"},
        load_timeout_ms=180000,
    )

    assistant_text: list[str] = []
    tool_calls: list[str] = []
    final_text = ""
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            assistant_text.extend(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            tool_calls.extend(
                block.name
                for block in message.content
                if isinstance(block, ToolUseBlock)
            )
        if isinstance(message, ResultMessage):
            if message.is_error:
                detail = message.result or "; ".join(message.errors or [])
                raise RuntimeError(f"Claude Agent SDKの実行に失敗しました: {detail}")
            if isinstance(message.structured_output, dict):
                return AgentRun(
                    response=message.structured_output,
                    tool_calls=tuple(tool_calls),
                )
            if message.result:
                final_text = message.result

    response = final_text or "\n".join(assistant_text)
    if not response.strip():
        raise RuntimeError("Claude Agent SDKから最終応答を取得できませんでした。")
    return AgentRun(response=response, tool_calls=tuple(tool_calls))


async def async_main() -> int:
    args = parse_args()
    try:
        session = prepare_run("claude", args.prompt)
        if not os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
            raise RuntimeError(
                "CLAUDE_CODE_OAUTH_TOKEN が未設定です。"
                "`claude setup-token` の結果を .env に設定してください。"
            )
        detach_submission_secret()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        response = await invoke_agent(session.prompt)
        print_result(finalize_run(session, response))
        return 0
    except (ApiError, NoOpenEvent, ValidationError, ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
