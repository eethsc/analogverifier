"""
Harness² Recursive Analog Circuit Netlist Generation & Refinement Loop.
Inspired by Google Research's Harness² (https://github.com/google-research/harness2.git).

Combines:
  - NVIDIA NIM API (Deep Reasoning Proposer / Harness Improver)
  - OpenRouter API (Ultra-Fast Netlist Synthesis / Probe Generator)
  - 8-Component Modular Execution Harness (C1-C8: System Prompt, Guardrails, Memory, Tool Guidance, Skills, Scripts, Subagents, Plugins)
  - Task-Level Recursion (Contrastive Propose-Probe-Compose cycle)
  - Harnessing-Level Recursion (Cross-Task Persistent Experience Library)
"""
import os
import sys
import json
import time
import argparse

repo_root = os.path.abspath(r"C:\Users\Sera\.gemini\antigravity\worktrees\Project2\graph_analyze_codebase")
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from ai_analog_designer.llm_client import LLMClient
from ai_analog_designer.circuit_synthesis.harness2 import (
    HarnessTree,
    HarnessLibrary,
    Harness2CircuitArchitect,
    Harness2Proposer
)
from ai_analog_designer.neuro_symbolic_verifier.verification_flow import plan_for, verify


def main():
    parser = argparse.ArgumentParser(description="Harness² Netlist Generation & Refinement Loop")
    parser.add_argument("--ic", default="lm358", help="Target IC to design (lm358, ref30e, etc.)")
    args = parser.parse_args()

    ic_name = args.ic.lower().strip()

    # Setup directories
    out_dir = os.path.join(repo_root, "output_harness2_netlist")
    os.makedirs(out_dir, exist_ok=True)
    lib_dir = os.path.join(repo_root, "harness2_library")
    os.makedirs(lib_dir, exist_ok=True)

    # 1. Load Device Data
    print(f"=== [STEP 1] Loading Device Specifications and PDG Schedule ({ic_name.upper()}) ===", flush=True)
    if ic_name == "lm358":
        gt_path = os.path.join(repo_root, "benchmarks", "pdg_groundtruth", "lm358.json")
        with open(gt_path, "r", encoding="utf-8") as f:
            gt_data = json.load(f)
        part_name = "LM358"
        ic_info = {"name": part_name, "category": "Operational Amplifier", "description": "Dual High-Gain Operational Amplifier"}
        pins = gt_data.get("pins", [])
        specs = gt_data.get("specs", [])
        schedule_us = {
            "V-": 0.0,
            "V+": 20.0,
            "IN1+": 50.0,
            "IN1-": 50.0,
            "OUT1": 100.0,
            "IN2+": 50.0,
            "IN2-": 50.0,
            "OUT2": 100.0
        }
    elif ic_name == "ref30e":
        kg_path = os.path.join(repo_root, "output_ref30e_pipeline", "ref30e_knowledge_graph.json")
        pdg_path = os.path.join(repo_root, "output_ref30e_pipeline", "ref30e_pdg.json")
        with open(kg_path, "r", encoding="utf-8") as f:
            kg_data = json.load(f)
        with open(pdg_path, "r", encoding="utf-8") as f:
            pdg_data = json.load(f)
        part_name = "REF30E"
        ic_info = kg_data.get("ic_device", {"name": part_name, "category": "Voltage Reference"})
        ic_info["name"] = part_name
        pins = kg_data.get("pins", [])
        specs = kg_data.get("electrical_parameters", [])
        schedule_us = pdg_data.get("schedule_us", {"GND": 0.0, "IN": 20.0, "OUT": 120.0})
    else:
        raise ValueError(f"Unknown IC {ic_name}. Supported: lm358, ref30e")

    print(f"-> Target Device: {part_name} ({ic_info.get('category')})", flush=True)
    print(f"-> Pins ({len(pins)}): {[p['name'] for p in pins]}", flush=True)
    print(f"-> Electrical Specifications: {len(specs)} parameters", flush=True)
    print(f"-> PDG Activation Schedule: {schedule_us} us", flush=True)

    # 2. Configure Dual-Backend LLMs
    print(f"\n=== [STEP 2] Configuring Dual-Backend LLM Engines (NVIDIA NIM + OpenRouter) ===", flush=True)
    nim_key = os.getenv("NVIDIA_API_KEY", "")
    openrouter_key = os.getenv("OPENROUTER_API_KEY", "")

    # Proposer/Improver on NIM (High reasoning depth)
    nim_client = LLMClient(
        api_key=nim_key,
        provider="nim",
        default_model="nvidia/nemotron-3-ultra-550b-a55b",
        request_timeout=120
    )
    nim_client.fallback_models = []

    # Synthesizer/Prober on OpenRouter (Blazing fast generation)
    openrouter_client = LLMClient(
        api_key=openrouter_key,
        provider="openrouter",
        default_model="nvidia/nemotron-3.5-lightning:free",
        request_timeout=90
    )
    openrouter_client.fallback_models = []

    print(f"-> Harness Improver (Proposer/Composer): NVIDIA NIM ({nim_client.default_model})", flush=True)
    print(f"-> Circuit Synthesizer (Probe Generator): OpenRouter ({openrouter_client.default_model})", flush=True)

    # 3. Initialize Harness² Engine & Library
    print(f"\n=== [STEP 3] Initializing Harness² Framework & Experience Library ===", flush=True)
    architect = Harness2CircuitArchitect(
        nim_client=nim_client,
        openrouter_client=openrouter_client,
        library_root=lib_dir,
        improver_provider="nim",
        synthesizer_provider="openrouter"
    )

    # Bootstrap from library
    initial_tree = architect.bootstrap_for_device(category=ic_info.get("category", "Analog"), part_name=part_name)
    print(f"-> Bootstrapped C1-C8 Harness Tree (System Prompt: {len(initial_tree.c1_system_prompt)} chars, Guardrails: {len(initial_tree.c2_guardrails)} chars)", flush=True)

    # 4. Build Testbench Plan
    plan = plan_for(ic_info, pins, specs)
    print(f"-> Verification Plan Built: {plan['summary']['planned']} planned fixtures across {plan['summary']['distinct_fixtures']} distinct tests", flush=True)

    # 5. Iteration 1: Baseline Netlist Synthesis & Probe
    print(f"\n=== [STEP 4] Iteration 1: Baseline Synthesis under Initial Harness ===", flush=True)
    t0 = time.time()
    base_synth = architect.synthesize_candidate(
        tree=initial_tree,
        ic_info=ic_info,
        pins=pins,
        specifications=specs,
        pdg_schedule={k: v*1e-6 for k, v in schedule_us.items()},
        measurement_plan=plan
    )
    print(f"-> Baseline Netlist Generated in {time.time()-t0:.1f}s!", flush=True)

    base_netlist = base_synth["spice_netlist"]
    base_file = os.path.join(out_dir, f"{part_name}_iter1_base.cir")
    with open(base_file, "w", encoding="utf-8") as f:
        f.write(base_netlist)

    print(f"-> Simulating Baseline Netlist in LTspice with PDG Schedule...", flush=True)
    eval_base = verify(
        netlist=base_netlist,
        plan=plan,
        schedule_us=schedule_us,
        out_dir=out_dir,
        dut_name=part_name,
        label="iter1_base"
    )

    pass_base = eval_base["pass_rate_percent"]
    cov_base = eval_base["coverage_pct"]
    passed_specs_base = eval_base["passed_specs"]
    total_specs_base = eval_base["total_specs"]
    print(f"-> Baseline Result: Pass Rate = {pass_base:.1f}% ({passed_specs_base}/{total_specs_base} specs), Coverage = {cov_base:.1f}%", flush=True)
    if eval_base.get("simulation_errors"):
        print(f"   Errors detected: {eval_base['simulation_errors'][:2]}", flush=True)

    # 6. Iteration 2: Harness² Task-Level Recursion (Propose - Probe - Compose)
    print(f"\n=== [STEP 5] Iteration 2: Harness² Contrastive Propose–Probe–Compose Cycle ===", flush=True)

    def probe_evaluator(netlist_code: str, probe_tag: str) -> dict:
        probe_cir = os.path.join(out_dir, f"{part_name}_{probe_tag}.cir")
        with open(probe_cir, "w", encoding="utf-8") as f:
            f.write(netlist_code)
        ev = verify(
            netlist=netlist_code,
            plan=plan,
            schedule_us=schedule_us,
            out_dir=out_dir,
            dut_name=part_name,
            label=probe_tag
        )
        return ev

    t_rec = time.time()
    winning_synth, evolved_tree, decision_notes = architect.execute_contrastive_iteration(
        ic_info=ic_info,
        pins=pins,
        specifications=specs,
        pdg_schedule={k: v*1e-6 for k, v in schedule_us.items()},
        base_eval=eval_base,
        eval_probe_fn=probe_evaluator,
        measurement_plan=plan
    )
    print(f"-> Contrastive Propose-Probe-Compose finished in {time.time()-t_rec:.1f}s!", flush=True)
    print(f"-> Harness Decision: {decision_notes}", flush=True)

    # 7. Final Evaluation of Winning Netlist
    final_netlist = winning_synth["spice_netlist"]
    final_file = os.path.join(out_dir, f"{part_name}_FINAL_HARNESS2.cir")
    with open(final_file, "w", encoding="utf-8") as f:
        f.write(final_netlist)

    eval_final = verify(
        netlist=final_netlist,
        plan=plan,
        schedule_us=schedule_us,
        out_dir=out_dir,
        dut_name=part_name,
        label="final_harness2"
    )

    pass_final = eval_final["pass_rate_percent"]
    passed_specs_final = eval_final["passed_specs"]
    total_specs_final = eval_final["total_specs"]
    delta_pass = pass_final - pass_base

    print(f"\n=== [STEP 6] Harness² Optimization Summary ===", flush=True)
    print(f"Baseline Pass Rate: {pass_base:.1f}% ({passed_specs_base}/{total_specs_final} specs passed)")
    print(f"Harness² Final Pass Rate: {pass_final:.1f}% ({passed_specs_final}/{total_specs_final} specs passed)")
    print(f"Improvement Delta: {'+' if delta_pass >= 0 else ''}{delta_pass:.1f}%")

    # Save Evolved Harness Tree & Run Report
    tree_out = os.path.join(out_dir, f"evolved_harness_tree_{ic_name}.json")
    with open(tree_out, "w", encoding="utf-8") as f:
        json.dump(evolved_tree.to_dict(), f, indent=2, ensure_ascii=False)

    report = {
        "device": part_name,
        "framework": "Google Research Harness² (https://github.com/google-research/harness2.git)",
        "llm_engines": {
            "improver": f"NVIDIA NIM ({nim_client.default_model})",
            "synthesizer": f"OpenRouter ({openrouter_client.default_model})"
        },
        "baseline_pass_rate_pct": pass_base,
        "final_pass_rate_pct": pass_final,
        "pass_rate_delta_pct": round(delta_pass, 2),
        "harness_decision_notes": decision_notes,
        "evolved_tree_components": {
            "c1_system_prompt_len": len(evolved_tree.c1_system_prompt),
            "c2_guardrails_len": len(evolved_tree.c2_guardrails),
            "c3_memory_entries": len(evolved_tree.c3_memory.splitlines()),
            "c5_skills_count": len(evolved_tree.c5_skills)
        },
        "final_netlist_path": final_file
    }

    report_out = os.path.join(out_dir, f"harness2_run_report_{ic_name}.json")
    with open(report_out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"-> Saved Evolved Harness Tree to: {tree_out}", flush=True)
    print(f"-> Saved Run Report to: {report_out}", flush=True)
    print(f"-> Final Production Netlist: {final_file}", flush=True)
    print("\n=== Harness² Netlist Loop Completed Successfully! ===", flush=True)


if __name__ == "__main__":
    main()
