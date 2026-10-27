#!/usr/bin/env bash
# One git worktree per KC-6 patch, plus base, at base-ref (default f48a92c).
set -euo pipefail
src=${1:?patch folder}; wt=${2:?worktrees dir}; base=${3:-f48a92c}
repo=$(git rev-parse --show-toplevel)
mkdir -p "$wt"
[ -d "$wt/base" ] || git -C "$repo" worktree add -q --detach "$wt/base" "$base"
for p in "$src"/*.patch; do
  name=$(basename "${p%.patch}" | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9]\+/-/g')
  d="$wt/$name"
  [ -d "$d" ] || git -C "$repo" worktree add -q --detach "$d" "$base"
  if git -C "$d" am -q --3way "$(realpath "$p")" >/dev/null 2>&1; then
    printf '%-40s %s commit(s)\n' "$name" "$(git -C "$d" rev-list --count "$base..HEAD")"
  else
    git -C "$d" am --abort >/dev/null 2>&1 || true
    printf '%-40s DOES NOT APPLY\n' "$name"
  fi
done
