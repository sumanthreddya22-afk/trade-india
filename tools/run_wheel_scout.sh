#!/bin/bash
# tools/run_wheel_scout.sh
#
# Nightly intelligence-driven wheel candidate generator. Invoked by
# launchd at 21:00 ET (after market close, before pre-market). Runs
# claude --print with the wheel_scout routine prompt, which researches
# the market and writes data/wheel_candidates_today.json.
#
# Subscription-billed via the operator's Claude Code account.
#
# SECURITY MODEL
# --------------
#   * --add-dir scopes filesystem to the project root (needs Read on
#     state.db, news_sentiment.db, config; Write on data/wheel_candidates_today.json)
#   * --allowedTools whitelists Read, Write, Glob, Grep, WebSearch,
#     WebFetch, and a tightly-scoped Bash for sqlite3 + mv only
#   * --max-budget-usd 1.00 cap as defense-in-depth
#   * 15-min wall-clock timeout via perl alarm

set -u

PROJECT="/Users/bharathkandala/Trading"
LOG_DIR="${PROJECT}/runs/_launchd"
LOG_FILE="${LOG_DIR}/wheel-scout.log"
ROUTINE_PROMPT_FILE="${PROJECT}/tools/wheel_scout_routine.md"
TIMEOUT_SECONDS=900   # 15 min

mkdir -p "${LOG_DIR}"

{
  TS_START="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo
  echo "=== ${TS_START} wheel-scout starting ==="

  if ! command -v claude >/dev/null 2>&1; then
    echo "ERROR: claude CLI not found in PATH (PATH=${PATH})"
    exit 2
  fi

  cd "${PROJECT}" || {
    echo "ERROR: cannot cd to ${PROJECT}"
    exit 2
  }

  if [ ! -f "${ROUTINE_PROMPT_FILE}" ]; then
    echo "ERROR: routine prompt missing: ${ROUTINE_PROMPT_FILE}"
    exit 2
  fi

  perl -e 'alarm shift @ARGV; exec @ARGV' "${TIMEOUT_SECONDS}" \
    claude \
      --print \
      --add-dir "${PROJECT}" \
      --allowedTools Read Write Glob Grep WebSearch WebFetch \
        "Bash(sqlite3 *)" "Bash(mv *)" "Bash(cat *)" "Bash(date *)" \
      --max-budget-usd 1.00 \
      --model claude-opus-4-7 \
      "Run the wheel scout routine. The full procedure is documented in tools/wheel_scout_routine.md — read that file first, then execute it. Output: data/wheel_candidates_today.json." \
    2>&1 | sed 's/^/  claude: /'

  EXIT_CODE=${PIPESTATUS[0]}
  TS_END="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "claude exit code: ${EXIT_CODE}"

  # Verification: did the scout actually write the file?
  CANDIDATES_FILE="${PROJECT}/data/wheel_candidates_today.json"
  if [ -f "${CANDIDATES_FILE}" ]; then
    SIZE=$(wc -c < "${CANDIDATES_FILE}" | tr -d ' ')
    AGE_MIN=$(( ( $(date +%s) - $(stat -f %m "${CANDIDATES_FILE}") ) / 60 ))
    echo "scout file: ${CANDIDATES_FILE} (${SIZE} bytes, age ${AGE_MIN} min)"
  else
    echo "WARN: scout file not produced — wheel will fall back to allowlist/discovered universe"
  fi

  echo "=== ${TS_END} wheel-scout done ==="
  exit "${EXIT_CODE}"
} >> "${LOG_FILE}" 2>&1
