#!/usr/bin/env bash
# PostToolUse: fast feedback on the file just written. The heavy gates stay in `make check`; this is
# the two-second version so style and obvious lint problems never reach a commit.
set -uo pipefail

file_path="$(cat | jq -r '.tool_input.file_path // .tool_input.path // empty' 2>/dev/null)"
[ -z "$file_path" ] && exit 0
[ -f "$file_path" ] || exit 0

case "$file_path" in
  *.py)
    command -v ruff >/dev/null 2>&1 || exit 0
    out="$(ruff check --quiet "$file_path" 2>&1; ruff format --check --quiet "$file_path" 2>&1)"
    if [ -n "$out" ]; then
      echo "ruff on $file_path:" >&2
      echo "$out" >&2
      exit 2
    fi
    ;;
esac
exit 0
