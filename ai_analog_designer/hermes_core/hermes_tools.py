"""
Hermes Analog Tools: Domain-specific toolset exposed to the Hermes Agent.
Provides LTspice PDG 4-stage causal verification, SPICE physical guardrail validation,
subcircuit blueprint skill retrieval/persistence, and long-term memory operations.
"""
from __future__ import annotations

import os
import re
import json
import logging
from typing import Dict, Any, List, Optional, Callable

from ai_analog_designer.circuit_synthesis.netlist_validator import NetlistValidator

logger = logging.getLogger("HermesAnalogTools")


class HermesAnalogTools:
    """
    Tool registry and dispatcher for Hermes Agent during analog IC synthesis.
    """

    def __init__(
        self,
        output_dir: str,
        dut_name: str,
        pins: List[Dict[str, Any]],
        specifications: List[Dict[str, Any]],
        pdg_schedule: Dict[str, float],
        measurement_plan: Optional[Dict[str, Any]] = None,
        verify_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        skills_dir: Optional[str] = None,
        memory_file: Optional[str] = None,
    ):
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)

        self.dut_name = re.sub(r'[^a-zA-Z0-9_]', '_', dut_name).upper()
        self.pins = pins
        self.specifications = specifications
        self.pdg_schedule = pdg_schedule
        self.measurement_plan = measurement_plan
        self.verify_fn = verify_fn

        self.skills_dir = os.path.abspath(skills_dir) if skills_dir else os.path.join(self.output_dir, "skills")
        os.makedirs(self.skills_dir, exist_ok=True)

        self.memory_file = os.path.abspath(memory_file) if memory_file else os.path.join(self.output_dir, "memories", "MEMORY.md")
        os.makedirs(os.path.dirname(self.memory_file), exist_ok=True)

        self.validator = NetlistValidator()
        self.evaluation_history: List[Dict[str, Any]] = []

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Returns the list of tool JSON schemas in OpenAI / Hermes tool-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": "validate_netlist_syntax",
                    "description": "Validates SPICE syntax, subcircuit port alignment, and strict physical analog rules (zero behavioral B-sources, zero digital A-devices, zero undeclared rails, zero node 0 leakage).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "netlist": {
                                "type": "string",
                                "description": "The complete SPICE netlist code containing the .subckt definition."
                            },
                            "subckt_name": {
                                "type": "string",
                                "description": "Optional subcircuit name override; defaults to current target IC."
                            }
                        },
                        "required": ["netlist"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "verify_circuit_pdg_gate",
                    "description": "Executes the 4-Stage PDG Causal Verification in LTspice: Stage 0 (Topology & signal path) -> Stage 1 (Power & DC bias current) -> Stage 2 (Causal DC transfer dVout >= 0.5V) -> Stage 3 (Staged spec fixtures). Returns exact row pass rate, unique kind pass rate, and diagnostic feedback.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "netlist": {
                                "type": "string",
                                "description": "The complete SPICE subcircuit netlist to simulate and measure."
                            },
                            "tag": {
                                "type": "string",
                                "description": "Diagnostic tag or iteration label, e.g. 'hermes_iter1'."
                            }
                        },
                        "required": ["netlist"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "retrieve_subcircuit_blueprint",
                    "description": "Queries Hermes's subcircuit blueprint skills library (C5 skills) for verified analog topologies (e.g. comparator cores, Darlington diff pairs, active current mirrors, bandgap references, op-amp Miller stages).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search keyword or topology type, e.g. 'comparator', 'darlington', 'two_stage_opamp', 'bandgap', 'tl431'."
                            },
                            "category": {
                                "type": "string",
                                "description": "Optional category filter: 'comparator', 'operational_amplifier', 'voltage_reference', 'timer', 'ldo'."
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "save_subcircuit_blueprint",
                    "description": "Persists a newly designed, high-performing subcircuit as a reusable blueprint skill in Hermes's skills directory (C5 skills) and the permanent Harness² library.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Blueprint name identifier, e.g. 'high_speed_bipolar_comparator_core'."
                            },
                            "category": {
                                "type": "string",
                                "description": "IC category, e.g. 'comparator', 'operational_amplifier', 'voltage_reference'."
                            },
                            "subckt_code": {
                                "type": "string",
                                "description": "Complete SPICE subcircuit netlist code of the blueprint."
                            },
                            "description": {
                                "type": "string",
                                "description": "Technical description of the architecture, operating points, and design equations."
                            },
                            "terminal_pins": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Ordered pin names on the .subckt line."
                            }
                        },
                        "required": ["name", "category", "subckt_code", "description"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "manage_memory",
                    "description": "Hermes's native long-term memory tool (C3 memory). Records failure lessons, convergence traps (e.g. parallel mirror resistor trap), or patches existing heuristics in MEMORY.md.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {
                                "type": "string",
                                "enum": ["add", "patch", "view"],
                                "description": "Memory operation to execute."
                            },
                            "content": {
                                "type": "string",
                                "description": "Lesson, convergence trap description, or note to record in MEMORY.md."
                            },
                            "target": {
                                "type": "string",
                                "description": "Optional section name or substring to target during patch."
                            }
                        },
                        "required": ["action"]
                    }
                }
            }
        ]

    def dispatch(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches an incoming tool call from Hermes."""
        if tool_name == "validate_netlist_syntax":
            return self.validate_netlist_syntax(**arguments)
        elif tool_name == "verify_circuit_pdg_gate":
            return self.verify_circuit_pdg_gate(**arguments)
        elif tool_name == "retrieve_subcircuit_blueprint":
            return self.retrieve_subcircuit_blueprint(**arguments)
        elif tool_name == "save_subcircuit_blueprint":
            return self.save_subcircuit_blueprint(**arguments)
        elif tool_name == "manage_memory":
            return self.manage_memory(**arguments)
        else:
            return {"error": f"Unknown tool: '{tool_name}'"}

    def validate_netlist_syntax(self, netlist: str, subckt_name: Optional[str] = None) -> Dict[str, Any]:
        """Executes NetlistValidator."""
        target_name = (subckt_name or self.dut_name).upper()
        res = self.validator.validate(netlist, expected_subckt_name=target_name, expected_pins=self.pins)
        return {
            "is_valid": res.get("is_valid", False),
            "errors": res.get("errors", []),
            "subcircuit_name": res.get("subcircuit_name", target_name),
            "device_counts": res.get("device_counts", {}),
            "cleaned_netlist": res.get("cleaned_netlist", netlist),
            "guardrail_status": "PASS" if res.get("is_valid") else "FAIL_SYNTAX_OR_GUARDRAIL"
        }

    def verify_circuit_pdg_gate(self, netlist: str, tag: str = "hermes_eval") -> Dict[str, Any]:
        """Runs the 4-stage causal gate verifier."""
        # 1. First run syntax validation
        val_res = self.validate_netlist_syntax(netlist)
        cleaned_netlist = val_res.get("cleaned_netlist", netlist)
        if not val_res.get("is_valid", False):
            return {
                "status": "REJECTED_BY_GUARDRAIL",
                "pass_rate_percent": 0.0,
                "unique_kind_pass_rate_pct": 0.0,
                "passed_specs": 0,
                "failed_specs": len(self.specifications),
                "errors": val_res.get("errors", []),
                "diagnostic_feedback": "Netlist failed strict physical SPICE guardrail checks:\n" + "\n".join(val_res.get("errors", []))
            }

        # 2. Write netlist file
        netlist_file = os.path.join(self.output_dir, f"{self.dut_name}_{tag}.cir")
        with open(netlist_file, "w", encoding="utf-8") as f:
            f.write(cleaned_netlist)

        # 3. Execute verify function
        if self.verify_fn:
            eval_res = self.verify_fn(cleaned_netlist, tag)
        else:
            # Fallback to importing verify
            from ai_analog_designer.neuro_symbolic_verifier.verification_flow import verify
            sched_us = {k: v * 1e6 if v < 1.0 else v for k, v in self.pdg_schedule.items()}
            eval_res = verify(
                netlist=cleaned_netlist,
                plan=self.measurement_plan,
                schedule_us=sched_us,
                out_dir=self.output_dir,
                dut_name=self.dut_name,
                label=tag
            )

        self.evaluation_history.append({
            "tag": tag,
            "pass_rate_percent": eval_res.get("pass_rate_percent", 0.0),
            "unique_kind_pass_rate_pct": eval_res.get("unique_kind_pass_rate_pct", 0.0),
            "passed_specs": eval_res.get("passed_specs", 0),
            "failed_specs": eval_res.get("failed_specs", 0),
            "stage_status": eval_res.get("stage_status", "UNKNOWN"),
            "simulation_errors": eval_res.get("simulation_errors", [])
        })

        # Return a structured, compact diagnostic summary
        failed_items = [
            {
                "symbol": e.get("symbol"),
                "measured": e.get("measured"),
                "expected": f"[{e.get('min')}, {e.get('max')}]",
                "unit": e.get("unit"),
                "reason": e.get("fail_reason", "Out of bounds")
            }
            for e in eval_res.get("spec_evaluations", []) if not e.get("passed")
        ]

        return {
            "status": "SIMULATION_COMPLETE",
            "pass_rate_percent": eval_res.get("pass_rate_percent", 0.0),
            "unique_kind_pass_rate_pct": eval_res.get("unique_kind_pass_rate_pct", 0.0),
            "passed_specs": eval_res.get("passed_specs", 0),
            "failed_specs": eval_res.get("failed_specs", 0),
            "stage_status": eval_res.get("stage_status", "STAGE3_COMPLETE"),
            "stage0_passed": eval_res.get("stage0_passed", True),
            "stage1_passed": eval_res.get("stage1_passed", True),
            "stage2_passed": eval_res.get("stage2_passed", True),
            "diagnostic_feedback": eval_res.get("diagnostic_feedback", ""),
            "failed_specs_summary": failed_items[:15],
            "simulation_errors": eval_res.get("simulation_errors", []),
            "total_specs": len(self.specifications)
        }

    def retrieve_subcircuit_blueprint(self, query: str, category: Optional[str] = None) -> Dict[str, Any]:
        """Searches blueprint skills in skills/analog_blueprints/."""
        query_norm = query.lower().strip()
        cat_norm = category.lower().strip() if category else ""
        matches = []

        bp_root = os.path.join(self.skills_dir, "analog_blueprints")
        if os.path.exists(bp_root):
            for root, dirs, files in os.walk(bp_root):
                for f in files:
                    if f.endswith(".md") or f.endswith(".json"):
                        p = os.path.join(root, f)
                        try:
                            with open(p, "r", encoding="utf-8") as fp:
                                content = fp.read()
                            is_match = False
                            if query_norm in content.lower() or query_norm in f.lower():
                                is_match = True
                            if cat_norm and cat_norm not in content.lower() and cat_norm not in root.lower():
                                is_match = False
                            if is_match:
                                matches.append({
                                    "file": os.path.relpath(p, self.skills_dir),
                                    "title": f,
                                    "content": content[:3000]
                                })
                        except Exception:
                            pass

        return {
            "query": query,
            "category": category,
            "matches_found": len(matches),
            "blueprints": matches[:5]
        }

    def save_subcircuit_blueprint(
        self,
        name: str,
        category: str,
        subckt_code: str,
        description: str,
        terminal_pins: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Saves a subcircuit blueprint skill to skills/analog_blueprints/<name>/SKILL.md."""
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '_', name).lower()
        bp_dir = os.path.join(self.skills_dir, "analog_blueprints", safe_name)
        os.makedirs(bp_dir, exist_ok=True)

        pins_str = ", ".join(terminal_pins) if terminal_pins else "N/A"
        skill_md = f"""---
name: {safe_name}
category: {category}
description: {description[:120]}
pins: [{pins_str}]
---

# Subcircuit Blueprint: {name}

## Category
`{category}`

## Description
{description}

## Pin Interface
`{pins_str}`

## SPICE Subcircuit Implementation
```spice
{subckt_code.strip()}
```
"""
        skill_path = os.path.join(bp_dir, "SKILL.md")
        with open(skill_path, "w", encoding="utf-8") as f:
            f.write(skill_md)

        logger.info(f"Saved subcircuit blueprint skill: {skill_path}")
        return {
            "status": "SUCCESS",
            "name": safe_name,
            "path": os.path.relpath(skill_path, self.output_dir),
            "category": category
        }

    def manage_memory(self, action: str, content: str = "", target: Optional[str] = None) -> Dict[str, Any]:
        """Manages long-term memories in MEMORY.md."""
        action = action.lower().strip()
        current_memory = ""
        if os.path.exists(self.memory_file):
            with open(self.memory_file, "r", encoding="utf-8") as f:
                current_memory = f.read()

        if action == "view":
            return {"status": "SUCCESS", "memory_content": current_memory}

        elif action == "add":
            new_entry = f"\n- [{self.dut_name}] {content.strip()}\n"
            updated = current_memory + new_entry
            with open(self.memory_file, "w", encoding="utf-8") as f:
                f.write(updated)
            return {"status": "SUCCESS", "action": "added", "entry": new_entry}

        elif action == "patch":
            if target and target in current_memory:
                updated = current_memory.replace(target, content.strip())
            else:
                updated = current_memory + f"\n- {content.strip()}\n"
            with open(self.memory_file, "w", encoding="utf-8") as f:
                f.write(updated)
            return {"status": "SUCCESS", "action": "patched"}

        else:
            return {"status": "ERROR", "message": f"Unsupported action: '{action}'"}
