#!/usr/bin/env bash
# Resilient runner for OpenCode phase work.
#
# Why this exists: the free DeepSeek tier rate-limits ("AI_APICallError: Rate limit
# exceeded"). When that happens mid-stream, `opencode run` neither retries nor exits --
# it hangs forever with no output. One phase was lost to a 2-hour silent stall.
#
# This wrapper detects the stall and retries with --continue, so the agent resumes the
# same session instead of starting over.
#
# Usage:  ./scripts/run_agent.sh <prompt-file> <log-file> [max-attempts]
#
# Env:
#   NYX_MODEL      model id           (default opencode/deepseek-v4.1-flash-free)
#   NYX_STALL_SECS seconds of no log growth before we call it stalled (default 420)

set -uo pipefail

PROMPT_FILE="${1:?usage: run_agent.sh <prompt-file> <log-file> [max-attempts]}"
LOG_FILE="${2:?usage: run_agent.sh <prompt-file> <log-file> [max-attempts]}"
MAX_ATTEMPTS="${3:-3}"
MODEL="${NYX_MODEL:-opencode/deepseek-v4.1-flash-free}"
STALL_SECS="${NYX_STALL_SECS:-420}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# Native tools (opencode.exe) do NOT understand MSYS paths like /d/NYX -- they resolve
# them relative to the current drive, silently creating D:\d\NYX instead. Ask bash for the
# Windows form. Fall back to the POSIX form if pwd -W is unavailable.
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd -W 2>/dev/null)"
[ -n "$REPO_DIR" ] || REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_DIR" || exit 1

# Guard against the wrong-directory class of bug: refuse to run anywhere that is not the repo.
if [ ! -f "$REPO_DIR/pyproject.toml" ]; then
  echo "run_agent: $REPO_DIR does not look like the NYX repo (no pyproject.toml)" >&2
  exit 2
fi

if [ ! -f "$PROMPT_FILE" ]; then
  echo "run_agent: prompt file not found: $PROMPT_FILE" >&2
  exit 2
fi

size_of() { stat -c %s "$1" 2>/dev/null || echo 0; }

kill_opencode() {
  MSYS_NO_PATHCONV=1 taskkill /F /IM opencode.exe >/dev/null 2>&1 || true
  sleep 3
}

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  if [ "$attempt" -eq 1 ]; then
    echo "=== attempt $attempt/$MAX_ATTEMPTS : fresh session ==="
    continue_flag=""
  else
    echo "=== attempt $attempt/$MAX_ATTEMPTS : resuming last session (--continue) ==="
    continue_flag="--continue"
  fi

  # shellcheck disable=SC2086
  opencode run --auto $continue_flag -m "$MODEL" --dir "$REPO_DIR" \
      "$(cat "$PROMPT_FILE")" >>"$LOG_FILE" 2>&1 &
  agent_pid=$!

  last_size=$(size_of "$LOG_FILE")
  stalled_for=0

  while kill -0 "$agent_pid" 2>/dev/null; do
    sleep 30
    now_size=$(size_of "$LOG_FILE")
    if [ "$now_size" -eq "$last_size" ]; then
      stalled_for=$((stalled_for + 30))
      echo "   [watchdog] no output for ${stalled_for}s"
    else
      stalled_for=0
      last_size=$now_size
    fi

    if [ "$stalled_for" -ge "$STALL_SECS" ]; then
      echo "   [watchdog] STALLED ${stalled_for}s -- killing and retrying"
      kill_opencode
      break
    fi
  done

  # Did it finish cleanly?
  if ! kill -0 "$agent_pid" 2>/dev/null; then
    wait "$agent_pid"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "=== attempt $attempt finished cleanly (exit 0) ==="
      exit 0
    fi
    echo "=== attempt $attempt exited rc=$rc ==="
  else
    kill_opencode
  fi

  attempt=$((attempt + 1))
done

echo "=== all $MAX_ATTEMPTS attempts exhausted; see $LOG_FILE ===" >&2
exit 1
