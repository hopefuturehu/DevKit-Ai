---
name: kunpeng-performance-analysis
description: Use when diagnosing, comparing, or explaining application and system performance on Kunpeng, especially after x86-to-ARM migration; combines KSYS broad evidence with selective DevKit Tuner deep analysis.
metadata:
  bot:
    platforms: [linux, darwin]
---

# Kunpeng performance analysis

Treat this as an expert playbook, not a mandatory workflow. Start from the evidence the user already
has. Skip steps that do not add information, and return to earlier assumptions when measurements
contradict them.

## Establish the question

Clarify the workload, performance metric, baseline, expected change, machine architecture, operating
system, compiler/runtime, and whether the result is from x86 or Kunpeng ARM. Separate a migration
regression from a generally slow workload. Prefer comparable inputs, concurrency, affinity, warm-up,
and measurement windows.

## Check execution capability

Inspect the current environment before calling a domain Tool. KSYS may support some x86 environments,
but most Tuner analysis must run on a physical Kunpeng Linux host. If the current CLI cannot execute a
required command, use the Tool to construct a validated command, tell the user exactly where to run it,
and ask them to paste the unedited output. Never claim that a manual command has already run.

## Build a broad evidence base

KSYS is usually a good starting point when the bottleneck is unclear:

- use `stability-check` when noisy or unstable load may invalidate comparison;
- use `collect` for multidimensional system and application evidence;
- use `report` to analyze a collected JSON result;
- use `diff` for comparable before/after or x86/ARM collection results.

Do not call KSYS mechanically when the user already supplied equivalent evidence or the issue is
clearly isolated.

## Select deep analysis

Use Tuner only when the current hypothesis justifies its cost. Typical directions include:

- `top-down` for pipeline and microarchitecture bounds;
- `hotspot` for expensive functions, call stacks, and source association;
- `miss` for cache, TLB, remote access, or long-latency load evidence;
- `numafast` for cross-NUMA traffic and locality problems;
- `hpc-perf` for HPC application behavior;
- `roofline` for compute-versus-memory limits on supported physical Kunpeng hosts.

Read `references/tool-selection.md` with `load_skill_resource` when the choice is ambiguous.

## Reason from evidence

Label each conclusion as measured evidence, inference, or unverified hypothesis. Correlate system-level
signals with workload behavior and source code. Where appropriate, inspect compiler flags,
architecture-specific code, vectorization, memory layout, affinity, NUMA placement, libraries, and
runtime configuration. Recommend one change at a time and define how to verify the improvement.

## Deliverable

Summarize the observed bottleneck, supporting evidence, confidence, recommended next action, and an
explicit verification command or measurement. Include unresolved uncertainty and avoid presenting a
generic optimization checklist as a diagnosis.

