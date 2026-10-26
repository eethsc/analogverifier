import re
from typing import Dict, List, Any, Optional
from .data_models import (
    DocumentDatabase, CircuitInformation, PinDefinition,
    ElectricalParameter, EquationDefinition
)

class Stage1CircuitExtractor:
    """
    Stage 1: Circuit Information Extraction (Paper Appendix B, Algorithm 4 & 5)
    Transforms raw document database into a unified schema: CircuitInformation.
    - Resolves pin definitions and infers initial functional roles
    - Consolidates and deduplicates electrical parameters (Algorithm 5)
    - Formalizes mathematical equations for downstream SymPy parsing
    - Calculates confidence score via Eq. 6:
      Confidence(C) = alpha1*Ctype + alpha2*Cpins + alpha3*Cparams + alpha4*Ceqns
    """
    def __init__(self, alpha1=0.3, alpha2=0.2, alpha3=0.3, alpha4=0.2):
        self.alpha1 = alpha1
        self.alpha2 = alpha2
        self.alpha3 = alpha3
        self.alpha4 = alpha4

    def extract(self, doc_db: DocumentDatabase, default_circuit_type: str = "OTA") -> CircuitInformation:
        circuit_name = doc_db.metadata.get("circuit_name", "Analog_DUT")
        circuit_type = default_circuit_type
        circuit_desc = f"Characterization and verification model for {circuit_name} ({circuit_type})"
        key_features = []
        raw_pins: List[PinDefinition] = []
        raw_params: List[ElectricalParameter] = []
        raw_eqs: List[EquationDefinition] = []
        operating_cond: Dict[str, Any] = {}

        # Scan blocks
        for blk in doc_db.blocks:
            if blk.block_type == "feature_list" and "features" in blk.metadata:
                key_features.extend(blk.metadata["features"])
            elif blk.block_type == "application_info" and "test_conditions" in blk.metadata:
                operating_cond.update(blk.metadata["test_conditions"])

            # Pins
            for p in blk.extracted_pins:
                pin_def = PinDefinition(
                    pin_name=p["pin_name"],
                    pin_number=p.get("pin_number", {}),
                    pin_type=p.get("pin_type", "ANALOG"),
                    direction=p.get("direction", "INPUT"),
                    description=p.get("description", ""),
                    electrical_constraints=p.get("electrical_constraints", {}),
                    inferred_role=self._infer_pin_role(p["pin_name"], p.get("pin_type", "ANALOG"), circuit_type),
                    source_block=blk.block_id
                )
                raw_pins.append(pin_def)

            # Parameters
            for param in blk.extracted_parameters:
                e_param = ElectricalParameter(
                    symbol=param["symbol"],
                    name=param.get("name", param["symbol"]),
                    description=param.get("description", ""),
                    min_value=param.get("min_value"),
                    typ_value=param.get("typ_value"),
                    max_value=param.get("max_value"),
                    unit=param.get("unit", ""),
                    test_conditions=param.get("test_conditions", []),
                    category=param.get("category", self._infer_param_category(param["symbol"], param.get("unit", ""))),
                    is_testable=param.get("is_testable", True),
                    related_pins=self._infer_related_pins(param["symbol"], raw_pins),
                    test_type_hint=self._infer_test_hint(param["symbol"], param.get("unit", "")),
                    source_block=blk.block_id
                )
                raw_params.append(e_param)

            # Equations
            for eq in blk.extracted_equations:
                e_eq = EquationDefinition(
                    equation_id=eq.get("equation_id", "EQ_001"),
                    raw_form=eq.get("raw_form", ""),
                    sympy_form=eq.get("sympy_form", eq.get("raw_form", "")),
                    latex_form=eq.get("latex_form", ""),
                    description=eq.get("description", ""),
                    output_variable=eq.get("output_variable", ""),
                    input_variables=eq.get("input_variables", []),
                    source_block=blk.block_id
                )
                raw_eqs.append(e_eq)

        # Algorithm 5: Parameter Deduplication and Harmonization
        dedup_params = self._deduplicate_parameters(raw_params)

        # Pin deduplication by pin_name
        dedup_pins = {}
        for p in raw_pins:
            if p.pin_name not in dedup_pins:
                dedup_pins[p.pin_name] = p
            else:
                # Merge constraints
                dedup_pins[p.pin_name].electrical_constraints.update(p.electrical_constraints)
        pins_list = list(dedup_pins.values())

        # Equation deduplication
        dedup_eqs = {eq.equation_id: eq for eq in raw_eqs}
        eqs_list = list(dedup_eqs.values())

        # Confidence Scoring (Eq. 6)
        c_type = 0.98
        c_pins = 1.0 if pins_list else 0.8
        c_params = len([p for p in dedup_params if p.typ_value is not None or p.min_value is not None or p.max_value is not None]) / max(1, len(dedup_params))
        c_eqns = 1.0 if eqs_list else 0.9
        confidence = self.alpha1 * c_type + self.alpha2 * c_pins + self.alpha3 * c_params + self.alpha4 * c_eqns

        return CircuitInformation(
            circuit_name=circuit_name,
            circuit_type=circuit_type,
            circuit_description=circuit_desc,
            key_features=key_features,
            pins=pins_list,
            parameters=dedup_params,
            equations=eqs_list,
            operating_conditions=operating_cond,
            extraction_confidence=round(confidence, 3),
            extraction_notes=[f"Deduplicated to {len(dedup_params)} parameters and {len(pins_list)} pins."]
        )

    def _infer_pin_role(self, pin_name: str, pin_type: str, circuit_type: str) -> str:
        name_up = pin_name.upper()
        if any(k in name_up for k in ["VDD", "VCC", "VIN", "IN", "VBAT", "SUPPLY"]) and not any(k in name_up for k in ["INP", "INM", "IN+", "IN-"]):
            return "POWER"
        if any(k in name_up for k in ["GND", "VSS", "VEE", "GROUND"]):
            return "GROUND"
        if any(k in name_up for k in ["BST", "BOOT", "BIAS", "IREF", "VREF", "VBIAS"]):
            return "BIAS"
        if any(k in name_up for k in ["EN", "ENABLE", "SHDN", "RESET", "STBY"]):
            return "ENABLE"
        if any(k in name_up for k in ["CTRL", "MODE", "SYNC", "CLK", "CLOCK"]):
            return "CONTROL"
        if any(k in name_up for k in ["FB", "FEEDBACK", "SENSE"]):
            return "FEEDBACK"
        if any(k in name_up for k in ["OUT", "SW", "VOUT"]):
            return "OUTPUT"
        return "SIGNAL"

    def _infer_param_category(self, symbol: str, unit: str) -> str:
        u_low = unit.lower()
        if "v" in u_low or "mv" in u_low or "uv" in u_low:
            return "VOLTAGE"
        if "a" in u_low or "ma" in u_low or "ua" in u_low or "na" in u_low:
            return "CURRENT"
        if "hz" in u_low or "khz" in u_low or "mhz" in u_low:
            return "FREQUENCY"
        if "s" in u_low or "us" in u_low or "ns" in u_low:
            return "TIMING"
        if "db" in u_low:
            return "GAIN"
        if "ohm" in u_low:
            return "RESISTANCE"
        if "f" in u_low or "pf" in u_low:
            return "CAPACITANCE"
        if "w" in u_low or "mw" in u_low:
            return "POWER"
        return "OTHER"

    def _infer_test_hint(self, symbol: str, unit: str) -> str:
        sym_low = symbol.lower()
        if any(k in sym_low for k in ["gain", "avol", "cmrr", "psrr"]):
            return "AC_GAIN"
        if any(k in sym_low for k in ["gbw", "bandwidth", "f3db", "freq", "fsw"]):
            return "AC_BANDWIDTH"
        if any(k in sym_low for k in ["pm", "phase", "margin"]):
            return "AC_PHASE"
        if any(k in sym_low for k in ["sr", "slew", "trise", "tfall", "delay", "tpd", "deadtime"]):
            return "TRANSIENT_TIMING"
        if any(k in sym_low for k in ["load_reg", "step_response", "transient"]):
            return "TRANSIENT_LOAD"
        if any(k in sym_low for k in ["ocp", "ovp", "uvlo", "short", "isc"]):
            return "PROTECTION"
        if any(k in sym_low for k in ["noise", "enoise"]):
            return "NOISE"
        if any(k in sym_low for k in ["line_reg", "swing", "vom"]):
            return "DC_SWEEP"
        return "DC_STATIC"

    def _infer_related_pins(self, symbol: str, pins: List[PinDefinition]) -> List[str]:
        sym_low = symbol.lower()
        res = []
        for p in pins:
            p_name = p.pin_name
            if p_name.lower() in sym_low:
                res.append(p_name)
        return res

    def _deduplicate_parameters(self, raw_params: List[ElectricalParameter]) -> List[ElectricalParameter]:
        """Algorithm 5: Normalizes symbols, merges redundant extractions."""
        seen: Dict[str, ElectricalParameter] = {}
        for p in raw_params:
            norm_sym = re.sub(r'[^a-zA-Z0-9]', '_', p.symbol).lower()
            if norm_sym in seen:
                existing = seen[norm_sym]
                # Merge values: prefer non-null
                if existing.min_value is None and p.min_value is not None:
                    existing.min_value = p.min_value
                if existing.typ_value is None and p.typ_value is not None:
                    existing.typ_value = p.typ_value
                if existing.max_value is None and p.max_value is not None:
                    existing.max_value = p.max_value
                if not existing.test_conditions and p.test_conditions:
                    existing.test_conditions = p.test_conditions
                if not existing.related_pins and p.related_pins:
                    existing.related_pins = p.related_pins
            else:
                seen[norm_sym] = p
        return list(seen.values())
