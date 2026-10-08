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
# Exit codes: 0 pushed; 3 the same file changed upstream (rebase aborted, the commit is
# left unpushed); 1 still rejected after 3 attempts.
set -u

for attempt in 1 2 3; do
  if git push; then
    exit 0
  fi
  echo "push rejected (attempt $attempt/3) -- rebasing onto the latest main"
  if ! git fetch origin main; then
    continue
  fi
  if ! git rebase origin/main; then
    git rebase --abort
    echo "::error::another run changed the same ledger file; this commit was not pushed"
    exit 3
  fi
done
echo "::error::failed to push after 3 attempts"
exit 1
