#!/usr/bin/env bash
# PreToolUse guard: keeps the hexagonal layers honest AT WRITE TIME.
#
# Constitution §4 / ADR-0002 say `domain/` imports the standard library only, and `application/`
# never imports `infrastructure`. `lint-imports` proves that at commit time; this hook refuses the
# write ten minutes earlier, while you still remember what you were doing.
#
# It exists because the previous project listed exactly this hook as a Phase 0 item and still had not
# wired it four slices later, leaning on a commit-time gate to catch a one-line mistake that a
# write-time gate makes impossible. Cheap lesson, imported.
#
# Exit 2 = block the tool call and show stderr to Claude. Exit 0 = allow.
set -uo pipefail

payload="$(cat)"

read -r file_path content <<<"$(printf '%s' "$payload" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("", ""); raise SystemExit
ti = d.get("tool_input") or {}
path = ti.get("file_path") or ti.get("path") or ""
# Whatever text this call is trying to put into the file.
parts = [ti.get("content") or "", ti.get("new_string") or ""]
for e in ti.get("edits") or []:
    parts.append(e.get("new_string") or "")
text = "\n".join(parts)
print(path, json.dumps(text))
' 2>/dev/null)"

[ -z "${file_path:-}" ] && exit 0
case "$file_path" in *.py) ;; *) exit 0 ;; esac

text="$(printf '%s' "${content:-\"\"}" | python3 -c 'import json,sys; print(json.loads(sys.stdin.read() or "\"\""))' 2>/dev/null)"
[ -z "$text" ] && exit 0

# Only the import lines matter; a third-party name inside a docstring or a comment is not a violation.
imports="$(printf '%s\n' "$text" | grep -E '^[[:space:]]*(from|import)[[:space:]]+' || true)"
[ -z "$imports" ] && exit 0

banned='fastapi|sqlalchemy|pydantic|celery|httpx|requests|redis|alembic|starlette|jose|passlib|argon2|weasyprint|pypdf|docx|trafilatura|google|anyio|tailorcraft\.(application|infrastructure)'

if [[ "$file_path" == */tailorcraft/domain/* ]]; then
  hit="$(printf '%s\n' "$imports" | grep -Ein "(^|[^A-Za-z0-9_.])($banned)([^A-Za-z0-9_]|$)" || true)"
  if [ -n "$hit" ]; then
    cat >&2 <<MSG
BLOCKED — domain purity (Constitution §4, ADR-0002).

$file_path is in the domain layer, which may import the standard library and nothing else.
The offending import line(s):

$hit

Fix it where it belongs, not here:
  - Need validation?      A frozen @dataclass validating in __post_init__. Pydantic is the wire
                          format and lives in infrastructure/api/schemas/.
  - Need persistence?     A Protocol port here; the SQLAlchemy adapter in infrastructure/persistence/.
  - Need an external API? A Protocol port here speaking the domain's language; the SDK call in an
                          infrastructure adapter (ADR-0004).
  - Need the current time? The Clock port. Never datetime.now() in the domain.
MSG
    exit 2
  fi
fi

if [[ "$file_path" == */tailorcraft/application/* ]]; then
  hit="$(printf '%s\n' "$imports" | grep -Ein 'tailorcraft\.infrastructure|^[[:space:]]*(from|import)[[:space:]]+(sqlalchemy|fastapi|celery|httpx|redis)' || true)"
  if [ -n "$hit" ]; then
    cat >&2 <<MSG
BLOCKED — layer violation (Constitution §4, ADR-0002).

$file_path is in the application layer, which may import 'domain' and the standard library only.
The offending import line(s):

$hit

A use case receives its dependencies as ports through its constructor. It never imports, constructs
or names an adapter — that is what makes it testable without a database and swappable without a
rewrite. Wire the adapter in the composition root instead.
MSG
    exit 2
  fi
fi

exit 0
