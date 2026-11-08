#!/bin/bash
# Copyright (c) 2026 Alina Weber
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# Starts the UI. Uses the Python environment named in $VENV, else ui/.venv,
# else whatever python3 is active (see INSTALL.md).
# All arguments are forwarded; UI.py defaults --code to test/src/hello_world.cpp.
# Compiler flags go after '--', e.g. (from the repository root):
#   ui/UI_run.sh --code test/src/hello_world.cpp -- -O3
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ -z "$VENV" ] && [ -d "$HERE/.venv" ]; then
    VENV="$HERE/.venv"
fi
if [ -n "$VENV" ]; then
    source "$VENV/bin/activate" || exit 1
fi
exec python3 "$HERE/UI.py" "$@"
