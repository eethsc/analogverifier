import os
import re
import time
import subprocess
from typing import Dict, Any, Tuple, Optional
from agent_engine.ltspice_runner import LTspiceRunner

class SimulatorInterface:
    """
    Interface for SPICE Simulation Engine (LTspice / Ngspice).
    Runs batch simulation, detects error types (SYNTAX, CONVERGENCE, NONE),
    and extracts measurement tables.
    """
    def __init__(self, simulator_path: Optional[str] = None):
        self.runner = LTspiceRunner(simulator_path=simulator_path) if simulator_path else LTspiceRunner()

    def run_testbench(self, testbench_file: str, timeout: int = 60) -> Tuple[str, Dict[str, float], str]:
        """
        Executes testbench and returns:
        - log_type: 'PASS', 'SYNTAXERROR', 'CONVERGENCEERROR', or 'ASSERTIONFAIL'
        - measurements: dictionary of extracted .MEASURE values
        - raw_log: raw simulator log content
        """
        testbench_file = os.path.abspath(testbench_file)
        workdir = os.path.dirname(testbench_file)
        base_name = os.path.splitext(os.path.basename(testbench_file))[0]
        log_file = os.path.join(workdir, f"{base_name}.log")

        t0 = time.time()
        try:
            measurements = self.runner.run_netlist(testbench_file, timeout=timeout)
        except Exception as e:
            err_msg = str(e)
            if "syntax" in err_msg.lower() or "unknown subcircuit" in err_msg.lower() or "missing" in err_msg.lower():
                return "SYNTAXERROR", {}, err_msg
            elif "timed out" in err_msg.lower() or "convergence" in err_msg.lower() or "step size" in err_msg.lower():
                return "CONVERGENCEERROR", {}, err_msg
            else:
                return "CONVERGENCEERROR", {}, err_msg

        # Read log file to check for hidden convergence or syntax warnings
        raw_log = ""
        if os.path.exists(log_file):
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    raw_log = f.read()
            except Exception:
                pass

        # Check for convergence errors in log
        log_lower = raw_log.lower()
        if "singular matrix" in log_lower or "time step too small" in log_lower or "analysis failed" in log_lower:
            return "CONVERGENCEERROR", measurements, raw_log
        if "syntax error" in log_lower or "unknown parameter" in log_lower or "error on line" in log_lower:
            return "SYNTAXERROR", measurements, raw_log

        # If measurements were extracted, simulation ran successfully
        if measurements:
            return "PASS", measurements, raw_log
        else:
            # Check if there are no measurements defined
            if ".meas" in open(testbench_file, "r").read().lower():
                return "CONVERGENCEERROR", {}, raw_log
            return "PASS", {}, raw_log
