import os
import copy
from agent_engine.ltspice_runner import LTspiceRunner
from agent_engine.spec_verifier import SpecVerifier
from agent_engine.parametric_synthesizer import ParametricSynthesizer

class ClosedLoopOptimizer:
    def __init__(self, specs_json_path, model_path="models/lm741_macromodel.sub"):
        self.specs_json_path = specs_json_path
        self.model_path = model_path
        self.verifier = SpecVerifier(specs_json_path)
        self.runner = LTspiceRunner()
        self.synthesizer = ParametricSynthesizer(self.verifier.specs)
        self.testbenches = [
            "tb_ac_openloop.cir",
            "tb_slew_rate.cir",
            "tb_cmrr.cir",
            "tb_psrr.cir",
            "tb_dc_offset_bias.cir",
            "tb_output_swing.cir"
        ]

    def evaluate_parameters(self, params):
        """Generates netlist, runs all testbenches, and extracts unified metrics."""
        self.synthesizer.generate_subcircuit(params, self.model_path)
        raw_results = {}
        for tb in self.testbenches:
            tb_path = os.path.join("testbenches", tb)
            raw_results[tb] = self.runner.run_netlist(tb_path)

        metrics = self.verifier.extract_unified_metrics(raw_results)
        report, all_passed = self.verifier.verify(metrics)
        return metrics, report, all_passed

    def optimize(self, max_iterations=5):
        """Executes automated feedback loop tuning."""
        params = self.synthesizer.synthesize_initial_parameters()
        history = []

        print(f"[*] Starting Closed-Loop Optimization (Max Iterations: {max_iterations})...")

        for iteration in range(1, max_iterations + 1):
            metrics, report, all_passed = self.evaluate_parameters(params)
            history.append({
                "iteration": iteration,
                "params": copy.deepcopy(params),
                "metrics": metrics,
                "all_passed": all_passed,
                "report": report
            })

            print(f"--- Iteration {iteration} Summary ---")
            gbw = metrics.get('GBW_Hz', 0)
            pm = metrics.get('Phase_Margin_deg', 0)
            sr = metrics.get('Slew_Rate_pos_V_per_us', 0)
            avol = metrics.get('A_vol_dB', 0)
            print(f"  GBW: {gbw/1e6:.4f} MHz | PM: {pm:.2f}° | SR+: {sr:.4f} V/us | Avol: {avol:.2f} dB | All Passed: {all_passed}")

            # Termination condition: All passed and errors within tight target (< 3%)
            target_gbw = self.verifier.specs["GBW_Hz"]["typ"]
            target_sr = self.verifier.specs["Slew_Rate_pos_V_per_us"]["typ"]
            target_avol = self.verifier.specs["A_vol_dB"]["typ"]
            target_pm = self.verifier.specs["Phase_Margin_deg"]["typ"]

            gbw_err = abs(gbw - target_gbw) / target_gbw
            sr_err = abs(sr - target_sr) / target_sr
            avol_err = abs(avol - target_avol) / target_avol

            if all_passed and gbw_err < 0.02 and sr_err < 0.02 and avol_err < 0.01:
                print(f"[+] Converged to target tolerances at iteration {iteration}!")
                break

            # Gradient / proportional corrections:
            # 1. GBW correction
            if gbw > 0:
                correction_gbw = target_gbw / gbw
                params["Gm1"] *= correction_gbw

            # 2. Slew rate correction
            if sr > 0:
                correction_sr = target_sr / sr
                params["I_tail"] *= correction_sr

            # 3. DC gain correction
            if avol > 0:
                gain_linear_curr = 10.0 ** (avol / 20.0)
                gain_linear_target = 10.0 ** (target_avol / 20.0)
                correction_gain = gain_linear_target / gain_linear_curr
                params["Ro_stage1"] *= correction_gain

            # 4. Phase margin correction
            if pm > 0 and abs(pm - target_pm) > 0.5:
                # adjust Cp2
                pm_diff = target_pm - pm
                params["Cp2"] *= (1.0 - pm_diff * 0.02)

        return params, metrics, report, history
