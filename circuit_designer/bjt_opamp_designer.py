from typing import Dict, Any, List
from .base_designer import BaseCircuitDesigner, CircuitDesignResult, CircuitPort, ComponentSizing
from agent_engine.parametric_synthesizer import ParametricSynthesizer

class BjtOpampDesigner(BaseCircuitDesigner):
    """
    Automated Synthesizer for BJT Monolithic / Macromodel Operational Amplifiers (LM741 Architecture).
    """
    def __init__(self):
        super().__init__(name="LM741_Operational_Amplifier", circuit_type="OPAMP")

    def design(self, target_specs: Dict[str, Any]) -> CircuitDesignResult:
        defaults = {
            "A_vol_dB": {"typ": 106.0, "min": 94.0, "max": None},
            "GBW_Hz": {"typ": 1.0e6, "min": 0.7e6, "max": 1.5e6},
            "Phase_Margin_deg": {"typ": 65.0, "min": 45.0, "max": 90.0},
            "Slew_Rate_pos_V_per_us": {"typ": 0.5, "min": 0.3, "max": None},
            "V_os_mV": {"typ": 1.0, "min": 0.0, "max": 5.0},
            "I_b_nA": {"typ": 80.0, "min": 0.0, "max": 500.0},
            "I_os_nA": {"typ": 20.0, "min": 0.0, "max": 200.0},
            "I_q_mA": {"typ": 1.7, "min": 0.8, "max": 2.8},
            "CMRR_dB": {"typ": 90.0, "min": 70.0, "max": None},
            "PSRR_pos_dB": {"typ": 96.0, "min": 77.0, "max": None},
            "V_om_pos_V": {"typ": 13.5, "min": 12.0, "max": 14.5}
        }
        formatted_specs = dict(defaults)
        for k, v in target_specs.items():
            if isinstance(v, dict) and "typ" in v:
                formatted_specs[k] = v
            elif isinstance(v, (int, float)):
                formatted_specs[k] = {"typ": float(v), "min": float(v)*0.8, "max": float(v)*1.2}

        # Use analytical parametric synthesizer
        synth = ParametricSynthesizer(formatted_specs)
        params = synth.synthesize_initial_parameters()
        
        # We can produce the subcircuit string directly
        subckt_name = "LM741_MACRO"
        temp_path = "generated_netlists/lm741_autosynth.sub"
        synth.generate_subcircuit(params, temp_path)
        with open(temp_path, "r", encoding="utf-8") as f:
            code = f.read()

        ports = [
            CircuitPort("INP", 1, "SIGNAL", "INPUT", "Non-inverting input", 0.0, {"min": -12.0, "max": 12.0}),
            CircuitPort("INM", 2, "SIGNAL", "INPUT", "Inverting input", 0.0, {"min": -12.0, "max": 12.0}),
            CircuitPort("VCC", 3, "POWER", "POWER", "Positive power supply rail (+15V)", 15.0, {"min": 5.0, "max": 22.0}),
            CircuitPort("VEE", 4, "GROUND", "POWER", "Negative power supply rail (-15V)", -15.0, {"min": -22.0, "max": -5.0}),
            CircuitPort("OUT", 5, "SIGNAL", "OUTPUT", "Amplifier output terminal", 0.0, {"min": -14.0, "max": 14.0})
        ]

        components = [
            ComponentSizing("Gm1_stage", "TRANSCONDUCTOR", {"gm1": f"{params['Gm1']:.4e}S"}, "Input differential transconductance stage"),
            ComponentSizing("Cc", "CAPACITOR", {"Value": f"{params['Cc']*1e12:.1f}pF"}, "Dominant pole compensation capacitor"),
            ComponentSizing("Ro1", "RESISTOR", {"Value": f"{params['Ro_stage1']/1e6:.2f}MOhm"}, "Stage 1 high output impedance"),
            ComponentSizing("Cp2", "CAPACITOR", {"Value": f"{params['Cp2']*1e12:.2f}pF"}, "Second pole compensation capacitor")
        ]

        predicted = {
            "A_vol_dB": round(formatted_specs.get("A_vol_dB", {}).get("typ", 106.0), 1),
            "GBW_Hz": round(formatted_specs.get("GBW_Hz", {}).get("typ", 1.0e6), 0),
            "Phase_Margin_deg": round(formatted_specs.get("Phase_Margin_deg", {}).get("typ", 65.0), 1),
            "Slew_Rate_pos_V_per_us": round(formatted_specs.get("Slew_Rate_pos_V_per_us", {}).get("typ", 0.5), 2),
            "I_q_mA": round(params['Iq'] * 1e3, 2)
        }

        return CircuitDesignResult(
            circuit_name="LM741_Operational_Amplifier",
            circuit_type="OPAMP",
            subcircuit_name=subckt_name,
            ports=ports,
            components=components,
            design_parameters=params,
            predicted_metrics=predicted,
            netlist_code=code,
            design_notes=["Synthesized LM741 OpAmp macromodel from datasheet specifications."]
        )
