# Working rules for coding agents

- Design and roadmap: PLAN.md. Deploying, verifying and operating each phase: docs/RUNBOOK.md.
  Record decisions that change the plan (and why) in the runbook, not only in chat.
- Build and test: `scripts/ci.sh` (lint + tests in the pinned Python 3.12 CI image, no network;
  `--image` also builds the production image). It is the CI: GitHub Actions only runs on demand.
  It takes the shared Docker lock `/ssd/Personal/repositories/.locks/docker-ci.lock` and waits
  for free memory, so run it freely from any worktree; never run `docker run`/`docker build` for
  tests by hand. Outside Docker: `pip install -e '.[dev]' && ruff check . && pytest -q`.
- **Never add `Co-authored-by`, `Claude-Session` or similar attribution trailers to commits or
  pull requests.** Commit messages describe the change and nothing else.
- Keep unit tests for pure logic (pipeline, matching, normalise, merge) and for every upstream
  behaviour learned the hard way (Lidarr 3 adding monitor=none artists unmonitored, Caddy's
  directive order, …). Mock upstreams with `tests/conftest.py`'s FakeUpstream; never call real
  services from tests.

## Branch and release workflow

- Never commit features or fixes directly to `main`. Each change goes on a branch
  (`feat/...`, `fix/...`, `chore/...`) and lands through a pull request; local CI must be green.
  Merge with `scripts/merge_when_green.sh <pr>` (squash by default, `--merge` to keep a
  branch's commits): it runs `scripts/ci.sh --image` on the PR's exact head in a throwaway
  worktree (or reuses a successful `local-ci` status on it), records the result as the
  `local-ci` commit status, and merges only that commit.
- Work is tracked in GitHub issues (labels `area/*`, `type/*`, `priority/*`; milestones per plan
  phase). Reference the issue in the PR ("Closes #n").
- Releases are batched: several merged PRs, then a `vX.Y.Z` tag when the user asks. Images are
  built locally on the Jetson (native ARM64); publishing them to ghcr is issue #3.

## Live system

continuo runs on the homelab Jetson next to services people use (Navidrome, Lidarr, Home
Assistant). The homelab compose repo is `/ssd/homelab` (its own branch-and-merge workflow, sops
secrets you must not decrypt to the terminal). Changing Lidarr, Navidrome, Caddy or Home Assistant
settings, or anything that downloads, needs the user's go-ahead first.
