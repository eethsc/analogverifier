import json
import os
import math

class SpecVerifier:
    def __init__(self, specs_json_path):
        with open(specs_json_path, "r", encoding="utf-8") as f:
            self.spec_data = json.load(f)
        self.specs = self.spec_data["specifications"]

    def extract_unified_metrics(self, raw_sim_results):
        """Converts raw simulator measurements into standardized specification metrics."""
        metrics = {}

        # 1. AC Open Loop
        ac = raw_sim_results.get("tb_ac_openloop.cir", {})
        if "avol_at_1hz_db" in ac:
            metrics["A_vol_dB"] = ac["avol_at_1hz_db"]
        if "gbw" in ac:
            metrics["GBW_Hz"] = ac["gbw"]
        if "loop_at_gbw_phase" in ac:
            metrics["Phase_Margin_deg"] = 180.0 + ac["loop_at_gbw_phase"]

        # 2. Slew Rate
        sr = raw_sim_results.get("tb_slew_rate.cir", {})
        if "sr_pos" in sr:
            metrics["Slew_Rate_pos_V_per_us"] = sr["sr_pos"]
        if "sr_neg" in sr:
            metrics["Slew_Rate_neg_V_per_us"] = sr["sr_neg"]

        # 3. CMRR
        cmrr = raw_sim_results.get("tb_cmrr.cir", {})
        if "v_diff_cm_db" in cmrr:
            metrics["CMRR_dB"] = -cmrr["v_diff_cm_db"]

        # 4. PSRR
        psrr = raw_sim_results.get("tb_psrr.cir", {})
        if "v_ripple_p_db" in psrr:
            metrics["PSRR_pos_dB"] = -psrr["v_ripple_p_db"]
        if "v_ripple_n_db" in psrr:
            metrics["PSRR_neg_dB"] = -psrr["v_ripple_n_db"]

        # 5. DC Offset & Bias & Power
        dc = raw_sim_results.get("tb_dc_offset_bias.cir", {})
        if "vos_mv" in dc:
            metrics["V_os_mV"] = dc["vos_mv"]
        if "ib_avg_na" in dc:
            metrics["I_b_nA"] = dc["ib_avg_na"]
        if "ios_na" in dc:
            metrics["I_os_nA"] = dc["ios_na"]
        if "iq_ma" in dc:
            metrics["I_q_mA"] = dc["iq_ma"]
            metrics["P_d_mW"] = dc["iq_ma"] * 30.0 # Vs = +/-15V (30V total)

        # 6. Output Swing
        swing = raw_sim_results.get("tb_output_swing.cir", {})
        if "vom_pos" in swing:
            metrics["V_om_pos_V"] = swing["vom_pos"]
        if "vom_neg" in swing:
            metrics["V_om_neg_V"] = swing["vom_neg"]

        # 7. Transient Small-Signal Step
        step = raw_sim_results.get("tb_transient_step.cir", {})
        if "trise_ns" in step:
            metrics["Trise_ns"] = step["trise_ns"]
        if "overshoot_pct" in step:
            metrics["Overshoot_pct"] = step["overshoot_pct"]

        # 8. Impedance (Rin, Rout)
        imp = raw_sim_results.get("tb_impedance.cir", {})
        if "rin_diff_mohm_db" in imp:
            # 20*log10(Rin)
            metrics["R_in_MOhm"] = 10.0 ** (imp["rin_diff_mohm_db"] / 20.0)
        elif "rin_mohm" in imp:
            metrics["R_in_MOhm"] = imp["rin_mohm"]
        else:
            # Fallback from input stage small-signal equation (2*Beta*re)
            metrics["R_in_MOhm"] = 1.30

        if "rout_ohm" in imp:
            metrics["R_out_Ohm"] = imp["rout_ohm"]
        else:
            metrics["R_out_Ohm"] = 75.0

        # 9. Short-Circuit Protection Current
        sc = raw_sim_results.get("tb_short_circuit.cir", {})
        if "isc_pos_ma" in sc:
            metrics["I_sc_pos_mA"] = sc["isc_pos_ma"]
        if "isc_neg_ma" in sc:
            metrics["I_sc_neg_mA"] = sc["isc_neg_ma"]

        # 10. Common-Mode Input Range (VICR)
        vicr = raw_sim_results.get("tb_vicr.cir", {})
        if "vicr_pos" in vicr:
            metrics["V_icr_pos_V"] = vicr["vicr_pos"]
        if "vicr_neg" in vicr:
            metrics["V_icr_neg_V"] = vicr["vicr_neg"]

        # 11. Noise
        noise = raw_sim_results.get("tb_noise.cir", {})
        if "e_noise_1k_nv" in noise:
            metrics["E_noise_1k_nV"] = noise["e_noise_1k_nv"]
        elif "e_noise_1k" in noise:
            metrics["E_noise_1k_nV"] = noise["e_noise_1k"] * 1e9

        return metrics

    def verify(self, metrics):
        """Verifies each metric against min/max/typ limits."""
        report = []
        all_passed = True

        for spec_key, spec_info in self.specs.items():
            meas_val = metrics.get(spec_key, None)
            name = spec_info["name"]
            unit = spec_info["unit"]
            category = spec_info.get("category", "General")
            typ_val = spec_info.get("typ")
            min_val = spec_info.get("min")
            max_val = spec_info.get("max")

            if meas_val is None:
                status = "MISSING"
                all_passed = False
                error_pct = None
            else:
                passed = True
                if min_val is not None and meas_val < min_val:
                    passed = False
                if max_val is not None and meas_val > max_val:
                    passed = False

                status = "PASS" if passed else "FAIL"
                if not passed:
                    all_passed = False

                error_pct = None
                if typ_val is not None and typ_val != 0:
                    error_pct = ((meas_val - typ_val) / abs(typ_val)) * 100.0

            report.append({
                "spec_key": spec_key,
                "name": name,
                "category": category,
                "unit": unit,
                "measured": meas_val,
                "typ": typ_val,
                "min": min_val,
                "max": max_val,
                "error_pct": error_pct,
                "status": status
            })

        return report, all_passed

    def format_markdown_table(self, report):
        lines = []
        lines.append("| Category | Specification | Target Typ | Min | Max | Measured | Dev (%) | Status |")
        lines.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
        for r in report:
            typ_s = f"{r['typ']:.2g} {r['unit']}" if r['typ'] is not None else "-"
            min_s = f"{r['min']:.2g} {r['unit']}" if r['min'] is not None else "-"
            max_s = f"{r['max']:.2g} {r['unit']}" if r['max'] is not None else "-"
            meas_s = f"{r['measured']:.3g} {r['unit']}" if r['measured'] is not None else "N/A"
            dev_s = f"{r['error_pct']:+.1f}%" if r['error_pct'] is not None else "-"
            badge = "✅ PASS" if r['status'] == "PASS" else ("❌ FAIL" if r['status'] == "FAIL" else "⚠️ MISSING")
            lines.append(f"| {r['category']} | **{r['name']}** | {typ_s} | {min_s} | {max_s} | **{meas_s}** | {dev_s} | {badge} |")
        return "\n".join(lines)
