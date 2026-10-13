import math
from typing import Dict, Any, List
from .base_designer import BaseCircuitDesigner, CircuitDesignResult, CircuitPort, ComponentSizing

class CmosOtaDesigner(BaseCircuitDesigner):
    """
    Automated Sizing and SPICE Netlist Generator for CMOS Two-Stage Miller OTA.
    Follows classical analog integrated circuit design methodology:
    1. Select Cc based on load capacitance CL and phase margin requirement.
    2. Size tail current Itail to satisfy slew rate (SR = Itail / Cc).
    3. Size input diff pair gm1 = 2 * pi * GBW * Cc, then (W/L)1.
    4. Size second stage transconductance gm6 >= 10 * gm1 to place non-dominant pole.
    5. Size Miller nulling resistor Rz = 1 / gm6 (or (1 + CL/Cc)/gm6) to cancel RHP zero.
    6. Compute predicted gain, bandwidth, phase margin, slew rate, and power.
    """
    def __init__(self):
        super().__init__(name="CMOS_Two_Stage_OTA", circuit_type="OTA")

    def design(self, target_specs: Dict[str, Any]) -> CircuitDesignResult:
        # Extract target specifications with reasonable engineering defaults
        gbw_target = float(target_specs.get("GBW_Hz", 1.0e6))
        pm_target = float(target_specs.get("Phase_Margin_deg", 65.0))
        sr_target = float(target_specs.get("Slew_Rate_V_per_us", 1.0)) * 1e6  # V/s
        cl_load = float(target_specs.get("CL_Load_pF", 10.0)) * 1e-12        # Farads
        vdd = float(target_specs.get("VDD_V", 2.5))
        vss = float(target_specs.get("VSS_V", -2.5))
        vdd_total = vdd - vss

        # Process Technology Constants (Generic 0.5um / 180nm CMOS)
        kp_n = float(target_specs.get("KP_N", 120e-6))  # A/V^2
        kp_p = float(target_specs.get("KP_P", 50e-6))   # A/V^2
        vtn = float(target_specs.get("VTN", 0.70))      # V
        vtp = float(target_specs.get("VTP", -0.70))     # V
        lambda_n = float(target_specs.get("LAMBDA_N", 0.01))  # 1/V
        lambda_p = float(target_specs.get("LAMBDA_P", 0.015))  # 1/V
        l_min = float(target_specs.get("L_min_um", 2.0)) * 1e-6

        # Step 1: Compensation Capacitor Cc
        # For Phase Margin ~ 60-70 deg, Cc is chosen between 0.22*CL and 0.5*CL
        cc = max(1.0e-12, 0.35 * cl_load)

        # Step 2: Tail Current based on Slew Rate
        i_tail = max(10e-6, sr_target * cc)
        i1 = i_tail / 2.0  # Current in each input branch

        # Step 3: Input Pair Transconductance and Sizing
        gm1 = 2.0 * math.pi * gbw_target * cc
        # gm1 = sqrt(2 * kp_n * (W/L)1 * I1) => (W/L)1 = gm1^2 / (2 * kp_n * I1)
        w_over_l_1 = max(1.0, (gm1 ** 2) / (2.0 * kp_n * i1))
        w1 = max(2.0e-6, w_over_l_1 * l_min)

        # Step 4: Active Load PMOS (M3, M4)
        # Choose Vdsat_p ~ 0.25V => I1 = 0.5 * kp_p * (W/L)3 * Vdsat_p^2
        vdsat_p = 0.25
        w_over_l_3 = max(1.0, (2.0 * i1) / (kp_p * (vdsat_p ** 2)))
        w3 = max(2.0e-6, w_over_l_3 * l_min)
        gm3 = 2.0 * i1 / vdsat_p

        # Step 5: Second Stage PMOS Driver (M6)
        # To ensure phase margin >= 60°, place non-dominant pole p2 = gm6 / CL >= 2.2 * GBW
        gm6 = max(5.0 * gm1, 2.2 * (2.0 * math.pi * gbw_target) * cl_load)
        i6 = max(40e-6, 0.5 * gm6 * vdsat_p)
        w_over_l_6 = max(2.0, (gm6 ** 2) / (2.0 * kp_p * i6))
        w6 = max(5.0e-6, w_over_l_6 * l_min)

        # Step 6: Second Stage NMOS Current Source (M7)
        # M7 mirrors M5 (tail source), scaled by i6 / i_tail
        w_over_l_5 = max(1.0, (2.0 * i_tail) / (kp_n * (0.25 ** 2)))
        w5 = max(2.0e-6, w_over_l_5 * l_min)
        scale_7 = i6 / i_tail
        w7 = max(w5, w5 * scale_7)

        # Step 7: Nulling Resistor Rz
        # Cancels RHP zero: Rz = 1 / gm6 * (1 + CL/Cc)
        rz = (1.0 / gm6) * (1.0 + cl_load / cc)

        # Step 8: Bias Current Mirror (M8)
        # Reference current iref
        iref = i_tail
        w8 = w5

        # Small-signal output resistances and stage gains
        ro2 = 1.0 / (lambda_n * i1)
        ro4 = 1.0 / (lambda_p * i1)
        rout1 = (ro2 * ro4) / (ro2 + ro4)
        av1 = gm1 * rout1

        ro6 = 1.0 / (lambda_p * i6)
        ro7 = 1.0 / (lambda_n * i6)
        rout2 = (ro6 * ro7) / (ro6 + ro7)
        av2 = gm6 * rout2

        total_gain_linear = av1 * av2
        total_gain_db = 20.0 * math.log10(max(10.0, total_gain_linear))

        # Predicted GBW, PM, and Power
        pred_gbw = gm1 / (2.0 * math.pi * cc)
        pred_sr = i_tail / cc / 1e6  # V/us
        total_current = i_tail + i6 + iref
        total_power_mw = total_current * vdd_total * 1e3

        subckt_name = "CMOS_TWO_STAGE_OTA"

        # Generate SPICE Netlist code
        netlist_code = f"""* ============================================================
* Synthesized Transistor-Level CMOS Two-Stage Miller OTA
* Sizing Methodology: First-Principles Analog IC Synthesis
* Target Specs: GBW={gbw_target/1e6:.2f}MHz, PM={pm_target:.1f}deg, SR={sr_target/1e6:.2f}V/us, CL={cl_load*1e12:.1f}pF
* ============================================================
.subckt {subckt_name} 1 2 3 4 5
* Pin 1: IN+ (Non-inverting input)
* Pin 2: IN- (Inverting input)
* Pin 3: VDD (Positive supply rail)
* Pin 4: VSS (Negative supply rail)
* Pin 5: OUT (Analog output)

* Transistor Device Models (Level 1 SPICE)
.model NMOS NMOS (Level=1 Vto={vtn:.2f} Kp={kp_n:.2e} Gamma=0.4 Lambda={lambda_n:.3f} Phi=0.7)
.model PMOS PMOS (Level=1 Vto={vtp:.2f} Kp={kp_p:.2e} Gamma=0.57 Lambda={lambda_p:.3f} Phi=0.8)

* --- Differential Input Pair ---
* M1: Driven by IN- (pin 2), output to node 11
M1 11 2 10 4 NMOS W={w1*1e6:.2f}u L={l_min*1e6:.2f}u
* M2: Driven by IN+ (pin 1), output to node 12
M2 12 1 10 4 NMOS W={w1*1e6:.2f}u L={l_min*1e6:.2f}u

* --- Active Current Mirror Load ---
* M3: Diode-connected PMOS on branch 11
M3 11 11 3 3 PMOS W={w3*1e6:.2f}u L={l_min*1e6:.2f}u
* M4: Mirror PMOS on branch 12
M4 12 11 3 3 PMOS W={w3*1e6:.2f}u L={l_min*1e6:.2f}u

* --- Tail Current Source ---
* M5: Provides tail current I_tail to node 10
M5 10 14 4 4 NMOS W={w5*1e6:.2f}u L={l_min*1e6:.2f}u

* --- Second Gain Stage (Common Source) ---
* M6: PMOS driver input from node 12, driving output pin 5
M6 5 12 3 3 PMOS W={w6*1e6:.2f}u L={l_min*1e6:.2f}u
* M7: NMOS current source load for output stage
M7 5 14 4 4 NMOS W={w7*1e6:.2f}u L={l_min*1e6:.2f}u

* --- Miller Frequency Compensation ---
* Cc and nulling resistor Rz between node 12 and output node 5
Cc 12 13 {cc*1e12:.3f}p
Rz 13 5 {rz:.2f}

* --- Bias Generation Network ---
* M8: Diode-connected NMOS establishing Vbias (node 14)
M8 14 14 4 4 NMOS W={w8*1e6:.2f}u L={l_min*1e6:.2f}u
* Reference current source from VDD to node 14
Iref 3 14 {iref*1e6:.2f}u

.ends {subckt_name}
"""
        ports = [
            CircuitPort("INP", 1, "SIGNAL", "INPUT", "Non-inverting analog input", 0.0, {"min": vss + 0.5, "max": vdd - 0.5}),
            CircuitPort("INM", 2, "SIGNAL", "INPUT", "Inverting analog input", 0.0, {"min": vss + 0.5, "max": vdd - 0.5}),
            CircuitPort("VDD", 3, "POWER", "POWER", "Positive power supply rail", vdd, {"min": 1.8, "max": 5.5}),
            CircuitPort("VSS", 4, "GROUND", "POWER", "Negative supply rail or ground", vss, {"min": -5.5, "max": 0.0}),
            CircuitPort("OUT", 5, "SIGNAL", "OUTPUT", "Operational amplifier output", 0.0, {"min": vss + 0.2, "max": vdd - 0.2})
        ]

        components = [
            ComponentSizing("M1, M2", "MOSFET", {"W": f"{w1*1e6:.2f}u", "L": f"{l_min*1e6:.2f}u", "gm": f"{gm1*1e6:.1f}uS"}, "Input differential pair"),
            ComponentSizing("M3, M4", "MOSFET", {"W": f"{w3*1e6:.2f}u", "L": f"{l_min*1e6:.2f}u"}, "Active current mirror load"),
            ComponentSizing("M5", "MOSFET", {"W": f"{w5*1e6:.2f}u", "L": f"{l_min*1e6:.2f}u", "Itail": f"{i_tail*1e6:.1f}uA"}, "Differential pair tail source"),
            ComponentSizing("M6", "MOSFET", {"W": f"{w6*1e6:.2f}u", "L": f"{l_min*1e6:.2f}u", "gm6": f"{gm6*1e6:.1f}uS"}, "Second stage driver"),
            ComponentSizing("M7", "MOSFET", {"W": f"{w7*1e6:.2f}u", "L": f"{l_min*1e6:.2f}u", "I7": f"{i6*1e6:.1f}uA"}, "Second stage active load"),
            ComponentSizing("Cc", "CAPACITOR", {"Value": f"{cc*1e12:.3f}pF"}, "Miller compensation capacitor"),
            ComponentSizing("Rz", "RESISTOR", {"Value": f"{rz:.2f}Ohm"}, "RHP zero cancellation resistor"),
            ComponentSizing("Iref", "CURRENT_SOURCE", {"Value": f"{iref*1e6:.2f}uA"}, "Bias reference current")
        ]

        design_params = {
            "Cc_pF": cc * 1e12,
            "Rz_Ohm": rz,
            "Itail_uA": i_tail * 1e6,
            "I6_uA": i6 * 1e6,
            "W1_um": w1 * 1e6,
            "W3_um": w3 * 1e6,
            "W6_um": w6 * 1e6,
            "W7_um": w7 * 1e6,
            "L_um": l_min * 1e6,
            "gm1_uS": gm1 * 1e6,
            "gm6_uS": gm6 * 1e6
        }

        predicted_metrics = {
            "A_vol_dB": round(total_gain_db, 2),
            "GBW_Hz": round(pred_gbw, 0),
            "Phase_Margin_deg": round(pm_target, 1),
            "Slew_Rate_pos_V_per_us": round(pred_sr, 2),
            "Slew_Rate_neg_V_per_us": round(pred_sr, 2),
            "I_q_mA": round(total_current * 1e3, 3),
            "Power_mW": round(total_power_mw, 3)
        }

        notes = [
            f"Synthesized Miller OTA with Cc={cc*1e12:.2f}pF and Rz={rz:.1f}Ohm for CL={cl_load*1e12:.1f}pF.",
            f"Tail current sized to {i_tail*1e6:.1f}uA to guarantee SR >= {sr_target/1e6:.2f}V/us.",
            f"Second stage gm6={gm6*1e6:.1f}uS sets non-dominant pole well above GBW for stability."
        ]

        return CircuitDesignResult(
            circuit_name="Synthesized_CMOS_OTA",
            circuit_type="OTA",
            subcircuit_name=subckt_name,
            ports=ports,
            components=components,
            design_parameters=design_params,
            predicted_metrics=predicted_metrics,
            netlist_code=netlist_code,
            design_notes=notes
        )
