#!/usr/bin/env bash
# Build the presentation charts and force-push them as one parentless commit
# to the `analysis` branch (same approach as publish-cells.sh), so the branch
# never grows history. START / END (YYYY-MM-DD) optionally set the window.
set -eu
OUT=$(mktemp -d)
trap 'rm -rf "$OUT"' EXIT
args=(--repo . --out "$OUT")
[ -n "${START:-}" ] && args+=(--start "$START")
[ -n "${END:-}" ] && args+=(--end "$END")
python analysis/make_charts.py "${args[@]}"

GIT_DIR_ABS=$(git rev-parse --absolute-git-dir)
INDEX=$(mktemp -u)
tree=$(cd "$OUT" && GIT_INDEX_FILE="$INDEX" git --git-dir="$GIT_DIR_ABS" --work-tree=. add -A . &&
       GIT_INDEX_FILE="$INDEX" git --git-dir="$GIT_DIR_ABS" write-tree)
rm -f "$INDEX"
commit=$(git -c user.name="github-actions[bot]" \
             -c user.email="github-actions[bot]@users.noreply.github.com" \
             commit-tree "$tree" -m "analysis: built $(date -u +'%Y-%m-%dT%H:%M:%SZ')")
for i in 1 2 3; do
  git push -q -f origin "$commit:refs/heads/analysis" && { echo "Published analysis ($commit)"; exit 0; }
  sleep 5
done
echo "Pushing analysis failed" >&2; exit 1
