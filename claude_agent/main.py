"""Claude Agent SDK entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

from eac.runtime import (
    CLI_ERRORS,
    AlreadySubmitted,
    AgentRun,
    ROOT,
    detach_submission_secret,
    exit_with_error,
    finalize_run,
    load_system_prompt,
    parse_prompt_args,
    prepare_run,
    print_result,
    resolve_momonga_settings,
)


def parse_args() -> argparse.Namespace:
    return parse_prompt_args(
        "Claude Agent SDKで決算カレンダーの銘柄を分析し、提出用JSONを作成します。"
    )


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

    momonga = resolve_momonga_settings()
    if momonga is not None:
        servers["momonga"] = {
            "type": "stdio",
            "command": "uv",
            "args": [
                "--directory",
                str(momonga.mcp_dir),
                "run",
                "momonga-search-mcp",
            ],
            "env": {"MOMONGA_SEARCH_API_KEY": momonga.api_key},
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
        session = prepare_run(
            "claude", args.prompt, skip_if_submitted=args.skip_if_submitted
        )
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
    except AlreadySubmitted as skipped:
        print(str(skipped))
        return 0
    except CLI_ERRORS as error:
        return exit_with_error(error)


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
