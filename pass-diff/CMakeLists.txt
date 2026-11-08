# Copyright (c) 2026 Alina Weber
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

cmake_minimum_required(VERSION 3.20)
project(pass-diff CXX)

# Optimised by default; pass -DCMAKE_BUILD_TYPE=Debug to debug pass-diff itself.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
    set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

# Which LLVM to build against is chosen with -DLLVM_DIR=<prefix>/lib/cmake/llvm.
# clang++ of the same LLVM must be installed alongside it: the UI takes clang++
# from the tools directory recorded below.
find_package(LLVM REQUIRED CONFIG)

set(PASS_DIFF_MIN_LLVM 18)
if(LLVM_VERSION_MAJOR LESS PASS_DIFF_MIN_LLVM)
    message(FATAL_ERROR
        "pass-diff needs LLVM ${PASS_DIFF_MIN_LLVM} or newer, but found "
        "LLVM ${LLVM_PACKAGE_VERSION} at ${LLVM_DIR}. Point -DLLVM_DIR at a newer "
        "<prefix>/lib/cmake/llvm.")
endif()

message(STATUS "Found LLVM ${LLVM_PACKAGE_VERSION}")
message(STATUS "Using LLVMConfig.cmake from ${LLVM_DIR}")
message(STATUS "LLVM tools (clang++, llvm-cxxfilt) expected in ${LLVM_TOOLS_BINARY_DIR}")

add_executable(pass-diff src/main.cpp)
target_compile_features(pass-diff PRIVATE cxx_std_17)

separate_arguments(LLVM_DEFINITIONS_LIST NATIVE_COMMAND ${LLVM_DEFINITIONS})
target_compile_definitions(pass-diff PRIVATE
    ${LLVM_DEFINITIONS_LIST}
    # Reported by `pass-diff toolchain-info`, so the UI uses clang++ from the
    # very LLVM pass-diff was built against.
    PASS_DIFF_LLVM_TOOLS_DIR="${LLVM_TOOLS_BINARY_DIR}")
target_include_directories(pass-diff SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS})

# Our classes must match LLVM's RTTI setting, or linking fails with undefined
# 'typeinfo for llvm::...' symbols. Release/package builds usually have it off.
if(NOT LLVM_ENABLE_RTTI)
    target_compile_options(pass-diff PRIVATE -fno-rtti)
endif()

if(LLVM_LINK_LLVM_DYLIB)
    # Distribution packages (Debian/Ubuntu, Fedora, Arch, Homebrew) ship one
    # shared libLLVM.so; the per-component static libraries may be missing.
    target_link_libraries(pass-diff PRIVATE LLVM)
else()
    # Self-built LLVM: link only the components we use.
    llvm_map_components_to_libnames(LLVM_LIBS
        core
        support
        irreader
        passes          # PassBuilder, StandardInstrumentations
        analysis
        transformutils
        scalaropts      # SROA, GVN, SimplifyCFG
        target          # TargetMachine
        codegen
        mc
        # All targets, so a TargetMachine can be built for whatever triple the
        # module carries (matters once we compile for non-host targets).
        AllTargetsInfos
        AllTargetsDescs
        AllTargetsCodeGens
    )
    target_link_libraries(pass-diff PRIVATE ${LLVM_LIBS})
endif()

# Keep the path to a shared libLLVM outside the system library folders (e.g. a
# self-built LLVM in ~/.local) in the installed binary; install drops it otherwise.
set_target_properties(pass-diff PROPERTIES INSTALL_RPATH_USE_LINK_PATH TRUE)

include(GNUInstallDirs)
install(TARGETS pass-diff RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
