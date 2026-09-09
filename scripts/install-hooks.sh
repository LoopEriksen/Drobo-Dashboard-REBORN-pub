#!/bin/sh
# Point this clone's git at the hooks tracked in scripts/hooks/.
#
# Run once per clone:
#
#     sh scripts/install-hooks.sh
#
# (On Windows, run it from Git Bash.)
#
# This sets core.hooksPath instead of copying the hook into .git/hooks/.
# Copying made every install a snapshot: a clone kept running whatever
# version of the hook it copied on the day it was set up, so improving
# the hook in the repo left existing clones enforcing the old rules, with
# nothing to signal the drift. Pointing git at the tracked directory
# means the hook updates with `git pull` like any other file.
set -e
REPO_ROOT=$(git rev-parse --show-toplevel)
cd "$REPO_ROOT"

git config core.hooksPath scripts/hooks

# Git refuses to run a hook that is not executable, and it does so
# SILENTLY — no warning, the commit just sails through ungated. The
# tracked mode is 100755 so a fresh clone is already correct; this line
# repairs a working tree where the bit was lost (a zip download, a copy
# through a filesystem that drops permissions).
chmod +x scripts/hooks/pre-commit 2>/dev/null || true

echo "core.hooksPath -> scripts/hooks"
echo "The pre-commit hook now tracks the repo; no need to re-run this after a pull."

# A hook copied in by the old version of this script is now ignored, but
# leaving a stale copy behind invites someone to edit the wrong file.
if [ -f .git/hooks/pre-commit ]; then
    echo ""
    echo "NOTE: .git/hooks/pre-commit is left over from the old copy-based install."
    echo "      Git now ignores it (core.hooksPath takes precedence). Remove it with:"
    echo "      rm .git/hooks/pre-commit"
fi
