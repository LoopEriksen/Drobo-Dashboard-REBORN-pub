#!/bin/sh
# Install the Drobo-Dashboard-REBORN-public git hooks into this clone's .git/hooks/.
# Git hooks are NOT shared through the repo, so each person who clones
# runs this once:
#
#     sh scripts/install-hooks.sh
#
# (On Windows, run it from Git Bash.)
set -e
REPO_ROOT=$(git rev-parse --show-toplevel)
cp "$REPO_ROOT/scripts/hooks/pre-commit" "$REPO_ROOT/.git/hooks/pre-commit"
chmod +x "$REPO_ROOT/.git/hooks/pre-commit"
echo "Installed pre-commit hook -> .git/hooks/pre-commit"
echo "It will block commits containing databases, .env files, or personal data."
