#!/usr/bin/env bash
#
# Remove what this repository generated, and name what it did not.
#
# Three directories here are ours to delete. `trustvian dev`'s state is not: it
# lives outside this repository, under a path dev derives from a hash of the
# workload directory, and this script deletes no path it did not derive itself.
#
# That is not caution for its own sake. A hash-keyed path under $HOME is exactly
# the kind of thing a second implementation gets subtly wrong — a different
# working directory, a symlink resolved differently, a changed key length — and
# the failure mode of getting it wrong is `rm -rf` on somebody else's directory.
# So this script prints what dev reported and stops there.

set -euo pipefail
DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DEMO_ROOT"

DEV_STATE_PATH_FILE=".runtime/dev-state-path"

# Read before the removal below, because .runtime/ is one of the things going.
DEV_STATE=""
if [ -r "$DEV_STATE_PATH_FILE" ]; then
    DEV_STATE="$(cat "$DEV_STATE_PATH_FILE")"
fi

rm -rf .demo .runtime .trustvian
echo "Removed .demo/, .runtime/ and .trustvian/"

echo
if [ -n "$DEV_STATE" ]; then
    cat <<NOTE
'trustvian dev' keeps its own state outside this repository, and the last run
reported it at:

    $DEV_STATE

It holds the generated Collector configuration, both helper logs, and the
learned baseline for each candidate. Nothing here removes it: this repository
did not derive that path and will not delete one it was only told about.

    rm -rf "$DEV_STATE"
NOTE
else
    cat <<'NOTE'
'trustvian dev' keeps its own state outside this repository, under
~/.trustvian/dev/<hash of this directory>/. No run has reported a path yet, so
there is nothing to name here; dev prints it on the `State` line of every start.
NOTE
fi
