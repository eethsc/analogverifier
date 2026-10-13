import os
import sys
import time
import json
from integrated_system import IntegratedCircuitDesignVerifier

def run_all_benchmarks():
    print("=" * 90)
    print("   ANALOGVERIFIER: NEURO-SYMBOLIC FRAMEWORK FOR ANALOG VERIFICATION & NETLIST DESIGN")
    print("   Paper: AnalogVerifier (ICML 2026) Implementation with Direct Netlist Synthesis")
    print("   Target Simulator: LTspice (Batch Engine)")
    print("=" * 90)

    verifier = IntegratedCircuitDesignVerifier()

    benchmarks = [
        {
            "id": "BM_01_OTA",
            "name": "CMOS Two-Stage Miller OTA",
            "type": "OTA",
            "specs": {
                "GBW_Hz": 2.0e6,
                "CL_Load_pF": 10.0,
                "VDD_V": 2.5,
                "VSS_V": -2.5,
                "Slew_Rate_V_per_us": 2.8,
                "Phase_Margin_deg": 65.0
            }
        },
        {
            "id": "BM_02_DCDC",
            "name": "Industrial Synchronous DC-DC Buck Converter",
            "type": "DCDC_BUCK",
            "specs": {
                "Vin_nom_V": 12.0,
                "Vout_V": 3.3,
                "Iout_max_A": 2.0,
                "fsw_Hz": 800e3,
                "Vref_V": 0.800,
                "Vout_ripple_mV": 65.0
            }
        },
        {
            "id": "BM_03_HSC",
            "name": "High-Speed Decision Comparator",
            "type": "HSC",
            "specs": {
                "tpd_ns": 15.0,
                "VDD_V": 3.3,
                "VSS_V": 0.0,
                "Vos_mV": 2.0
            }
        },
        {
            "id": "BM_04_OPAMP",
            "name": "Monolithic Operational Amplifier (LM741)",
            "type": "OPAMP",
            "specs": {
                "A_vol_dB": 106.0,
                "GBW_Hz": 1.0e6,
                "Phase_Margin_deg": 65.0,
                "Slew_Rate_pos_V_per_us": 0.5,
                "I_q_mA": 1.7
            }
        }
    ]

    all_results = []
    total_start = time.time()

    for bm in benchmarks:
        print(f"\n{'#' * 90}")
        print(f"   Executing Benchmark: {bm['name']} ({bm['type']})")
        print(f"{'#' * 90}")
        
        res = verifier.run_full_design_and_verification(
            circuit_type=bm["type"],
            specifications=bm["specs"],
            doc_id=bm["id"]
        )
        all_results.append({
            "benchmark": bm,
            "result": res
        })

    total_time = time.time() - total_start

    # Generate Grand Summary Markdown Table
    print("\n" + "=" * 90)
    print("   GRAND MULTI-CIRCUIT BENCHMARK & VERIFICATION RESULTS")
    print("=" * 90)

    summary_rows = []
    summary_rows.append("| Benchmark Circuit | Circuit Class | Netlist Synthesis Level | #Tasks | Pass Rate (%) | Design Violations | Total Time (s) |")
    summary_rows.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")

    for item in all_results:
        bm = item["benchmark"]
        res = item["result"]
        design = res["design_result"]
        tasks = res["task_result"].tasks
        pass_rate = res["pass_rate"]
        violations = sum(1 for r in res["repair_reports"] if r.final_status == "DESIGN_VIOLATION")
        elapsed = res["total_elapsed"]

        summary_rows.append(f"| **{bm['name']}** | `{bm['type']}` | Transistor/Behavioral IC | {len(tasks)} | **{pass_rate:.1f}%** | {violations} | {elapsed:.2f}s |")

    summary_table = "\n".join(summary_rows)
    print(summary_table)

    # Write Complete Markdown Report
    report_content = f"""# AnalogVerifier: Neuro-Symbolic Analog Circuit Verification & Direct Netlist Synthesis System

## 1. Executive Overview

This system implements the state-of-the-art methodology introduced in **"AnalogVerifier: A Neuro-Symbolic Framework for Analog Circuit Verification"** (Liu et al., ICML 2026), enhanced with a **First-Principles Direct Analog Circuit Netlist Design & Sizing Engine**.

While the original paper verifies pre-existing circuit testbenches, this implementation bridges the entire lifecycle:
1. **Direct Circuit Netlist Synthesis (`circuit_designer/`)**: Generates and sizes parameterized SPICE netlists (CMOS Transistor-level OTA, Synchronous DC-DC Buck Converter, High-Speed Regenerative Comparator, and BJT OpAmp) from high-level specifications.
2. **Context-Aware Task Serialization (Stages 0–2)**: Parses specification documents into typed constraint units and decomposes them into executable SPICE test tasks.
3. **Stage 2.5 Cross-Validation**: Validates port existence (Algorithm 13 Fuzzy Matching), parameter references, and variable containment.
4. **Graph-Symbolic Scheduling (Stage 3)**: Constructs weighted Port Dependency Graphs (PDG) enforcing physical causality ($POWER \\prec BIAS \\prec CONTROL \\prec SIGNAL$) and computes longest-path stimulus schedules $\\sigma(p)$ (Algorithm 1).
5. **Numerical-Symbolic Grounding (Stage 4)**: Derives SymPy mathematical oracles, eliminates hallucinations, and generates comprehensive test vectors with interval tolerance bounds.
6. **Structured RAG SPICE Testbench Synthesis (Stage 5)**: Synthesizes complete, executable SPICE netlists injecting graph delays and `.MEASURE` directives.
7. **Simulation-Grounded Closed-Loop Repair (Algorithm 2)**: Executes simulations in LTspice batch mode, repairs syntax errors, heals convergence traps via PDG settling time relaxation ($\\delta \\leftarrow 1.5\\delta$), and verifies measurements against Grounded Oracles.

---

## 2. Multi-Circuit Benchmark & Verification Results

{summary_table}

- **Total Execution Time Across All Circuits:** {total_time:.2f} seconds
- **Simulation Engine:** LTspice Batch CLI (`LTspice.exe -b`)
- **Symbolic Engine:** SymPy 1.14 (64-bit precision)
- **Graph Engine:** NetworkX 3.6 (Topological Sort & Critical Path Analysis)

---

## 3. Detailed Architectural Validation

### 3.1. Stage 0 & 1: Specification Parsing & Circuit Information Extraction
- Automatically parses block structures into 12 predefined categories (`section_header`, `pin_definition`, `electrical_characteristics`, etc.).
- Performs symbol normalization and parameter deduplication (Algorithm 5).
- Scores overall extraction confidence using Equation 6:
  $$Confidence(C) = \\alpha_1 C_{{type}} + \\alpha_2 C_{{pins}} + \\alpha_3 C_{{params}} + \\alpha_4 C_{{eqns}} \\ge 0.97$$

### 3.2. Stage 3: Port Dependency Graph (PDG) & Critical-Path Scheduling
- Enforces domain-invariant physical startup sequence:
  $$\\text{{POWER}} \\prec \\text{{BIAS}} \\prec \\text{{CONTROL}} \\prec \\text{{SIGNAL}}$$
- Cycle detection guarantees absence of circular dependencies.
- Resolves activation times $\\sigma(p)$ using Algorithm 1:
  $$\\sigma(p) = \\max_{{(q,p) \\in E}} (\\sigma(q) + \\delta_{{qp}})$$

### 3.3. Stage 4: SymPy Oracle & Tolerance Propagation
- Mathematical containment check: $\\text{{GROUNDED}}(f) \\iff \\text{{vars}}(f) \\subseteq (\\Theta \\cup P)$.
- Evaluates worst-case bounds over parameter space:
  $$\\theta_{{min}} = \\inf_{{\\theta \\in I(\\Theta)}} f(\\theta), \\quad \\theta_{{max}} = \\sup_{{\\theta \\in I(\\Theta)}} f(\\theta)$$

### 3.4. Algorithm 2: Simulation-Grounded Closed-Loop Repair
- Convergence errors trigger automatic PDG time constant relaxation ($\\delta \\leftarrow 1.5\\delta$).
- Un-grounded assertion failures trigger threshold re-derivation, while grounded failures correctly flag true design bugs (`DESIGN_VIOLATION`).
"""

    report_path = "ANALOG_VERIFIER_IMPLEMENTATION_REPORT.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    print(f"\n[+] Full Audit & Implementation Report successfully written to: {report_path}")
    return all_results

if __name__ == "__main__":
    run_all_benchmarks()
