#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

cd "$ROOT_DIR"
mkdir -p "$ROOT_DIR/logs"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

unset ANTHROPIC_API_KEY

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "仮想環境がありません。先にこのディレクトリで 'uv sync' を実行してください。" >&2
  exit 1
fi

RUN_LOG="$ROOT_DIR/logs/claude-$(date -u +%Y%m%d).log"
"$PYTHON_BIN" -m claude_agent.main "$@" 2>&1 | tee -a "$RUN_LOG"
exit "${PIPESTATUS[0]}"
