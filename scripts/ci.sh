#!/usr/bin/env bash
# Local CI for continuo. GitHub Actions only runs on demand (see .github/workflows/ci.yml);
# this is the gate every PR passes before merge (scripts/merge_when_green.sh runs it).
#
#   scripts/ci.sh            ruff + pytest in the CI image
#   scripts/ci.sh --image    also build the production image (native ARM64 on the Jetson)
#
# Exit 0 = green. Safe to run from several worktrees at once: runs are serialised by a lock
# (CI_LOCK, default /ssd/Personal/repositories/.locks/docker-ci.lock — shared with Polyphony's
# local CI, so heavy Docker work on the Jetson never overlaps) and wait until enough memory is free
# (CI_MIN_FREE_MB, default 700), because the Jetson shares its RAM with the homelab.
# Tests run with no network, so they can never reach a real service.
#
# CI_SRC=<dir> checks another tree (a PR's head) with this script and this Dockerfile.dev, so
# PRs opened before a CI change are still checked the current way.
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
src="$(cd "${CI_SRC:-$here}" && pwd)"
cd "$src"

build_image=0
case "${1:-}" in
  --image) build_image=1 ;;
  "") ;;
  *) echo "usage: $0 [--image]" >&2; exit 64 ;;
esac

lock="${CI_LOCK:-/ssd/Personal/repositories/.locks/docker-ci.lock}"
min_mb="${CI_MIN_FREE_MB:-700}"

exec 9>>"$lock"
if ! flock -x -n 9; then
  echo "ci: another CI run holds $lock, waiting…"
  flock -x 9
fi

free_mb() { awk '/^MemAvailable:/ { print int($2 / 1024) }' /proc/meminfo; }
for _ in $(seq 60); do
  [ "$(free_mb)" -ge "$min_mb" ] && break
  echo "ci: $(free_mb) MB available, waiting for ${min_mb} MB…"
  sleep 10
done
[ "$(free_mb)" -ge "$min_mb" ] || { echo "ci: not enough free memory after 10 minutes" >&2; exit 75; }

tag="continuo-ci:$(cat pyproject.toml "$here/Dockerfile.dev" | sha256sum | cut -c1-12)"
if ! docker image inspect "$tag" >/dev/null 2>&1; then
  echo "ci: building $tag"
  docker build -q -t "$tag" -f "$here/Dockerfile.dev" . >/dev/null
fi

echo "ci: ruff + pytest ($(git rev-parse --short HEAD 2>/dev/null || echo worktree))"
docker run --rm --network none --memory 768m \
  -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD:/src" -w /src "$tag" \
  sh -c 'ruff check --no-cache . && python -m pytest -q -p no:cacheprovider'

if [ "$build_image" = 1 ]; then
  img="continuo:ci-$$"
  echo "ci: building the production image"
  docker build -q -t "$img" . >/dev/null
  docker rmi -f "$img" >/dev/null
fi
echo "ci: green"
