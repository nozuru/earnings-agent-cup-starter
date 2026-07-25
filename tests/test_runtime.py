from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from eac.api import ApiError
from eac.runtime import (
    ROOT,
    AlreadySubmitted,
    _raise_if_submitted,
    build_prompt,
    codex_output_schema,
    detach_submission_secret,
    extract_json_object,
    load_system_prompt,
    parse_prompt_args,
)
from codex_agent.main import _codex_config_overrides, _codex_environment
from claude_agent.main import _mcp_config
from eac.momonga_mcp import _rewrite_initialize_response


class _FakeClient:
    """verify() の応答だけを差し替えた最小のスタブ。"""

    def __init__(self, *, verify_status: int | None, token: str = "eac_test") -> None:
        self.token = token
        self.verify_status = verify_status
        self.verified: list[str] = []

    def verify(self, target_date: str) -> dict[str, object]:
        self.verified.append(target_date)
        if self.verify_status is not None:
            raise ApiError("stub", status=self.verify_status)
        return {"target_date": target_date, "orders": []}


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

    def test_scheduled_run_stops_when_the_day_is_already_submitted(self) -> None:
        client = _FakeClient(verify_status=None)
        with self.assertRaises(AlreadySubmitted):
            _raise_if_submitted(client, "2026-07-27")
        self.assertEqual(client.verified, ["2026-07-27"])

    def test_scheduled_run_continues_when_nothing_is_submitted_yet(self) -> None:
        client = _FakeClient(verify_status=404)
        _raise_if_submitted(client, "2026-07-27")
        self.assertEqual(client.verified, ["2026-07-27"])

    def test_submission_check_is_skipped_without_a_token(self) -> None:
        client = _FakeClient(verify_status=None, token="")
        _raise_if_submitted(client, "2026-07-27")
        self.assertEqual(client.verified, [])

    def test_submission_check_surfaces_unexpected_api_errors(self) -> None:
        client = _FakeClient(verify_status=500)
        with self.assertRaises(ApiError):
            _raise_if_submitted(client, "2026-07-27")

    def test_scheduled_runs_opt_in_through_a_flag(self) -> None:
        with patch("sys.argv", ["run", "--skip-if-submitted", "分析して"]):
            args = parse_prompt_args("test")
        self.assertTrue(args.skip_if_submitted)
        self.assertEqual(args.prompt, "分析して")

    def test_manual_runs_may_overwrite_an_existing_submission(self) -> None:
        with patch("sys.argv", ["run"]):
            args = parse_prompt_args("test")
        self.assertFalse(args.skip_if_submitted)

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
