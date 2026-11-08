# LLVM-Whitebox

Make LLVM optimization passes and the IR changes they cause visible, on demand.

You give it a source file and the compiler flags you would normally use. It shows
you the source, the final assembly, and, per function, the list of optimization
passes that ran on it — with the ones that actually changed the IR highlighted.
Click a pass and you get a side-by-side diff of the IR before and after it,
computed on the spot.

Useful for answering "why didn't that optimization fire?" and "which pass broke
this?".

## Relation to Compiler Explorer

Compiler Explorer is the tool people reach for to explore compiler behaviour
interactively: paste a snippet, switch compilers and versions, see output
immediately. This project is aimed at a different job — debugging the compilation
of an actual application, with its real flags, where the thing you need is a
specific answer about a specific pass. Two consequences of that:

- **IR is produced lazily.** Nothing dumps the IR at every pass up front.
  `list-passes` only compares before/after snapshots to decide *whether* a pass
  changed anything, and discards the text; the IR for a diff is generated only for
  the one pass you clicked. That is what keeps a long compilation workable instead
  of writing out large amounts of dumps to find one diff.
- **Whole projects are the target.** The intended end state is pointing the tool at
  a real project's build and investigating one file or one function through the
  whole compilation. Tracking file and function identity across the pipeline is the
  bulk of that work and is **not implemented yet** — today the tool handles one
  translation unit at a time.

## Status

Prototype. Single C++ translation unit, host target, desktop UI.

## Design note: the pipeline is clang's own

The tool never reconstructs a pass pipeline from `-O` levels and hopes it matches.
It asks clang what it would actually run:

```
clang++ <your flags> -mllvm -print-pipeline-passes -S file.ll -o /dev/null
```

`file.ll` is the pre-optimization IR the UI creates first (see below). Only that
first step parses the C++ source, and the C++ frontend is almost all of clang's
runtime. The pipeline query and the assembly (`clang++ <your flags> -S file.ll`)
both start from the `.ll` and take well under a second, while producing the same
pipeline and assembly as starting from the `.cpp`. C++-only flags (`-I`,
`-stdlib=`, …) are unused for IR input, so the UI adds
`-Wno-unused-command-line-argument` to keep `-Werror` from failing on them.

The pipeline query cannot be merged into the IR step: with `-disable-llvm-passes`,
clang prints only `verify,verify,print` and writes an empty `.ll` (exit code 0),
because printing the pipeline stops it before anything runs.

That printed string is fully parameterized (unroll thresholds, `simplifycfg`
options, coro passes, …) because clang builds it with the real `TargetMachine` and
the `PipelineTuningOptions` derived from every flag on the command line. It
round-trips through `PassBuilder::parsePassPipeline`, so `pass-diff` replays it
verbatim. Consequences worth knowing:

- The starting IR comes from `-Xclang -disable-llvm-passes` with **your** flags, not
  from `-O0`. Forcing `-O0` would tag every function `optnone` (so passes skip it)
  and change CodeGen decisions, giving you IR that no real compile produces. The
  UI also adds `-fno-discard-value-names`, because a release clang drops IR value
  names by default, which makes the diffs much harder to read.
- `pass-diff` builds its `TargetMachine` from the module's own triple and the
  per-function `target-cpu` / `target-features` attributes clang baked in. So
  `-march`, `-mtune`, and friends reach the cost models with no flag translation on
  our side, for any target.
- `-mllvm <opt>` flags are forwarded separately, because the handful of passes that
  read global `cl::opt` state instead of pipeline parameters would otherwise
  disagree with your compile.

Pass detection uses LLVM's real `PassInstrumentationCallbacks` API — it is not
scraping `opt` output.

## Install & run

See **[INSTALL.md](INSTALL.md)**. In short: LLVM ≥ 18 with clang of the same
version (distribution package or self-compiled), then

```
cd pass-diff
cmake -S . -B build -DLLVM_DIR=<llvm prefix>/lib/cmake/llvm && cmake --build build
cd .. && python3 -m venv ui/.venv && ui/.venv/bin/pip install -r ui/requirements.txt
ui/UI_run.sh
```

You only ever choose the `pass-diff` binary. The UI takes clang++ and
`llvm-cxxfilt` from the LLVM that `pass-diff` was built against (`pass-diff
toolchain-info`), so the two always match.

With no arguments the UI opens `test/src/hello_world.cpp`. To pick a file
and pass compiler flags — everything after `--` goes to clang untouched (all
examples run from the repository root):

```
ui/UI_run.sh --code test/src/hello_world.cpp -- -O3
```

The same flags drive the IR you start from, the assembly shown, and the pipeline
replayed, so all three describe one consistent compile.

| Option | Meaning |
|---|---|
| `--code FILE` | C++ source to analyze. Default `test/src/hello_world.cpp`. |
| `--output DIR` | Output folder for this run (created if missing). Default `tmp/<file>_<timestamp>`. |
| `--cache DIR` | Reopen the output folder of an earlier run (see below). Not combinable with `--code` or `--output`. |
| `--no-overwrite` | Keep earlier versions instead of overwriting them (see below). |
| `--pass-diff PATH` | `pass-diff` binary to use; clang comes from its LLVM. Default: with `--cache` the one of that run, else `pass-diff` on `PATH`, else `pass-diff/build/pass-diff`. |
| `--verbose` | Print every clang / `pass-diff` command. |
| `-- FLAGS...` | Compiler flags, handed to clang verbatim. |

### In the window

- **Function dropdown** above the pass list: like Compiler Explorer's Opt Pipeline
  view, each pass run is listed under the function it ran on; module passes are
  under `<Globals>`. Passes that changed the IR have a blue background.
- **Search field** for the pass list (Enter / Shift+Enter for next / previous hit,
  hits marked on the scrollbar).
- **Old IR / New IR**: side-by-side diff, scroll-linked. Deleted lines are red,
  inserted and changed lines green, and each panel's scrollbar marks where its
  colored lines are.
- **Hide Code/Assembly** collapses the top row.
- **Demangle** (on by default) shows demangled C++ names in the assembly, the
  function dropdown and the IR panels. It is display only: the files and
  `pass-diff` keep the mangled names, and the pass list is not demangled.

The assembly is in Intel syntax by default; `-- -masm=att` switches back.

### Output folders and `--cache`

Every run writes its results into an output folder. The terminal prints which
folder is used.

```
tmp/<file>_<YYYY-MM-DD_HH-MM-SS>/
  session.json          original source path, last used src-N / cfg-N
  src-N/                one per version of the source
    <file>.cpp          copy of that version — this copy is what gets compiled
    source.json         content hash
    cfg-N/              one per settings (compiler flags + pass-diff)
      config.json       flags, pass-diff path and LLVM version, clang path and version
      <file>.ll  <file>.s  pipeline.txt  passes.txt
      diffs/fK_rN.txt   show-diff output, saved on the first click
```

- If a `cfg-N` already holds the `.ll`, `.s`, `pipeline.txt` and `passes.txt`,
  they are loaded and clang and `pass-diff` are not called at all. A diff is
  computed only the first time you click a pass; after that it comes from
  `diffs/` (`K` is the function's position in the dropdown, `N` the run's position
  in its list — numbers, because mangled names can exceed the file name limit).
- The copy is compiled with `-iquote <folder of the original>`, so
  `#include "x.h"` next to the original still works.
- **Overwriting (default):** running again with `--output DIR` and a changed
  source overwrites the last used `src-N` (its `cfg-*` are deleted); changed
  flags or `pass-diff` (path or LLVM version) overwrite the last used `cfg-N`.
  **With `--no-overwrite`** they get a new `src-N` / `cfg-N` instead. Either way,
  a folder that already holds the same source content or the same settings is
  reused.
- **`--cache DIR`** reopens the last used source version in `DIR`. Its flags and
  `pass-diff` are those of the last used `cfg-N`, unless you give new flags after
  `--` or `--pass-diff`, which select (or compute) the matching `cfg-N`. Output
  folders written before `--pass-diff` existed cannot be reopened; `--cache`
  stops with a warning for them.

```
ui/UI_run.sh --code test/src/hello_world.cpp -- -O3   # computes, prints the folder
ui/UI_run.sh --cache tmp/hello_world_<timestamp>       # instant, same results
ui/UI_run.sh --cache tmp/hello_world_<timestamp> -- -O1
```

## Using `pass-diff` on its own

The C++ tool is independently usable and takes `.ll` input.

```
pass-diff list-passes <input.ll> [--pipeline <spec>] [--mllvm <opt>]...
pass-diff show-diff   <input.ll> <Function> <N> [--pipeline <spec>] [--mllvm <opt>]...
pass-diff toolchain-info
```

`toolchain-info` prints the LLVM version `pass-diff` was built against and that
LLVM's tools folder (`llvm-version 18.1.3` / `tools-dir /usr/lib/llvm-18/bin`).

`--pipeline` defaults to `default<O3>`; pass clang's printed pipeline for fidelity.

`list-passes` prints one tab-separated line per pass run, in execution order,
attributed to the function it ran on. Module passes are reported under
`<Globals>`, a CGSCC run gets one line per function in the SCC, and a loop pass
is labelled with its loop (`LICMPass on loop %for.body`):

```
$ pass-diff list-passes hello_world.ll
# Executed Passes and if they changed IR (pipeline: default<O3>)
<Globals>	MemProfRemoveInfo	did no changes
<Globals>	Annotation2MetadataPass	did no changes
<Globals>	ForceFunctionAttrsPass	did no changes
<Globals>	InferFunctionAttrsPass	did changes
<Globals>	CoroEarlyPass	did no changes
_Z3addii	EntryExitInstrumenterPass	did no changes
_Z3addii	LowerExpectIntrinsicPass	did no changes
_Z3addii	SimplifyCFGPass	did no changes
_Z3addii	SROAPass	did changes
...
```

`show-diff` prints the IR before and after the `N`-th (1-based) run in
`<Function>`'s list, in `--- IR BEFORE ---` / `+++ IR AFTER +++` sections —
`pass-diff show-diff hello_world.ll _Z3addii 4` is the `SROAPass` above. Function
runs show the function, loop runs the loop, module runs the whole module. Both
subcommands number runs the same way because they share one tracker; only the
watched function is ever serialized to text in `show-diff`.

Two implementation details that were not obvious:

- Before/after snapshots are paired with a **stack**, not a name→snapshot map,
  mirroring LLVM's own `ChangeReporter`. A map is wrong because passes rename the
  thing they run on — `LoopRotatePass` rewrites the loop header, so the after-lookup
  misses and the pass silently reports nothing.
- CGSCC and Loop IR units are rendered explicitly. Without that they stringify to
  `""`, compare equal, and every `InlinerPass` / `LICM` / `CoroSplitPass` is
  reported as "did no changes".

## Layout

```
pass-diff/
  src/main.cpp       the whole C++ tool
  CMakeLists.txt
  build/             default build folder, used by the UI if no other pass-diff is given
ui/
  UI.py              PySide6 app: source | assembly | pass list | old IR | new IR
  UI_run.sh          venv activation + launcher
  requirements.txt   Python dependencies
test/src/            sample inputs (hello_world.cpp, hello_opt.cpp)
tmp/                 output folders of UI runs
docs/                HTML documentation
```

Build artifacts and output (`tmp/`, `pass-diff/build/`, `pass-diff/build-*/`,
`ui/.venv/`) are gitignored.

## Known limitations

- One C++ translation unit at a time; no project-level build integration.
- The pass list is computed eagerly on the first run of a source and settings, so
  that costs one full pipeline run. Each diff costs another the first time it is
  clicked (the lazy part: only for the pass you asked about). Both are cached in
  the output folder.
- Only the source file is copied into the output folder; headers it includes are
  read from their original location and not versioned.
- Compiler flags can only be changed by restarting; the Source panel is editable
  but edits are not recompiled.
- No source-line ↔ IR mapping.
- Compilation errors are reported on stderr and exit, rather than in the UI.

## License

Copyright (c) 2026 Alina Weber.

LLVM-Whitebox is licensed under the Apache License v2.0 with LLVM Exceptions,
the same license as LLVM itself. See [LICENSE.txt](LICENSE.txt) for the full
text.

`SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception`

## Acknowledgments
This project was developed with the aid of AI to speed up coding.
