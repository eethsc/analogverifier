import os
import sys
import json
import logging
import time
from typing import List, Dict, Any
from ai_analog_designer import AutonomousAnalogDesignAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("10ICBenchmark")

TARGET_ICS = [
    {
        "id": "LM358",
        "name": "LM358",
        "category": "Dual Operational Amplifier",
        "pdf": "testcases/lm358.pdf",
        "max_pages": 6
    },
    {
        "id": "TL431",
        "name": "TL431",
        "category": "Precision Programmable Voltage Reference",
        "pdf": "testcases/tl431.pdf",
        "max_pages": 6
    },
    {
        "id": "NE555",
        "name": "NE555",
        "category": "Precision Monolithic Timer / Oscillator",
        "pdf": "testcases/ne555.pdf",
        "max_pages": 6
    },
    {
        "id": "LM393",
        "name": "LM393",
        "category": "Dual Differential Comparator",
        "pdf": "testcases/lm393.pdf",
        "max_pages": 6
    },
    {
        "id": "LM317",
        "name": "LM317",
        "category": "3-Terminal Adjustable Linear Voltage Regulator",
        "pdf": "testcases/lm317.pdf",
        "max_pages": 6
    },
    {
        "id": "LM386",
        "name": "LM386",
        "category": "Low Voltage Audio Power Amplifier",
        "pdf": "testcases/lm386.pdf",
        "max_pages": 6
    },
    {
        "id": "INA128",
        "name": "INA128",
        "category": "Precision Instrumentation Amplifier",
        "pdf": "testcases/ina128.pdf",
        "max_pages": 6
    },
    {
        "id": "OPA2134",
        "name": "OPA2134",
        "category": "SoundPlus High-Performance Audio Op-Amp",
        "pdf": "testcases/opa2134.pdf",
        "max_pages": 6
    },
    {
        "id": "CDCVF2505-Q1",
        "name": "CDCVF2505-Q1",
        "category": "3.3V PLL Clock Driver",
        "pdf": "testcases/cdcvf2505-q1.pdf",
        "max_pages": 6
    },
    {
        "id": "TLC2932A",
        "name": "TLC2932A",
        "category": "Phase-Locked Loop / VCO Building Block",
        "pdf": "testcases/tlc2932a.pdf",
        "max_pages": 6
    }
]

def main():
    output_root = "benchmark_results"
    os.makedirs(output_root, exist_ok=True)

    summary_json_path = os.path.join(output_root, "10_IC_BENCHMARK_SUMMARY.json")
    existing_entries = {}
    if os.path.exists(summary_json_path):
        try:
            with open(summary_json_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
                for item in saved:
                    if "error" not in item:
                        existing_entries[item["id"]] = item
        except Exception:
            pass

    benchmark_summary = []
    start_time_all = time.time()

    logger.info(f"Starting/Resuming 10 Analog IC Autonomous Design & Verification Benchmark...")
    logger.info(f"Target IC count: {len(TARGET_ICS)} | Existing valid runs: {len(existing_entries)}")

    for idx, target in enumerate(TARGET_ICS, 1):
        ic_id = target["id"]
        pdf_path = target["pdf"]
        category = target["category"]
        max_pages = target["max_pages"]

        # Check if already completed successfully
        if ic_id in existing_entries:
            logger.info(f"[{idx}/{len(TARGET_ICS)}] Reusing existing completed benchmark for {ic_id}.")
            benchmark_summary.append(existing_entries[ic_id])
            continue

        logger.info("\n" + "="*80)
        logger.info(f"[{idx}/{len(TARGET_ICS)}] BENCHMARK RUN: {ic_id} ({category})")
        logger.info(f"Datasheet PDF: {pdf_path}")
        logger.info("="*80)

        if not os.path.exists(pdf_path):
            logger.error(f"Datasheet file not found: {pdf_path}. Skipping.")
            continue

        ic_output_dir = os.path.join(output_root, ic_id)
        os.makedirs(ic_output_dir, exist_ok=True)

        agent = AutonomousAnalogDesignAgent(
            output_dir=ic_output_dir,
            max_iterations=10
        )

        t0 = time.time()
        try:
            res = agent.run(datasheet_pdf_path=pdf_path, max_pages=max_pages)
            elapsed = time.time() - t0

            summary = res.get("summary", {})
            iterations = summary.get("iterations", [])

            loop1_res = iterations[0] if len(iterations) > 0 else {}
            loop2_res = iterations[1] if len(iterations) > 1 else {}

            entry = {
                "id": ic_id,
                "category": category,
                "subckt_name": summary.get("subckt_name", ic_id),
                "total_specs": summary.get("spec_count", 0),
                "loop_count": len(iterations),
                "loop1": {
                    "pass_rate": loop1_res.get("pass_rate_percent", 0.0),
                    "passed_specs": loop1_res.get("passed_specs", 0),
                    "failed_specs": loop1_res.get("failed_specs", 0),
                    "shortfalls": loop1_res.get("shortfalls", []),
                    "improvements": loop1_res.get("improvements", [])
                },
                "loop2": {
                    "pass_rate": loop2_res.get("pass_rate_percent", loop1_res.get("pass_rate_percent", 0.0)),
                    "passed_specs": loop2_res.get("passed_specs", loop1_res.get("passed_specs", 0)),
                    "failed_specs": loop2_res.get("failed_specs", loop1_res.get("failed_specs", 0)),
                    "shortfalls": loop2_res.get("shortfalls", []),
                    "improvements": loop2_res.get("improvements", [])
                },
                "final_pass_rate": summary.get("pass_rate_percent", 0.0),
                "elapsed_seconds": elapsed,
                "final_netlist_path": summary.get("final_netlist_path"),
                "report_markdown_path": os.path.join(ic_output_dir, f"{ic_id}_DESIGN_VERIFICATION_REPORT.md"),
                "knowledge_graph_path": os.path.join(ic_output_dir, f"{ic_id}_knowledge_graph.json")
            }
            benchmark_summary.append(entry)
            logger.info(f"[{ic_id}] Completed in {elapsed:.1f}s. Final Pass Rate: {entry['final_pass_rate']:.1f}%")

        except Exception as e:
            logger.exception(f"Error processing {ic_id}: {e}")
            benchmark_summary.append({
                "id": ic_id,
                "category": category,
                "error": str(e)
            })

    # Save summary JSON
    with open(summary_json_path, "w", encoding="utf-8") as f:
        json.dump(benchmark_summary, f, indent=2)

    # Generate Master 10-IC Benchmark Report
    generate_master_report(benchmark_summary, output_root)
    logger.info(f"All benchmarks finished in {time.time() - start_time_all:.1f}s. Summary saved to {summary_json_path}")

def generate_master_report(benchmark_summary: List[Dict[str, Any]], output_root: str):
    """Compiles the comprehensive 10 Analog IC verification report."""
    md = [
        "# Comprehensive 10 Analog IC Autonomous Synthesis & Verification Benchmark",
        "## End-to-End First-Principles Design from Arbitrary Datasheet PDFs",
        "",
        "### Executive Summary Table",
        "| # | Target IC | Category | Subcircuit | Specs | Loops | Loop 1 Pass % | Loop 2 Pass % | Final Pass % | Status |",
        "| :- | :--- | :--- | :--- | :-: | :-: | :-: | :-: | :-: | :---: |"
    ]

    for idx, item in enumerate(benchmark_summary, 1):
        if "error" in item:
            md.append(f"| {idx} | **{item['id']}** | {item['category']} | - | - | - | - | - | - | ❌ Error: {item['error'][:30]} |")
            continue
        l1_p = item.get("loop1", {}).get("pass_rate", 0.0)
        l2_p = item.get("loop2", {}).get("pass_rate", l1_p)
        fin_p = item.get("final_pass_rate", l2_p)
        status = "✅ Verified" if fin_p > 0 else "⚡ Simulated"
        md.append(f"| {idx} | **{item['id']}** | {item['category']} | `{item.get('subckt_name')}` | {item.get('total_specs', 0)} | {item.get('loop_count', 0)} | {l1_p:.1f}% | {l2_p:.1f}% | **{fin_p:.1f}%** | {status} |")

    md.extend([
        "",
        "---",
        "## Detailed Per-IC Verification, PDG Analysis & Loop Progression"
    ])

    for idx, item in enumerate(benchmark_summary, 1):
        if "error" in item:
            continue
        ic_id = item["id"]
        cat = item["category"]
        subckt = item.get("subckt_name", ic_id)

        md.extend([
            f"\n### {idx}. {ic_id} ({cat})",
            f"- **Subcircuit Identifier:** `{subckt}`",
            f"- **Extracted Specifications Count:** {item.get('total_specs', 0)}",
            f"- **Execution Time:** {item.get('elapsed_seconds', 0.0):.1f}s",
            f"- **Synthesized Netlist:** `{item.get('final_netlist_path')}`",
            "",
            "#### Loop Iteration Audit:"
        ])

        # Loop 1
        l1 = item.get("loop1", {})
        md.append(f"##### Loop 1 (Initial First-Principles Synthesis):")
        md.append(f"- **Pass Rate:** `{l1.get('pass_rate', 0.0):.1f}%` ({l1.get('passed_specs', 0)} specs passed)")
        md.append("- **Shortfall Audit (미달 원인 분석):**")
        for sf in l1.get("shortfalls", []):
            md.append(f"  - {sf}")
        if not l1.get("shortfalls"):
            md.append("  - No compilation or measurement failures.")
        md.append("- **Improvement Actions (초기 합성 내역):**")
        for imp in l1.get("improvements", []):
            md.append(f"  - {imp}")

        # Loop 2
        l2 = item.get("loop2", {})
        md.append(f"\n##### Loop 2 (Autonomous Closed-Loop Refinement):")
        md.append(f"- **Pass Rate:** `{l2.get('pass_rate', 0.0):.1f}%` ({l2.get('passed_specs', 0)} specs passed)")
        md.append("- **Shortfall Audit (미달 원인 분석):**")
        for sf in l2.get("shortfalls", []):
            md.append(f"  - {sf}")
        if not l2.get("shortfalls"):
            md.append("  - All remaining specifications satisfied.")
        md.append("- **Improvement Actions (개선 및 수정 사항):**")
        for imp in l2.get("improvements", []):
            md.append(f"  - {imp}")

        # Load knowledge graph for PDG diagram if available
        kg_path = item.get("knowledge_graph_path")
        if kg_path and os.path.exists(kg_path):
            try:
                with open(kg_path, "r", encoding="utf-8") as f:
                    kg_data = json.load(f)
                pins = kg_data.get("pins", [])
                md.extend([
                    "\n#### Port Dependency Graph (PDG) Topology:",
                    "```mermaid",
                    "graph TD"
                ])
                for p in pins:
                    pname = p.get("name", "PIN")
                    prole = p.get("role", "SIGNAL")
                    md.append(f'    {pname}["{pname} ({prole})"]')
                # Connect power -> bias -> control -> signal
                pwrs = [p["name"] for p in pins if p.get("role") == "POWER"]
                biases = [p["name"] for p in pins if p.get("role") == "BIAS"]
                ctrls = [p["name"] for p in pins if p.get("role") == "CONTROL"]
                sigs = [p["name"] for p in pins if p.get("role") in ["SIGNAL", "INPUT", "OUTPUT"]]
                for pwr in pwrs:
                    for b in biases:
                        md.append(f"    {pwr} --> {b}")
                    if not biases:
                        for c in ctrls:
                            md.append(f"    {pwr} --> {c}")
                        if not ctrls:
                            for s in sigs:
                                md.append(f"    {pwr} --> {s}")
                for b in biases:
                    for c in ctrls:
                        md.append(f"    {b} --> {c}")
                    if not ctrls:
                        for s in sigs:
                            md.append(f"    {b} --> {s}")
                for c in ctrls:
                    for s in sigs:
                        md.append(f"    {c} --> {s}")
                md.append("```")
            except Exception:
                pass

    master_md_path = os.path.join(output_root, "10_ANALOG_IC_BENCHMARK_REPORT.md")
    with open(master_md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    logger.info(f"Master 10-IC Benchmark report written to {master_md_path}")

if __name__ == "__main__":
    main()

