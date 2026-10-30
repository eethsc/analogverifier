"""
Hermes Analog Workflow Runner.
Orchestrates the autonomous circuit synthesis and iterative verification loop
using the Harness²-tuned Hermes Agent.
"""
from __future__ import annotations

import os
import re
import json
import time
import logging
from typing import Dict, Any, List, Optional, Callable, Tuple

from ai_analog_designer.llm_client import LLMClient
from ai_analog_designer.circuit_synthesis.harness2.harness_tree import HarnessTree
from .hermes_tools import HermesAnalogTools
from .hermes_agent import HermesAgent
from .harness_hermes_tuner import HarnessHermesTuner

logger = logging.getLogger("HermesAnalogWorkflowRunner")


class HermesAnalogWorkflowRunner:
    """
    High-level orchestrator that runs the Hermes Agent workflow tuned by Harness².
    """

    def __init__(
        self,
        output_dir: str = "output_design",
        llm_client: Optional[LLMClient] = None,
        library_root: str = "harness2_library",
        max_turns: int = 5,
        target_pass_rate: float = 100.0,
    ):
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.llm_client = llm_client or LLMClient()
        self.library_root = library_root
        self.max_turns = max_turns
        self.target_pass_rate = target_pass_rate
        self.tuner = HarnessHermesTuner(library_root=library_root)

    def run_design_flow(
        self,
        ic_info: Dict[str, Any],
        pins: List[Dict[str, Any]],
        specifications: List[Dict[str, Any]],
        pdg_schedule: Dict[str, float],
        measurement_plan: Optional[Dict[str, Any]] = None,
        user_prompt: Optional[str] = None,
        verify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        progress_callback: Optional[Callable[[int, str, str], None]] = None,
        base_harness_tree: Optional[HarnessTree] = None,
    ) -> Dict[str, Any]:
        """
        Executes the Hermes Agent circuit synthesis workflow:
          1. Bootstrap HarnessTree for device category.
          2. Initialize Hermes workspace and toolset.
          3. Tune Hermes (SOUL.md, MEMORY.md, blueprint skills).
          4. Execute autonomous agent loop with multi-turn tool interaction.
          5. Sync newly discovered skills and memories back into Harness² library.
        """
        part_name = ic_info.get("part_name") or ic_info.get("name") or "ANALOG_IC"
        subckt_name = re.sub(r'[^a-zA-Z0-9_]', '_', part_name).upper()
        category = ic_info.get("category", "analog_ic")

        logger.info(f"=== Starting Hermes Agent Workflow for {part_name} ({category}) ===")

        # 1. Bootstrap HarnessTree
        active_tree = self.tuner.bootstrap_tree(
            category=category,
            part_name=part_name,
            base_tree=base_harness_tree
        )

        # 2. Setup Hermes workspace
        workspace_dir = os.path.join(self.output_dir, "hermes_workspace")
        os.makedirs(workspace_dir, exist_ok=True)

        tools = HermesAnalogTools(
            output_dir=self.output_dir,
            dut_name=subckt_name,
            pins=pins,
            specifications=specifications,
            pdg_schedule=pdg_schedule,
            measurement_plan=measurement_plan,
            verify_fn=verify_fn,
            skills_dir=os.path.join(workspace_dir, "skills"),
            memory_file=os.path.join(workspace_dir, "memories", "MEMORY.md")
        )

        agent = HermesAgent(
            workspace_dir=workspace_dir,
            tools=tools,
            llm_client=self.llm_client
        )

        # 3. Tune Hermes with Harness² (C1-C8)
        self.tuner.tune_hermes(agent=agent, tree=active_tree, category=category, part_name=part_name)

        # 4. Formulate Task Prompt
        pins_summary = "\n".join([
            f"  - Pin '{p['name']}' (Role: {p.get('role', 'SIGNAL')}, Dir: {p.get('direction', 'INPUT')}, Desc: {p.get('description', '')})"
            for p in pins
        ])

        specs_summary = "\n".join([
            f"  - Parameter '{s['symbol']}': Min={s.get('min')}, Typ={s.get('typ')}, Max={s.get('max')} {s.get('unit', '')} | Conditions: {s.get('test_conditions', '')}"
            for s in specifications
        ])

        schedule_summary = "\n".join([
            f"  - Pin {p:10s} activation: {t*1e6:.2f} us"
            for p, t in pdg_schedule.items()
        ])

        measurement_section = ""
        if measurement_plan:
            try:
                from ai_analog_designer.neuro_symbolic_verifier.spec_planner import fixture_brief
                measurement_section = f"""
## HOW THIS CIRCUIT WILL BE MEASURED (LTspice Deterministic Testbench):
{fixture_brief(measurement_plan)}
The subcircuit must operate with its negative supply pin tied to 0 V (single supply), must not reference external node '0' directly, and must not contain analysis directives.
"""
            except Exception:
                pass

        user_prompt_section = f"\n## USER DIRECTIVES:\n{user_prompt}\n" if user_prompt else ""

        # 4b. Pre-fetch Web Circuit References via Tavily + Exa
        web_ref_section = ""
        try:
            web_res = tools.search_web_circuit_reference(
                query=f"{part_name} {category} internal schematic transistor circuit SPICE topology",
                ic_name=part_name,
                category=category
            )
            if web_res and web_res.get("formatted_markdown"):
                web_ref_section = f"\n## WEB CIRCUIT DESIGN REFERENCES (Tavily & Exa):\n{web_res['formatted_markdown']}\n"
                logger.info(
                    f"Retrieved {web_res.get('references_count', 0)} web circuit references "
                    f"(Tavily={web_res.get('tavily_ok')}, Exa={web_res.get('exa_ok')}) for {part_name}"
                )
        except Exception as e:
            logger.warning(f"Pre-synthesis web reference fetch skipped: {e}")

        task_prompt = f"""Synthesize a production-ready, pure transistor-level SPICE netlist for {part_name}.

## TARGET IC METADATA:
- Name: {part_name}
- Category: {category}
- Description: {ic_info.get('description', '')}

## MANDATORY PIN INTERFACE (Must match .subckt line in exact order):
{pins_summary}

## TARGET ELECTRICAL SPECIFICATIONS:
{specs_summary}

## PORT DEPENDENCY SCHEDULE (Settling time sequence):
{schedule_summary}
{measurement_section}
{web_ref_section}
{user_prompt_section}

### AGENT INSTRUCTIONS:
1. First, search available subcircuit blueprint skills using `retrieve_subcircuit_blueprint` (and `search_web_circuit_reference` if additional topology details are needed) to find the proven topology for this IC family.
2. Formulate your schematic sizing (bias currents, differential pair, active load, gain stage, output buffer) using ONLY real SPICE primitives (`Q`, `M`, `R`, `C`, `D`, `I`, `V`) and explicit `.model` cards. NEVER write undefined `X...` placeholder subcircuit calls.
3. Validate your SPICE syntax and guardrails using `validate_netlist_syntax`.
4. Run LTspice physical simulation via `verify_circuit_pdg_gate`.
5. Examine any failed specifications or stage cutoffs. If a design trap or convergence issue occurs, record it in memory using `manage_memory`.
6. Refine component sizes and re-verify until 100% pass rate is achieved.
7. Save your final verified circuit as a reusable blueprint using `save_subcircuit_blueprint`.
"""

        # 5. Run Hermes Agentic Loop
        t0 = time.time()
        loop_res = agent.run_autonomous_loop(
            task_prompt=task_prompt,
            max_turns=self.max_turns,
            target_pass_rate=self.target_pass_rate,
            progress_callback=progress_callback
        )
        elapsed_s = time.time() - t0

        best_netlist = loop_res.get("best_netlist")
        best_pass_rate = loop_res.get("best_pass_rate", 0.0)

        # 6. Final verification & Artifact Generation (with Harness² Blueprint Anchor)
        final_eval = {}
        if best_netlist:
            final_eval = tools.verify_circuit_pdg_gate(best_netlist, tag="final_hermes_harness2")
            best_pass_rate = final_eval.get("pass_rate_percent", best_pass_rate)

        if not best_netlist or best_pass_rate < 50.0:
            logger.info(f"Agent direct synthesis score ({best_pass_rate:.1f}%) < 50%. Activating Harness² Blueprint Adaptation...")
            bp_res = tools.retrieve_subcircuit_blueprint(query=part_name, category=category)
            if not bp_res.get("blueprints"):
                bp_res = tools.retrieve_subcircuit_blueprint(query=category, category=category)
            if not bp_res.get("blueprints"):
                bp_res = tools.retrieve_subcircuit_blueprint(query="comparator" if "comp" in category.lower() else "opamp")

            for bp in bp_res.get("blueprints", []):
                raw_code = bp.get("content", "")
                m_spice = re.search(r'```(?:spice)?\s*(\.subckt[\s\S]*?\.ends[^\r\n]*)', raw_code, re.I)
                candidate_raw = m_spice.group(1).strip() if m_spice else ""
                if not candidate_raw and ".subckt" in raw_code.lower() and ".ends" in raw_code.lower():
                    m_sub = re.search(r'(?m)^\s*(\.subckt\b[\s\S]*?^\s*\.ends\b[^\r\n]*)', raw_code, re.I)
                    if m_sub:
                        candidate_raw = m_sub.group(1).strip()

                if candidate_raw:
                    adapted = tools.adapt_blueprint_to_dut(candidate_raw, subckt_name)
                    eval_try = tools.verify_circuit_pdg_gate(adapted, tag="harness2_blueprint_anchor")
                    if eval_try.get("pass_rate_percent", 0.0) > best_pass_rate:
                        logger.info(f"Harness² Blueprint Adaptation succeeded with {eval_try.get('pass_rate_percent'):.1f}% pass rate!")
                        best_netlist = adapted
                        best_pass_rate = eval_try.get("pass_rate_percent", 0.0)
                        final_eval = eval_try
                        break

        final_pass_rate = best_pass_rate
        if best_netlist:
            final_cir_path = os.path.join(self.output_dir, f"{subckt_name}_FINAL_HERMES_HARNESS2.cir")
            with open(final_cir_path, "w", encoding="utf-8") as f:
                f.write(best_netlist)
        else:
            final_cir_path = None

        # 7. Synchronize back to HarnessTree & HarnessLibrary
        evolved_tree = self.tuner.sync_back_to_harness(
            agent=agent,
            tree=active_tree,
            task_id=part_name,
            category=category,
            score_before=0.0,
            score_after=final_pass_rate
        )

        return {
            "subcircuit_name": subckt_name,
            "best_netlist": best_netlist,
            "final_cir_path": final_cir_path,
            "pass_rate_percent": final_pass_rate,
            "unique_kind_pass_rate_pct": final_eval.get("unique_kind_pass_rate_pct", 0.0),
            "passed_specs": final_eval.get("passed_specs", 0),
            "total_specs": len(specifications),
            "elapsed_seconds": round(elapsed_s, 1),
            "turns_executed": loop_res.get("total_turns_executed", 0),
            "trajectory": loop_res.get("trajectory", []),
            "harness_tree": evolved_tree.to_dict(),
            "workspace_dir": workspace_dir
        }
