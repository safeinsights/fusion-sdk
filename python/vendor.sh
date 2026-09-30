#!/bin/sh
# Emit a single-directory drop of safeinsights_fusion for vendoring next to researcher code
# (ADR 0003 fallback for base images that do not ship the SDK):
#
#   sh python/vendor.sh <out-dir>
#
# The drop is the plain `safeinsights_fusion/` package directory (stdlib only), so a research
# container whose WORKDIR contains it can `import safeinsights_fusion` with no install step,
# which is how `crane mutate` places files today.
set -eu
if [ "$#" -ne 1 ]; then
    echo "usage: sh python/vendor.sh <out-dir>" >&2
    exit 2
fi
here="$(cd "$(dirname "$0")" && pwd)"
out="$1"
mkdir -p "$out"
rm -rf "$out/safeinsights_fusion"
cp -R "$here/src/safeinsights_fusion" "$out/safeinsights_fusion"
find "$out/safeinsights_fusion" -name __pycache__ -type d -prune -exec rm -rf {} +
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$here/src/safeinsights_fusion/__init__.py")"
printf 'safeinsights_fusion %s vendored from python/vendor.sh; pure stdlib. Requires Python >= 3.10.\n' "$version" > "$out/safeinsights_fusion/VENDORED.txt"
echo "vendored safeinsights_fusion $version into $out/safeinsights_fusion"
