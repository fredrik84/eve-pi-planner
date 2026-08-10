#!/usr/bin/env bash
# Print a symbol index (name + line number) for a source file, instead of reading it.
#
# Why this exists: the big modules here are 2000-4000 lines. Reading windows of them to find
# a function costs ~1k tokens a look, and it usually takes three looks. This costs ~200 tokens
# once, and it can never go stale because it reads the file you're asking about.
#
#   scripts/symbols.sh app/reactions/jobs.py      # one file
#   scripts/symbols.sh app static                 # every big file in a tree
#   scripts/symbols.sh                            # every file over 500 lines in the repo
#
# Python:  top-level def/class/async def, decorated routes, and section-marker comments.
# JS:      top-level function/const-arrow/let-arrow, and section-marker comments.

set -uo pipefail

index_py() {
  # Top-level defs/classes/decorators/section markers, plus INDENTED `# ──` step markers —
  # the functions here run 300-450 lines, so the steps inside one are the thing worth finding.
  grep -nE '^(async def |def |class |@|# ──)|^[[:space:]]+# ──' "$1" \
    | grep -vE '^[0-9]+:@(staticmethod|classmethod|property|dataclass|functools)' \
    | sed 's/:\(async def \|def \|class \)/:  \1/'
}

index_js() {
  grep -nE '^((async )?function |(const|let) [A-Za-z_$][A-Za-z0-9_$]* *= *(async )?(\(|function)|// ──)' "$1"
}

index_one() {
  local f="$1" lines
  [ -f "$f" ] || return 0
  lines=$(wc -l < "$f")
  printf '\n══ %s (%s lines)\n' "$f" "$lines"
  case "$f" in
    *.py) index_py "$f" ;;
    *.js|*.mjs) index_js "$f" ;;
    *) printf '  (no indexer for this file type)\n' ;;
  esac
}

collect() {
  find "$@" -type f \( -name '*.py' -o -name '*.js' -o -name '*.mjs' \) \
    -not -path '*/node_modules/*' -not -path '*/.git/*' -not -path '*/.claude/*' \
    -not -path '*/__pycache__/*' -not -path '*/dist/*' -not -path '*/.venv/*'
}

if [ $# -eq 0 ]; then
  # No args: index everything big enough to be worth not reading.
  collect app static scripts . 2>/dev/null | sort -u | while read -r f; do
    [ "$(wc -l < "$f")" -ge 500 ] && index_one "$f"
  done
elif [ $# -eq 1 ] && [ -f "$1" ]; then
  index_one "$1"
else
  for target in "$@"; do
    if [ -f "$target" ]; then
      index_one "$target"
    else
      collect "$target" 2>/dev/null | sort | while read -r f; do index_one "$f"; done
    fi
  done
fi
