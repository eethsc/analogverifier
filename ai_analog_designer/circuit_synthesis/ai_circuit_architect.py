import os
import re
from typing import Dict, Any, List, Optional
from ..openrouter_client import OpenRouterClient

class AICircuitArchitect:
    """
    AI-Driven Analog Circuit Netlist Generator powered by Google Research's Harness² framework.
    Uses multi-component execution harnesses (C1-C8) and contrastive Propose-Probe-Compose cycles
    with OpenRouter and NVIDIA NIM API to synthesize transistor-level topologies and sizings.
    """
    def __init__(
        self,
        llm_client: Optional[Any] = None,
        use_harness2: bool = True,
        improver_provider: str = "nim",
        synthesizer_provider: str = "openrouter"
    ):
        self.llm = llm_client or OpenRouterClient()
        self.use_harness2 = use_harness2
        if self.use_harness2:
            from .harness2 import Harness2CircuitArchitect
            self.harness2_engine = Harness2CircuitArchitect(
                llm_client=self.llm,
                improver_provider=improver_provider,
                synthesizer_provider=synthesizer_provider
            )
        else:
            self.harness2_engine = None

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
        Synthesizes complete SPICE netlist from datasheet specifications.
        Leverages Harness² recursive engine when enabled.
        """
        if self.use_harness2 and self.harness2_engine:
            return self.harness2_engine.synthesize_netlist(
                ic_info=ic_info,
                pins=pins,
                specifications=specifications,
                pdg_schedule=pdg_schedule,
                iteration=iteration,
                prior_feedback=prior_feedback,
                user_prompt=user_prompt,
                measurement_plan=measurement_plan
            )

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
            from ..neuro_symbolic_verifier.spec_planner import fixture_brief
            measurement_section = f"""
## HOW THIS CIRCUIT WILL BE MEASURED (DUT is instantiated with V- = ground, V+ = VS; mid-supply node exists)
{fixture_brief(measurement_plan)}
The subcircuit must therefore work with its negative supply pin tied to 0 V (single supply) unless the notes say
otherwise, must not reference the external node '0' internally except through its supply pins, and must not
contain any analysis directives.
"""
        user_prompt_section = ""
        if user_prompt:
            user_prompt_section = f"""
## USER NATURAL LANGUAGE DIRECTIVES & SPECIAL REQUIREMENTS:
The user explicitly requested the following custom circuit specifications and design criteria:
"{user_prompt}"
You MUST explicitly tailor the circuit topology, bias branches, operating voltages, component values, and pin behavior to satisfy these user instructions alongside the base specifications.
"""

        feedback_section = ""
        if prior_feedback:
            feedback_section = f"""
## PREVIOUS SIMULATION FEEDBACK (CRITICAL: FIX ALL REPORTED DISCREPANCIES):
{prior_feedback}
"""

        prompt = f"""You are a World-Class Principal Analog & Mixed-Signal IC Design Engineer.
Your task is to synthesize from scratch a complete, highly realistic, physically accurate SPICE netlist for the following device.

## DEVICE INFORMATION:
- Product Part Name: {part_name}
- Category / Description: {ic_info.get('description', '')}

## PIN INTERFACE (MANDATORY SUBCIRCUIT TERMINALS IN EXACT ORDER):
{pins_summary}

## TARGET ELECTRICAL SPECIFICATIONS (EVERY SINGLE SPECIFICATION MUST BE MET):
{specs_summary}

## PORT DEPENDENCY SCHEDULE (PHYSICAL STARTUP SEQUENCE):
{schedule_summary}
{measurement_section}
{user_prompt_section}
{feedback_section}

## DESIGN & SPICE REQUIREMENTS:
1. Subcircuit Header: `.subckt {subckt_name} {' '.join([p['name'] for p in pins])}`
2. MUST define standard Level 1 or standard SPICE transistor and diode models (e.g. `.model NMOS NMOS (...)`, `.model PMOS PMOS (...)`, `.model QNPN NPN (...)`, etc.).
3. MUST synthesize the complete internal circuit architecture:
   - Input stages, bias current mirrors, internal amplifiers/VCO/charge pumps, frequency compensation, output drivers, and feedback networks.
   - Sizing of MOSFETs (W/L) and passives (R, C, L) MUST be analytically calculated to satisfy the specified timings, frequencies, voltages, and currents.
4. STRICT SPICE SYNTAX & COMPONENT RULES:
   - For transistors: Use standard `M... d g s b <model> W=... L=...`, `Q... c b e <model>`, `D... a c <model>`, `R... n1 n2 <val>`, `C... n1 n2 <val>`.
   - For behavioral modeling: ALWAYS use standard SPICE behavioral sources:
     * Voltage: `B<name> <pos> <neg> V = {{ <expr> }}` or `E<name> <pos> <neg> VALUE = {{ <expr> }}`
     * Current: `B<name> <pos> <neg> I = {{ <expr> }}` or `G<name> <pos> <neg> VALUE = {{ <expr> }}`
   - STRICTLY PROHIBITED: DO NOT use non-standard pseudo devices such as `A_xxx` or `.model xxx logic(...)` or `.model xxx Schmitt(...)`. These cause fatal SPICE compilation errors. Use standard MOSFET inverters, Schmitt trigger subcircuits, or `B...` behavioral sources!
   - In B-source expressions `{{ ... }}`, use single `&` for logical AND and single `|` for logical OR. NEVER use `&&` or `||`!
   - B-sources MUST evaluate a SINGLE mathematical expression (e.g. `B1 out 0 V = {{ (sin(2*pi*f*time) > 0) ? 3.3 : 0 }}`). NEVER write procedural multi-variable code like `{{ a = 1 b = 2 c }}` inside a B-source! If you need intermediate values, define separate B-sources for each intermediate node or inline them.
   - Charge pumps (CP) driving loop filters MUST be modeled as CURRENT sources: `B_cp n_ctrl gnd I = {{ <expr> }}`, NOT voltage sources! Ideal voltage sources driving capacitors or referencing their own node voltage cause Singular Matrix errors in SPICE.
   - NEVER write self-referential behavioral sources: e.g. B1 X 0 V = {{ (cond) ? val : V(X) }}. SPICE cannot compute the DC operating point of an unbuffered self-loop and crashes with 'Singular matrix: check node X'.
   - HOW TO MODEL MIXED-SIGNAL LATCHES / SAMPLE-AND-HOLD:
     Use an ideal voltage-controlled switch S_sw and capacitor C_hold with a leak resistor:
     S_sample IN HOLD_NODE CTRL 0 S_MODEL
     C_sample HOLD_NODE 0 10p
     R_sample HOLD_NODE 0 1G
     E_buffer OUT 0 HOLD_NODE 0 1
   - HOW TO MODEL DIGITAL TIMERS / ONE-SHOT DELAYS (e.g. BUSY pulse):
     Do NOT write ddt(V(signal)) or 	ime - V(t_start)! Use an RC timing ramp:
     R_time TRIG T_NODE 10k
     C_time T_NODE 0 27p
     B_busy BUSY 0 V = {{ (V(T_NODE) > 1.65) ? 3.3 : 0 }}
   - HOW TO MODEL MULTI-BIT ADC/DAC CONVERSION:
     Compute digital bits algebraically directly from analog input voltage:
     B_code CODE 0 V = {{ min(16383, max(0, floor(V(IN) / V(VREF) * 16384))) }}
     Then compute bit voltages e.g. B_db13 DB13_INT 0 V = {{ ((floor(V(CODE)/8192)) % 2 > 0.5) ? 3.3 : 0 }}
   - Every device instance name (M1, M2, R1, B1, etc.) must be strictly UNIQUE within each subcircuit.
   - For mid-line comments, use `;` (semicolon), NOT `*`.
   - DO NOT include `.global` or `.temp` inside `.subckt`.
   - NEVER write simulation commands (`.tran`, `.ac`, `.op`, `.dc`) inside a subcircuit!
   - NEVER place parentheses around MOSFET parameters: write `W=10u L=0.5u`, NEVER `(W=10u L=0.5u)`.
   - NEVER invent non-standard parameters like `TD=` on B-sources or `TRISTATE=` on E-sources. For tri-state digital outputs, use standard voltage-controlled switches: `S_buf out node_in en 0 S_MODEL` with `.model S_MODEL VSWITCH (RON=10 ROFF=1G VTH=0.5 VH=0.1)`.
   - NEVER write `.model ... V (...)`. Independent sources `V...` have no model.
   - Independent source parameters (`PULSE`, `PWL`) must be fixed numbers or parameter names, NEVER dynamic node voltages `V(...)`.
   - All arithmetic expressions on component values MUST be wrapped in curly braces: e.g. {{2n/1k}}.
5. End the subcircuit with `.ends {subckt_name}`.
6. All node names must be valid SPICE identifiers (alphanumeric and underscores, no spaces).
7. HARD STRUCTURE RULES (violations make LTspice reject the whole file):
   - NEVER nest `.subckt` blocks. Define helper subcircuits FIRST at top level, then `.subckt {subckt_name}` LAST.
   - Line continuation is a leading `+` on the next line. NEVER end a line with `\\`.
   - NEVER write prose, ellipses or "repeat N times" inside the netlist. Write every device explicitly, or use `M=` / `m=` multiplicity.
   - Behavioral sources: `B... n+ n- V={{expr}}` or `E... n+ n- VALUE={{expr}}` (exactly two nodes before V=/VALUE=).
     Do NOT use `freq`, `TEMP`, `temp_nom` or other undefined names inside expressions; use `.param` for every constant.
   - Every `.param` you reference must be defined in the same file. Subcircuit parameters must be written as `PARAMS: a=1 b=2` on the `.subckt` line.
   - Noise sources, temperature coefficients and drift models are NOT needed: omit them.
   - The design must reach a DC operating point with V- = 0 V and V+ = the test supply, all inputs at mid-supply.

OUTPUT FORMAT:
Respond using the following XML tags:
<architecture_summary>
Detailed technical explanation of the chosen topology and component sizing methodology.
</architecture_summary>

<spice_netlist>
* Complete SPICE subcircuit netlist code here
.subckt {subckt_name} {' '.join([p['name'] for p in pins])}
...
.ends {subckt_name}
</spice_netlist>
"""
        system_prompt = "You are an autonomous AI Analog Circuit Architect. Synthesize production-quality, convergent SPICE netlists that strictly satisfy all datasheet requirements."

        raw_resp = self.llm.complete(prompt, system_prompt=system_prompt, temperature=0.1, max_tokens=8192, tag="synth")

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
            # Fallback: check for markdown block
            m_code = re.search(r'```(?:spice|cir)?\s*(.*?)\s*```', raw_resp, re.DOTALL | re.IGNORECASE)
            if m_code:
                netlist_code = m_code.group(1).strip()
            elif ".subckt" in raw_resp.lower():
                netlist_code = raw_resp.strip()

        # Clean netlist string
        if netlist_code.startswith("```"):
            netlist_code = re.sub(r'^```[a-zA-Z]*\n', '', netlist_code)
            netlist_code = re.sub(r'\n```$', '', netlist_code)

        return {
            "subcircuit_name": subckt_name,
            "architecture_summary": arch_summary,
            "spice_netlist": netlist_code.strip(),
            "design_rationale": {"summary": arch_summary}
        }
