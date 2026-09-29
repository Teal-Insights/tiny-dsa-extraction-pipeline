#!/usr/bin/env bash
# Deploy the committed dist/ to the generated package's repository.
#
# The committed dist/ tree becomes a deploy commit on top of the previous
# deploy commit, which is then merged into the package repo's main. Commits
# made directly in the package repo therefore survive, and a conflict with
# one stops the deploy before anything is pushed. The package test suite runs
# on the merged tree before pushing.
#
# Usage: scripts/deploy_dist.sh [--remote URL] [--dry-run] [--skip-tests]
#
#   --remote URL   Package repository to deploy to. Defaults to the GitHub
#                  repository in workbook_config.DIST_METADATA.repository_url.
#   --dry-run      Build and test the merge, but do not push it.
#   --skip-tests   Do not run the package test suite before pushing.

set -euo pipefail

DEPLOY_SUBJECT="Deploy generated package from"

die() {
  echo "deploy_dist: $*" >&2
  exit 1
}

remote=""
dry_run=0
skip_tests=0
while [ $# -gt 0 ]; do
  case "$1" in
    --remote)
      [ $# -ge 2 ] || die "--remote needs a URL"
      remote="$2"
      shift 2
      ;;
    --dry-run) dry_run=1; shift ;;
    --skip-tests) skip_tests=1; shift ;;
    -h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

for tool in git tar; do
  command -v "$tool" >/dev/null 2>&1 || die "required tool not on PATH: $tool"
done

pipeline_root="$(git rev-parse --show-toplevel)"
cd "$pipeline_root"

[ -n "$(git ls-tree HEAD dist)" ] ||
  die "no committed dist/ at HEAD; generate the package and commit dist/ first"
git diff --quiet HEAD -- dist ||
  die "dist/ has uncommitted changes; commit them so the deploy matches a pipeline commit"

source_sha="$(git rev-parse HEAD)"
origin_url="$(git remote get-url origin 2>/dev/null || true)"
# Match after any userinfo so a credentialed HTTPS remote is recorded as owner/repo.
if [[ "$origin_url" =~ github\.com[:/]([^/]+/[^/]+)$ ]]; then
  source_name="${BASH_REMATCH[1]%.git}"
else
  source_name="$(basename "$pipeline_root")"
fi

if [ -z "$remote" ]; then
  slug="$(uv run --quiet python -c 'from src.pipeline_config import load_pipeline_config; print(load_pipeline_config().dist_metadata.repository_slug() or "")')"
  [ -n "$slug" ] ||
    die "DIST_METADATA.repository_url is not a GitHub repository; pass --remote"
  remote="https://github.com/${slug}.git"
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone --quiet "$remote" "$work/target"
cd "$work/target"

git rev-parse --verify --quiet origin/main >/dev/null ||
  die "$remote has no main branch"
base="$(git log --format=%H -1 --grep="^${DEPLOY_SUBJECT} " origin/main)"
[ -n "$base" ] ||
  die "no '${DEPLOY_SUBJECT} ...' commit on $remote main to merge from"

git checkout --quiet --detach "$base"
git rm -r --quiet .
git -C "$pipeline_root" archive --format=tar HEAD:dist | tar -xf -
git add -A
if git diff --cached --quiet "$base"; then
  echo "Nothing to deploy: dist/ matches the last deploy (${base:0:7})."
  exit 0
fi
git commit --quiet -m "${DEPLOY_SUBJECT} ${source_name}@${source_sha}"
deploy_commit="$(git rev-parse HEAD)"

git checkout --quiet main
if ! git merge --quiet --no-edit -m "Merge deploy of ${source_name}@${source_sha:0:7}" "$deploy_commit" >/dev/null; then
  git diff --name-only --diff-filter=U >&2
  die "merge conflict between dist/ and commits made directly in $remote (files above); bring those edits into the pipeline, regenerate dist/, and redeploy"
fi

if [ "$skip_tests" -eq 0 ]; then
  echo "Running the package test suite on the merged tree..."
  uv run --quiet --locked --with pytest pytest -q ||
    die "package tests failed on the merged tree; nothing was pushed"
fi

if [ "$dry_run" -eq 1 ]; then
  echo "Dry run: merged ${source_name}@${source_sha:0:7} onto $remote main but did not push."
  git log --oneline -3
  exit 0
fi

git push --quiet origin HEAD:main
echo "Deployed ${source_name}@${source_sha:0:7} to $remote main."
