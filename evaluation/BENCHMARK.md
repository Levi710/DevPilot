# DevPilot Empirical Benchmark Specification

This document provides complete instructions, fixture specifications, scoring definitions, environment specifications, and multi-trial empirical validation results for the DevPilot evaluation benchmark.

---

## 1. Benchmark Overview & Objectives

DevPilot does not merely generate speculative explanations; it executes a controlled, sandboxed reproduction and shadow patch validation pipeline.

The benchmark evaluates two distinct dimensions:
1. **Analysis & Explanation Quality:** Evaluates whether DevPilot correctly identifies the suspected root cause file, pinpointed lines, relevant symbols, and generates actionable, coherent explanations and recommendations.
2. **Empirical Sandbox Verification:** Executes real tests against isolated shadow repository clones under Windows Job Object resource limits and runtime audit hooks (PEP 578) to empirically verify reproduction, regression boundaries, and patch soundness.

---

## 2. Benchmark Case Catalog (8 Fixtures)

The suite contains 8 test cases spanning 3 fixture repositories (`known_bug`, `data_flow_bug`, `boundary_bug`):

| Case ID | Fixture Repo | Category | Description | Expected Verification Status | Expected Fix Verified | Expected Regression Passed |
|---|---|---|---|---|---|---|
| `empty-list-division` | `known_bug` | Reproducible Bug | Division by zero on empty input in `calculate_average` | `fully_verified` | `True` | `True` |
| `normalized-username-lookup` | `data_flow_bug` | Reproducible Bug | User lookup fails when email contains uppercase characters | `fully_verified` | `True` | `True` |
| `off-by-one-total` | `boundary_bug` | Reproducible Bug | Boundary iteration in `tax_bracket` misses highest tier | `fully_verified` | `True` | `True` |
| `non-reproducible-bogus` | `known_bug` | Negative Control | Spurious bug report describing nonexistent failure | `reproduction_failed` | `False` | `True` |
| `flawed-patch-regression` | `known_bug` | Regression Guard | Patch fixes reproduction but breaks existing test suite | `regression_failed` | `False` | `False` |
| `missing-dependency` | `known_bug` | Dependency / Env | Code fails due to uninstalled runtime dependency | `dependency_error` | `False` | `True` |
| `runaway-infinite-loop` | `known_bug` | Sandbox Timeout | Infinite loop test terminated by sandbox timeout (1.0s) | `timeout_enforced` | `False` | `True` |
| `ambiguous-contract-empty` | `known_bug` | Contract Ambiguity | Undefined behavior with ambiguous return expectations | `contract_ambiguity` | `False` | `True` |

---

## 3. Verification Semantics & Status Distinction

To guarantee that the UI and API never misrepresent a flawed patch as verified:

- **`reproduction_resolved`**: Indicates whether the minimal reproduction test passes after applying the shadow patch.
- **`regression_suite_passed`**: Indicates whether all preexisting regression tests pass without regression.
- **`fix_verified`**: Strictly defined as:
  $$\text{fix\_verified} = \text{reproduction\_confirmed} \land \text{reproduction\_resolved} \land (\text{regression\_suite\_passed} = \text{True})$$
  If a patch resolves the reproduction test but no suite is available or regression tests fail, `reproduction_resolved` is `True`, but `fix_verified` is strictly `False`.
- **`verification_status`**:
  - `fully_verified`: Both reproduction confirmed and resolved with 100% regression pass.
  - `regression_suite_missing`: Reproduction resolved, but no existing regression suite was available to complete verification.
  - `regression_failed`: Reproduction resolved, but introduced regressions in preexisting tests.
  - `reproduction_failed`: Test did not reproduce the reported error on the unpatched codebase.
  - `timeout_enforced`: Execution exceeded CPU/wall-clock limit and was killed.
  - `dependency_error`: Execution halted due to missing runtime module/package.
  - `contract_ambiguity`: Expected behavior is underspecified in code contract.

---

## 4. Sandbox Security & OS Isolation Boundary

Execution occurs in ephemeral shadow workspaces created via `src/devpilot/sandbox.py`. Safeguards operate across two defense tiers:

### Tier 1: Windows Job Object Resource Limits
- **Bounded Lifetime (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000`):** Every subprocess spawned inside the job object is unconditionally killed when the parent handle closes, preventing orphaned runaway processes.
- **Memory Caps (`JOB_OBJECT_LIMIT_JOB_MEMORY = 256 MB`):** Strict job-wide commit memory limits prevent denial-of-service via memory exhaustion.
- **Process Count Caps (`JOB_OBJECT_LIMIT_ACTIVE_PROCESS_LIMIT = 5`):** Restricts fork/process-bomb attacks.
- **Fail-closed setup:** Validation is rejected if the Job Object cannot be configured or assigned. DevPilot does not currently create Low Integrity or restricted Windows tokens.

### Tier 2: Python Runtime Audit Hooks (PEP 578 via `sandbox_runner.py`)
- **Network Denial:** Audit events `socket.connect`, `socket.bind`, `socket.sendto`, and `socket.getaddrinfo` raise immediate `PermissionError`.
- **Filesystem Confinement:** Audit events permit reads only from the shadow workspace and selected Python runtime roots; writes and filesystem changes must remain in the ephemeral `shadow_root`. Disallowed host access raises `PermissionError`.
- **Process Spawn Traps:** Traps `subprocess.Popen` and `os.system` invocation.
- **Dynamic Native Extension Traps:** Traps `ctypes` and native library loading.

### Scope & Security Limitations Disclosure
> [!WARNING]
> **Benchmark Determinism vs. General Codebases:** The 100% consistency across 40 benchmark executions proves determinism and repeatability on the specified fixtures. It does not establish universal infallibility across arbitrary repositories or untrusted codebases.
>
> **Sandbox Isolation Scope:** While Windows Job Objects, memory caps, kill-on-close handles, and PEP 578 runtime audit hooks provide strong defense-in-depth against resource exhaustion, runaway processes, and unauthorized host writes, Python-level audit hooks are not an impenetrable VM/hypervisor boundary. Untrusted native binaries or kernel-level exploits could potentially bypass userland hooks. A formal third-party security audit of the process token lifecycle and filesystem access controls must be completed before deploying DevPilot in multi-tenant or untrusted repository environments.

---

## 5. Machine & Runtime Configuration

The benchmark was executed and verified on the following testbed:

| Parameter | Specification |
|---|---|
| **Operating System** | Microsoft Windows 11 Pro (Build 26100, x86_64) |
| **Python Runtime** | Python 3.14.3 (isolated execution `-I`) |
| **Node / Vite Runtime**| Node.js v22.13.0, Vite 5.4.11 |
| **Sandbox Memory Limit** | 256 MB per shadow job |
| **Sandbox Execution Timeout** | 1.0 second per test execution |
| **Active Process Limit** | 5 processes |

---

## 6. How to Reproduce

### Run the Sandbox Engine Benchmark (Multi-Trial)
To run the full sandbox execution benchmark across multiple repeated trials with per-case latency statistics:
```powershell
.\.venv\Scripts\python.exe evaluation/run_engine_benchmark.py --trials 5
```

### Run the Offline Baseline Evaluation
To evaluate static analysis, retrieval accuracy, and explanation scoring across fixtures:
```powershell
.\.venv\Scripts\python.exe evaluation/run_baseline.py
```

### Run Adversarial Sandbox Security Tests
To verify OS boundaries (network denial, filesystem confinement, process blocking):
```powershell
.\.venv\Scripts\pytest.exe tests/test_sandbox.py -v
```

---

## 7. Empirical Results (5-Trial Benchmark Distribution)

Running 5 consecutive trials across all 8 cases (40 total sandboxed executions) yielded **100% deterministic consistency** (0% flakiness):

| Benchmark Case | Expected Status | Observed Status | Consistency | Latency (Min) | Latency (Mean) | Latency (Max) | Latency (p95) |
|---|---|---|---|---|---|---|---|
| `empty-list-division` | `fully_verified` | `fully_verified` | 100% (5/5) | 0.27s | 0.28s | 0.30s | 0.30s |
| `normalized-username-lookup` | `fully_verified` | `fully_verified` | 100% (5/5) | 0.28s | 0.29s | 0.31s | 0.31s |
| `off-by-one-total` | `fully_verified` | `fully_verified` | 100% (5/5) | 0.27s | 0.28s | 0.29s | 0.29s |
| `non-reproducible-bogus` | `reproduction_failed` | `reproduction_failed` | 100% (5/5) | 0.11s | 0.11s | 0.12s | 0.12s |
| `flawed-patch-regression` | `regression_failed` | `regression_failed` | 100% (5/5) | 0.29s | 0.31s | 0.32s | 0.32s |
| `missing-dependency` | `dependency_error` | `dependency_error` | 100% (5/5) | 0.11s | 0.11s | 0.11s | 0.11s |
| `runaway-infinite-loop` | `timeout_enforced` | `timeout_enforced` | 100% (5/5) | 1.01s | 1.02s | 1.03s | 1.03s |
| `ambiguous-contract-empty` | `contract_ambiguity` | `contract_ambiguity` | 100% (5/5) | 0.11s | 0.12s | 0.12s | 0.12s |

### Summary Statistics
- **Total Test Cases:** 8
- **Total Sandboxed Executions:** 40 (5 trials $\times$ 8 cases)
- **Analysis Quality Pass Rate:** 8/8 (100%)
- **Reproduction Confirmed Rate:** 5/8 (62.5%)
- **Reproduction Resolved Rate:** 4/8 (50.0%)
- **Cleanly Verified Fix Rate:** 3/8 (37.5%)
- **Regressions Caught:** 1/1 (100% precision)
- **False Positives Rejected:** 1/1 (100% precision)
- **Timeouts Enforced:** 1/1 (100% precision)
- **Flakiness / Variance Across Runs:** **0%**
