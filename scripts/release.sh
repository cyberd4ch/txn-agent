#!/usr/bin/env bash
# One-command release for txn-agent.
#
# Usage:
#   scripts/release.sh 0.4.4 "one-line release summary"
#   scripts/release.sh 0.4.4 "summary" --dry-run   # validate + gate only, change nothing
#   scripts/release.sh 0.4.4 "summary" --no-push   # commit + tag locally, push yourself
#
# What it does, in order:
#   1. sanity checks (on main, clean tree, in sync with origin, tag unused)
#   2. the same gate CI runs: ruff check, mypy strict, pytest with the 85% floor
#   3. bump `version` in pyproject.toml (must end up matching the tag)
#   4. scaffold a CHANGELOG entry + .github/releases/vX.Y.Z.md when either is missing
#   5. commit, annotated tag vX.Y.Z, push main + tag
#
# Pushing the tag makes CI build the sdist/wheel, create the GitHub release with
# SHA256SUMS + assets, and attempt the PyPI upload (that job stays red until the
# pending publisher from docs/deploy.md section 8 is configured on pypi.org).
#
# Rule of thumb: avoid apostrophes in the summary — they have broken commit
# quoting here before. Keep the tag version and pyproject version identical.

set -euo pipefail

die() { echo "release: error: $*" >&2; exit 1; }
say() { echo "==> $*"; }

usage() {
  grep '^#' "$0" | sed 's/^# \{0,1\}//' | tail -n +2
  exit 0
}

V=""
SUMMARY=""
DRY=0
NOPUSH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-push) NOPUSH=1 ;;
    -h|--help) usage ;;
    -*) die "unknown flag: $1 (see --help)" ;;
    *) if [ -z "$V" ]; then V="$1"; elif [ -z "$SUMMARY" ]; then SUMMARY="$1"; else die "unexpected argument: $1"; fi ;;
  esac
  shift
done

[ -n "$V" ] || die "usage: scripts/release.sh X.Y.Z [summary] [--dry-run] [--no-push]"
printf '%s' "$V" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || die "version must be X.Y.Z, got: $V"
TAG="v$V"
[ -n "$SUMMARY" ] || SUMMARY="Release $TAG"

# --- 1. sanity checks -------------------------------------------------------

git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not inside a git repo"
BRANCH="$(git branch --show-current)"
[ "$BRANCH" = "main" ] || die "must run on main (on: $BRANCH)"
[ -z "$(git status --porcelain)" ] || die "working tree not clean; commit or stash first"

say "fetching origin"
git fetch --quiet origin main
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "local main differs from origin/main; pull or push first"

git rev-parse -q --verify "refs/tags/$TAG" >/dev/null 2>&1 && die "tag $TAG already exists locally"
if git ls-remote --tags origin "refs/tags/$TAG" | grep -q .; then
  die "tag $TAG already exists on origin"
fi

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi
command -v "$PY" >/dev/null 2>&1 || die "python not found (set PYTHON=/path/to/python)"

# --- 2. the gate (identical to CI) ------------------------------------------

say "gate: ruff check ."
"$PY" -m ruff check .
say "gate: mypy txn_agent (strict)"
"$PY" -m mypy txn_agent
say "gate: pytest with coverage floor 85%"
"$PY" -m pytest -q --cov=txn_agent --cov-report=term --cov-fail-under=85

if [ "$DRY" -eq 1 ]; then
  say "dry run: gate passed; would bump pyproject to $V, scaffold notes, commit, tag $TAG, push main + $TAG"
  exit 0
fi

# --- 3. version bump ---------------------------------------------------------

say "bumping pyproject.toml to $V"
"$PY" - "$V" <<'PYEOF'
import re
import sys
from pathlib import Path

p = Path("pyproject.toml")
s = p.read_text()
new = re.sub(r'(?m)^version = "[^"]+"', f'version = "{sys.argv[1]}"', s, count=1)
if new == s:
    sys.exit("could not find a `version = \"...\"` line in pyproject.toml")
p.write_text(new)
PYEOF

# --- 4. scaffold changelog entry + release notes -----------------------------

say "scaffolding CHANGELOG entry for $V"
"$PY" - "$V" <<'PYEOF'
import re
import sys
from datetime import date
from pathlib import Path

v = sys.argv[1]
p = Path("CHANGELOG.md")
s = p.read_text()
if re.search(r"(?m)^## \[" + re.escape(v) + r"\]", s):
    print(f"CHANGELOG already has a [{v}] entry; leaving it alone")
else:
    m = re.search(r"(?m)^## \[", s)
    if m is None:
        sys.exit("no previous changelog entries found")
    entry = f"## [{v}] - {date.today().isoformat()}\n\n### Added\n\n- See the commit history since the previous release.\n\n"
    p.write_text(s[: m.start()] + entry + s[m.start() :])
    print("NOTE: edit the CHANGELOG entry with real highlights before pushing")
PYEOF

NOTES=".github/releases/$TAG.md"
if [ -f "$NOTES" ]; then
  say "release notes already present: $NOTES"
else
  say "scaffolding $NOTES"
  mkdir -p .github/releases
  cat > "$NOTES" <<NOTEEOF
# $TAG — $SUMMARY

Full changelog:
[CHANGELOG.md](https://github.com/cyberd4ch/txn-agent/blob/main/CHANGELOG.md).

## Highlights

- Edit me: one bullet per user-visible change, with the user-visible why.

**Tests, coverage and lint status: enforced by CI on this tag.**
NOTEEOF
fi

# --- 5. commit, tag, push -----------------------------------------------------

say "staging release files"
git add pyproject.toml CHANGELOG.md "$NOTES"

say "committing"
git commit -m "$TAG: $SUMMARY"

say "tagging $TAG"
git tag -a "$TAG" -m "txn-agent $TAG"

if [ "$NOPUSH" -eq 1 ]; then
  say "--no-push: committed and tagged locally; push with: git push origin main $TAG"
  exit 0
fi

say "pushing main + $TAG"
git push origin main "$TAG"

cat <<DONE

Done. CI will now:
  - build the sdist/wheel from $TAG
  - create the GitHub release $TAG with SHA256SUMS.txt and all assets
  - attempt the PyPI upload (stays red until the pending publisher from
    docs/deploy.md section 8 is configured; re-run the job afterwards)

Watch: gh run watch --exit-status "$(git rev-parse --abbrev-ref HEAD)" 2>/dev/null || gh run list --limit 3
DONE
