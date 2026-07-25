from __future__ import annotations

import json
import unittest
import urllib.error
from io import BytesIO
from unittest.mock import patch

from eac.api import ApiError, Client, ValidationError, validate_output


class FakeResponse:
    def __init__(self, payload: dict[str, object]):
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class ValidationTests(unittest.TestCase):
    def test_accepts_valid_long_short_output(self) -> None:
        result = validate_output(
            {
                "orders": [
                    {"code": "72030", "weight_bps": 2000, "reason": "long"},
                    {"code": "99840", "weight_bps": -1500, "reason": "short"},
                ],
                "summary": "balanced",
            },
            allowed_codes={"72030", "99840"},
        )
        self.assertEqual(result["orders"][0]["code"], "72030")

    def test_accepts_empty_orders(self) -> None:
        self.assertEqual(
            validate_output(
                {"orders": [], "summary": "ノートレード"},
                allowed_codes={"72030"},
            ),
            {"orders": [], "summary": "ノートレード"},
        )

    def test_rejects_rule_breaches(self) -> None:
        with self.assertRaises(ValidationError) as context:
            validate_output(
                {
                    "orders": [
                        {
                            "code": "72030",
                            "weight_bps": 2001,
                            "reason": "first",
                        },
                        {
                            "code": "72030",
                            "weight_bps": 2000,
                            "reason": "duplicate",
                        },
                        {
                            "code": "99840",
                            "weight_bps": -7000,
                            "reason": "gross",
                        },
                    ],
                    "summary": "invalid",
                },
                allowed_codes={"72030"},
            )
        combined = " ".join(context.exception.errors)
        self.assertIn("重複", combined)
        self.assertIn("20%", combined)
        self.assertIn("100%", combined)
        self.assertIn("対象銘柄", combined)


class ClientTests(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_submit_uses_bearer_auth_and_orders_only(self, urlopen: object) -> None:
        urlopen.return_value = FakeResponse({"accepted": True})  # type: ignore[attr-defined]
        client = Client(token="secret", base_url="https://example.test")

        result = client.submit(
            "2026-07-27",
            [{"code": "72030", "weight_bps": 1000, "reason": "test"}],
        )

        self.assertTrue(result["accepted"])
        request = urlopen.call_args.args[0]  # type: ignore[attr-defined]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(
            json.loads(request.data),
            {"orders": [{"code": "72030", "weight_bps": 1000, "reason": "test"}]},
        )

    @patch("urllib.request.urlopen")
    def test_surfaces_api_error_message(self, urlopen: object) -> None:
        urlopen.side_effect = urllib.error.HTTPError(  # type: ignore[attr-defined]
            "https://example.test/api/events/open",
            422,
            "unprocessable",
            {},
            BytesIO(
                json.dumps({"error": {"message": "締切を過ぎています。"}}).encode()
            ),
        )
        with self.assertRaisesRegex(ApiError, "締切"):
            Client(base_url="https://example.test").open_event()


if __name__ == "__main__":
    unittest.main()
