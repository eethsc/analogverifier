"""
Hermes Autonomous Agent Core.
Implements the NousResearch Hermes Agent architecture (SOUL.md, MEMORY.md, skills/,
conversation loop, multi-turn tool dispatch, and self-improving reflection).
"""
from __future__ import annotations

import os
import re
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional, Tuple, Callable

from ai_analog_designer.llm_client import LLMClient
from .hermes_tools import HermesAnalogTools

logger = logging.getLogger("HermesAgent")


@dataclass
class AgentTurnResult:
    """Represents the outcome of an agent execution turn."""
    turn_index: int
    raw_response: str
    thought: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    tool_outputs: List[Dict[str, Any]] = field(default_factory=list)
    netlist_extracted: Optional[str] = None
    pass_rate: float = 0.0
    unique_pass_rate: float = 0.0
    passed_specs: int = 0
    total_specs: int = 0
    is_terminal: bool = False


class HermesAgent:
    """
    Hermes Agent Wrapper for Analog Circuit Synthesis.
    Integrates NousResearch Hermes Agent standards:
      - SOUL.md: Autonomous Persona & Physics Laws (tuned by Harness² C1)
      - memories/MEMORY.md: Persistent Convergence & Bug Traps (tuned by Harness² C3)
      - memories/USER.md: Target IC Mission & Datasheet Directives
      - skills/analog_blueprints/: Subcircuit Blueprint Skills (tuned by Harness² C5)
      - Tool Calling: Dynamic dispatch for simulation, syntax validation, memory & blueprint saving
    """

    def __init__(
        self,
        workspace_dir: str,
        tools: HermesAnalogTools,
        llm_client: Optional[LLMClient] = None,
        model: Optional[str] = None,
        temperature: float = 0.1,
    ):
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.tools = tools
        self.llm_client = llm_client or LLMClient()
        self.model = model or getattr(self.llm_client, "default_model", "openai/gpt-6-luna")
        self.temperature = temperature

        # File paths inside Hermes workspace
        self.soul_path = os.path.join(self.workspace_dir, "SOUL.md")
        self.memory_dir = os.path.join(self.workspace_dir, "memories")
        self.memory_path = os.path.join(self.memory_dir, "MEMORY.md")
        self.user_context_path = os.path.join(self.memory_dir, "USER.md")
        self.skills_dir = os.path.join(self.workspace_dir, "skills")
        self.blueprints_dir = os.path.join(self.skills_dir, "analog_blueprints")

        self.messages: List[Dict[str, str]] = []
        self.trajectory: List[AgentTurnResult] = []

        self._ensure_workspace()

    def _ensure_workspace(self):
        """Ensures the directory structure matching ~/.hermes layout exists."""
        os.makedirs(self.workspace_dir, exist_ok=True)
        os.makedirs(self.memory_dir, exist_ok=True)
        os.makedirs(self.blueprints_dir, exist_ok=True)

        if not os.path.exists(self.soul_path):
            with open(self.soul_path, "w", encoding="utf-8") as f:
                f.write("# Hermes Analog Agent Persona\nYou are an Expert Autonomous Analog IC Designer.")

        if not os.path.exists(self.memory_path):
            with open(self.memory_path, "w", encoding="utf-8") as f:
                f.write("# Hermes Long-Term Memory (MEMORY.md)\n\n## CONVERGENCE TRAPS & FAILURE MODES\n")

    def load_soul(self) -> str:
        """Reads SOUL.md persona."""
        if os.path.exists(self.soul_path):
            with open(self.soul_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def load_memory(self) -> str:
        """Reads active MEMORY.md snapshot."""
        if os.path.exists(self.memory_path):
            with open(self.memory_path, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def list_available_blueprint_skills(self) -> List[Dict[str, str]]:
        """Scans skills/analog_blueprints/ for registered blueprint skills."""
        skills = []
        if os.path.exists(self.blueprints_dir):
            for entry in os.listdir(self.blueprints_dir):
                skill_file = os.path.join(self.blueprints_dir, entry, "SKILL.md")
                if os.path.exists(skill_file):
                    try:
                        with open(skill_file, "r", encoding="utf-8") as f:
                            lines = f.readlines()
                        desc = ""
                        for line in lines:
                            if line.lower().startswith("description:"):
                                desc = line.split(":", 1)[1].strip()
                                break
                        skills.append({"name": entry, "description": desc, "path": skill_file})
                    except Exception:
                        pass
        return skills

    def compile_system_prompt(self) -> str:
        """
        Assembles Hermes's comprehensive system prompt including:
          1. SOUL.md (Persona & Physical Laws)
          2. Active MEMORY.md snapshot (Convergence traps)
          3. Available Blueprint Skills list
          4. Tool schemas and usage instructions
        """
        soul = self.load_soul()
        memory = self.load_memory()
        skills = self.list_available_blueprint_skills()

        skills_summary = "\n".join([f"- `{s['name']}`: {s['description']}" for s in skills]) if skills else "No skills indexed yet."

        tools_schema_json = json.dumps(self.tools.get_tool_schemas(), indent=2)

        prompt = f"""{soul}

=== PERSISTENT MEMORY SNAPSHOT (MEMORY.md) ===
{memory}

=== AVAILABLE SUBCIRCUIT BLUEPRINT SKILLS (skills/analog_blueprints/) ===
{skills_summary}
(Use tool `retrieve_subcircuit_blueprint` to fetch full schematics and SPICE code)

=== TOOL CALLING PROTOCOL ===
You have access to specialized tools. When you need to inspect blueprints, validate syntax, execute simulation, or update memory, invoke tools using either standard function calling or the Hermes `<tool_call>` format:
<tool_call>
{{"name": "<tool_name>", "arguments": {{...}}}}
</tool_call>

Available Tools:
{tools_schema_json}

=== AGENT EXECUTION POLICY ===
1. FIRST, if appropriate, call `retrieve_subcircuit_blueprint` to check for relevant proven circuit topologies.
2. SYNTHESIZE your candidate SPICE netlist. Before final evaluation, you may call `validate_netlist_syntax` to verify pin order and physical guardrails.
3. EXECUTE simulation via `verify_circuit_pdg_gate`. Inspect the 4-stage causal gate feedback carefully:
   - Stage 0 failure: Signal path disconnected or illegal behavioral elements.
   - Stage 1 failure: DC bias current out of range (short circuit or cutoff).
   - Stage 2 failure: DC transfer gain/switching failed (e.g. parallel mirror resistor trap).
   - Stage 3 failure: Specific parametric spec failed (slew rate, VOL, PSRR).
4. IF a failure reveals a new design trap, invoke `manage_memory` with action="add" to record the trap in MEMORY.md.
5. REVISE and re-verify until 100% pass rate is achieved.
6. ONCE fully verified, call `save_subcircuit_blueprint` to record your winning topology into skills/!
7. When you are done or providing your final candidate, output:
<architecture_summary>
...
</architecture_summary>
<spice_netlist>
.subckt {self.tools.dut_name} ...
...
.ends {self.tools.dut_name}
</spice_netlist>
"""
        return prompt.strip()

    def parse_tool_calls(self, text: str) -> List[Dict[str, Any]]:
        """Extracts tool calls from LLM response text (supports Hermes <tool_call> tags and markdown JSON)."""
        calls = []
        # Pattern 1: <tool_call> ... </tool_call>
        for m in re.finditer(r'<tool_call>\s*(.*?)\s*</tool_call>', text, re.DOTALL | re.IGNORECASE):
            raw_call = m.group(1).strip()
            try:
                data = json.loads(raw_call)
                if "name" in data:
                    calls.append(data)
            except Exception:
                pass

        # Pattern 2: ```tool_call ... ```
        if not calls:
            for m in re.finditer(r'```(?:tool_call|json)\s*({.*?"name":.*?})\s*```', text, re.DOTALL):
                try:
                    data = json.loads(m.group(1).strip())
                    if "name" in data:
                        calls.append(data)
                except Exception:
                    pass

        return calls

    def extract_netlist(self, text: str) -> Optional[str]:
        """Extracts SPICE netlist from response text."""
        m_net = re.search(r'<spice_netlist>(.*?)</spice_netlist>', text, re.DOTALL | re.IGNORECASE)
        if m_net:
            return m_net.group(1).strip()
        m_code = re.search(r'```(?:spice|cir)?\s*(\.subckt.*?\bend\w*)\s*```', text, re.DOTALL | re.IGNORECASE)
        if m_code:
            return m_code.group(1).strip()
        if ".subckt" in text.lower() and ".ends" in text.lower():
            m_sub = re.search(r'(\.subckt.*?\b\.ends[^\n]*)', text, re.DOTALL | re.IGNORECASE)
            if m_sub:
                return m_sub.group(1).strip()
        return None

    def run_turn(self, turn_idx: int) -> AgentTurnResult:
        """Executes a single conversational turn in the Hermes loop."""
        sys_prompt = self.compile_system_prompt()

        # Build prompt from conversation messages
        # To maximize model compatibility across all providers, format as system + user/assistant dialog
        conv_text = "\n\n".join([
            f"[{msg['role'].upper()}]:\n{msg['content']}"
            for msg in self.messages
        ])
        full_user_prompt = f"### AGENT CONVERSATION HISTORY & CURRENT STATE (Turn {turn_idx}):\n\n{conv_text}\n\n[INSTRUCTION]: Continue your design, reflection, tool usage, or final netlist generation."

        logger.info(f"Hermes Agent executing turn {turn_idx}...")
        resp_text = self.llm_client.complete(
            prompt=full_user_prompt,
            system_prompt=sys_prompt,
            model=self.model,
            temperature=self.temperature,
            max_tokens=8192,
            tag=f"hermes_turn_{turn_idx}"
        )

        thought = ""
        m_thought = re.search(r'<(?:thought|think)>(.*?)</(?:thought|think)>', resp_text, re.DOTALL | re.IGNORECASE)
        if m_thought:
            thought = m_thought.group(1).strip()

        tool_calls = self.parse_tool_calls(resp_text)
        tool_outputs = []

        turn_result = AgentTurnResult(
            turn_index=turn_idx,
            raw_response=resp_text,
            thought=thought,
            tool_calls=tool_calls
        )

        # Append assistant response to messages
        self.messages.append({"role": "assistant", "content": resp_text})

        # Execute any requested tool calls
        if tool_calls:
            for call in tool_calls:
                name = call.get("name", "")
                args = call.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        args = {}

                logger.info(f"Hermes invoking tool: '{name}'")
                output = self.tools.dispatch(name, args)
                tool_outputs.append({"tool": name, "output": output})

                # Check if simulation was run
                if name == "verify_circuit_pdg_gate":
                    turn_result.pass_rate = output.get("pass_rate_percent", 0.0)
                    turn_result.unique_pass_rate = output.get("unique_kind_pass_rate_pct", 0.0)
                    turn_result.passed_specs = output.get("passed_specs", 0)
                    turn_result.total_specs = output.get("total_specs", 0)

            turn_result.tool_outputs = tool_outputs
            # Append tool outputs to conversation messages for next turn
            tool_feedback = "\n\n".join([
                f"<tool_output name=\"{o['tool']}\">\n{json.dumps(o['output'], indent=2)}\n</tool_output>"
                for o in tool_outputs
            ])
            self.messages.append({
                "role": "user",
                "content": f"[TOOL EXECUTION RESULTS]:\n{tool_feedback}\n\nAnalyze these results, reflect on any discrepancies, and proceed."
            })

        # Check for extracted netlist
        netlist = self.extract_netlist(resp_text)
        if netlist:
            turn_result.netlist_extracted = netlist
            # If no tool calls in this turn and netlist exists, trigger automatic verification
            if not tool_calls:
                val_res = self.tools.validate_netlist_syntax(netlist)
                turn_result.netlist_extracted = val_res.get("cleaned_netlist", netlist)
                eval_out = self.tools.verify_circuit_pdg_gate(turn_result.netlist_extracted, tag=f"turn{turn_idx}")
                turn_result.pass_rate = eval_out.get("pass_rate_percent", 0.0)
                turn_result.unique_pass_rate = eval_out.get("unique_kind_pass_rate_pct", 0.0)
                turn_result.passed_specs = eval_out.get("passed_specs", 0)
                turn_result.total_specs = eval_out.get("total_specs", 0)
                turn_result.tool_outputs.append({"tool": "auto_verify_circuit_pdg_gate", "output": eval_out})

        self.trajectory.append(turn_result)
        return turn_result

    def run_autonomous_loop(
        self,
        task_prompt: str,
        max_turns: int = 5,
        target_pass_rate: float = 100.0,
        progress_callback: Optional[Callable[[int, str, str], None]] = None
    ) -> Dict[str, Any]:
        """
        Executes the full autonomous agentic loop:
          - Initializes with user task prompt.
          - Runs multiple turns with reasoning, tool execution, and reflection.
          - Terminates early upon 100% pass rate.
        """
        self.messages = [{"role": "user", "content": task_prompt}]
        best_netlist = None
        best_pass_rate = 0.0
        best_turn = None

        logger.info(f"Starting Hermes Autonomous Design Loop (Max Turns: {max_turns}, Target: {target_pass_rate}%)...")

        for turn in range(1, max_turns + 1):
            if progress_callback:
                progress_callback(3, f"Hermes Agent Loop (Turn {turn}/{max_turns})", "Agent reasoning, tool dispatching & verifying...")

            turn_res = self.run_turn(turn)

            if turn_res.netlist_extracted and turn_res.pass_rate >= best_pass_rate:
                best_pass_rate = turn_res.pass_rate
                best_netlist = turn_res.netlist_extracted
                best_turn = turn_res

            logger.info(f"Turn {turn} Complete: Pass Rate = {turn_res.pass_rate:.1f}% ({turn_res.passed_specs}/{turn_res.total_specs} specs)")

            if turn_res.pass_rate >= target_pass_rate and turn_res.total_specs > 0:
                logger.info(f"Target pass rate ({target_pass_rate}%) reached at turn {turn}! Terminating loop.")
                turn_res.is_terminal = True
                break

        return {
            "best_netlist": best_netlist,
            "best_pass_rate": best_pass_rate,
            "best_turn": best_turn,
            "total_turns_executed": len(self.trajectory),
            "trajectory": [
                {
                    "turn": t.turn_index,
                    "pass_rate": t.pass_rate,
                    "unique_pass_rate": t.unique_pass_rate,
                    "tools_called": [c.get("name") for c in t.tool_calls],
                    "thought": t.thought[:200]
                }
                for t in self.trajectory
            ]
        }
