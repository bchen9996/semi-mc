#!/usr/bin/env bash
# Push the commit a trading job just made, rebasing it onto the latest main if the push
# is rejected. Never re-runs the command that wrote it: re-running a cycle repeated its
# Trader call (billed, but only the last attempt reached the ledger), and on the real
# account it would discard the ledger rows of orders already at the broker. Found
# 2026-10-07 (tasks/todo.md).
#
# A rejected push almost always means another workflow pushed a different file (paper
# marks and intraday cycles fire in the same minutes), so the rebase applies cleanly. A
# rebase conflict means another run changed the same file, and a binary SQLite ledger
# can't be merged: the rebase is aborted and the caller decides what to do.
#
# GitHub itself sometimes refuses a push. On 2026-11-05 it answered two pushes a second
# apart with "Internal Server Error"; the three attempts were spent within two seconds,
# and the 10:00 CT cycle's rows were lost after its orders had reached Alpaca. So each
# retry now waits longer than the last: PUSH_LEDGER_WAITS, in seconds (tests set zeros),
# about 2.5 minutes in all, well inside the 30 minutes between cycles.
#
# A commit that never lands takes rows with it that nothing else holds: on 2026-11-05 the
# lost cycle's proposals, its Trader call and the placebo's entry existed only on the
# runner. So before giving up, the script copies the commit's files to
# $RUNNER_TEMP/unpushed and sets LEDGER_UNPUSHED for the rest of the job, and the
# workflow's next step uploads them as an artifact. A paper job re-runs after exit 3; if
# a later attempt lands, the copy is dropped again. Outside GitHub Actions (no
# RUNNER_TEMP) none of this happens.
#
# Exit codes: 0 pushed; 3 the same file changed upstream (rebase aborted, the commit is
# left unpushed); 1 still rejected after every attempt.
set -u

keep_unpushed() {
  if [[ -z "${RUNNER_TEMP:-}" ]]; then
    return 0
  fi
  local dir="$RUNNER_TEMP/unpushed"
  rm -rf "$dir"
  mkdir -p "$dir"
  local path
  while IFS= read -r path; do
    mkdir -p "$dir/$(dirname "$path")"
    cp "$path" "$dir/$path"
  done < <(git diff-tree --no-commit-id --name-only -r --diff-filter=d HEAD)
  git log -1 --format='%H%n%s' > "$dir/COMMIT.txt"
  if [[ -n "${GITHUB_ENV:-}" ]]; then
    echo "LEDGER_UNPUSHED=$dir" >> "$GITHUB_ENV"
  fi
  echo "kept this commit's files in $dir; the workflow uploads them as an artifact"
}

drop_kept() {
  if [[ -n "${RUNNER_TEMP:-}" && -d "$RUNNER_TEMP/unpushed" ]]; then
    rm -rf "$RUNNER_TEMP/unpushed"
    if [[ -n "${GITHUB_ENV:-}" ]]; then
      echo "LEDGER_UNPUSHED=" >> "$GITHUB_ENV"
    fi
  fi
}

read -r -a waits <<< "${PUSH_LEDGER_WAITS:-2 5 10 20 40 60}"
attempts=$(( ${#waits[@]} + 1 ))
for (( attempt = 1; attempt <= attempts; attempt++ )); do
  if git push; then
    drop_kept
    exit 0
  fi
  if (( attempt == attempts )); then
    break
  fi
  wait_seconds=${waits[attempt - 1]}
  echo "push rejected (attempt $attempt/$attempts) -- waiting ${wait_seconds}s, then rebasing onto the latest main"
  sleep "$wait_seconds"
  if ! git fetch origin main; then
    continue
  fi
  if ! git rebase origin/main; then
    git rebase --abort
    echo "::error::another run changed the same ledger file; this commit was not pushed"
    keep_unpushed
    exit 3
  fi
done
echo "::error::failed to push after $attempts attempts"
keep_unpushed
exit 1
