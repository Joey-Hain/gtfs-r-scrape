#!/usr/bin/env bash
# Build the dashboard's pre-aggregated heatmap cells (build_cells.py) and
# force-push them as a single parentless commit to the `cells` branch, which
# the dashboard downloads from raw.githubusercontent.com. Force-pushing one
# orphan commit keeps that branch from growing history; files that didn't
# change (finished days) are already on GitHub and aren't uploaded again.
# Safe to call repeatedly; a failure never touches main.
set -u

OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT

# Start from the last published build so unchanged days aren't rebuilt.
if git fetch -q --depth 1 origin cells 2>/dev/null; then
  git archive FETCH_HEAD | tar -x -C "$OUT"
fi

python build_cells.py --repo . --out "$OUT" || { echo "build_cells.py failed" >&2; exit 1; }

GIT_DIR_ABS=$(git rev-parse --absolute-git-dir)
INDEX=$(mktemp -u)
(
  cd "$OUT" &&
  GIT_INDEX_FILE="$INDEX" git --git-dir="$GIT_DIR_ABS" --work-tree=. add -A . &&
  GIT_INDEX_FILE="$INDEX" git --git-dir="$GIT_DIR_ABS" write-tree > "$OUT.tree"
) || { echo "Couldn't stage cells" >&2; rm -f "$INDEX"; exit 1; }
rm -f "$INDEX"
tree=$(cat "$OUT.tree"); rm -f "$OUT.tree"

commit=$(git -c user.name="github-actions[bot]" \
             -c user.email="github-actions[bot]@users.noreply.github.com" \
             commit-tree "$tree" -m "cells: built $(date -u +'%Y-%m-%dT%H:%M:%SZ')")
for i in 1 2 3; do
  git push -q -f origin "$commit:refs/heads/cells" && { echo "Published cells ($commit)"; exit 0; }
  sleep 5
done
echo "Pushing cells failed after 3 attempts" >&2
exit 1
