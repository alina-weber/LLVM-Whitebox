# Installing LLVM-Whitebox

LLVM-Whitebox has two parts:

- **`pass-diff`**: a small C++ tool built against LLVM's libraries. It runs the
  pass pipeline and reports what each pass changed.
- **The UI** (`ui/UI.py`): a Python/PySide6 desktop app that calls
  clang and `pass-diff`.

The one rule to keep in mind: **clang and `pass-diff` must come from the same
LLVM**, or clang's pass pipeline and `.ll` files may not parse in `pass-diff`.
You therefore never configure clang: the UI asks `pass-diff` which LLVM it was
built against and uses the `clang++` (and `llvm-cxxfilt`) of that LLVM.

Tested on Linux (Ubuntu 24.04) with LLVM 18 (distribution package) and LLVM 23
(self-built). macOS should work; Windows is untested.

## 1. Requirements

- An LLVM **18 or newer** with its development files (`LLVMConfig.cmake`,
  headers, libraries) **and clang of the same version**
- CMake ≥ 3.20 and a C++17 compiler
- Python 3 (tested with 3.12)

### Getting LLVM

Pick one. The last column is the `LLVM_DIR` you need in step 2.

| Source | Install | `LLVM_DIR` |
|---|---|---|
| Ubuntu / Debian package | `sudo apt install llvm-18-dev clang-18` | `/usr/lib/llvm-18/lib/cmake/llvm` |
| Newer version from [apt.llvm.org](https://apt.llvm.org) | `sudo apt install llvm-NN-dev clang-NN` | `/usr/lib/llvm-NN/lib/cmake/llvm` |
| Fedora *(untested)* | `sudo dnf install llvm-devel clang` | usually found without it |
| Arch *(untested)* | `sudo pacman -S llvm clang` | usually found without it |
| macOS Homebrew *(untested)* | `brew install llvm` | `$(brew --prefix llvm)/lib/cmake/llvm` |
| Self-compiled | see below | `<install prefix>/lib/cmake/llvm` |

**Self-compiled LLVM:** clang has to be part of the build, and the build needs
to be installed so that `lib/cmake/llvm/LLVMConfig.cmake` exists:

```
cmake -S llvm -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
      -DLLVM_ENABLE_PROJECTS=clang -DCMAKE_INSTALL_PREFIX=$HOME/.local/llvm-install
cmake --build build && cmake --install build
```

A Debug build with `-DLLVM_ENABLE_ASSERTIONS=ON` works too, and gives you
LLVM's assertions and debug output. Its clang is much slower at parsing C++,
though.

## 2. Build `pass-diff`

```
cd pass-diff
cmake -S . -B build -DLLVM_DIR=<LLVM_DIR from the table>
cmake --build build
```

`build/` is where the UI looks by default, so you can stop here. To check which
LLVM the binary uses:

```
$ build/pass-diff toolchain-info
llvm-version 18.1.3
tools-dir /usr/lib/llvm-18/bin
```

`tools-dir` must contain `clang++`; the UI exits with a message if it does not.

Notes:

- `pass-diff` is built as `Release` unless you pass `-DCMAKE_BUILD_TYPE=Debug`.
  This is independent of how LLVM itself was built.
- The build adapts to the LLVM it finds: it links the single shared `libLLVM`
  when the LLVM was built that way (typical for distribution packages), and
  matches LLVM's RTTI setting.

### Optional: install it

```
cmake --install build --prefix $HOME/.local
```

This puts `pass-diff` into `$HOME/.local/bin`. If that folder is on your
`PATH`, the UI finds it there (see step 4).

### Several LLVMs side by side

Use one build folder per LLVM and choose with `--pass-diff` when starting the
UI. For example, with a Release LLVM as default and a Debug LLVM for
investigating LLVM itself:

```
cmake -S . -B build       -DLLVM_DIR=$HOME/.local/llvm-release-install/lib/cmake/llvm
cmake -S . -B build-debug -DLLVM_DIR=$HOME/.local/llvm-debug-install/lib/cmake/llvm
cmake --build build && cmake --build build-debug
```

## 3. Set up Python

From the repository root:

```
python3 -m venv ui/.venv
ui/.venv/bin/pip install -r ui/requirements.txt
```

On Debian/Ubuntu, `python3 -m venv` needs the `python3-venv` package.

## 4. Run

```
ui/UI_run.sh --code test/src/hello_world.cpp -- -O3
```

`UI_run.sh` activates a Python environment, then starts `UI.py` with all
arguments. Which environment:

1. the one in `$VENV`, if set (`VENV=~/venvs/foo ui/UI_run.sh ...`)
2. else `ui/.venv`, if it exists
3. else no activation; the `python3` already on your `PATH` is used

Which `pass-diff` (and therefore which clang):

1. `--pass-diff <path>`, if given
2. with `--cache`, the one that earlier run used
3. else `pass-diff` on your `PATH`
4. else `pass-diff/build/pass-diff`

The terminal shows the choice at startup:

```
Using pass-diff .../pass-diff/build/pass-diff (LLVM 18.1.3), clang++ /usr/lib/llvm-18/bin/clang++
```

See the [README](README.md) for the UI's options and how to use it.

## Troubleshooting

**`clang++ not found at <dir>/clang++`.** The LLVM that `pass-diff` was built
against has no clang. Install the matching clang (e.g. `clang-18` for
`llvm-18-dev`), or build `pass-diff` against an LLVM that includes clang.

**`pass-diff needs LLVM 18 or newer`.** CMake found an older LLVM. Pass
`-DLLVM_DIR` for a newer one.

**`... does not support 'toolchain-info'`.** That `pass-diff` was built from an
older version of this repository. Rebuild it.

**CMake complains about a mismatched cache directory** after moving or renaming
the repository or a build folder. Reconfigure from scratch:
`cmake --fresh -S . -B build -DLLVM_DIR=...`.

**`--cache` stops with "written by an older version of this tool".** Output
folders from before the switch to `--pass-diff` cannot be reopened. Start a new
run with `--code`.

**`Could not load the Qt platform plugin "xcb"`** (Linux): Qt 6 needs
`libxcb-cursor0` (Debian/Ubuntu: `sudo apt install libxcb-cursor0`).
