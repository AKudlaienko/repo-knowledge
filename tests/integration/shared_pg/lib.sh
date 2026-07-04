#!/usr/bin/env bash
# Shared helpers for the shared-PostgreSQL integration harness.
# Sourced by run.sh — not executable on its own.

# --- result tracking -------------------------------------------------------
PASS=0
FAIL=0
declare -a FAILURES=()

c_green=$'\033[32m'; c_red=$'\033[31m'; c_dim=$'\033[2m'; c_bold=$'\033[1m'; c_off=$'\033[0m'

log()  { printf '%s\n' "${c_dim}· $*${c_off}"; }
step() { printf '\n%s\n' "${c_bold}== $* ==${c_off}"; }

pass() { PASS=$((PASS+1)); printf '  %sPASS%s %s\n' "$c_green" "$c_off" "$1"; }
fail() {
  FAIL=$((FAIL+1)); FAILURES+=("$1")
  printf '  %sFAIL%s %s\n' "$c_red" "$c_off" "$1"
  [ -n "${2:-}" ] && printf '       %s%s%s\n' "$c_dim" "$2" "$c_off"
}

# Portable `timeout`: GNU timeout / gtimeout when present (Linux, brew
# coreutils), else a pure-bash fallback (macOS ships neither). Returns the
# command's real exit code, or 124 if it was killed for running too long.
portable_timeout() {
  local secs="$1"; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; return $?; fi
  if command -v gtimeout >/dev/null 2>&1; then gtimeout "$secs" "$@"; return $?; fi
  "$@" &
  local pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) & local watcher=$!
  local rc=0
  wait "$pid" 2>/dev/null || rc=$?
  kill -TERM "$watcher" 2>/dev/null || true
  wait "$watcher" 2>/dev/null || true
  return "$rc"
}

# LAST_OUT holds combined stdout+stderr of the most recent expect_* call.
LAST_OUT=""

# expect_exit <want> <description> -- <cmd...>
# Runs cmd, captures rc + output, asserts rc == want.
expect_exit() {
  local want="$1" desc="$2"; shift 2
  [ "$1" = "--" ] && shift
  local rc
  set +e
  LAST_OUT="$("$@" 2>&1)"; rc=$?
  set -e
  if [ "$rc" -eq "$want" ]; then
    pass "$desc (exit $rc)"
  else
    fail "$desc" "expected exit $want, got $rc :: ${LAST_OUT##*$'\n'}"
  fi
}

# assert_contains <description> <needle>   (haystack = $LAST_OUT)
assert_contains() {
  local desc="$1" needle="$2"
  if printf '%s' "$LAST_OUT" | grep -qF -- "$needle"; then
    pass "$desc"
  else
    fail "$desc" "missing '$needle' in: ${LAST_OUT:0:300}"
  fi
}

# assert_eq <description> <expected> <actual>
assert_eq() {
  local desc="$1" exp="$2" act="$3"
  if [ "$exp" = "$act" ]; then pass "$desc"; else fail "$desc" "expected '$exp', got '$act'"; fi
}

summary() {
  printf '\n%s---------------------------------------------%s\n' "$c_bold" "$c_off"
  if [ "$FAIL" -eq 0 ]; then
    printf '%sALL PASSED%s  (%d checks)\n' "$c_green" "$c_off" "$PASS"
    return 0
  fi
  printf '%s%d FAILED%s / %d passed\n' "$c_red" "$FAIL" "$c_off" "$PASS"
  local f; for f in "${FAILURES[@]}"; do printf '  - %s\n' "$f"; done
  return 1
}
