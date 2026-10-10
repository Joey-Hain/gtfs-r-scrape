#!/usr/bin/env bash
# Commit any new rows in data/ and push them, rebasing over pushes that
# landed meanwhile (data-local/ from the home collector, shapes/ from the
# route-shapes workflow). Safe to call repeatedly: does nothing if no change.
set -u

git config user.name  "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"
git add data/
git diff --cached --quiet && { echo "No new data to commit"; exit 0; }
git commit -q -m "data: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
for i in 1 2 3; do
  git pull --rebase -q && git push -q && { echo "Pushed data commit"; exit 0; }
  sleep 5
done
echo "Push failed after 3 attempts" >&2
exit 1
