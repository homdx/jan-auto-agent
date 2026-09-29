#!/usr/bin/env bash
# 2legs/make_target.sh — KC-77: (re)build the external demo repo at $TARGET.
#
# A Calc class with one test, the two contest scripts and tickets 01/02,
# committed as one base commit. Rebuilding is refused unless $TARGET is
# missing or is a demo repo this script made (its first commit subject
# matches), so a mistyped TARGET never wipes a real repository.
#
# usage: source 2legs/env.sh && 2legs/make_target.sh
set -euo pipefail
: "${JAN:?source 2legs/env.sh first}" "${TARGET:?source 2legs/env.sh first}"
SUBJECT="demo: Calc + contest scripts + tickets 01/02"

if [ -e "$TARGET" ]; then
  first=$(git -C "$TARGET" log --reverse --format=%s 2>/dev/null | head -1 || true)
  if [ "$first" != "$SUBJECT" ]; then
    echo "!! $TARGET exists and is not the 2legs demo repo — refusing to delete it" >&2
    exit 2
  fi
  rm -rf "$TARGET"
fi

mkdir -p "$TARGET"/{scripts,epic-tasks,tests}
cd "$TARGET"
git init -q
cat > calc.py <<'EOF'
class Calc:
    def __init__(self, value=0):
        self.value = value

    def add(self, n):
        self.value += n
        return self
EOF
cat > tests/test_calc.py <<'EOF'
from calc import Calc


def test_add():
    assert Calc(1).add(2).value == 3
EOF
touch tests/__init__.py
printf 'contest-out/\n__pycache__/\n.pytest_cache/\n' > .gitignore
cp "$JAN/scripts/next_task.py" "$JAN/scripts/append_task.py" scripts/
cp "$JAN/2legs/01-kc-ext01-add-repr-to-calc.md" "$JAN/2legs/02-kc-ext02-add-eq-to-calc.md" epic-tasks/
git add -A
git commit -qm "$SUBJECT"
echo "target ready: $TARGET @ $(git rev-parse --short HEAD)"
