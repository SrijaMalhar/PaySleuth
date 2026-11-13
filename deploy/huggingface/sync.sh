#!/usr/bin/env bash
# Publish the committed tree to a Hugging Face Space.
#
#   deploy/huggingface/sync.sh <user>/<space>          publish HEAD
#   deploy/huggingface/sync.sh <user>/<space> --dry-run   build it, push nothing
#
# A Space is its own git repository, and it takes its configuration from YAML
# at the top of README.md. That block would put a stray table above the first
# line of the README on GitHub, so it lives in deploy/huggingface/README.md
# and is swapped in here instead. Everything else is the committed tree
# exactly as it is, so the Space cannot drift from what the repository says.
#
# The first push asks for a username and password: the password is a Hugging
# Face access token with write permission, from huggingface.co/settings/tokens.
# macOS stores it in the keychain afterwards. Passing it on the command line
# would leave it in your shell history, which is why this does not take one.
set -euo pipefail

space="${1:-}"
dry_run="${2:-}"
if [ -z "$space" ]; then
    echo "usage: $(basename "$0") <user>/<space> [--dry-run]" >&2
    exit 2
fi

root=$(git rev-parse --show-toplevel)
cd "$root"

if ! git diff --quiet HEAD; then
    echo "error: uncommitted changes. The Space publishes a commit, so commit first." >&2
    exit 1
fi

revision=$(git rev-parse --short HEAD)
staging=$(mktemp -d)
trap 'rm -rf "$staging"' EXIT

# git archive takes the committed tree, so an untracked scratch file or a real
# institution's statement sitting in the working directory cannot be published.
git archive HEAD | tar -x -C "$staging"
cp deploy/huggingface/README.md "$staging/README.md"

cd "$staging"
git init -q -b main
git add -A
git commit -q -m "PaySleuth $revision"

if [ "$dry_run" = "--dry-run" ]; then
    echo "built $(git ls-files | wc -l | tr -d ' ') files for $space at $revision; pushed nothing"
    echo "README.md starts:"
    head -8 README.md | sed 's/^/    /'
    exit 0
fi

# Force, because the Space's history is a mirror of this repository's commits
# rather than a branch of its own.
git push -q --force "https://huggingface.co/spaces/$space" main
echo "pushed $revision to https://huggingface.co/spaces/$space"
# A Space's hostname is its owner and name joined by a hyphen, lowercased.
host=$(printf '%s' "${space/\//-}" | tr '[:upper:]' '[:lower:]')
echo "it builds for 3-5 minutes, then answers at https://$host.hf.space/health"
