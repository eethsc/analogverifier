"""
Harness²-Enhanced Circuit Architect for Recursive Analog Netlist Synthesis.
Integrates Google Research's Harness² framework (arXiv:2603.22472) with OpenRouter and NVIDIA NIM API.
"""
from __future__ import annotations

import os
import re
import json
import logging
from typing import Dict, Any, List, Optional, Tuple, Callable

from ai_analog_designer.llm_client import LLMClient
from ai_analog_designer.circuit_synthesis.netlist_validator import NetlistValidator
from .harness_tree import HarnessTree
from .harness_library import HarnessLibrary
from .harness_proposer import Harness2Proposer

logger = logging.getLogger("Harness2CircuitArchitect")


class Harness2CircuitArchitect:
    """
    Harness² Recursive Analog Circuit Architect.
    Replaces static one-shot prompts with an evolutionary 8-component execution harness
    (C1-C8) refined via contrastive Propose-Probe-Compose cycles using NVIDIA NIM and OpenRouter.
    """

    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        nim_client: Optional[LLMClient] = None,
        openrouter_client: Optional[LLMClient] = None,
        library_root: str = "harness2_library",
        improver_provider: str = "nim",
        synthesizer_provider: str = "openrouter"
    ):
        # OpenRouter & NVIDIA NIM API clients
        self.nim_client = nim_client or LLMClient(provider="nim")
        self.openrouter_client = openrouter_client or (
            llm_client if (llm_client and getattr(llm_client, "provider", "") == "openrouter") else LLMClient(provider="openrouter")
        )

        self.library = HarnessLibrary(root_dir=library_root)
        self.proposer = Harness2Proposer(
            nim_client=self.nim_client,
            openrouter_client=self.openrouter_client,
            improver_provider=improver_provider,
            synthesizer_provider=synthesizer_provider
        )
        self.validator = NetlistValidator()

        # Active harness tree (initialized from library bootstrapping)
        self.active_tree = HarnessTree()

    def bootstrap_for_device(self, category: str, part_name: str) -> HarnessTree:
        """Bootstraps the harness tree from the persistent improvement library."""
        self.active_tree = self.library.bootstrap_harness(category=category, base_tree=self.active_tree)
        logger.info(f"Harness² bootstrapped for {part_name} ({category}) with accumulated library lessons.")
        return self.active_tree

    def synthesize_candidate(
        self,
        tree: HarnessTree,
        ic_info: Dict[str, Any],
        pins: List[Dict[str, Any]],
        specifications: List[Dict[str, Any]],
        pdg_schedule: Dict[str, float],
        measurement_plan: Optional[Dict[str, Any]] = None,
        user_prompt: Optional[str] = None,
        prior_feedback: Optional[str] = None
    ) -> Dict[str, Any]:
        """Synthesizes a SPICE netlist under a specific harness tree."""
        part_name = ic_info.get("name", "ANALOG_IC")
        subckt_name = re.sub(r'[^a-zA-Z0-9_]', '_', part_name).upper()

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
## HOW THIS CIRCUIT WILL BE MEASURED (DUT is instantiated with V- = ground, V+ = VS):
{fixture_brief(measurement_plan)}
The subcircuit must work with its negative supply pin tied to 0 V (single supply), must not reference external node '0' directly, and must not contain analysis directives.
"""
            except Exception:
                pass

        user_prompt_section = f"\n## USER DIRECTIVES:\n{user_prompt}\n" if user_prompt else ""
        feedback_section = f"\n## PREVIOUS FEEDBACK & DIAGNOSTICS:\n{prior_feedback}\n" if prior_feedback else ""

        # Compile full structured prompt from the harness tree
        full_prompt = tree.compile_synthesis_prompt(
            part_name=subckt_name,
            pins_summary=pins_summary,
            specs_summary=specs_summary,
            schedule_summary=schedule_summary,
            measurement_section=measurement_section,
            user_prompt_section=user_prompt_section,
            feedback_section=feedback_section
        )

        synth_client = self.proposer.get_synthesizer_client()
        logger.info(f"Harness² Synthesizer querying {synth_client.provider.upper()} ({synth_client.default_model})...")

        raw_resp = synth_client.complete(
            full_prompt,
            system_prompt=tree.c1_system_prompt,
            temperature=0.1,
            max_tokens=8192,
            tag="harness2_synth"
        )

        # Parse XML tags
        arch_summary = ""
        m_arch = re.search(r'<architecture_summary>(.*?)</architecture_summary>', raw_resp, re.DOTALL | re.IGNORECASE)
        if m_arch:
            arch_summary = m_arch.group(1).strip()

        netlist_code = ""
        m_net = re.search(r'<spice_netlist>(.*?)</spice_netlist>', raw_resp, re.DOTALL | re.IGNORECASE)
        if m_net:
            netlist_code = m_net.group(1).strip()
        else:
            m_code = re.search(r'```(?:spice|cir)?\s*(.*?)\s*```', raw_resp, re.DOTALL | re.IGNORECASE)
            if m_code:
                netlist_code = m_code.group(1).strip()
            elif ".subckt" in raw_resp.lower():
                netlist_code = raw_resp.strip()

        if netlist_code.startswith("```"):
            netlist_code = re.sub(r'^```[a-zA-Z]*\n', '', netlist_code)
            netlist_code = re.sub(r'\n```$', '', netlist_code)

        val_res = self.validator.validate(netlist_code, expected_subckt_name=subckt_name, expected_pins=pins)
        cleaned_netlist = val_res.get("cleaned_netlist", netlist_code.strip())

        return {
            "subcircuit_name": subckt_name,
            "architecture_summary": arch_summary,
            "spice_netlist": cleaned_netlist,
            "raw_netlist": netlist_code,
            "design_rationale": {"summary": arch_summary},
            "harness_snapshot": tree.to_dict()
        }

    def execute_contrastive_iteration(
        self,
        ic_info: Dict[str, Any],
        pins: List[Dict[str, Any]],
        specifications: List[Dict[str, Any]],
        pdg_schedule: Dict[str, float],
        base_eval: Dict[str, Any],
        eval_probe_fn: Callable[[str, str], Dict[str, Any]],
        measurement_plan: Optional[Dict[str, Any]] = None,
        user_prompt: Optional[str] = None
    ) -> Tuple[Dict[str, Any], HarnessTree, str]:
        """
        Executes a full Harness² Propose–Probe–Compose step:
          1. Proposes Candidate A (Invariant) and Candidate B (Domain)
          2. Probes both candidates using the simulation evaluator probe function
          3. Contrasts behaviors and composes the evolved harness tree
        """
        part_name = ic_info.get("name", "ANALOG_IC")
        feedback = base_eval.get("diagnostic_feedback", "")
        failed_specs = [e for e in base_eval.get("spec_evaluations", []) if not e.get("passed")]
        compiler_errors = base_eval.get("simulation_errors", [])

        # 1. Propose
        tree_inv, tree_dom = self.proposer.propose_edits(
            current_tree=self.active_tree,
            part_name=part_name,
            specifications=specifications,
            simulation_feedback=feedback,
            failed_specs=failed_specs,
            compiler_errors=compiler_errors
        )

        # 2. Probe A (Invariant)
        logger.info("Executing Probe A (Invariant Candidate)...")
        res_a = self.synthesize_candidate(
            tree=tree_inv,
            ic_info=ic_info,
            pins=pins,
            specifications=specifications,
            pdg_schedule=pdg_schedule,
            measurement_plan=measurement_plan,
            user_prompt=user_prompt,
            prior_feedback="Focus strictly on SPICE convergence and syntax invariants."
        )
        eval_a = eval_probe_fn(res_a["spice_netlist"], "probe_a")

        # 3. Probe B (Domain)
        logger.info("Executing Probe B (Domain Candidate)...")
        res_b = self.synthesize_candidate(
            tree=tree_dom,
            ic_info=ic_info,
            pins=pins,
            specifications=specifications,
            pdg_schedule=pdg_schedule,
            measurement_plan=measurement_plan,
            user_prompt=user_prompt,
            prior_feedback="Focus strictly on circuit topology sizing to satisfy unmet specifications."
        )
        eval_b = eval_probe_fn(res_b["spice_netlist"], "probe_b")

        # 4. Compose
        composed_tree, decision_notes, evidence = self.proposer.compose_harness(
            base_tree=self.active_tree,
            tree_a=tree_inv,
            tree_b=tree_dom,
            eval_base=base_eval,
            eval_a=eval_a,
            eval_b=eval_b
        )

        # Pick winning rollout
        score_base = base_eval.get("pass_rate_percent", 0.0)
        score_a = eval_a.get("pass_rate_percent", 0.0)
        score_b = eval_b.get("pass_rate_percent", 0.0)

        winning_res = res_b if score_b >= score_a else res_a
        best_score = max(score_a, score_b)

        # If improvement was made, save to persistent library
        if best_score > score_base:
            cue = f"Unsatisfied specs on {part_name}: {[s.get('symbol') for s in failed_specs[:4]]}"
            self.library.record_experience(
                task_id=part_name,
                category=ic_info.get("category", "ANALOG"),
                cue=cue,
                lesson=decision_notes,
                evidence=evidence,
                tree=composed_tree,
                score_before=score_base,
                score_after=best_score
            )

        self.active_tree = composed_tree
        return winning_res, composed_tree, decision_notes

    def synthesize_netlist(
        self,
        ic_info: Dict[str, Any],
        pins: List[Dict[str, Any]],
        specifications: List[Dict[str, Any]],
        pdg_schedule: Dict[str, float],
        iteration: int = 1,
        prior_feedback: Optional[str] = None,
        user_prompt: Optional[str] = None,
        measurement_plan: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Backward-compatible interface with AICircuitArchitect.
        Bootstraps on iteration 1 and synthesizes under the active Harness² tree.
        """
        part_name = ic_info.get("name", "ANALOG_IC")
        if iteration == 1:
            self.bootstrap_for_device(category=ic_info.get("category", "ANALOG"), part_name=part_name)

        return self.synthesize_candidate(
            tree=self.active_tree,
            ic_info=ic_info,
            pins=pins,
            specifications=specifications,
            pdg_schedule=pdg_schedule,
            measurement_plan=measurement_plan,
            user_prompt=user_prompt,
            prior_feedback=prior_feedback
        )
