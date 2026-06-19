#!/usr/bin/env bash
# Build the native LZX decompressor as a Python extension module (lzx.so) and
# place it in ../build/ so strata_akc_dump.lit._lzx() prefers it over the
# pure-Python lzxbuild/lzx.py fallback (kept in its own dir so importing the
# pure-Python module by name still works).
#
# Source: calibre's LZX reader (lzxd.c et al.), derived from libmspack
# (Stuart Caie). GPLv3 — same lineage as lit.py / lzx.py (see NEX-379).
#
# Usage: python is taken from $PYTHON or the active interpreter.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$here/../build"
out="$here/../build/lzx.so"
PY="${PYTHON:-python}"
pyinc="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("include"))')"
arch="$(uname -m)"

cc -O2 -arch "$arch" -bundle -undefined dynamic_lookup \
   -I"$here" -I"$pyinc" \
   "$here/lzxd.c" "$here/lzxmodule.c" -o "$out"

echo "built $out ($arch)"
file "$out"
