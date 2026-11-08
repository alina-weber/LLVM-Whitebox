// Copyright (c) 2026 Alina Weber
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

#include "llvm/Analysis/LazyCallGraph.h"
#include "llvm/Analysis/LoopInfo.h"
#include "llvm/Config/llvm-config.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Verifier.h"
#include "llvm/IRReader/IRReader.h"
#include "llvm/MC/TargetRegistry.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/StandardInstrumentations.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Error.h"
#include "llvm/Support/SourceMgr.h"
#include "llvm/Support/TargetSelect.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Target/TargetMachine.h"
#include "llvm/Target/TargetOptions.h"
#include "llvm/TargetParser/Triple.h"

#include <cstdlib>
#include <functional>
#include <memory>
#include <vector>

using namespace llvm;

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

static std::string moduleToString(const Module &M) {
    std::string buf;
    raw_string_ostream os(buf);
    M.print(os, nullptr);
    os.flush();
    return buf;
}

static std::string functionToString(const Function &F) {
    std::string buf;
    raw_string_ostream os(buf);
    F.print(os, nullptr);
    os.flush();
    return buf;
}

// Render the IR unit as text so before/after can be compared.
//
// Module and Function are the easy cases. The other two matter more than they
// look: CGSCC passes (CoroSplitPass, InlinerPass, ArgumentPromotionPass) and
// Loop passes (LICM, LoopRotate, LoopFullUnroll) previously fell through to ""
// here, so every one of them compared ""=="" and was reported as "did no
// changes" with empty IR. Mirrors printIR() in StandardInstrumentations.cpp.
static std::string irSnapshot(Any IR) {
    if (const auto **F = any_cast<const Function *>(&IR))
        return functionToString(**F);
    if (const auto **M = any_cast<const Module *>(&IR))
        return moduleToString(**M);
    if (const auto **C = any_cast<const LazyCallGraph::SCC *>(&IR)) {
        std::string buf;
        raw_string_ostream os(buf);
        for (const LazyCallGraph::Node &N : **C) {
            const Function &F = N.getFunction();
            if (!F.isDeclaration())
                F.print(os, nullptr);
        }
        os.flush();
        return buf;
    }
    if (const auto **L = any_cast<const Loop *>(&IR)) {
        std::string buf;
        raw_string_ostream os(buf);
        // const_cast: LLVM < 21 declares printLoop(Loop &) though it only reads.
        printLoop(const_cast<Loop &>(**L), os);
        os.flush();
        return buf;
    }
    return "";
}

// ---------------------------------------------------------------------------
// Per-function pass runs (Compiler Explorer's Opt Pipeline model)
// ---------------------------------------------------------------------------

// Pseudo function name under which module passes are reported.
static const char *const GlobalsUnit = "<Globals>";

// One entry in one function's pass list.
//
// The pipeline runs function by function, so the list is organised the same
// way Compiler Explorer does it: every pass run is attributed to the function
// it ran on. A CGSCC run is split into one entry per function of the SCC, each
// with only that function's IR. A Loop run goes to the loop's function and
// shows only the loop. A Module run goes to the "<Globals>" pseudo function
// and shows the whole module.
struct PassRun {
    std::string Unit;   // function name, or GlobalsUnit
    std::string Label;  // PassID, plus the loop for Loop passes
    std::string Before;
    std::string After;
    bool Invalidated;   // the pass invalidated its IR unit; no after IR exists
};

// Before/after snapshots are paired with a stack, not a key->snapshot map,
// following LLVM's own ChangeReporter in StandardInstrumentations.cpp.
//
// A map keyed on the IR unit's name looks natural but is wrong: passes may
// rename the very thing they run on. LoopRotatePass rewrites the loop header,
// which changes the loop's name, so the after-lookup misses and the pass
// silently produces no output at all (measured: 3 of 7 LoopRotatePass
// occurrences vanished). CoroSplitPass has the same problem -- it adds
// .resume/.destroy functions to its SCC.
//
// Passes nest properly, so a stack pairs them exactly. The invalidated
// callback must also pop, or the stack desynchronises for every pass after it.
//
// list-passes and show-diff both use this class so that they number a
// function's runs identically. `Wants` decides which units get snapshotted:
// everything for list-passes, only the watched function for show-diff.
class PassRunTracker {
public:
    using WantsFn = std::function<bool(StringRef Unit)>;
    using SinkFn  = std::function<void(const PassRun &)>;

    PassRunTracker(const Module &M, WantsFn Wants, SinkFn Sink)
        : M(M), Wants(std::move(Wants)), Sink(std::move(Sink)) {}

    void registerOn(PassInstrumentationCallbacks &PIC) {
        PIC.registerBeforeNonSkippedPassCallback([this](StringRef PassID, Any IR) {
            BeforeStack.push_back(makeFrame(PassID, IR));
        });

        PIC.registerAfterPassCallback(
            [this](StringRef PassID, Any IR, const PreservedAnalyses &PA) {
                if (BeforeStack.empty()) return;
                Frame frame = std::move(BeforeStack.back());
                BeforeStack.pop_back();
                finish(frame, &IR);
            });

        PIC.registerAfterPassInvalidatedCallback(
            [this](StringRef PassID, const PreservedAnalyses &PA) {
                if (BeforeStack.empty()) return;
                Frame frame = std::move(BeforeStack.back());
                BeforeStack.pop_back();
                finish(frame, nullptr);
            });
    }

private:
    // One in-flight pass: the runs it will report, with before snapshots.
    struct Frame {
        std::string PassID;
        std::vector<PassRun> Runs;
    };

    PassRun makeRun(std::string Unit, std::string Label, const Any &IR) {
        PassRun run{std::move(Unit), std::move(Label), "", "", false};
        if (Wants(run.Unit))
            run.Before = irSnapshot(IR);
        return run;
    }

    Frame makeFrame(StringRef PassID, Any IR) {
        Frame frame{PassID.str(), {}};
        if (const auto **F = any_cast<const Function *>(&IR)) {
            frame.Runs.push_back(makeRun((*F)->getName().str(), PassID.str(), IR));
        } else if (any_cast<const Module *>(&IR)) {
            frame.Runs.push_back(makeRun(GlobalsUnit, PassID.str(), IR));
        } else if (const auto **L = any_cast<const Loop *>(&IR)) {
            frame.Runs.push_back(makeRun(
                (*L)->getHeader()->getParent()->getName().str(),
                PassID.str() + " on loop %" + (*L)->getName().str(), IR));
        } else if (const auto **C = any_cast<const LazyCallGraph::SCC *>(&IR)) {
            for (const LazyCallGraph::Node &N : **C) {
                const Function &F = N.getFunction();
                if (F.isDeclaration())
                    continue;
                PassRun run{F.getName().str(), PassID.str(), "", "", false};
                if (Wants(run.Unit))
                    run.Before = functionToString(F);
                frame.Runs.push_back(std::move(run));
            }
        }
        return frame;
    }

    // `IR` is null when the pass invalidated its unit.
    void finish(Frame &frame, const Any *IR) {
        if (!IR) {
            for (PassRun &run : frame.Runs)
                run.Invalidated = true;
        } else if (const auto *C = any_cast<const LazyCallGraph::SCC *>(IR)) {
            // Look functions up by name rather than through the SCC: the pass
            // may have deleted a function or moved it to another SCC.
            for (PassRun &run : frame.Runs) {
                const Function *F = M.getFunction(run.Unit);
                if (F && !F->isDeclaration() && Wants(run.Unit))
                    run.After = functionToString(*F);
            }
            // Functions the pass created (e.g. CoroSplitPass's .resume and
            // .destroy) have no before IR.
            for (const LazyCallGraph::Node &N : **C) {
                const Function &F = N.getFunction();
                if (F.isDeclaration())
                    continue;
                bool known = false;
                for (const PassRun &run : frame.Runs)
                    known |= (run.Unit == F.getName());
                if (known)
                    continue;
                PassRun run{F.getName().str(), frame.PassID, "", "", false};
                if (Wants(run.Unit))
                    run.After = functionToString(F);
                frame.Runs.push_back(std::move(run));
            }
        } else {
            for (PassRun &run : frame.Runs)
                if (Wants(run.Unit))
                    run.After = irSnapshot(*IR);
        }

        for (const PassRun &run : frame.Runs)
            Sink(run);
    }

    const Module &M;
    WantsFn Wants;
    SinkFn Sink;
    // Frames of the in-flight passes, innermost last.
    std::vector<Frame> BeforeStack;
};

// ---------------------------------------------------------------------------
// Pipeline helpers
// ---------------------------------------------------------------------------

// Build a TargetMachine matching the module we were handed.
//
// Without this, PassBuilder gets TM=nullptr and PassRegistry.def falls back to
// a bare TargetIRAnalysis() -- a generic, target-agnostic cost model. Passes
// that ask "how wide is a vector register?" or "what does this instruction
// cost?" (loop vectorizer, SLP vectorizer, unroller, inliner) would then make
// different decisions than clang's backend does.
//
// We deliberately take the target description from the IR rather than from our
// own command line: clang bakes the resolved triple into the module and the
// resolved "target-cpu"/"target-features" into each function's attributes, and
// TargetMachine::getSubtargetImpl(const Function &) re-reads those per function
// (see X86TargetMachine::getSubtargetImpl). So whatever -march/-mtune/-m<feature>
// flags the user passed to clang reach TTI through the IR automatically, for
// any target, with no flag translation on our side.
static std::unique_ptr<TargetMachine> createTargetMachineForModule(const Module &M) {
    // A Triple since LLVM 21, a std::string before; this works for both.
    const Triple TT(M.getTargetTriple());
    if (TT.getArch() == Triple::UnknownArch) {
        errs() << "warning: module has no target triple; "
                  "falling back to a target-agnostic cost model\n";
        return nullptr;
    }

    std::string Error;
#if LLVM_VERSION_MAJOR >= 21
    const Target *T = TargetRegistry::lookupTarget(TT, Error);
#else
    const Target *T = TargetRegistry::lookupTarget(TT.str(), Error);
#endif
    if (!T) {
        errs() << "warning: no registered target for triple '" << TT.str()
               << "' (" << Error << "); "
                  "falling back to a target-agnostic cost model\n";
        return nullptr;
    }

    // Module-level defaults only; per-function attributes still win in
    // getSubtargetImpl. Taken from the first defined function so that a module
    // whose functions all share one target (the normal case) gets sensible
    // defaults for any function that happens to lack the attributes.
    StringRef CPU, Features;
    for (const Function &F : M) {
        if (F.isDeclaration())
            continue;
        if (F.hasFnAttribute("target-cpu"))
            CPU = F.getFnAttribute("target-cpu").getValueAsString();
        if (F.hasFnAttribute("target-features"))
            Features = F.getFnAttribute("target-features").getValueAsString();
        break;
    }

    TargetOptions Options;
#if LLVM_VERSION_MAJOR >= 21
    return std::unique_ptr<TargetMachine>(
        T->createTargetMachine(TT, CPU, Features, Options, /*RM=*/std::nullopt));
#else
    return std::unique_ptr<TargetMachine>(
        T->createTargetMachine(TT.str(), CPU, Features, Options, /*RM=*/std::nullopt));
#endif
}

static bool runPipeline(Module &M, ModulePassManager &MPM,
                        PassInstrumentationCallbacks &PIC,
                        const std::string &pipeline) {
    LoopAnalysisManager     LAM;
    FunctionAnalysisManager FAM;
    CGSCCAnalysisManager    CGAM;
    ModuleAnalysisManager   MAM;

    std::unique_ptr<TargetMachine> TM = createTargetMachineForModule(M);

    PipelineTuningOptions PTO;
    PassBuilder PB(TM.get(), PTO, std::nullopt, &PIC);

    PB.registerModuleAnalyses(MAM);
    PB.registerCGSCCAnalyses(CGAM);
    PB.registerFunctionAnalyses(FAM);
    PB.registerLoopAnalyses(LAM);
    PB.crossRegisterProxies(LAM, FAM, CGAM, MAM);

    if (auto Err = PB.parsePassPipeline(MPM, pipeline)) {
        errs() << "Failed to parse pipeline '" << pipeline
               << "': " << llvm::toString(std::move(Err)) << "\n";
        return false;
    }

    MPM.run(M, MAM);
    return true;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

static void printUsage(const char *prog) {
    errs() << "Usage:\n"
           << "  " << prog << " list-passes  <input.ll> [--pipeline <pipeline>]\n"
           << "  " << prog << " show-diff    <input.ll> <Function> <N> [--pipeline <pipeline>]\n"
           << "  " << prog << " toolchain-info\n"
           << "\n"
           << "Subcommands:\n"
           << "  list-passes   Print every pass run, attributed to the function it ran on:\n"
           << "                '<Function>\\t<Pass>\\tdid changes|did no changes'.\n"
           << "                Module passes are reported under '<Globals>'.\n"
           << "\n"
           << "  show-diff     Print the IR before and after the N-th (1-based) run in\n"
           << "                <Function>'s list, as numbered by list-passes.\n"
           << "\n"
           << "  toolchain-info  Print the LLVM version pass-diff was built against and\n"
           << "                the directory of that LLVM's tools (clang++, llvm-cxxfilt).\n"
           << "\n"
           << "Options:\n"
           << "  --pipeline <spec>   LLVM pipeline string (default: default<O3>).\n"
           << "                      Pass clang's own pipeline here (obtained via\n"
           << "                      'clang -mllvm -print-pipeline-passes') so the\n"
           << "                      passes observed are the ones clang really runs.\n"
           << "  --mllvm <opt>       Forward an LLVM backend option, as clang's -mllvm\n"
           << "                      does. Repeatable. Needed for the few passes that\n"
           << "                      read global cl::opt state instead of pipeline\n"
           << "                      parameters.\n"
           << "\n"
           << "Examples:\n"
           << "  " << prog << " list-passes  file.ll\n"
           << "  " << prog << " show-diff    file.ll main 3\n"
           << "  " << prog << " show-diff    file.ll \"<Globals>\" 1 --pipeline default<O2>\n";
}

int main(int argc, char **argv) {
    // clang++ must come from the same LLVM as pass-diff, or clang's pipeline
    // string and .ll may not parse here. The UI asks for these two lines.
    if (argc == 2 && std::string(argv[1]) == "toolchain-info") {
        outs() << "llvm-version " << LLVM_VERSION_STRING << "\n";
        outs() << "tools-dir " << PASS_DIFF_LLVM_TOOLS_DIR << "\n";
        return 0;
    }

    if (argc < 3) {
        printUsage(argv[0]);
        return 1;
    }

    std::string subcommand = argv[1];
    std::string inputFile  = argv[2];

    // Parse optional --pipeline / --mllvm flags (scan remaining args)
    std::string pipeline = "default<O3>";
    std::string unit;
    long runIndex = 0;
    std::vector<std::string> mllvmOpts;

    for (int i = 3; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--pipeline" && i + 1 < argc) {
            pipeline = argv[++i];
        } else if (arg == "--mllvm" && i + 1 < argc) {
            mllvmOpts.push_back(argv[++i]);
        } else if (subcommand == "show-diff" && unit.empty()) {
            unit = arg;
        } else if (subcommand == "show-diff" && runIndex == 0) {
            runIndex = std::strtol(arg.c_str(), nullptr, 10);
            if (runIndex < 1) {
                errs() << "Run index must be a positive number: " << arg << "\n";
                return 1;
            }
        } else {
            errs() << "Unknown argument: " << arg << "\n";
            printUsage(argv[0]);
            return 1;
        }
    }

    if (subcommand == "show-diff" && runIndex == 0) {
        errs() << "show-diff requires a function and a run index.\n\n";
        printUsage(argv[0]);
        return 1;
    }

    if (subcommand != "list-passes" && subcommand != "show-diff") {
        errs() << "Unknown subcommand: " << subcommand << "\n\n";
        printUsage(argv[0]);
        return 1;
    }

    // Register targets so TargetRegistry::lookupTarget can find the module's
    // triple. Needed before any TargetMachine can be constructed.
    InitializeAllTargetInfos();
    InitializeAllTargets();
    InitializeAllTargetMCs();

    // Apply any forwarded -mllvm options. Most pass behaviour is already
    // captured by the parameterized pipeline string, but a handful of passes
    // still read global cl::opt values directly, and clang's own -mllvm flags
    // set exactly these globals -- so forwarding them keeps us consistent with
    // clang for those cases too.
    if (!mllvmOpts.empty()) {
        std::vector<const char *> clArgv;
        clArgv.push_back(argv[0]);
        for (const std::string &opt : mllvmOpts)
            clArgv.push_back(opt.c_str());
        cl::ParseCommandLineOptions(static_cast<int>(clArgv.size()),
                                    clArgv.data(),
                                    "pass-diff forwarded LLVM options\n");
    }

    // Parse the input IR
    LLVMContext Ctx;
    SMDiagnostic Err;
    auto M = parseIRFile(inputFile, Err, Ctx);
    if (!M) {
        Err.print(argv[0], errs());
        return 1;
    }

    // Wire up instrumentation
    PassInstrumentationCallbacks PIC;
    StandardInstrumentations SI(Ctx, /*DebugLogging=*/false);
    SI.registerCallbacks(PIC);

    ModulePassManager MPM;

    if (subcommand == "list-passes") {
        PassRunTracker tracker(
            *M, [](StringRef) { return true; },
            [](const PassRun &run) {
                bool changed = run.Invalidated || run.Before != run.After;
                outs() << run.Unit << "\t" << run.Label << "\t"
                       << (changed ? "did changes" : "did no changes") << "\n";
            });
        tracker.registerOn(PIC);

        outs() << "# Executed Passes and if they changed IR (pipeline: " << pipeline << ")\n";
        return runPipeline(*M, MPM, PIC, pipeline) ? 0 : 1;

    } else { // show-diff
        long seen = 0;
        PassRunTracker tracker(
            *M, [&](StringRef U) { return U == unit; },
            [&](const PassRun &run) {
                if (run.Unit != unit || ++seen != runIndex)
                    return;
                outs() << "\n";
                outs() << "======================================================\n";
                outs() << "Pass : " << run.Label << "\n";
                outs() << "Function: " << run.Unit << "\n";
                outs() << "======================================================\n";
                outs() << "\n--- IR BEFORE ---\n" << run.Before << "\n";
                outs() << "\n+++ IR AFTER +++\n"
                       << (run.Invalidated ? "(IR unit invalidated by the pass)" : run.After)
                       << "\n";
            });
        tracker.registerOn(PIC);

        outs() << "# Pipeline : " << pipeline << "\n";
        outs() << "# Watching : run " << runIndex << " of " << unit << "\n";
        return runPipeline(*M, MPM, PIC, pipeline) ? 0 : 1;
    }
}
