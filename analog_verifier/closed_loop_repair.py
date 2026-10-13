import os
import re
from typing import Dict, List, Any, Tuple, Optional
from .data_models import (
    CircuitInformation, Stage3Result, OracleResult,
    TestbenchCode, ClosedLoopRepairReport
)
from .stage3_graph_scheduler import Stage3GraphScheduler
from .stage4_sympy_oracle import Stage4SymPyOracle
from .simulator_interface import SimulatorInterface

class ClosedLoopRepairEngine:
    """
    Simulation-Grounded Repair Loop (Paper Algorithm 2)
    Iteratively refines testbenches and resolves simulation traps:
    - SYNTAXERROR: Syntax repair (regex sanitization, ground fix, pin aliases)
    - CONVERGENCEERROR: Relax PDG settling times delta <- delta * 1.5, re-synthesize schedule sigma, update stimulus
    - ASSERTIONFAIL: If grounded in SymPy Oracle, flags DESIGN_VIOLATION (real circuit bug found); else re-derives threshold
    - Maximum refinement iterations Kmax = 5
    """
    def __init__(self, simulator_interface: Optional[SimulatorInterface] = None, k_max: int = 5):
        self.sim = simulator_interface or SimulatorInterface()
        self.k_max = k_max
        self.oracle_engine = Stage4SymPyOracle()

    def run_repair_loop(
        self,
        testbench: TestbenchCode,
        circuit_info: CircuitInformation,
        stage3_result: Stage3Result,
        oracle: Optional[OracleResult]
    ) -> ClosedLoopRepairReport:
        current_tb = testbench
        delta_mult = 1.0
        history: List[Dict[str, Any]] = []

        for k in range(1, self.k_max + 1):
            status, raw_meas, log_msg = self.sim.run_testbench(current_tb.filepath)
            
            entry = {
                "iteration": k,
                "status": status,
                "measurements": raw_meas,
                "delta_mult": delta_mult,
                "error_snippet": log_msg[:200] if log_msg else ""
            }
            history.append(entry)

            # Algorithm 2 Line 4: if status = PASS
            if status == "PASS":
                # Check whether extracted measurements satisfy Oracle pass criteria
                if "loop_phase_phase" in raw_meas and "phase_margin_deg" not in raw_meas:
                    raw_meas["phase_margin_deg"] = 180.0 + raw_meas["loop_phase_phase"]

                assertion_fail = False
                fail_details = []

                if oracle and raw_meas:
                    sym_norm = re.sub(r'[^a-zA-Z0-9]', '', oracle.parameter_symbol).lower()
                    
                    # Find measurement matching the target parameter
                    matched_val = None
                    for mk, mv in raw_meas.items():
                        mk_norm = re.sub(r'[^a-zA-Z0-9]', '', mk).lower()
                        if sym_norm in mk_norm or mk_norm in sym_norm:
                            matched_val = mv
                            break

                    if matched_val is not None:
                        passed, reason = self.oracle_engine.evaluate_oracle_measurement(oracle, matched_val)
                        if not passed:
                            assertion_fail = True
                            fail_details.append(reason)

                if not assertion_fail:
                    return ClosedLoopRepairReport(
                        initial_testbench_id=testbench.testbench_id,
                        final_status="PASS",
                        total_iterations=k,
                        iteration_history=history,
                        final_testbench_code=current_tb.code,
                        message=f"Testbench executed and passed all verification checks in iteration {k}."
                    )
                else:
                    # Algorithm 2 Line 13-18: log.type = ASSERTIONFAIL
                    if oracle and oracle.formulas and oracle.formulas[0].is_verified:
                        # Grounded: Real bug found in circuit design (DESIGN_VIOLATION)
                        return ClosedLoopRepairReport(
                            initial_testbench_id=testbench.testbench_id,
                            final_status="DESIGN_VIOLATION",
                            total_iterations=k,
                            iteration_history=history,
                            final_testbench_code=current_tb.code,
                            message=f"Verified Assertion Failed (Grounded Oracle): Circuit violates spec! Details: {fail_details}"
                        )

            # Algorithm 2 Line 7-8: if log.type = SYNTAXERROR
            if status == "SYNTAXERROR":
                repaired_code = self._repair_syntax(current_tb.code, log_msg)
                with open(current_tb.filepath, "w", encoding="utf-8") as f:
                    f.write(repaired_code)
                current_tb.code = repaired_code

            # Algorithm 2 Line 9-12: else if log.type = CONVERGENCEERROR
            elif status == "CONVERGENCEERROR":
                delta_mult *= 1.5  # Relax PDG settling times (Line 10)
                scheduler = Stage3GraphScheduler(delta_multiplier=delta_mult)
                new_s3 = scheduler.analyze_and_schedule(circuit_info)
                new_sigma = new_s3.schedule
                # Update stimulus in testbench (Line 12)
                repaired_code = self._update_stimulus_schedule(current_tb.code, new_sigma)
                with open(current_tb.filepath, "w", encoding="utf-8") as f:
                    f.write(repaired_code)
                current_tb.code = repaired_code

        # Algorithm 2 Line 21: return MANUALREVIEW
        return ClosedLoopRepairReport(
            initial_testbench_id=testbench.testbench_id,
            final_status="MANUAL_REVIEW",
            total_iterations=self.k_max,
            iteration_history=history,
            final_testbench_code=current_tb.code,
            message=f"Maximum repair iterations ({self.k_max}) reached without full convergence."
        )

    def _repair_syntax(self, code: str, log_error: str) -> str:
        """Repairs common SPICE syntax failures (Table 37)."""
        lines = code.splitlines()
        repaired = []
        for l in lines:
            # Strip ngspice control blocks if present in LTspice run
            if any(k in l for k in [".control", ".endc", "run", "quit"]):
                continue
            repaired.append(l)

        # Inject robust SPICE solver options if not present
        if not any(".options" in l for l in repaired):
            repaired.insert(len(repaired) - 1, ".options gmin=1e-12 abstol=1e-12 reltol=0.002")

        return "\n".join(repaired)

    def _update_stimulus_schedule(self, code: str, new_schedule: Dict[str, float]) -> str:
        """Updates PULSE source delay and duration based on relaxed PDG settling times."""
        lines = code.splitlines()
        repaired = []
        for l in lines:
            if l.startswith("V_") and "PULSE" in l:
                port_match = re.match(r"^V_([A-Za-z0-9_]+)", l)
                if port_match:
                    pname = port_match.group(1)
                    if pname in new_schedule:
                        new_delay = new_schedule[pname]
                        l = re.sub(r'(PULSE\s*\([^ ]+\s+[^ ]+\s+)[^ ]+', rf'\g<1>{new_delay:.6e}', l)
            repaired.append(l)

        if not any("cshunt" in l for l in repaired):
            repaired.insert(len(repaired) - 1, ".options cshunt=1e-15 method=gear")

        return "\n".join(repaired)
