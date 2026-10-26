from typing import Dict, List, Any, Optional
from .data_models import (
    CircuitInformation, TestTask, MathEvidence,
    TaskExtractionResult, ElectricalParameter
)

class Stage2TaskDecomposer:
    """
    Stage 2: Test Task Extraction Pipeline (Paper Appendix C, Algorithms 6, 7, 8)
    Transforms static circuit parameters into dynamic executable verification tasks.
    - Semantic Test Type Inference (Algorithm 7)
    - Mathematical Evidence Extraction & SymPy-compatible Constraint Building (Algorithm 8, Eq. 8)
    - Parameter Category Classification (Table 10: SPEC_MEASURABLE, DERIVED_VARIABLE, etc.)
    - Weak supervision validation filtering
    """
    def __init__(self):
        self.task_counter = 0

    def decompose(self, circuit_info: CircuitInformation, doc_id: str = "DOC_001") -> TaskExtractionResult:
        tasks: List[TestTask] = []
        all_params = circuit_info.parameters
        testable_params = [p for p in all_params if p.is_testable]

        pin_names = [p.pin_name for p in circuit_info.pins]

        for p in testable_params:
            test_type = self._infer_test_type(p, circuit_info.circuit_type)
            evidence = self._build_math_evidence(p)
            stim_ports, meas_ports, ctrl_ports = self._assign_ports(p, test_type, circuit_info)
            category = self._classify_parameter_category(p)

            # Determine test domain
            if test_type in ["DC_STATIC", "DC_SWEEP"]:
                domain = "DC"
            elif test_type in ["AC_GAIN", "AC_BANDWIDTH", "AC_PHASE", "NOISE"]:
                domain = "AC"
            elif test_type in ["TRANSIENT_TIMING", "TRANSIENT_LOAD"]:
                domain = "TRANSIENT"
            else:
                domain = "MIXED"

            self.task_counter += 1
            task = TestTask(
                task_id=f"task_{self.task_counter:03d}",
                task_name=f"Verify {p.name} ({p.symbol})",
                test_type=test_type,
                primary_parameter=p.symbol,
                math_evidences=[evidence] if evidence else [],
                stimulus_ports=stim_ports,
                measurement_ports=meas_ports,
                control_ports=ctrl_ports,
                priority="HIGH" if category == "SPEC_MEASURABLE" else "MEDIUM",
                parameter_category=category,
                test_domain=domain,
                reasoning=f"Mapped parameter {p.symbol} to {test_type} analysis on domain {domain}."
            )
            tasks.append(task)

        return TaskExtractionResult(
            document_id=doc_id,
            circuit_name=circuit_info.circuit_name,
            circuit_type=circuit_info.circuit_type,
            tasks=tasks,
            total_parameters=len(all_params),
            testable_parameters=len(testable_params),
            generated_tasks=len(tasks),
            processing_stats={
                "high_priority_tasks": len([t for t in tasks if t.priority == "HIGH"]),
                "domains": {
                    "DC": len([t for t in tasks if t.test_domain == "DC"]),
                    "AC": len([t for t in tasks if t.test_domain == "AC"]),
                    "TRANSIENT": len([t for t in tasks if t.test_domain == "TRANSIENT"]),
                    "MIXED": len([t for t in tasks if t.test_domain == "MIXED"])
                }
            }
        )

    def _infer_test_type(self, p: ElectricalParameter, circuit_type: str) -> str:
        """Algorithm 7: Semantic Test Type Inference based on physical meaning."""
        sym_low = p.symbol.lower()
        sym_norm = sym_low.replace("_", "")
        desc_low = p.description.lower()
        unit_low = p.unit.lower()

        # Protection check
        if any(k in sym_norm or k in desc_low for k in ["protection", "ocp", "ovp", "uvlo", "thermal", "short"]):
            return "PROTECTION"

        # Phase margin check
        if any(k in sym_norm for k in ["pm", "phase", "margin"]):
            return "AC_PHASE"

        # Bandwidth check
        if any(k in sym_norm for k in ["gbw", "bandwidth", "f3db"]) or ("hz" in unit_low and "fsw" not in sym_norm):
            return "AC_BANDWIDTH"

        # AC Gain check
        if "db" in unit_low or any(k in sym_norm for k in ["gain", "avol", "avd", "cmrr", "psrr"]):
            return "AC_GAIN"

        # Timing / Transient check
        if any(u in unit_low for u in ["s", "us", "ns", "ms"]) or any(k in sym_norm for k in ["sr", "slew", "trise", "tfall", "delay", "tpd", "deadtime"]):
            return "TRANSIENT_TIMING"
        if any(k in sym_norm for k in ["loadreg", "transient", "step", "overshoot"]):
            return "TRANSIENT_LOAD"

        # DC sweep vs Static
        if any(k in sym_norm for k in ["linereg", "swing", "vom", "vicr", "transfer"]):
            return "DC_SWEEP"

        return "DC_STATIC"

    def _build_math_evidence(self, p: ElectricalParameter) -> Optional[MathEvidence]:
        """Algorithm 8 & Eq. 8: Mathematical Evidence Extraction."""
        sym = p.symbol
        p_min = p.min_value
        p_max = p.max_value
        p_typ = p.typ_value if isinstance(p.typ_value, (int, float)) else None

        if p_min is not None and p_max is not None:
            expr = f"{sym} >= {p_min} and {sym} <= {p_max}"
            pass_str = f"Must lie within [{p_min}, {p_max}] {p.unit}"
        elif p_min is not None:
            expr = f"{sym} >= {p_min}"
            pass_str = f"Must be at least {p_min} {p.unit}"
        elif p_max is not None:
            expr = f"{sym} <= {p_max}"
            pass_str = f"Must not exceed {p_max} {p.unit}"
        elif p_typ is not None:
            # 10% default tolerance
            tol = abs(p_typ) * 0.10 if p_typ != 0 else 0.05
            expr = f"{sym} >= {p_typ - tol} and {sym} <= {p_typ + tol}"
            pass_str = f"Nominal target {p_typ} {p.unit} (+/- 10%)"
        else:
            return None

        return MathEvidence(
            parameter_symbol=sym,
            constraint_expression=expr,
            pass_criteria=pass_str,
            test_conditions=p.test_conditions,
            provenance="SPEC"
        )

    def _assign_ports(self, p: ElectricalParameter, test_type: str, circuit_info: CircuitInformation):
        """Assigns circuit ports to stimulus, measurement, and control according to circuit type and role."""
        stim_ports = []
        meas_ports = []
        ctrl_ports = []

        pins_by_role = {}
        for pin in circuit_info.pins:
            role = pin.inferred_role
            pins_by_role.setdefault(role, []).append(pin.pin_name)

        # Measurement ports: match related pins or default to output
        if p.related_pins:
            for rp in p.related_pins:
                if rp in [pin.pin_name for pin in circuit_info.pins]:
                    meas_ports.append(rp)

        if not meas_ports:
            # Default measurement port is circuit output
            outputs = pins_by_role.get("OUTPUT", [])
            meas_ports = outputs if outputs else ["OUT"]

        # Stimulus ports: input pins
        signals = pins_by_role.get("SIGNAL", []) + pins_by_role.get("STIMULUS", [])
        if signals:
            stim_ports.extend([sig for sig in signals if sig not in meas_ports])
        else:
            stim_ports.extend(["INP", "INM"] if "INP" in [p.pin_name for p in circuit_info.pins] else ["IN"])

        # Control ports
        ctrl_ports.extend(pins_by_role.get("CONTROL", []))
        ctrl_ports.extend(pins_by_role.get("ENABLE", []))

        return stim_ports, meas_ports, ctrl_ports

    def _classify_parameter_category(self, p: ElectricalParameter) -> str:
        """Table 10: Parameter Category Taxonomy."""
        if p.min_value is not None or p.max_value is not None or (p.typ_value is not None and p.unit):
            return "SPEC_MEASURABLE"
        if "=" in p.description or "calculated" in p.description.lower():
            return "DERIVED_VARIABLE"
        if not p.is_testable:
            return "DESIGN_CONSTRAINT"
        return "META_INFO"
