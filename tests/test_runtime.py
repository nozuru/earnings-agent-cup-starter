from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from eac.runtime import (
    ROOT,
    build_prompt,
    codex_output_schema,
    detach_submission_secret,
    extract_json_object,
    load_system_prompt,
)
from codex_agent.main import _codex_config_overrides, _codex_environment
from claude_agent.main import _mcp_config
from eac.momonga_mcp import _rewrite_initialize_response


class RuntimeTests(unittest.TestCase):
    def test_extracts_fenced_json(self) -> None:
        result = extract_json_object(
            """調査結果です。
```json
{"orders": [], "summary": "見送り"}
```
"""
        )
        self.assertEqual(result["orders"], [])

    def test_extracts_json_after_prose(self) -> None:
        result = extract_json_object(
            '判断: {"orders":[{"code":"72030","weight_bps":1000,'
            '"reason":"test"}],"summary":"long"}'
        )
        self.assertEqual(result["orders"][0]["code"], "72030")

    def test_prompt_contains_only_published_targets(self) -> None:
        prompt = build_prompt(
            "分析して",
            {
                "target_date": "2026-07-27",
                "deadline_at": "2026-07-27T08:00:00Z",
                "rule_version": "2026-v2",
                "events": [
                    {
                        "code": "72030",
                        "company_name": "Example",
                        "status": "published",
                    },
                    {
                        "code": "99990",
                        "company_name": "Cancelled",
                        "status": "cancelled",
                    },
                ],
            },
            {"type": "object"},
        )
        self.assertIn("72030", prompt)
        self.assertNotIn("99990", prompt)

    def test_codex_schema_keeps_structure_and_drops_unsupported_constraints(
        self,
    ) -> None:
        schema = codex_output_schema(
            {
                "$schema": "draft",
                "type": "integer",
                "minimum": -2000,
                "not": {"type": "integer", "const": 0},
            }
        )
        self.assertEqual(schema, {"type": "integer"})

    def test_agents_guidance_contains_shared_system_prompt(self) -> None:
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(load_system_prompt(), agents)

    def test_submission_token_is_removed_before_agent_launch(self) -> None:
        with patch.dict(os.environ, {"EAC_API_TOKEN": "secret"}):
            detach_submission_secret()
            self.assertNotIn("EAC_API_TOKEN", os.environ)

    def test_codex_environment_excludes_unrelated_secrets(self) -> None:
        with patch.dict(
            os.environ,
            {
                "EAC_API_TOKEN": "eac-secret",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
                "MOMONGA_SEARCH_API_KEY": "momonga-secret",
            },
            clear=True,
        ):
            environment = _codex_environment()
        self.assertNotIn("EAC_API_TOKEN", environment)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
        self.assertNotIn("MOMONGA_SEARCH_API_KEY", environment)

    def test_codex_configures_web_and_yfinance_without_global_config(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            overrides = _codex_config_overrides()
        self.assertIn('web_search="live"', overrides)
        self.assertIn('mcp_servers.yfinance.command="uvx"', overrides)

    def test_yfinance_tools_are_loaded_eagerly_for_claude(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            servers, allowed = _mcp_config()
        self.assertTrue(servers["yfinance"]["alwaysLoad"])
        self.assertIn("ToolSearch", allowed)
        self.assertIn("mcp__yfinance__*", allowed)

    def test_momonga_launcher_negotiates_requested_protocol_version(self) -> None:
        rewritten = _rewrite_initialize_response(
            '{"jsonrpc":"2.0","id":7,"result":'
            '{"protocolVersion":"2025-11-25","capabilities":{}}}\n',
            7,
            "2025-06-18",
        )
        self.assertEqual(
            json.loads(rewritten)["result"]["protocolVersion"],
            "2025-06-18",
        )


if __name__ == "__main__":
    unittest.main()
