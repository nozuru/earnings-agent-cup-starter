"""Shared deterministic runner around the two model SDKs."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .api import (
    ApiError,
    Client,
    ValidationError,
    ensure_submission_open,
    validate_output,
)

ROOT = Path(__file__).resolve().parents[1]
LOGS_DIR = ROOT / "logs"
SCHEMA_PATH = ROOT / "schemas" / "order.schema.json"
SYSTEM_PROMPT_PATH = ROOT / "prompts" / "system.md"
JSON_FENCE_PATTERN = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL
)
CODEX_UNSUPPORTED_SCHEMA_KEYS = {
    "$schema",
    "$id",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "not",
}


@dataclass(frozen=True)
class RunSession:
    track: str
    client: Client
    event: dict[str, Any]
    schema: dict[str, Any]
    prompt: str
    artifact_prefix: Path


@dataclass(frozen=True)
class AgentRun:
    """Model response plus the tools observed by its SDK."""

    response: str | dict[str, Any]
    tool_calls: tuple[str, ...] = ()


def prepare_run(track: str, user_prompt: str) -> RunSession:
    """Load local configuration, fetch targets, and build the model prompt."""

    load_dotenv(ROOT / ".env", override=False)
    client = Client(
        token=os.getenv("EAC_API_TOKEN", ""),
        base_url=os.getenv("EAC_BASE_URL", "https://earnings.jpsi-association.com"),
    )
    event = client.open_event()
    schema = load_schema()
    prompt = build_prompt(user_prompt, event, schema)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_date = str(event.get("target_date", "unknown"))
    prefix = LOGS_DIR / f"{timestamp}-{track}-{target_date}"
    return RunSession(track, client, event, schema, prompt, prefix)


def finalize_run(
    session: RunSession,
    raw_response: AgentRun | str | dict[str, Any],
) -> dict[str, Any]:
    """Extract, validate, save, and optionally submit a model response."""

    agent_run = (
        raw_response
        if isinstance(raw_response, AgentRun)
        else AgentRun(response=raw_response)
    )
    raw_response = agent_run.response
    raw_text = (
        json.dumps(raw_response, ensure_ascii=False, indent=2)
        if isinstance(raw_response, dict)
        else raw_response
    )
    _write_text(
        session.artifact_prefix.with_name(f"{session.artifact_prefix.name}-raw.txt"),
        raw_text,
    )

    try:
        parsed = (
            raw_response
            if isinstance(raw_response, dict)
            else extract_json_object(raw_response)
        )
        published = [
            event
            for event in session.event.get("events", [])
            if isinstance(event, dict) and event.get("status") == "published"
        ]
        allowed_codes = {
            str(event.get("code", "")).strip().upper() for event in published
        }
        # 旧APIの応答にはshortableが無い。その場合はサーバー判定に任せる。
        shortable_codes = (
            {
                str(event.get("code", "")).strip().upper()
                for event in published
                if event.get("shortable") is True
            }
            if any("shortable" in event for event in published)
            else None
        )
        validated = validate_output(
            parsed, allowed_codes=allowed_codes, shortable_codes=shortable_codes
        )
    except (ValueError, ValidationError) as error:
        error_path = session.artifact_prefix.with_name(
            f"{session.artifact_prefix.name}-error.txt"
        )
        _write_text(error_path, str(error))
        raise

    analysis_path = session.artifact_prefix.with_name(
        f"{session.artifact_prefix.name}-analysis.json"
    )
    orders_path = session.artifact_prefix.with_name(
        f"{session.artifact_prefix.name}-orders.json"
    )
    _write_json(analysis_path, validated)
    _write_json(orders_path, {"orders": validated["orders"]})
    tools_path = session.artifact_prefix.with_name(
        f"{session.artifact_prefix.name}-tools.json"
    )
    _write_json(
        tools_path,
        {
            "track": session.track,
            "tool_calls": list(agent_run.tool_calls),
        },
    )

    result: dict[str, Any] = {
        "status": "saved",
        "target_date": session.event.get("target_date"),
        "orders": validated["orders"],
        "summary": validated["summary"],
        "artifacts": {
            "analysis": str(analysis_path),
            "orders": str(orders_path),
            "tools": str(tools_path),
        },
    }
    if not session.client.token:
        result["message"] = (
            "EAC_API_TOKEN が未設定のため提出せず、手動提出用JSONを保存しました。"
        )
        return result

    ensure_submission_open(session.event)
    target_date = str(session.event["target_date"])
    accepted = session.client.submit(target_date, validated["orders"])
    submission_path = session.artifact_prefix.with_name(
        f"{session.artifact_prefix.name}-submission.json"
    )
    _write_json(submission_path, {"accepted": accepted, "verified": None})
    try:
        confirmed = session.client.verify(target_date)
    except ApiError as error:
        _write_json(
            submission_path,
            {
                "accepted": accepted,
                "verified": None,
                "verification_error": str(error),
            },
        )
        raise
    if _sorted_orders(confirmed.get("orders")) != _sorted_orders(
        accepted.get("orders")
    ):
        _write_json(
            submission_path,
            {
                "accepted": accepted,
                "verified": confirmed,
                "verification_error": "受理内容と照合結果が一致しません。",
            },
        )
        raise ApiError("提出後の照合結果が受理内容と一致しません。")

    _write_json(
        submission_path,
        {
            "accepted": accepted,
            "verified": confirmed,
        },
    )
    result.update(
        {
            "status": "submitted",
            "message": "提出と、提出後の内容照合が完了しました。",
            "submission": accepted,
        }
    )
    result["artifacts"]["submission"] = str(submission_path)
    return result


def build_prompt(
    user_prompt: str, event: dict[str, Any], schema: dict[str, Any]
) -> str:
    events = [
        {
            "code": item.get("code"),
            "company_name": item.get("company_name"),
            "market": item.get("market"),
            "sector": item.get("sector"),
            "shortable": item.get("shortable"),
        }
        for item in event.get("events", [])
        if isinstance(item, dict) and item.get("status") == "published"
    ]
    context = {
        "target_date": event.get("target_date"),
        "deadline_at": event.get("deadline_at"),
        "rule_version": event.get("rule_version"),
        "events": events,
    }
    return (
        f"{user_prompt.strip()}\n\n"
        "以下は Earnings Agent Cup のAPIから取得した、今日の対象日・締切・決算カレンダーです。"
        "注文は events に含まれる銘柄だけにしてください。\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "最終出力は次のJSON Schemaに従うJSONオブジェクト1つにしてください。\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}"
    )


def extract_json_object(text: str) -> dict[str, Any]:
    """Find a complete JSON object in a plain or Markdown model response."""

    stripped = text.strip()
    candidates = [stripped]
    candidates.extend(match.group(1) for match in JSON_FENCE_PATTERN.finditer(text))

    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict) and "orders" in loaded:
                return loaded
        except json.JSONDecodeError:
            pass

    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            loaded, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict) and "orders" in loaded:
            return loaded

    raise ValueError("応答から orders を含むJSONオブジェクトを抽出できませんでした。")


def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as stream:
        schema = json.load(stream)
    if not isinstance(schema, dict):
        raise ValueError("order.schema.json がJSONオブジェクトではありません。")
    return schema


def load_system_prompt() -> str:
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


def detach_submission_secret() -> None:
    """Keep the copied API token on Client, not in an agent subprocess env."""

    os.environ.pop("EAC_API_TOKEN", None)


def codex_output_schema(value: Any) -> Any:
    """Keep structural JSON Schema while local validation enforces constraints."""

    if isinstance(value, dict):
        return {
            key: codex_output_schema(item)
            for key, item in value.items()
            if key not in CODEX_UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(value, list):
        return [codex_output_schema(item) for item in value]
    return value


def print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _sorted_orders(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    clean = [item for item in value if isinstance(item, dict)]
    return sorted(clean, key=lambda item: str(item.get("code", "")))


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, value: str) -> None:
    path.write_text(value if value.endswith("\n") else value + "\n", encoding="utf-8")
