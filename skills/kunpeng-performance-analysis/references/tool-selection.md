# KSYS and Tuner selection notes

This reference is intentionally heuristic. Tool versions and supported processors differ, so inspect
the installed command help when a parameter is rejected.

| Evidence or question | Useful next capability | Why |
|---|---|---|
| Results fluctuate across identical runs | KSYS `stability-check` | Determine whether environmental noise invalidates comparison. |
| Bottleneck is unknown | KSYS `collect`, then `report` | Obtain broad signals before choosing a deep profiler. |
| Two comparable runs differ | KSYS `diff` | Highlight changed metrics while retaining workload context. |
| Low IPC or pipeline-bound symptoms | Tuner `top-down` | Break down front-end, back-end, speculation, and retirement behavior. |
| A small amount of code dominates CPU time | Tuner `hotspot` | Connect samples and call stacks to functions and source. |
| Cache/TLB/remote-access indicators are high | Tuner `miss` | Deepen the memory-access hypothesis. |
| Cross-socket access or uneven memory traffic | Tuner `numafast` | Inspect NUMA traffic and locality. |
| HPC kernel needs hardware-bound classification | Tuner `roofline` or `hpc-perf` | Distinguish compute and memory ceilings on supported systems. |

Before comparing x86 and ARM, confirm that workload, input, compiler optimization level, concurrency,
affinity, warm-up, and observation duration are sufficiently equivalent. Architectural counters are not
always directly comparable; prefer conclusions based on workload-level impact plus architecture-local
evidence.

