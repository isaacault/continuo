#!/usr/bin/env bash
# Usage: scripts/merge_when_green.sh <pr-number> [--merge]
#
# The merge gate. GitHub Actions only runs on demand, so the gate is local CI:
#   1. if the PR's current head already has a successful "local-ci" commit status, use it;
#   2. otherwise check that exact commit out in a throwaway worktree, run
#      `scripts/ci.sh --image` there, and record the result as the "local-ci" status;
#   3. merge exactly that commit — squash by default, --merge keeps the branch's commits — and
#      delete the branch. GitHub refuses the merge if the head moved meanwhile.
# It never merges on a failed, missing or interrupted run. Polyphony's script uses the same
# "local-ci" context, so a PR shows one green tick whichever repo it is in.
set -u
pr="${1:?pr number}"
mode="--squash"; [ "${2:-}" = "--merge" ] && mode="--merge"
cd "$(dirname "$0")/.." || exit 2

repo=$(gh repo view --json nameWithOwner -q .nameWithOwner) || exit 2
read -r sha state < <(gh pr view "$pr" --json headRefOid,state -q '.headRefOid + " " + .state') || exit 2
[ "$state" != "OPEN" ] && { echo "PR $pr is $state; nothing to do"; exit 0; }

current=$(gh api "repos/$repo/commits/$sha/statuses" \
  -q '[.[] | select(.context == "local-ci")][0].state // ""' 2>/dev/null)

if [ "$current" != "success" ]; then
  status() {
    gh api -X POST "repos/$repo/statuses/$sha" -f state="$1" -f context=local-ci \
      -f description="$2" >/dev/null || echo "warning: couldn't record the local-ci status" >&2
  }
  work=$(mktemp -d "${TMPDIR:-/tmp}/continuo-pr$pr.XXXXXX") || exit 2
  log="$work.log"
  cleanup() { git worktree remove --force "$work" >/dev/null 2>&1; rm -rf "$work"; }
  trap cleanup EXIT
  git fetch -q origin "pull/$pr/head" || exit 2
  git worktree add -q --detach "$work" "$sha" || exit 2

  status pending "scripts/ci.sh --image running on $(hostname)"
  echo "local CI on ${sha:0:7} (log: $log)"
  # This checkout's ci.sh against the PR's tree: PRs from before a CI change are gated the same way.
  if CI_SRC="$work" scripts/ci.sh --image >"$log" 2>&1; then
    status success "lint, tests and image build passed on $(hostname)"
  else
    status failure "local CI failed on $(hostname)"
    echo "NOT MERGING $pr: local CI failed on ${sha:0:7}"
    tail -n 40 "$log"
    exit 1
  fi
fi

echo "green on ${sha:0:7}"
gh pr merge "$pr" "$mode" --delete-branch --match-head-commit "$sha"
