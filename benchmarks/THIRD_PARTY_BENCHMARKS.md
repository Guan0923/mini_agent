# Third-party benchmark provenance

The Mini-Agent suite is an adapted, offline subset. The source projects and
their task contracts remain the authoritative references; this repository does
not claim official leaderboard compatibility.

| Adapted task family | Upstream source | Revision used | License/notice |
| --- | --- | --- | --- |
| Terminal-Bench: `analyze-access-logs`, `cancel-async-tasks`, `countdown-game` | [harbor-framework/terminal-bench](https://github.com/harbor-framework/terminal-bench) | `terminal-bench-core-0.1.1` task contracts | Apache-2.0; Harbor Framework attribution and the upstream task canary reference are retained in each task's source metadata |
| SWE-bench Lite: `psf__requests-2317` | [SWE-bench Lite dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) | base commit `091991be0da19de9108dbe5e3752917fea3d7fdc` | Requests Apache-2.0; SWE-bench data/harness MIT |
| SWE-bench Lite: `pytest-dev__pytest-11143` | [SWE-bench Lite dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) | base commit `6995257cf470d2143ad1683824962de4071c0eb7` | Pytest MIT; SWE-bench data/harness MIT |
| SWE-bench Lite: `astropy__astropy-14365` | [SWE-bench Lite dataset](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) | base commit `7269fa3e33e8d02485a647da91a5a2a60a06af61` | Astropy BSD-3-Clause; SWE-bench data/harness MIT |
| τ³-bench retail tasks 0 and 113 | [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | `v1.0.1` | MIT |
| τ³-bench airline task 3 | [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench) | `v1.0.1` | MIT |

The Mini-Agent adaptations intentionally remove Docker wrappers, network calls,
large dependencies, and multi-turn user simulators. The τ³ tasks flatten the
user simulator into a single authorized request while retaining policy,
tool-argument, and final-state requirements. The SWE tasks vendor only the
minimal affected code path and replace upstream test patches with local hidden
regression checks. These transformations are adaptation work, not upstream
gold patches.

Terminal-Bench attribution/canary notice: this suite names the Harbor Framework
source task and pinned task-contract revision in `benchmarks/tasks/open_source.py`.
The local fixtures are deliberately rewritten and therefore must not be used as
official Terminal-Bench canary submissions.
