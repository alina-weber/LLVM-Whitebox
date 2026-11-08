# Copyright (c) 2026 Alina Weber
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import sys
import os
import subprocess
import argparse
import difflib
import re
import json
import shutil
import hashlib
import tempfile
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QSplitter,
    QTextEdit, QListWidget, QListWidgetItem,
    QToolBar, QPushButton, QWidget, QLabel,
    QVBoxLayout, QSizePolicy, QLineEdit, QHBoxLayout,
    QStyledItemDelegate, QStyle, QStyleOptionViewItem,
    QStyleOptionSlider, QScrollBar, QAbstractItemView, QComboBox, QListView
)
from PySide6.QtCore import Qt, Signal, QRect, QPoint
from PySide6.QtGui import (
    QColor, QTextCursor, QTextBlockFormat, QPainter, QFontMetrics, QPalette
)

source_path = ""
source_basename = ""
arg_verbose = False
output_path = ""  # the output folder of this session (see "Output folder")
run_dir = ""      # its cfg-N folder in use: all results of this run go here

# clang and pass-diff must come from the same LLVM, or the pipeline string and
# .ll may not parse. So only pass-diff is configured (--pass-diff, else
# `pass-diff` on PATH, else the default build folder) and clang++ is taken
# from the LLVM it was built against, as reported by `pass-diff toolchain-info`.
DEFAULT_PASS_DIFF = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "..", "pass-diff", "build", "pass-diff"))
PASS_DIFF_BIN = ""
LLVM_VERSION = ""
CLANG = ""

# ---------------------------
# Helpers
# ---------------------------

def run_cmd(argv, stderr):
    """Run a command given as an argv list (no shell).

    Must not use shell=True: the real pass pipeline string we hand to
    pass-diff is several KB of '<', '>', ';', '(' and ')', which a shell
    would interpret as redirections and subshells instead of passing through.
    """
    result = subprocess.run(argv, capture_output=True, text=True)
    if result.returncode != 0:
        return None, result.stderr
    if stderr:
        return result.stdout + result.stderr, None
    return result.stdout, None


def write_temp_file(content, suffix):
    """Write `content` to a new uniquely named file in the current run's
    folder and return its path."""
    fd, path = tempfile.mkstemp(suffix=f".{suffix}", prefix=f"{source_basename}_",
                                dir=run_dir)
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


# ---------------------------
# Output folder
# ---------------------------
#
# Every run writes its results into an output folder, by default
# tmp/<file>_<timestamp> in the repository root:
#
#   session.json        original source path, last used src-N / cfg-N
#   src-N/              one per source version
#     <file>.cpp        copy of that version (this copy is what gets compiled)
#     source.json       content hash
#     cfg-N/            one per settings (compiler flags + pass-diff)
#       config.json     flags, pass-diff + its LLVM version, clang
#       <file>.ll  <file>.s  pipeline.txt  passes.txt
#       diffs/fK_rN.txt show-diff output, saved on first click
#
# Without --no-overwrite a changed source or changed settings overwrite the
# last used src-N / cfg-N; with it they get a new folder. An existing folder
# with the same content is reused either way.

OUTPUT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tmp"))
RESULT_FILES = ("pipeline.txt", "passes.txt")  # .ll/.s are named after the source


def read_json(path):
    if not os.path.isfile(path):
        return {}
    with open(path) as f:
        return json.load(f)


def write_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def new_output_folder(source):
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    base = os.path.splitext(os.path.basename(source))[0]
    return os.path.join(OUTPUT_ROOT, f"{base}_{stamp}")


def _numbered_dirs(parent, prefix):
    """{n: path} of the existing `<prefix>-<n>` folders in `parent`."""
    found = {}
    if os.path.isdir(parent):
        for name in os.listdir(parent):
            if name.startswith(prefix + "-") and name[len(prefix) + 1:].isdigit():
                found[int(name[len(prefix) + 1:])] = os.path.join(parent, name)
    return found


def _pick_dir(parent, prefix, last, matches, no_overwrite):
    """Choose the `<prefix>-N` folder for this run. `matches(path)` tells
    whether an existing folder already holds what we need. Returns
    (path, reused); a folder that is not reused is empty."""
    existing = _numbered_dirs(parent, prefix)
    for path in existing.values():
        if matches(path):
            return path, True
    if not no_overwrite and last in existing.values():
        shutil.rmtree(last)
        path = last
    else:
        path = os.path.join(parent, f"{prefix}-{max(existing, default=0) + 1}")
    os.makedirs(path)
    return path, False


def select_source_version(out_dir, source, no_overwrite):
    """Find or create the src-N folder holding the current content of
    `source`. Returns the path of the copy inside it."""
    with open(source, "rb") as f:
        content = f.read()
    digest = hashlib.sha256(content).hexdigest()
    session = read_json(os.path.join(out_dir, "session.json"))
    last = session.get("src") and os.path.join(out_dir, session["src"])

    src_dir, reused = _pick_dir(
        out_dir, "src", last,
        lambda p: read_json(os.path.join(p, "source.json")).get("sha256") == digest,
        no_overwrite)
    copy = os.path.join(src_dir, os.path.basename(source))
    if not reused:
        with open(copy, "wb") as f:
            f.write(content)
        write_json(os.path.join(src_dir, "source.json"),
                   {"sha256": digest, "copied": datetime.now().isoformat(timespec="seconds")})

    session.update(source=os.path.abspath(source), src=os.path.basename(src_dir))
    if not reused or session.get("cfg_of") != session["src"]:
        session.pop("cfg", None)  # the last cfg belonged to another source
    write_json(os.path.join(out_dir, "session.json"), session)
    return copy


def select_config(out_dir, src_copy, flags, no_overwrite):
    """Find or create the cfg-N folder for these settings next to
    `src_copy`. Returns (path, complete); complete means all results of an
    earlier run are there and can be loaded instead of recomputed."""
    session = read_json(os.path.join(out_dir, "session.json"))
    src_dir = os.path.dirname(src_copy)
    last = session.get("cfg") and os.path.join(src_dir, session["cfg"])
    settings = {"flags": list(flags), "pass_diff": PASS_DIFF_BIN,
                "llvm_version": LLVM_VERSION}

    def matches(path):
        config = read_json(os.path.join(path, "config.json"))
        return {k: config.get(k) for k in settings} == settings

    cfg_dir, reused = _pick_dir(src_dir, "cfg", last, matches, no_overwrite)
    if not reused:
        version = subprocess.run([CLANG, "--version"], capture_output=True,
                                 text=True).stdout.splitlines()[:1]
        write_json(os.path.join(cfg_dir, "config.json"),
                   {**settings, "clang": CLANG, "clang_version": "".join(version),
                    "created": datetime.now().isoformat(timespec="seconds")})

    session.update(cfg=os.path.basename(cfg_dir), cfg_of=session["src"])
    write_json(os.path.join(out_dir, "session.json"), session)
    base = os.path.splitext(os.path.basename(src_copy))[0]
    needed = (f"{base}.ll", f"{base}.s", *RESULT_FILES)
    complete = all(os.path.isfile(os.path.join(cfg_dir, n)) for n in needed)
    return cfg_dir, complete


# ---------------------------
# Compile to IR + ASM
# ---------------------------

def compile_to_ir(cpp_path, flags, ll_path):
    """Emit the IR exactly as clang hands it to the optimizer, before any
    optimization pass has run.

    `flags` (including its -O level) must be the *same* flags used for
    compile_to_asm. Forcing -O0 here would be wrong: besides tagging every
    function 'optnone' (which makes optimizer passes skip it entirely), -O0
    also changes CodeGen-level decisions -- it adds 'noinline', keeps
    "frame-pointer"="all", and drops TBAA metadata -- so the starting IR
    would no longer match what a real compile at the requested -O level
    produces. -disable-llvm-passes alone stops the pipeline while leaving
    every other flag-driven decision intact.

    -fno-discard-value-names: a release clang drops IR value names by default
    (%0 instead of %a.addr). Names don't change what the optimizer or codegen
    does, but keeping them makes the IR diffs readable.
    """
    argv = [CLANG, *flags, "-fno-discard-value-names", "-Xclang", "-disable-llvm-passes",
            "-S", "-emit-llvm", cpp_path, "-o", ll_path]
    if arg_verbose:
        command = " ".join(argv)
        print("compile_to_ir() executes: " + command)
    _, err = run_cmd(argv, False)
    # TODO handle error from compilation
    return ll_path, err


# The C++ frontend dominates clang's runtime (~26 s of a 26 s compile for a
# coroutine-heavy C++20 file on a debug clang), so only compile_to_ir parses the source.
# compile_to_asm and extract_pipeline start from the .ll it wrote, which
# skips the frontend and gives the same pipeline and assembly. C++-only flags
# (-I, -stdlib=, ...) are unused for IR input; silence that warning so a
# user's -Werror doesn't turn it into a failure.
LL_INPUT_FLAGS = ["-Wno-unused-command-line-argument"]


def compile_to_asm(ll_path, flags, asm_path):
    # Intel syntax by default; it comes before the user's flags so a
    # -masm=att after '--' wins. Non-x86 targets ignore it.
    argv = [CLANG, "-masm=intel", *flags, *LL_INPUT_FLAGS, "-S", ll_path, "-o", asm_path]
    if arg_verbose:
        command = " ".join(argv)
        print("compile_to_asm() executes: " + command)
    _, err = run_cmd(argv, False)
    # TODO handle error from compilation
    return asm_path, err


def extract_pipeline(ll_path, flags):
    """Ask clang to print the actual pass pipeline it would run for `flags`.

    This is the whole point of the design: rather than reconstructing a
    pipeline and hoping it matches, we take clang's own. The printed text is
    fully parameterized (unroll thresholds, simplifycfg options, coro passes,
    ...) because clang builds it with the real TargetMachine and the
    PipelineTuningOptions derived from every flag on the command line. It
    round-trips through PassBuilder::parsePassPipeline, so pass-diff can
    replay it verbatim.

    Note this must be its own invocation: -print-pipeline-passes suppresses
    real codegen (the .s comes out empty), so it cannot be folded into
    compile_to_asm. The pipeline goes to stdout.

    It also cannot be folded into compile_to_ir: with -disable-llvm-passes
    clang prints only "verify,verify,print" and, since printing the pipeline
    stops before running it, writes an empty .ll (still exit code 0).
    """
    argv = [CLANG, *flags, *LL_INPUT_FLAGS, "-mllvm", "-print-pipeline-passes",
            "-S", ll_path, "-o", os.devnull]
    if arg_verbose:
        command = " ".join(argv)
        print("extract_pipeline() executes: " + command)
    output, err = run_cmd(argv, False)
    if err:
        return None, err
    pipeline = output.strip()
    if not pipeline:
        return None, "clang printed an empty pass pipeline"
    return pipeline, None


def read_text_file(path):
    with open(path, "r") as f:
        return f.read()


# ---------------------------
# Demangling (display only)
# ---------------------------

# An Itanium-mangled name anywhere in IR or asm text: @_Z.., %_Z.. (block
# labels), $_Z.. (comdats), _Z..: (asm labels). Not when glued to a preceding
# identifier character, so `.L_Z..` style tokens are left alone. Clone
# suffixes (.resume, .exit48.1.peel) are part of the match; llvm-cxxfilt
# prints them as "(.resume)".
MANGLED_NAME_RE = re.compile(r"(?<![\w.])_Z[\w.$]+")
_demangle_cache = {}


def demangle_names(names):
    """Demangle `names` with the active toolchain's llvm-cxxfilt, in one call
    for all names not seen before. Names it can't demangle map to themselves."""
    missing = sorted(set(names) - _demangle_cache.keys())
    if missing:
        cxxfilt = os.path.join(os.path.dirname(CLANG), "llvm-cxxfilt")
        try:
            result = subprocess.run([cxxfilt], input="\n".join(missing) + "\n",
                                    capture_output=True, text=True)
            demangled = result.stdout.splitlines()
            ok = result.returncode == 0 and len(demangled) == len(missing)
        except OSError:  # this LLVM install has no llvm-cxxfilt
            ok = False
        if not ok:
            demangled = missing  # leave text as is rather than mismatch names
        _demangle_cache.update(zip(missing, demangled))
    return {name: _demangle_cache[name] for name in names}


def demangle_text(text):
    """Replace every mangled name in `text` by its demangled form. The result
    is for display only: demangled names contain spaces and parentheses, so
    it is no longer valid IR/asm."""
    names = demangle_names(MANGLED_NAME_RE.findall(text))
    return MANGLED_NAME_RE.sub(lambda m: names[m.group(0)], text)


# ---------------------------
# pass-diff wiring
# ---------------------------

def extract_mllvm_opts(flags):
    """Pull the '-mllvm <opt>' pairs out of the user's clang flags.

    Nearly all pass behaviour is already pinned by the parameterized pipeline
    string, but a few passes read global cl::opt values directly. clang's
    -mllvm sets exactly those globals, so pass-diff has to be told about them
    separately to stay consistent with the compile.
    """
    opts = []
    i = 0
    while i < len(flags):
        if flags[i] == "-mllvm" and i + 1 < len(flags):
            opts.append(flags[i + 1])
            i += 2
        else:
            i += 1
    return opts


def _pass_diff_argv(subcommand, ll_path, pipeline, mllvm_opts, extra=()):
    argv = [PASS_DIFF_BIN, subcommand, ll_path, *extra, "--pipeline", pipeline]
    for opt in mllvm_opts:
        argv += ["--mllvm", opt]
    return argv

def run_list_passes(ll_path, pipeline, mllvm_opts=()):
    """Run `pass-diff list-passes` and return (raw output, err). The output
    is what passes.txt stores; parse it with parse_list_passes."""
    argv = _pass_diff_argv("list-passes", ll_path, pipeline, mllvm_opts)
    if arg_verbose:
        command = " ".join(argv)
        print("run_list_passes() executes: " + command)
    return run_cmd(argv, False)


def parse_list_passes(output):
    """Turn `list-passes` output into the runs per function.

    Like Compiler Explorer's Opt Pipeline view, every pass run is attributed to
    the function it ran on; module passes are listed under "<Globals>". The
    result maps each function name to its runs in pipeline order, as
    (label, changed) pairs. A run is addressed by its 1-based position in its
    function's list, which is what `show-diff` takes.
    """
    runs_by_function = {}
    for line in output.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        function, label, verdict = fields
        runs_by_function.setdefault(function, []).append(
            (label, verdict == "did changes"))
    return runs_by_function


def run_show_diff(ll_path, function, run_index, pipeline, mllvm_opts=(),
                  cache_path=None):
    """Run `pass-diff show-diff` and return the before/after IR of the
    `run_index`-th (1-based) run in `function`'s list.

    With `cache_path`, the raw output is read from that file if it exists,
    and otherwise saved there after a successful run."""
    if cache_path and os.path.isfile(cache_path):
        output = read_text_file(cache_path)
    else:
        argv = _pass_diff_argv("show-diff", ll_path, pipeline, mllvm_opts,
                               extra=(function, str(run_index)))
        if arg_verbose:
            command = " ".join(argv)
            print("run_show_diff() executes: " + command)
        output, err = run_cmd(argv, False)
        if err:
            return None, None, err
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w") as f:
                f.write(output)

    before_marker = "--- IR BEFORE ---"
    after_marker = "+++ IR AFTER +++"
    if before_marker not in output or after_marker not in output:
        return "", "", None

    _, _, rest = output.partition(before_marker)
    before_ir, _, after_ir = rest.partition(after_marker)
    return before_ir.strip(), after_ir.strip(), None


# ---------------------------
# Side-by-side line diff (IDE-merge-view style)
# ---------------------------

LINE_COLORS = {
    "delete": QColor(164, 65, 65),   # only present on the left (Old IR)
    "insert": QColor(71, 158, 65),    # only present on the right (New IR) alternatively 56, 130, 56
    "replace": QColor(71, 158, 65),  # present on both sides but differs
    "filler": QColor(50, 50, 50),    # padding to keep the other side aligned
}


def build_side_by_side_diff(before_text: str, after_text: str):
    """Align before/after lines like an IDE merge view.

    Returns (left_lines, right_lines), each a list of (text, tag) pairs of
    equal length, where tag is one of "equal", "delete", "insert",
    "replace", "filler". Row i on the left always corresponds to row i on
    the right, so displaying both lists top-aligned keeps matching lines
    beside each other and pads gaps with "filler" rows.
    """
    before_lines = before_text.splitlines()
    after_lines = after_text.splitlines()
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)

    left, right = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                left.append((before_lines[i1 + k], "equal"))
                right.append((after_lines[j1 + k], "equal"))
        elif tag == "delete":
            for k in range(i1, i2):
                left.append((before_lines[k], "delete"))
                right.append(("", "filler"))
        elif tag == "insert":
            for k in range(j1, j2):
                left.append(("", "filler"))
                right.append((after_lines[k], "insert"))
        elif tag == "replace":
            left_seg = before_lines[i1:i2]
            right_seg = after_lines[j1:j2]
            for k in range(max(len(left_seg), len(right_seg))):
                left.append((left_seg[k], "replace") if k < len(left_seg) else ("", "filler"))
                right.append((right_seg[k], "replace") if k < len(right_seg) else ("", "filler"))

    return left, right


def diff_spans(lines):
    """Scrollbar spans for one side of a diff: each run of consecutive rows
    with the same colored tag becomes one (start, end, color) span, in
    fractions of the row count. Equal and filler rows get none."""
    spans = []
    n = len(lines)
    row = 0
    while row < n:
        tag = lines[row][1]
        end = row
        while end < n and lines[end][1] == tag:
            end += 1
        if tag in ("delete", "insert", "replace"):
            spans.append((row / n, end / n, LINE_COLORS[tag]))
        row = end
    return spans


def link_scrollbars(a: QTextEdit, b: QTextEdit):
    """Make scrolling one text edit scroll the other by the same amount, in
    both directions, so aligned diff rows stay beside each other."""
    for get_bar in (QTextEdit.verticalScrollBar, QTextEdit.horizontalScrollBar):
        bar_a, bar_b = get_bar(a), get_bar(b)
        bar_a.valueChanged.connect(bar_b.setValue)
        bar_b.valueChanged.connect(bar_a.setValue)


def render_diff_lines(text_edit: QTextEdit, lines):
    """Fill a QTextEdit with one block per (text, tag) pair, coloring each
    block's full-width background according to LINE_COLORS."""
    text_edit.clear()
    cursor = text_edit.textCursor()
    for idx, (text, tag) in enumerate(lines):
        block_format = QTextBlockFormat()
        color = LINE_COLORS.get(tag)
        if color is not None:
            block_format.setBackground(color)
        if idx > 0:
            cursor.insertBlock(block_format)
        else:
            cursor.setBlockFormat(block_format)
        cursor.insertText(text)

    # Inserting via the cursor drags the viewport along with it; reset to
    # the top so a freshly selected pass always opens at line 1.
    cursor.movePosition(QTextCursor.Start)
    text_edit.setTextCursor(cursor)
    text_edit.verticalScrollBar().setValue(0)
    text_edit.horizontalScrollBar().setValue(0)


# ---------------------------
# Function selector
# ---------------------------

class FunctionComboBox(QComboBox):
    """Combo box whose popup stays inside a bounding widget's footprint.

    Many styles show the popup as a menu listing every entry at once, which
    with a few hundred mangled names covers the whole screen. The popup is
    capped at half the bounding widget's height and exactly its width, and
    scrolls both ways so long names can still be read in full.
    """

    def __init__(self, bounds_widget: QWidget, parent=None):
        super().__init__(parent)
        self.bounds_widget = bounds_widget
        # Without this, menu-style popups ignore any size limit.
        self.setStyleSheet("QComboBox { combobox-popup: 0; }")
        # A plain QListView sizes its content to the widest entry, so long
        # names scroll horizontally instead of being cut off.
        view = QListView()
        self.setView(view)
        view.setTextElideMode(Qt.ElideNone)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def showPopup(self):
        super().showPopup()
        popup = self.view().window()
        view = self.view()

        rows_height = view.sizeHintForRow(0) * self.count() if self.count() else 0
        frame = popup.height() - view.viewport().height()
        hscroll = view.horizontalScrollBar().sizeHint().height()
        height = min(rows_height + frame + hscroll, self.bounds_widget.height() // 2)

        top_left = self.bounds_widget.mapToGlobal(QPoint(0, 0))
        below_combo = self.mapToGlobal(QPoint(0, self.height()))
        popup.setGeometry(top_left.x(), below_combo.y(),
                          self.bounds_widget.width(), height)


# ---------------------------
# Pass list search (IDE-style)
# ---------------------------

# Match highlights are drawn with alpha so they tint whatever is underneath.
# The row background already carries meaning -- orange means "this pass changed
# the IR" -- so an opaque highlight would destroy that signal for exactly the
# rows a search is most likely to land on.
SEARCH_MATCH_COLOR = QColor(0x5f, 0xbf, 0x72)
SEARCH_CURRENT_COLOR = QColor(0x5f, 0xbf, 0x72)

# Scrollbar ticks sit on the groove, not on a row, so they can be opaque.
MARKER_COLOR = QColor(100, 100, 100)
MARKER_CURRENT_COLOR = QColor(255, 110, 40)
MARKER_HEIGHT = 3


class SearchLineEdit(QLineEdit):
    """Search field that reports Enter / Shift+Enter as next / previous.

    QLineEdit's returnPressed signal carries no modifier state, so the key
    event has to be intercepted to tell the two apart.
    """

    search_next = Signal()
    search_prev = Signal()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                self.search_prev.emit()
            else:
                self.search_next.emit()
            return
        super().keyPressEvent(event)


class MarkerScrollBar(QScrollBar):
    """Scrollbar that paints a tick per search hit, like an IDE gutter.

    Ticks are positioned by row fraction rather than by pixel offset, so they
    stay correct no matter how the list is scrolled or resized.
    """

    def __init__(self, orientation, parent=None):
        super().__init__(orientation, parent)
        self._markers = []       # fractions in [0, 1]
        self._current = None     # fraction of the active hit, or None
        self._spans = []         # (start, end, color), fractions in [0, 1]

    def set_markers(self, markers, current=None):
        self._markers = list(markers)
        self._current = current
        self.update()

    def set_spans(self, spans):
        """Colored ranges, e.g. the changed blocks of an IR diff. Each is drawn
        as tall as the range it covers, but at least MARKER_HEIGHT."""
        self._spans = list(spans)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._markers and self._current is None and not self._spans:
            return

        # Ask the style for the groove rect rather than using the whole widget:
        # the groove excludes the arrow buttons, which is where a tick drawn at
        # fraction 0.0 or 1.0 would otherwise land.
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.CC_ScrollBar, opt, QStyle.SC_ScrollBarGroove, self
        )
        usable = groove.height() - MARKER_HEIGHT
        if usable <= 0:
            return

        inset = 2
        width = max(1, groove.width() - 2 * inset)
        painter = QPainter(self)
        for start, end, color in self._spans:
            height = max(MARKER_HEIGHT, int((end - start) * groove.height()))
            y = min(groove.top() + int(start * groove.height()),
                    groove.bottom() + 1 - height)
            painter.fillRect(QRect(groove.left() + inset, y, width, height), color)
        for fraction in self._markers:
            y = groove.top() + int(fraction * usable)
            painter.fillRect(QRect(groove.left() + inset, y, width, MARKER_HEIGHT),
                             MARKER_COLOR)
        if self._current is not None:
            y = groove.top() + int(self._current * usable)
            painter.fillRect(QRect(groove.left() + inset, y, width, MARKER_HEIGHT),
                             MARKER_CURRENT_COLOR)
        painter.end()


class PassItemDelegate(QStyledItemDelegate):
    """Draws pass names, highlighting the matched substring inside each hit.

    Done in a delegate rather than by recoloring items because the item
    background is already in use to mark passes that changed the IR. Painting
    the highlight behind just the matched characters leaves that intact and is
    what an IDE search actually looks like.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._query = ""
        self._current_row = -1

    def set_search(self, query: str, current_row: int):
        self._query = query
        self._current_row = current_row

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text

        style = opt.widget.style() if opt.widget else QApplication.style()
        # Text rect must be taken while the option still carries its text --
        # some styles size this rect from the string.
        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget)

        # Let the style paint background, selection and focus, but not the
        # text: the highlight has to go underneath the glyphs, so the text is
        # drawn by hand afterwards.
        opt.text = ""
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)

        painter.save()
        painter.setFont(opt.font)

        if self._query:
            metrics = QFontMetrics(opt.font)
            lowered = text.lower()
            needle = self._query.lower()
            color = (SEARCH_CURRENT_COLOR if index.row() == self._current_row
                     else SEARCH_MATCH_COLOR)
            start = 0
            while True:
                pos = lowered.find(needle, start)
                if pos < 0:
                    break
                x = text_rect.left() + metrics.horizontalAdvance(text[:pos])
                w = metrics.horizontalAdvance(text[pos:pos + len(needle)])
                painter.fillRect(QRect(x, text_rect.top(), w, text_rect.height()),
                                 color)
                start = pos + len(needle)

        role = (QPalette.HighlightedText if opt.state & QStyle.State_Selected
                else QPalette.Text)
        painter.setPen(opt.palette.color(role))
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, text)
        painter.restore()


# ---------------------------
# UI
# ---------------------------

def labeled(title: str, widget: QWidget) -> QWidget:
    """Wrap a widget with a bold header label above it."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)

    label = QLabel(title)
    label.setStyleSheet("font-weight: bold; padding: 2px 4px;")
    layout.addWidget(label)
    layout.addWidget(widget)
    return container


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LLVM Optimization Debugger")
        self.ll_path = None
        self.pipeline = None
        self.mllvm_opts = ()

        # --- Toolbar with toggle button ---
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        # Active toggle buttons are orange (same orange as the active search
        # marker); inactive ones keep the normal theme look.
        toolbar.setStyleSheet(
            "QPushButton:checked { background-color: rgb(255, 110, 40); color: black;"
            " border: 1px solid rgb(200, 80, 20); border-radius: 3px; padding: 4px 8px; }")
        # Spacer widget to push button to the right
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)

        self.toggle_btn = QPushButton("Hide Code/Assembly")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.clicked.connect(self.toggle_top_panel)
        toolbar.addWidget(self.toggle_btn)

        # Demangling only changes what is displayed. The raw texts are kept so
        # toggling just redraws, and pass-diff keeps getting mangled names.
        self.demangle_btn = QPushButton("Demangle")
        self.demangle_btn.setCheckable(True)
        # On by default; set before connecting, the panels don't exist yet.
        self.demangle_btn.setChecked(True)
        self.demangle_btn.toggled.connect(self.on_demangle_toggled)
        toolbar.addWidget(self.demangle_btn)
        self.addToolBar(toolbar)
        self._asm_raw = ""
        self._ir_raw = None  # (before, after) of the selected run

        # --- Top panel: C++ source | Assembly ---
        self.source_edit = QTextEdit()
        self.source_edit.setPlaceholderText("C++ source...")
        self.source_edit.setFontFamily("Monospace")

        self.asm_edit = QTextEdit()
        self.asm_edit.setReadOnly(True)
        self.asm_edit.setFontFamily("Monospace")
        self.asm_edit.setPlaceholderText("Assembly output...")

        self.top_splitter = QSplitter(Qt.Horizontal)
        self.top_splitter.addWidget(labeled("Source Code", self.source_edit))
        self.top_splitter.addWidget(labeled("Final Assembly", self.asm_edit))

        # --- Bottom panel: pass list | IR before | IR after ---
        self.pass_list = QListWidget()
        self.pass_list.currentItemChanged.connect(self.on_pass_selected)

        self.pass_scrollbar = MarkerScrollBar(Qt.Vertical)
        self.pass_list.setVerticalScrollBar(self.pass_scrollbar)
        # Keep the bar present even when the list fits: the tick marks are the
        # overview of where the hits are, and a bar that appears and vanishes
        # takes that overview with it.
        self.pass_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        self.pass_delegate = PassItemDelegate(self.pass_list)
        self.pass_list.setItemDelegate(self.pass_delegate)

        self._matches = []    # row indices of current hits, in list order
        self._match_pos = -1  # index into self._matches, -1 when there are none

        self.search_edit = SearchLineEdit()
        self.search_edit.setPlaceholderText("Search passes... (Enter / Shift+Enter)")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.on_search_changed)
        self.search_edit.search_next.connect(lambda: self.jump_to_match(1))
        self.search_edit.search_prev.connect(lambda: self.jump_to_match(-1))

        self.search_count = QLabel("")
        self.search_count.setStyleSheet("color: gray; padding: 0 4px;")

        search_row = QWidget()
        search_layout = QHBoxLayout(search_row)
        search_layout.setContentsMargins(0, 0, 0, 0)
        search_layout.setSpacing(2)
        search_layout.addWidget(self.search_edit)
        search_layout.addWidget(self.search_count)

        # Selects which function's passes the list shows (like Compiler
        # Explorer's Opt Pipeline view).
        pass_panel = QWidget()

        self.runs_by_function = {}
        self.function_combo = FunctionComboBox(bounds_widget=pass_panel)
        self.function_combo.setPlaceholderText("Select function...")
        # Mangled names are long; don't let the longest one widen the panel.
        self.function_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.function_combo.setMinimumContentsLength(10)
        # By index, not text: the displayed text may be demangled, the real
        # (mangled) name lives in the item data.
        self.function_combo.currentIndexChanged.connect(self.on_function_selected)

        pass_layout = QVBoxLayout(pass_panel)
        pass_layout.setContentsMargins(0, 0, 0, 0)
        pass_layout.setSpacing(2)
        pass_layout.addWidget(self.function_combo)
        pass_layout.addWidget(search_row)
        pass_layout.addWidget(self.pass_list)

        self.ir_before = QTextEdit()
        self.ir_before.setReadOnly(True)
        self.ir_before.setFontFamily("Monospace")
        self.ir_before.setLineWrapMode(QTextEdit.NoWrap)

        self.ir_after = QTextEdit()
        self.ir_after.setReadOnly(True)
        self.ir_after.setFontFamily("Monospace")
        self.ir_after.setLineWrapMode(QTextEdit.NoWrap)

        # Each bar marks where its own panel is colored (Old IR: deleted and
        # changed rows, New IR: inserted and changed rows). Set before linking,
        # which connects whatever bars the edits have at that point.
        self.ir_before.setVerticalScrollBar(MarkerScrollBar(Qt.Vertical))
        self.ir_after.setVerticalScrollBar(MarkerScrollBar(Qt.Vertical))
        link_scrollbars(self.ir_before, self.ir_after)
        # Always shown, as for the pass list: otherwise a diff that fits on
        # screen has no scrollbar and so no markers.
        self.ir_before.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.ir_after.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)

        self.bottom_splitter = QSplitter(Qt.Horizontal)
        self.bottom_splitter.addWidget(labeled("Compiler Passes", pass_panel))
        self.bottom_splitter.addWidget(labeled("Old IR", self.ir_before))
        self.bottom_splitter.addWidget(labeled("New IR", self.ir_after))

        # --- Root splitter ---
        self.root_splitter = QSplitter(Qt.Vertical)
        self.root_splitter.addWidget(self.top_splitter)
        self.root_splitter.addWidget(self.bottom_splitter)
        # Remember the last sizes so we can restore them after un-hiding
        self.root_splitter.splitterMoved.connect(self._save_top_size)
        self._top_size = None  # will be set on first splitterMoved or toggle

        self.setCentralWidget(self.root_splitter)

    # ------------------------------------------------------------------
    # Toggle top panel
    # ------------------------------------------------------------------

    def _save_top_size(self):
        sizes = self.root_splitter.sizes()
        if sizes[0] > 0:
            self._top_size = sizes[0]

    def toggle_top_panel(self, checked: bool):
        if checked:
            # Collapse: save current top height, set it to 0
            sizes = self.root_splitter.sizes()
            if sizes[0] > 0:
                self._top_size = sizes[0]
            total = sum(sizes)
            self.root_splitter.setSizes([0, total])
            self.toggle_btn.setText("Show Code/Assembly")
        else:
            # Restore
            total = sum(self.root_splitter.sizes())
            restored = self._top_size if self._top_size else total // 3
            self.root_splitter.setSizes([restored, total - restored])
            self.toggle_btn.setText("Hide Code/Assembly")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Set a sensible default split on first show (top ~35%, bottom ~65%)
        if self._top_size is None:
            total = self.root_splitter.height()
            if total > 0:
                top = int(total * 0.35)
                self.root_splitter.setSizes([top, total - top])
                self._top_size = top

    # ------------------------------------------------------------------
    # Demangling
    # ------------------------------------------------------------------

    def _display(self, text: str) -> str:
        return demangle_text(text) if self.demangle_btn.isChecked() else text

    def set_asm(self, asm: str):
        self._asm_raw = asm
        self.asm_edit.setPlainText(self._display(asm))

    def on_demangle_toggled(self, checked: bool):
        # Keep the asm view where it was; only the names change.
        bar = self.asm_edit.verticalScrollBar()
        pos = bar.value()
        self.asm_edit.setPlainText(self._display(self._asm_raw))
        bar.setValue(pos)

        for i in range(self.function_combo.count()):
            self.function_combo.setItemText(
                i, self._display(self.function_combo.itemData(i)))

        if self._ir_raw is not None:
            self._render_ir()

    # ------------------------------------------------------------------
    # Pass list population and selection
    # ------------------------------------------------------------------

    def set_ll_path(self, ll_path: str):
        self.ll_path = ll_path

    def set_pipeline(self, pipeline: str, mllvm_opts=()):
        """The real pass pipeline clang reported for this compile; replayed by
        pass-diff so the passes shown are the ones that actually ran."""
        self.pipeline = pipeline
        self.mllvm_opts = mllvm_opts

    def populate_functions(self, runs_by_function: dict):
        """Call this with the output of `run_list_passes`."""
        self.runs_by_function = runs_by_function
        self.function_combo.clear()
        for function in runs_by_function:
            self.function_combo.addItem(self._display(function), function)
        self.function_combo.setCurrentIndex(0)

    def on_function_selected(self, index: int):
        self._ir_raw = None
        self.ir_before.clear()
        self.ir_after.clear()
        self._set_ir_spans([], [])
        function = self.function_combo.itemData(index)
        self.populate_passes(self.runs_by_function.get(function, []))

    def populate_passes(self, runs):
        """Fill the list with one function's runs; each item remembers its
        1-based position, which show-diff uses to find the run."""
        self.pass_list.clear()
        for run_index, (label, changed) in enumerate(runs, start=1):
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, run_index)
            if changed:
                item.setBackground(QColor("#41779E"))
            self.pass_list.addItem(item)
        # Row indices just changed, so any recorded hits are stale.
        self.on_search_changed(self.search_edit.text())

    # ------------------------------------------------------------------
    # Pass search
    # ------------------------------------------------------------------

    def _row_fraction(self, row: int) -> float:
        """Map a row index onto the scrollbar groove, so the last row's tick
        lands at the bottom rather than one row short of it."""
        last = self.pass_list.count() - 1
        return row / last if last > 0 else 0.0

    def on_search_changed(self, query: str):
        """Recompute hits from scratch and jump to the first one, as
        incremental search in an IDE does."""
        needle = query.lower()
        self._matches = [
            row for row in range(self.pass_list.count())
            if needle and needle in self.pass_list.item(row).text().lower()
        ]
        self._match_pos = 0 if self._matches else -1
        self._apply_search_state(scroll=True)

    def jump_to_match(self, step: int):
        """Advance the active hit by `step`, wrapping at either end."""
        if not self._matches:
            return
        self._match_pos = (self._match_pos + step) % len(self._matches)
        self._apply_search_state(scroll=True)

    def _apply_search_state(self, scroll: bool):
        """Push current search state into the delegate, the scrollbar ticks and
        the hit counter, then optionally scroll the active hit into view.

        Deliberately does not touch the list selection: selecting a pass runs
        show-diff, which replays the whole pipeline, so cycling through hits
        would stall for seconds per keypress. The active hit is shown by its
        highlight and by the scrollbar tick instead.
        """
        query = self.search_edit.text()
        current_row = self._matches[self._match_pos] if self._matches else -1

        self.pass_delegate.set_search(query, current_row)
        self.pass_list.viewport().update()

        self.pass_scrollbar.set_markers(
            [self._row_fraction(row) for row in self._matches],
            self._row_fraction(current_row) if current_row >= 0 else None,
        )

        if not query:
            self.search_count.setText("")
        elif self._matches:
            self.search_count.setText(f"{self._match_pos + 1} / {len(self._matches)}")
        else:
            self.search_count.setText("no matches")

        if scroll and current_row >= 0:
            self.pass_list.scrollToItem(self.pass_list.item(current_row),
                                        QAbstractItemView.PositionAtCenter)

    def on_pass_selected(self, item: QListWidgetItem):
        self._ir_raw = None
        self._set_ir_spans([], [])
        if item is None or self.ll_path is None or self.pipeline is None:
            return
        function = self.function_combo.currentData()
        run_index = item.data(Qt.UserRole)
        # Numbered, not named: mangled names can exceed the file name limit.
        function_index = list(self.runs_by_function).index(function) + 1
        cache_path = os.path.join(run_dir, "diffs", f"f{function_index}_r{run_index}.txt")
        before, after, err = run_show_diff(self.ll_path, function, run_index,
                                           self.pipeline, self.mllvm_opts,
                                           cache_path=cache_path)
        if err:
            self.ir_before.setPlainText(f"Error running show-diff: {err}")
            self.ir_after.setPlainText("")
            return
        if not before and not after:
            placeholder = f"(no snapshot captured for {item.text()})"
            self.ir_before.setPlainText(placeholder)
            self.ir_after.setPlainText(placeholder)
            return

        self._ir_raw = (before, after)
        self._render_ir()

    def _render_ir(self):
        """Diff the (possibly demangled) texts, so both sides line up on what
        is actually shown."""
        before, after = self._ir_raw
        left_lines, right_lines = build_side_by_side_diff(
            self._display(before), self._display(after))
        render_diff_lines(self.ir_before, left_lines)
        render_diff_lines(self.ir_after, right_lines)
        self._set_ir_spans(diff_spans(left_lines), diff_spans(right_lines))

    def _set_ir_spans(self, left_spans, right_spans):
        self.ir_before.verticalScrollBar().set_spans(left_spans)
        self.ir_after.verticalScrollBar().set_spans(right_spans)


def split_own_and_compiler_args(argv):
    """Split argv at the first '--' into (our args, compiler flags).

    Everything after '--' is handed to clang untouched, so any flag clang
    understands works here -- and the same flags drive the pass pipeline we
    replay, which is what keeps the tool consistent with a real compile.
    """
    if "--" in argv:
        idx = argv.index("--")
        return argv[:idx], argv[idx + 1:]
    return argv, []


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def find_pass_diff(explicit):
    """The pass-diff binary to use: `explicit` (--pass-diff), else
    `pass-diff` on PATH, else the default build folder."""
    path = explicit or shutil.which("pass-diff") or DEFAULT_PASS_DIFF
    path = os.path.abspath(os.path.expanduser(path))
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        fail(f"pass-diff not found at {path}. Build it (see INSTALL.md) or "
             f"pass its path with --pass-diff.")
    return path


def set_toolchain(pass_diff):
    """Use `pass-diff` and the clang++ of the LLVM it was built against."""
    global PASS_DIFF_BIN, LLVM_VERSION, CLANG
    result = subprocess.run([pass_diff, "toolchain-info"], capture_output=True, text=True)
    info = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    if result.returncode != 0 or "tools-dir" not in info:
        fail(f"{pass_diff} does not support 'toolchain-info'; it was probably "
             f"built from an older version. Rebuild it (see INSTALL.md).")
    clang = os.path.join(info["tools-dir"], "clang++")
    if not os.path.isfile(clang):
        fail(f"clang++ not found at {clang}.\n{pass_diff} was built against "
             f"LLVM {info['llvm-version']}, and clang++ must come from that same "
             f"LLVM. Install its clang (e.g. the clang-<version> package) or "
             f"build pass-diff against an LLVM that includes clang.")
    PASS_DIFF_BIN, LLVM_VERSION, CLANG = pass_diff, info["llvm-version"], clang
    print(f"Using pass-diff {pass_diff} (LLVM {LLVM_VERSION}), clang++ {clang}")


def prepare_run(code, output, cache, compiler_flags, pass_diff, no_overwrite):
    """Pick the output folder and its src-N / cfg-N for this run, then load
    the results from there or compute them. Sets the module globals and
    returns (source copy, .ll, .s, pipeline, list-passes output, mllvm opts)."""
    global source_path, source_basename, output_path, run_dir

    if cache is not None:
        # Reopen the last used source version. Flags and pass-diff are those
        # of its last used settings unless new ones are given.
        output_path = cache
        session = read_json(os.path.join(output_path, "session.json"))
        if not session.get("src"):
            fail(f"{output_path} is not an output folder of an earlier run "
                 f"(no session.json).")
        original = session["source"]
        src_copy = os.path.join(output_path, session["src"], os.path.basename(original))
        if session.get("cfg"):
            config = read_json(os.path.join(os.path.dirname(src_copy),
                                            session["cfg"], "config.json"))
            if "pass_diff" not in config:
                fail(f"Warning: {output_path} was written by an older version of "
                     f"this tool (its config.json names no pass-diff) and cannot "
                     f"be reopened with --cache. Start a new run with --code.")
            if not compiler_flags:
                compiler_flags = config["flags"]
            if pass_diff is None:
                pass_diff = config["pass_diff"]
    else:
        original = code
        output_path = output if output is not None else new_output_folder(code)
        os.makedirs(output_path, exist_ok=True)

    set_toolchain(find_pass_diff(pass_diff))

    if cache is None:
        src_copy = select_source_version(output_path, original, no_overwrite)
    run_dir, complete = select_config(output_path, src_copy, compiler_flags,
                                      no_overwrite)
    source_path = src_copy
    source_basename = os.path.splitext(os.path.basename(src_copy))[0]
    print(f"Output folder: {os.path.relpath(run_dir)}")

    ll_path = os.path.join(run_dir, source_basename + ".ll")
    asm_path = os.path.join(run_dir, source_basename + ".s")
    pipeline_path = os.path.join(run_dir, "pipeline.txt")
    passes_path = os.path.join(run_dir, "passes.txt")
    mllvm_opts = extract_mllvm_opts(compiler_flags)

    if complete:
        print("Loading results of an earlier run")
        return (src_copy, ll_path, asm_path, read_text_file(pipeline_path),
                read_text_file(passes_path), mllvm_opts)

    # All three invocations get the identical user flags, so the IR we start
    # from, the assembly we show, and the pipeline we replay all describe the
    # same compile. Only the IR step parses the C++ source; the other two
    # start from its .ll (see LL_INPUT_FLAGS). The source compiled is the copy
    # in src-N; -iquote keeps `#include "x.h"` next to the original working.
    include_dir = os.path.dirname(os.path.abspath(original))
    print("Compiling to IR ...")
    _, err = compile_to_ir(src_copy, [*compiler_flags, "-iquote", include_dir], ll_path)
    print("Finished")
    if err:
        fail(f"Compilation failed:\n{err}")
    print("Compiling to Assembly ...")
    _, err = compile_to_asm(ll_path, compiler_flags, asm_path)
    print("Finished")
    if err:
        fail(f"Compilation failed:\n{err}")
    print("Extract Pipeline")
    pipeline, err = extract_pipeline(ll_path, compiler_flags)
    print("Finished")
    if err:
        fail(f"Could not determine clang's pass pipeline:\n{err}")
    passes, err = run_list_passes(ll_path, pipeline, mllvm_opts)
    if err:
        fail(f"pass-diff list-passes failed:\n{err}")

    # passes.txt last: its presence is what marks the folder as complete.
    with open(pipeline_path, "w") as f:
        f.write(pipeline)
    with open(passes_path, "w") as f:
        f.write(passes)
    return src_copy, ll_path, asm_path, pipeline, passes, mllvm_opts


def main():
    global arg_verbose
    parser = argparse.ArgumentParser(
        description="Run LLVM-Whitebox prototype",
        epilog="Compiler flags may be passed after '--', e.g.: "
               "UI.py --code foo.cpp -- -std=c++23 -O3",
    )

    default_code = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "test", "src", "hello_world.cpp"
    )
    parser.add_argument("--code", required=False,
                         help="File path to source code file. Supported languages: C++. "
                              "Default: test/src/hello_world.cpp")
    folder = parser.add_mutually_exclusive_group()
    folder.add_argument("--output", required=False,
                        help="Path to output folder. Folder is generated if missing. "
                             "Default: tmp/<file>_<timestamp>")
    folder.add_argument("--cache", required=False,
                        help="Reopen the output folder of an earlier run: its last used "
                             "source, and its last used settings unless new flags are given.")
    parser.add_argument("--no-overwrite", required=False, action='store_true',
                        help="Keep earlier versions: a changed source or changed settings get "
                             "a new src-N / cfg-N folder instead of overwriting the last one.")
    parser.add_argument("--verbose", required=False, action='store_true', help="Print commands to CLang and LLVM.")
    parser.add_argument("--pass-diff", required=False,
                        help="Path to the pass-diff binary. clang++ is taken from the LLVM it "
                             "was built against. Default: the one of --cache's last run, else "
                             "pass-diff on PATH, else pass-diff/build/pass-diff.")

    own_args, compiler_flags = split_own_and_compiler_args(sys.argv[1:])
    args = parser.parse_args(own_args)
    if args.cache is not None and args.code is not None:
        parser.error("--cache reopens the source stored in the output folder; "
                     "it cannot be combined with --code")
    arg_verbose = args.verbose

    src_copy, ll_path, asm_path, pipeline, passes, mllvm_opts = prepare_run(
        args.code or default_code, args.output, args.cache, compiler_flags,
        args.pass_diff, args.no_overwrite)

    app = QApplication(sys.argv[:1])
    w = MainWindow()
    w.resize(1400, 900)
    w.set_ll_path(ll_path)
    w.set_pipeline(pipeline, mllvm_opts)
    w.source_edit.setPlainText(read_text_file(src_copy))
    w.set_asm(read_text_file(asm_path))
    w.populate_functions(parse_list_passes(passes))

    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
