"""
Jev-Compatible System-1 Tool Decision Router & Guardrail Interceptor.
Decouples action classification from generation: computes calibrated
probabilities P(tool | state), intercepts invalid calls, and breaks repetition loops.
Inspired by TypeSafe AI's Jev model for deterministic agent routing.
"""
from __future__ import annotations

import os
import json
import logging
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger("ToolDecisionRouter")


class ToolDecisionRouter:
    """
    Evaluates agent workflow state and outputs calibrated decision probabilities
    for the available tool space:
      - verify_circuit_pdg_gate
      - validate_netlist_syntax
      - retrieve_subcircuit_blueprint
      - search_web_circuit_reference
      - manage_memory
      - save_subcircuit_blueprint
    """

    SUPPORTED_TOOLS = [
        "verify_circuit_pdg_gate",
        "validate_netlist_syntax",
        "retrieve_subcircuit_blueprint",
        "search_web_circuit_reference",
        "manage_memory",
        "save_subcircuit_blueprint",
    ]

    def __init__(
        self,
        dut_name: str = "DUT",
        jev_endpoint: Optional[str] = None,
        jev_api_key: Optional[str] = None
    ):
        self.dut_name = dut_name
        self.jev_endpoint = jev_endpoint or os.environ.get("JEV_ENDPOINT")
        self.jev_api_key = jev_api_key or os.environ.get("JEV_API_KEY")

    def evaluate_state(
        self,
        turn_idx: int,
        pass_rate: float,
        has_netlist: bool,
        syntax_valid: bool,
        history_tool_names: List[str],
        unmet_specs_count: int = 0
    ) -> Dict[str, Any]:
        """
        Computes a calibrated probability distribution over tools
        based on the physical circuit synthesis state.
        """
        # If external Jev endpoint is configured, try calling it
        if self.jev_endpoint and self.jev_api_key:
            try:
                import urllib.request
                payload = json.dumps({
                    "state": {
                        "turn": turn_idx,
                        "pass_rate": pass_rate,
                        "has_netlist": has_netlist,
                        "syntax_valid": syntax_valid,
                        "history": history_tool_names[-5:],
                        "unmet_specs": unmet_specs_count
                    },
                    "choices": self.SUPPORTED_TOOLS
                }).encode("utf-8")
                req = urllib.request.Request(
                    self.jev_endpoint,
                    data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.jev_api_key}"
                    }
                )
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        data = json.loads(resp.read().decode("utf-8"))
                        if "selected_tool" in data:
                            return data
            except Exception as e:
                logger.debug(f"Jev remote call skipped ({e}), falling back to System-1 local router.")

        # System-1 Calibrated Decision Logic
        probs: Dict[str, float] = {t: 0.05 for t in self.SUPPORTED_TOOLS}

        # Rule 1: Goal achieved (pass_rate >= 100%) -> save blueprint immediately
        if pass_rate >= 100.0:
            probs = {t: 0.01 for t in self.SUPPORTED_TOOLS}
            probs["save_subcircuit_blueprint"] = 0.95
            return self._format_decision("save_subcircuit_blueprint", probs, "Target 100% pass rate achieved.")

        # Rule 2: Turn 1 -> Prioritize blueprint / literature retrieval
        if turn_idx == 1 and not has_netlist:
            probs = {t: 0.01 for t in self.SUPPORTED_TOOLS}
            probs["retrieve_subcircuit_blueprint"] = 0.85
            probs["search_web_circuit_reference"] = 0.10
            return self._format_decision("retrieve_subcircuit_blueprint", probs, "Initial turn: retrieve topology blueprints.")

        # Rule 3: Netlist exists -> Prioritize physical simulation
        if has_netlist:
            probs = {t: 0.01 for t in self.SUPPORTED_TOOLS}
            probs["verify_circuit_pdg_gate"] = 0.85
            probs["validate_netlist_syntax"] = 0.10
            return self._format_decision("verify_circuit_pdg_gate", probs, "Netlist present: execute physical LTspice simulation.")

        # Rule 4: Repetition trap breaker (agent repeatedly calling manage_memory or read-only tools)
        recent_memory_calls = sum(1 for t in history_tool_names[-3:] if t == "manage_memory")
        if recent_memory_calls >= 2:
            # Force circuit synthesis
            probs = {t: 0.01 for t in self.SUPPORTED_TOOLS}
            probs["verify_circuit_pdg_gate"] = 0.70
            probs["validate_netlist_syntax"] = 0.25
            probs["manage_memory"] = 0.00
            return self._format_decision(
                "verify_circuit_pdg_gate",
                probs,
                "Repetition trap broken: manage_memory called consecutively. Forcing circuit synthesis."
            )

        # Rule 5: Default state -> synthesize and verify
        probs["verify_circuit_pdg_gate"] = 0.60
        probs["validate_netlist_syntax"] = 0.20
        probs["retrieve_subcircuit_blueprint"] = 0.10
        probs["manage_memory"] = 0.10

        best_tool = max(probs, key=probs.get)
        return self._format_decision(best_tool, probs, "Standard iterative synthesis cycle.")

    def _format_decision(self, selected_tool: str, probs: Dict[str, float], reason: str) -> Dict[str, Any]:
        # Normalize probabilities
        total = sum(probs.values()) or 1.0
        norm_probs = {k: round(v / total, 3) for k, v in probs.items()}
        return {
            "selected_tool": selected_tool,
            "confidence": norm_probs.get(selected_tool, 0.0),
            "probabilities": norm_probs,
            "reason": reason
        }

    def intercept_and_guardrail(
        self,
        proposed_calls: List[Dict[str, Any]],
        state_decision: Dict[str, Any],
        has_extracted_netlist: bool,
        current_netlist: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Guards against invalid actions:
          - If the agent calls 'manage_memory' while a valid netlist is waiting to be simulated,
            injects or prioritizes 'verify_circuit_pdg_gate'.
          - Strips redundant consecutive manage_memory calls.
        """
        if not proposed_calls:
            # If agent emitted no tool calls but emitted a netlist, auto-inject the recommended tool
            if has_extracted_netlist:
                args = {"tag": "auto_routed_jev"}
                if current_netlist and state_decision["selected_tool"] in ("verify_circuit_pdg_gate", "validate_netlist_syntax"):
                    args["netlist"] = current_netlist
                return [{
                    "name": state_decision["selected_tool"],
                    "arguments": args
                }]
            return proposed_calls

        filtered: List[Dict[str, Any]] = []
        for call in proposed_calls:
            name = call.get("name")
            # If Jev decided repetition trap breaker, suppress redundant manage_memory
            if "Repetition trap" in state_decision.get("reason", "") and name == "manage_memory":
                logger.info("Jev Router intercepted redundant manage_memory call.")
                continue
            filtered.append(call)

        if not filtered and "Repetition trap" in state_decision.get("reason", ""):
            return []
        return filtered or proposed_calls
