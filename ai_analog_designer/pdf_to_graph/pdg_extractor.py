import networkx as nx
from typing import Dict, List, Any, Tuple, Optional

class PortDependencyGraphExtractor:
    """
    Extracts the Port Dependency Graph (PDG) from the Hardware Knowledge Graph (HKG)
    strictly adhering to the AnalogVerifier methodology (Section 4.3, Appendix D).
    
    Precedence Rule (Eq. 3):
    POWER < BIAS < CONTROL < SIGNAL < OUTPUT
    
    Edge weights delta are grounded directly in datasheet timing specifications:
    - Turn-on settling time (t_settle)
    - Propagation delay / response time (t_pd)
    - Slew rate (SR) and step transient
    - Closed-loop bandwidth (GBW) and RC time constants
    """
    ROLE_PRECEDENCE = {
        "GROUND": 0,
        "POWER": 0,
        "BIAS": 1,
        "REFERENCE": 1,
        "ENABLE": 2,
        "CONTROL": 2,
        "SIGNAL": 3,
        "INPUT": 3,
        "FEEDBACK": 4,
        "OUTPUT": 4
    }

    DEFAULT_DELTAS = {
        "POWER_RAMP": 10.0e-6,        # Supply ramp & decoupling cap settling
        "BIAS_SETTLING": 20.0e-6,      # Internal bandgap / PTAT core stabilization
        "CONTROL_GATE": 5.0e-6,        # Control / enable logic propagation
        "SIGNAL_PROPAGATION": 2.0e-6,  # Signal input stabilization
        "OUTPUT_REGULATION": 50.0e-6   # Output driver & load cap settling
    }

    def __init__(self, delta_multiplier: float = 1.0):
        self.delta_multiplier = delta_multiplier

    @classmethod
    def extract_timing_specs_from_hkg(cls, hkg: nx.MultiDiGraph) -> Dict[str, Any]:
        """Extracts physical timing, transient, and frequency specs from HKG electrical parameters."""
        raw_extraction = hkg.graph.get("raw_extraction", {})
        params = raw_extraction.get("electrical_parameters", [])

        timing = {
            "turnon_settling_us": None,
            "propagation_delay_us": None,
            "slew_rate_v_us": None,
            "bandwidth_mhz": None,
            "condition_notes": []
        }

        for p in params:
            if not isinstance(p, dict):
                continue
            sym = (p.get("symbol") or "").lower()
            name = (p.get("name") or "").lower()
            unit = (p.get("unit") or "").lower()
            test_cond = (p.get("test_conditions") or "")
            val = p.get("typ") if p.get("typ") is not None else p.get("max")
            if val is None:
                val = p.get("min")

            if val is not None:
                try:
                    fval = float(val)
                except (ValueError, TypeError):
                    continue

                # Turnon settling time
                if any(k in name or k in sym for k in ["turnon", "turn-on", "settling", "startup"]):
                    if "ms" in unit:
                        timing["turnon_settling_us"] = fval * 1000.0
                    elif "ns" in unit:
                        timing["turnon_settling_us"] = fval / 1000.0
                    elif "s" in unit and "us" not in unit and "ms" not in unit:
                        timing["turnon_settling_us"] = fval * 1e6
                    else:
                        timing["turnon_settling_us"] = fval
                    if test_cond:
                        timing["condition_notes"].append(f"Turnon Settling: {fval}{unit} ({test_cond})")

                # Propagation delay / Response time
                elif any(k in name or k in sym for k in ["propagation", "tpd", "response", "delay", "lock"]):
                    if "ns" in unit:
                        timing["propagation_delay_us"] = fval / 1000.0
                    elif "ms" in unit:
                        timing["propagation_delay_us"] = fval * 1000.0
                    else:
                        timing["propagation_delay_us"] = fval
                    if test_cond:
                        timing["condition_notes"].append(f"Propagation Delay: {fval}{unit} ({test_cond})")

                # Slew rate
                elif any(k in name or k in sym for k in ["slew", "sr"]):
                    timing["slew_rate_v_us"] = fval

                # Bandwidth
                elif any(k in name or k in sym for k in ["bandwidth", "gbw"]):
                    if "khz" in unit:
                        timing["bandwidth_mhz"] = fval / 1000.0
                    elif "ghz" in unit:
                        timing["bandwidth_mhz"] = fval * 1000.0
                    else:
                        timing["bandwidth_mhz"] = fval

        return timing

    def extract_pdg(self, hkg: nx.MultiDiGraph) -> Tuple[nx.DiGraph, Dict[str, float], List[List[str]]]:
        """
        Constructs PDG DAG G = (P, E, delta) from pins and dependencies in HKG.
        Synthesizes time-optimal stimulus schedule sigma(p) via Algorithm 1.
        """
        timing_specs = self.extract_timing_specs_from_hkg(hkg)
        raw_extraction = hkg.graph.get("raw_extraction", {})

        # Collect PIN nodes
        pins = []
        for n, d in hkg.nodes(data=True):
            if d.get("node_type") == "PIN":
                pin_data = d.get("data", {})
                role = str(pin_data.get("role") or "SIGNAL").upper()
                pins.append((n, role, pin_data))

        # Fallback if node_type wasn't tagged
        if not pins:
            for p in raw_extraction.get("pins", []):
                pname = p.get("name", "PIN")
                role = str(p.get("role") or "SIGNAL").upper()
                pins.append((pname, role, p))

        pdg = nx.DiGraph()
        for pname, role, pdata in pins:
            pdg.add_node(pname, role=role, data=pdata)

        pin_names = [p[0] for p in pins]

        # 1. First, check if explicit port_dependencies were extracted from datasheet
        explicit_deps = raw_extraction.get("port_dependencies", [])
        injected_edges = set()

        for dep in explicit_deps:
            if isinstance(dep, dict):
                src = dep.get("source")
                tgt = dep.get("target")
                if src in pdg and tgt in pdg and src != tgt:
                    raw_delay = dep.get("settling_delay_us")
                    try:
                        delay_us = float(raw_delay) if raw_delay is not None else 10.0
                    except (ValueError, TypeError):
                        delay_us = 10.0

                    # If datasheet specified explicit turn-on settling time, respect it
                    if timing_specs.get("turnon_settling_us") and "output" in pdg.nodes[tgt].get("role", "").lower():
                        delay_us = max(delay_us, float(timing_specs["turnon_settling_us"]))

                    pdg.add_edge(
                        src, tgt,
                        weight=delay_us * 1e-6 * self.delta_multiplier,
                        delay_us=delay_us,
                        dependency_type=dep.get("dependency_type", "PHYSICAL_DEPENDENCY"),
                        condition=dep.get("condition") or "Datasheet Direct Specification"
                    )
                    injected_edges.add((src, tgt))

        # 2. Inject rule-based causal dependencies where explicit links were absent (Eq. 3)
        # Supplies stabilize before bias, bias before control, control before signal, signal before output
        power_pins = [p for p in pins if self.ROLE_PRECEDENCE.get(p[1], 3) == 0]
        bias_pins = [p for p in pins if self.ROLE_PRECEDENCE.get(p[1], 3) == 1]
        control_pins = [p for p in pins if self.ROLE_PRECEDENCE.get(p[1], 3) == 2]
        signal_pins = [p for p in pins if self.ROLE_PRECEDENCE.get(p[1], 3) == 3]
        output_pins = [p for p in pins if self.ROLE_PRECEDENCE.get(p[1], 3) == 4]

        # Determine physical output settling delay
        output_delay_us = 50.0  # sensible default for analog regulation
        output_condition = "Regulation & Closed-Loop Settling"

        if timing_specs["turnon_settling_us"]:
            output_delay_us = timing_specs["turnon_settling_us"]
            output_condition = f"Turn-on Settling to Spec ({output_delay_us:.1f}us)"
        elif timing_specs["propagation_delay_us"]:
            output_delay_us = timing_specs["propagation_delay_us"]
            output_condition = f"Propagation Delay ({output_delay_us:.2f}us)"
        elif timing_specs["slew_rate_v_us"]:
            output_delay_us = (2.0 / timing_specs["slew_rate_v_us"]) + 1.0
            output_condition = f"Slew-Rate Settling ({timing_specs['slew_rate_v_us']}V/us)"
        elif timing_specs["bandwidth_mhz"]:
            output_delay_us = max(1.0, 5.0 / (2.0 * 3.14159 * timing_specs["bandwidth_mhz"]))
            output_condition = f"GBW Loop Settling ({timing_specs['bandwidth_mhz']}MHz)"

        # Causal cascade: Power -> Bias
        for pw, _, _ in power_pins:
            for b, _, _ in bias_pins:
                if (pw, b) not in injected_edges:
                    pdg.add_edge(pw, b, weight=self.DEFAULT_DELTAS["BIAS_SETTLING"] * self.delta_multiplier,
                                 delay_us=self.DEFAULT_DELTAS["BIAS_SETTLING"] * 1e6,
                                 dependency_type="POWER_TO_BIAS",
                                 condition="Core Bias Current Stabilization")
                    injected_edges.add((pw, b))

        # Power/Bias -> Control
        pre_control = bias_pins if bias_pins else power_pins
        for src, _, _ in pre_control:
            for c, _, _ in control_pins:
                if (src, c) not in injected_edges:
                    pdg.add_edge(src, c, weight=self.DEFAULT_DELTAS["CONTROL_GATE"] * self.delta_multiplier,
                                 delay_us=self.DEFAULT_DELTAS["CONTROL_GATE"] * 1e6,
                                 dependency_type="BIAS_TO_CONTROL",
                                 condition="Control Logic & Enable Threshold Settling")
                    injected_edges.add((src, c))

        # Control/Bias/Power -> Signal
        pre_signal = control_pins if control_pins else (bias_pins if bias_pins else power_pins)
        for src, _, _ in pre_signal:
            for s, _, _ in signal_pins:
                if (src, s) not in injected_edges:
                    pdg.add_edge(src, s, weight=self.DEFAULT_DELTAS["SIGNAL_PROPAGATION"] * self.delta_multiplier,
                                 delay_us=self.DEFAULT_DELTAS["SIGNAL_PROPAGATION"] * 1e6,
                                 dependency_type="GATE_TO_SIGNAL",
                                 condition="Operating Point & Common-Mode Stabilization")
                    injected_edges.add((src, s))

        # Signal/Power -> Output
        driving_sources = signal_pins if signal_pins else power_pins
        for src, _, _ in driving_sources:
            for out, _, _ in output_pins:
                if (src, out) not in injected_edges:
                    pdg.add_edge(src, out, weight=output_delay_us * 1e-6 * self.delta_multiplier,
                                 delay_us=output_delay_us,
                                 dependency_type="SIGNAL_TO_OUTPUT",
                                 condition=output_condition)
                    injected_edges.add((src, out))

        # Ground reference dependencies to all outputs
        ground_pins = [p for p in pins if "ground" in p[1].lower() or p[0] in ["GND", "VEE", "VSS"]]
        for g, _, _ in ground_pins:
            for out, _, _ in output_pins:
                if (g, out) not in injected_edges:
                    pdg.add_edge(g, out, weight=output_delay_us * 1e-6 * self.delta_multiplier,
                                 delay_us=output_delay_us,
                                 dependency_type="GROUND_REFERENCE",
                                 condition="Substrate Reference & Output Voltage Return")
                    injected_edges.add((g, out))

        # Sanity check: cycle detection (Algorithm 1 Line 2)
        if not nx.is_directed_acyclic_graph(pdg):
            cycles = list(nx.simple_cycles(pdg))
            for c in cycles:
                if len(c) >= 2 and pdg.has_edge(c[-1], c[0]):
                    pdg.remove_edge(c[-1], c[0])

        # Topological sorting (Algorithm 1 Line 6)
        topo_order = list(nx.topological_sort(pdg))

        # Critical path schedule synthesis sigma(p) (Algorithm 1 Lines 7-13)
        sigma: Dict[str, float] = {}
        for p in topo_order:
            in_edges = list(pdg.in_edges(p, data=True))
            if not in_edges:
                sigma[p] = 0.0
            else:
                sigma[p] = max(sigma[q] + data.get("weight", 1e-6) for q, _, data in in_edges)

        # Dependency level assignment
        level_map: Dict[str, int] = {}
        sources = [n for n in pin_names if pdg.in_degree(n) == 0]
        for s in sources:
            level_map[s] = 0

        for v in topo_order:
            if v not in level_map:
                preds = list(pdg.predecessors(v))
                level_map[v] = max(level_map[u] for u in preds) + 1 if preds else 0

        max_lvl = max(level_map.values()) if level_map else 0
        levels = [[] for _ in range(max_lvl + 1)]
        for node, lvl in level_map.items():
            levels[lvl].append(node)

        return pdg, sigma, levels

