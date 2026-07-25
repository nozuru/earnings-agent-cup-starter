"""Earnings Agent Cup API client and local portfolio validation."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

DEFAULT_BASE_URL = "https://earnings.jpsi-association.com"
CODE_PATTERN = re.compile(r"^[0-9A-Z]{4,5}$")
MAX_SINGLE_WEIGHT_BPS = 2_000
MAX_GROSS_WEIGHT_BPS = 10_000
MAX_REASON_LENGTH = 500


class ApiError(RuntimeError):
    """The Cup API rejected a request or could not be reached."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class NoOpenEvent(ApiError):
    """There is no event accepting submissions right now."""


class ValidationError(ValueError):
    """The agent output does not satisfy the local submission contract."""

    def __init__(self, errors: list[str]):
        super().__init__("\n".join(f"- {error}" for error in errors))
        self.errors = errors


class Client:
    """Small dependency-free client for the public Cup API."""

    def __init__(
        self,
        *,
        token: str = "",
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30,
    ):
        self.token = token.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def open_event(self) -> dict[str, Any]:
        try:
            return self._request("GET", "/api/events/open")
        except ApiError as error:
            if error.status == 404:
                raise NoOpenEvent(
                    "現在、受付中の銘柄リストはありません。"
                ) from error
            raise

    def submit(self, target_date: str, orders: list[dict[str, Any]]) -> dict[str, Any]:
        self._require_token()
        return self._request(
            "PUT",
            f"/api/portfolios/{urllib.parse.quote(target_date, safe='')}",
            payload={"orders": orders},
            retries=3,
        )

    def verify(self, target_date: str) -> dict[str, Any]:
        self._require_token()
        query = urllib.parse.urlencode({"date": target_date})
        return self._request("GET", f"/api/portfolios/me?{query}", retries=3)

    def _require_token(self) -> None:
        if not self.token:
            raise ApiError(
                "EAC_API_TOKEN が設定されていません。提出せずローカル保存します。"
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        retries: int = 1,
    ) -> dict[str, Any]:
        body = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            if payload is not None
            else None
        )
        headers = {
            "Accept": "application/json",
            "User-Agent": "eac-starter-kit/2026-v1",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        for attempt in range(retries):
            request = urllib.request.Request(
                f"{self.base_url}{path}",
                data=body,
                headers=headers,
                method=method,
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    decoded = response.read().decode("utf-8")
                    loaded = json.loads(decoded)
                    if not isinstance(loaded, dict):
                        raise ApiError(
                            "Cup APIの応答がJSONオブジェクトではありません。"
                        )
                    return loaded
            except urllib.error.HTTPError as error:
                message = _http_error_message(error)
                retryable = error.code == 429 or error.code >= 500
                if not retryable or attempt + 1 == retries:
                    raise ApiError(message, status=error.code) from error
            except urllib.error.URLError as error:
                if attempt + 1 == retries:
                    raise ApiError(
                        f"Cup APIへの接続に失敗しました: {error.reason}"
                    ) from error
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ApiError("Cup APIから不正なJSON応答を受信しました。") from error

            time.sleep(2**attempt)

        raise ApiError("Cup APIへのリクエストに失敗しました。")


def validate_output(value: Any, *, allowed_codes: set[str]) -> dict[str, Any]:
    """Validate and normalize an agent response before any network write."""

    errors: list[str] = []
    if not isinstance(value, dict):
        raise ValidationError(["出力はJSONオブジェクトで指定してください。"])

    unexpected = set(value) - {"orders", "summary"}
    if unexpected:
        errors.append(f"未定義のキーがあります: {', '.join(sorted(unexpected))}")

    summary = value.get("summary")
    if not isinstance(summary, str):
        errors.append("summary は文字列で指定してください。")

    raw_orders = value.get("orders")
    if not isinstance(raw_orders, list):
        errors.append("orders は配列で指定してください。")
        raw_orders = []

    orders: list[dict[str, Any]] = []
    seen: set[str] = set()
    gross_weight_bps = 0

    for index, raw in enumerate(raw_orders):
        label = f"orders[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label} はオブジェクトで指定してください。")
            continue

        extra = set(raw) - {"code", "weight_bps", "reason"}
        if extra:
            errors.append(
                f"{label} に未定義のキーがあります: {', '.join(sorted(extra))}"
            )

        code_value = raw.get("code")
        code = code_value.strip().upper() if isinstance(code_value, str) else ""
        weight = raw.get("weight_bps")
        reason_value = raw.get("reason")
        reason = reason_value.strip() if isinstance(reason_value, str) else ""

        code_valid = bool(CODE_PATTERN.fullmatch(code))
        if not code_valid:
            errors.append(f"{label}.code は4〜5桁の銘柄コードで指定してください。")
        elif code in seen:
            errors.append(f"銘柄 {code} が重複しています。")
        else:
            seen.add(code)
            if code not in allowed_codes:
                errors.append(f"銘柄 {code} は本日決算を発表しません。")

        weight_valid = type(weight) is int and weight != 0
        if not weight_valid:
            errors.append(f"{label}.weight_bps は0以外の整数で指定してください。")
        else:
            gross_weight_bps += abs(weight)
            if abs(weight) > MAX_SINGLE_WEIGHT_BPS:
                errors.append(f"銘柄 {code or index} の比率が20%を超えています。")

        if not reason:
            errors.append(f"{label}.reason は1文字以上で指定してください。")
        elif len(reason) > MAX_REASON_LENGTH:
            errors.append(
                f"{label}.reason は{MAX_REASON_LENGTH}文字以内で指定してください。"
            )

        if code_valid and weight_valid and reason:
            orders.append(
                {
                    "code": code,
                    "weight_bps": weight,
                    "reason": reason,
                }
            )

    if gross_weight_bps > MAX_GROSS_WEIGHT_BPS:
        errors.append(
            "グロス投資比率が"
            f"{gross_weight_bps / 100:.2f}%です。100%以内にしてください。"
        )

    if errors:
        raise ValidationError(errors)

    return {
        "orders": orders,
        "summary": summary,
    }


def ensure_submission_open(event: dict[str, Any]) -> None:
    """Refuse a local submission after the advertised deadline."""

    if event.get("is_open") is False:
        raise ApiError("対象イベントの受付は終了しています。提出しません。")

    raw_deadline = event.get("deadline_at")
    if not isinstance(raw_deadline, str):
        raise ApiError("対象イベントの締切時刻を取得できません。提出しません。")

    try:
        deadline = datetime.fromisoformat(raw_deadline.replace("Z", "+00:00"))
    except ValueError as error:
        raise ApiError("対象イベントの締切時刻が不正です。提出しません。") from error
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)

    if datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc):
        raise ApiError(f"提出締切（{raw_deadline}）を過ぎています。提出しません。")


def _http_error_message(error: urllib.error.HTTPError) -> str:
    raw = error.read().decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
        detail = payload.get("error", {}) if isinstance(payload, dict) else {}
        message = detail.get("message") if isinstance(detail, dict) else None
        details = detail.get("details") if isinstance(detail, dict) else None
        if isinstance(message, str):
            suffix = ""
            if isinstance(details, list):
                clean = [item for item in details if isinstance(item, str)]
                if clean:
                    suffix = " " + " / ".join(clean)
            return f"Cup API HTTP {error.code}: {message}{suffix}"
    except json.JSONDecodeError:
        pass
    return f"Cup API HTTP {error.code}: {raw[:500]}"
