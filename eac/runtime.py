"""Shared deterministic runner around the two model SDKs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .api import (
    DEFAULT_BASE_URL,
    ApiError,
    Client,
    NoOpenEvent,
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
DEFAULT_PROMPT = (
    "本日の決算銘柄を分析し、決算サプライズの確度とリスクを比較して、"
    "自信のあるものだけで注文判断を出してください。"
)
CLI_ERRORS = (ApiError, NoOpenEvent, ValidationError, ValueError, RuntimeError)


class AlreadySubmitted(Exception):
    """その日の提出がすでにあるため実行を見送った。失敗ではない。"""


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


@dataclass(frozen=True)
class MomongaSettings:
    api_key: str
    mcp_dir: Path


def prepare_run(
    track: str,
    user_prompt: str,
    *,
    skip_if_submitted: bool = False,
) -> RunSession:
    """Load local configuration, fetch targets, and build the model prompt."""

    load_dotenv(ROOT / ".env", override=False)
    client = Client(
        token=os.getenv("EAC_API_TOKEN", ""),
        base_url=os.getenv("EAC_BASE_URL", DEFAULT_BASE_URL),
    )
    event = client.open_event()
    target_date = str(event.get("target_date", ""))
    if skip_if_submitted and _has_submission(client, target_date):
        raise AlreadySubmitted(
            f"{target_date} はすでに提出済みです。モデルを起動せずに終了します。"
        )
    schema = load_schema()
    prompt = build_prompt(user_prompt, event, schema)

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    prefix = LOGS_DIR / f"{timestamp}-{track}-{target_date or 'unknown'}"
    return RunSession(track, client, event, schema, prompt, prefix)


def _has_submission(client: Client, target_date: str) -> bool:
    """verify が提出を見つけたら True。404 は未提出。トークン無しは確認しない。"""

    if not client.token or not target_date:
        return False
    try:
        client.verify(target_date)
    except ApiError as error:
        if error.status == 404:
            return False
        raise
    return True


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
    _write_text(_artifact_path(session, "raw.txt"), raw_text)

    try:
        parsed = (
            raw_response
            if isinstance(raw_response, dict)
            else extract_json_object(raw_response)
        )
        allowed_codes, shortable_codes = order_constraints(session.event)
        validated = validate_output(
            parsed, allowed_codes=allowed_codes, shortable_codes=shortable_codes
        )
    except (ValueError, ValidationError) as error:
        _write_text(_artifact_path(session, "error.txt"), str(error))
        raise

    analysis_path = _artifact_path(session, "analysis.json")
    orders_path = _artifact_path(session, "orders.json")
    tools_path = _artifact_path(session, "tools.json")
    _write_json(analysis_path, validated)
    _write_json(orders_path, {"orders": validated["orders"]})
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
    submission_path = _artifact_path(session, "submission.json")
    _write_submission(submission_path, accepted=accepted)
    try:
        confirmed = session.client.verify(target_date)
    except ApiError as error:
        _write_submission(
            submission_path,
            accepted=accepted,
            verification_error=str(error),
        )
        raise
    if _sorted_orders(confirmed.get("orders")) != _sorted_orders(
        accepted.get("orders")
    ):
        _write_submission(
            submission_path,
            accepted=accepted,
            verified=confirmed,
            verification_error="受理内容と照合結果が一致しません。",
        )
        raise ApiError("提出後の照合結果が受理内容と一致しません。")

    _write_submission(
        submission_path,
        accepted=accepted,
        verified=confirmed,
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
        for item in published_events(event)
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


def published_events(event: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in event.get("events", [])
        if isinstance(item, dict) and item.get("status") == "published"
    ]


def order_constraints(
    event: dict[str, Any],
) -> tuple[set[str], set[str]]:
    """Return allowed and shortable codes for local validation."""

    published = published_events(event)
    allowed_codes = {
        str(item.get("code", "")).strip().upper() for item in published
    }
    shortable_codes = {
        str(item.get("code", "")).strip().upper()
        for item in published
        if item.get("shortable") is True
    }
    return allowed_codes, shortable_codes


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


def resolve_momonga_settings() -> MomongaSettings | None:
    """Resolve optional Momonga Search settings shared by both agent tracks."""

    key = os.getenv("MOMONGA_SEARCH_API_KEY", "").strip()
    directory = os.getenv("MOMONGA_MCP_DIR", "").strip()
    if not key and not directory:
        return None
    if not key or not directory:
        raise ValueError(
            "Momonga Searchを使うには MOMONGA_SEARCH_API_KEY と "
            "MOMONGA_MCP_DIR の両方を設定してください。"
        )
    mcp_dir = Path(directory).expanduser().resolve()
    if not mcp_dir.is_dir():
        raise ValueError(f"MOMONGA_MCP_DIR が見つかりません: {mcp_dir}")
    return MomongaSettings(api_key=key, mcp_dir=mcp_dir)


def parse_prompt_args(description: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "prompt",
        nargs="?",
        default=DEFAULT_PROMPT,
        help="自由形式の投資戦略プロンプト",
    )
    parser.add_argument(
        "--skip-if-submitted",
        action="store_true",
        help="その日の提出がすでにあればモデルを起動せずに終了する（定期実行向け）",
    )
    return parser.parse_args()


def exit_with_error(error: BaseException) -> int:
    print(str(error), file=sys.stderr)
    return 1


def exit_skipped(message: BaseException | str) -> int:
    """成功扱いの早期終了（提出済みスキップなど）。stdout に理由を出して 0 を返す。"""

    print(str(message))
    return 0


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


def _artifact_path(session: RunSession, suffix: str) -> Path:
    return session.artifact_prefix.with_name(
        f"{session.artifact_prefix.name}-{suffix}"
    )


def _write_submission(
    path: Path,
    *,
    accepted: dict[str, Any],
    verified: dict[str, Any] | None = None,
    verification_error: str | None = None,
) -> None:
    payload: dict[str, Any] = {"accepted": accepted, "verified": verified}
    if verification_error is not None:
        payload["verification_error"] = verification_error
    _write_json(path, payload)


def _sorted_orders(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    clean = [item for item in value if isinstance(item, dict)]
    return sorted(clean, key=lambda item: str(item.get("code", "")))


def _write_json(path: Path, value: Any) -> None:
    _write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _write_text(path: Path, value: str) -> None:
    path.write_text(value if value.endswith("\n") else value + "\n", encoding="utf-8")
