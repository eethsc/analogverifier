import networkx as nx
from typing import Dict, List, Any, Tuple, Optional
from .data_models import (
    CircuitInformation, TaskExtractionResult,
    PortAnalysis, SignalPath, DependencyGraph, Stage3Result
)

class Stage3GraphScheduler:
    """
    Stage 3: Graph-Symbolic Scheduling & Port Analysis (Paper Section 4.3, Appendix D, Algorithm 1 & 11)
    Resolves latent physical dependencies into a correct-by-construction stimulus schedule:
    1. Semantic Port Classification: Assigns rho(p) in {POWER, BIAS, CONTROL, SIGNAL, GROUND, FEEDBACK, etc.}
    2. Physical Precedence Rule Injection (Eq. 3):
       POWER < BIAS < CONTROL < SIGNAL
    3. Weighted Port Dependency Graph (PDG) G = (P, E, delta)
    4. Critical Path Schedule Synthesis (Algorithm 1):
       Cycle detection -> Topological sort -> sigma(p) = max_{(q,p) in E} (sigma(q) + delta_qp)
    5. Dependency Level Assignment (Algorithm 11)
    """
    # Canonical physical hierarchy weights / partial ordering
    ROLE_PRECEDENCE = {
        "GROUND": 0,
        "POWER": 0,
        "BIAS": 1,
        "ENABLE": 2,
        "CONTROL": 2,
        "SIGNAL": 3,
        "STIMULUS": 3,
        "FEEDBACK": 4,
        "OUTPUT": 4
    }

    # Default physical settling times (in seconds)
    DEFAULT_DELTAS = {
        "POWER_TO_BIAS": 10.0e-6,     # Supplies stabilize: 10us
        "BIAS_TO_CONTROL": 5.0e-6,    # Bias networks settle: 5us
        "CONTROL_TO_SIGNAL": 2.0e-6,  # Control enables settle: 2us
        "SIGNAL_TO_MEASURE": 1.0e-6   # Signal input to measurement: 1us
    }

    def __init__(self, delta_multiplier: float = 1.0):
        self.delta_multiplier = delta_multiplier

    def analyze_and_schedule(
        self,
        circuit_info: CircuitInformation,
        task_result: Optional[TaskExtractionResult] = None
    ) -> Stage3Result:
        ports = circuit_info.pins
        port_analyses: List[PortAnalysis] = []

        # Step 1: Semantic Port Classification & Testability (Eq. 10)
        for p in ports:
            role = p.inferred_role.upper()
            sig_type = "POWER" if role in ["POWER", "GROUND"] else ("DIGITAL" if role in ["ENABLE", "CONTROL"] else "ANALOG")
            
            # Testability score: tau_p = 0.4*acc + 0.3*obs + 0.3*ctrl (Eq. 10)
            acc = 1.0 if p.direction in ["INPUT", "BIDIRECTIONAL"] else 0.8
            obs = 1.0 if p.direction in ["OUTPUT", "BIDIRECTIONAL"] else 0.8
            ctrl = 0.95 if role in ["POWER", "CONTROL", "ENABLE"] else 0.85
            testability = 0.4 * acc + 0.3 * obs + 0.3 * ctrl

            pa = PortAnalysis(
                port_name=p.pin_name,
                pin_names=[p.pin_name],
                inferred_role=role,
                role_confidence=0.98,
                role_reasoning=f"Classified as {role} based on terminal definition '{p.description}'",
                signal_type=sig_type,
                signal_domain="voltage",
                typical_range=p.electrical_constraints.get("operating_voltage_range", {}),
                testability=round(testability, 3),
                recommended_stimulus="voltage_source" if p.direction == "INPUT" else "",
                recommended_measurement="voltage_probe" if p.direction == "OUTPUT" else ""
            )
            port_analyses.append(pa)

        # Step 2: Signal Path Inference (Table 15, 19)
        signal_paths = self._infer_signal_paths(circuit_info, port_analyses)

        # Step 3: PDG Construction & Schedule Synthesis (Algorithm 1)
        dep_graph, schedule, levels = self._build_pdg_and_synthesize_schedule(port_analyses, circuit_info.circuit_type)

        # Step 4: Topologies
        topologies = self._recommend_topologies(circuit_info.circuit_type, port_analyses)

        port_map = {pa.port_name: pa.pin_names for pa in port_analyses}

        return Stage3Result(
            circuit_name=circuit_info.circuit_name,
            circuit_type=circuit_info.circuit_type,
            port_analyses=port_analyses,
            signal_paths=signal_paths,
            dependency_graph=dep_graph,
            schedule=schedule,
            port_mapping=port_map,
            test_topologies=topologies
        )

    def _infer_signal_paths(self, circuit_info: CircuitInformation, port_analyses: List[PortAnalysis]) -> List[SignalPath]:
        paths = []
        ctype = circuit_info.circuit_type.upper()
        pnames = [pa.port_name for pa in port_analyses]

        if "BUCK" in ctype or "DCDC" in ctype:
            # Main conversion path: IN -> SW -> FB
            if "IN" in pnames and "SW" in pnames and "FB" in pnames:
                paths.append(SignalPath("p1", "MAIN_SIGNAL", "IN", ["SW"], "FB", is_critical=True, latency_critical=True, path_reasoning="Power conversion path"))
            if "FB" in pnames and "EN" in pnames:
                paths.append(SignalPath("p2", "FEEDBACK", "FB", [], "EN", is_critical=True, latency_critical=True, path_reasoning="Regulation loop"))
            if "EN" in pnames and "SW" in pnames:
                paths.append(SignalPath("p3", "CONTROL", "EN", [], "SW", is_critical=True, path_reasoning="Switch enable gate"))
            if "IN" in pnames and "BST" in pnames:
                paths.append(SignalPath("p4", "BIAS", "IN", [], "BST", is_critical=True, path_reasoning="Bootstrap bias charge"))
        else:
            # OpAmp / OTA: INP/INM -> OUT
            inp = "INP" if "INP" in pnames else ("1" if "1" in pnames else "IN")
            out = "OUT" if "OUT" in pnames else ("5" if "5" in pnames else "OUT")
            paths.append(SignalPath("p1", "MAIN_SIGNAL", inp, [], out, is_critical=True, latency_critical=True, path_reasoning="Primary differential amplification path"))

        return paths

    def _build_pdg_and_synthesize_schedule(
        self,
        port_analyses: List[PortAnalysis],
        circuit_type: str
    ) -> Tuple[DependencyGraph, Dict[str, float], List[List[str]]]:
        """
        Algorithm 1: Graph-Symbolic Schedule Synthesis.
        Constructs DAG enforcing POWER < BIAS < CONTROL < SIGNAL.
        Computes sigma(p) via longest-path critical path analysis.
        """
        G = nx.DiGraph()
        nodes = [pa.port_name for pa in port_analyses]
        for n in nodes:
            G.add_node(n)

        # Precedence mapping
        role_map = {pa.port_name: pa.inferred_role for pa in port_analyses}

        # Inject physical dependency edges
        edges_data = []
        for u in nodes:
            for v in nodes:
                if u == v:
                    continue
                rank_u = self.ROLE_PRECEDENCE.get(role_map[u], 3)
                rank_v = self.ROLE_PRECEDENCE.get(role_map[v], 3)

                if rank_u < rank_v:
                    # Select appropriate delta settling time
                    if rank_u == 0 and rank_v == 1:
                        delta = self.DEFAULT_DELTAS["POWER_TO_BIAS"]
                    elif rank_u <= 1 and rank_v == 2:
                        delta = self.DEFAULT_DELTAS["BIAS_TO_CONTROL"]
                    elif rank_u <= 2 and rank_v == 3:
                        delta = self.DEFAULT_DELTAS["CONTROL_TO_SIGNAL"]
                    else:
                        delta = self.DEFAULT_DELTAS["SIGNAL_TO_MEASURE"]

                    delta *= self.delta_multiplier
                    G.add_edge(u, v, weight=delta)
                    edges_data.append({"from": u, "to": v, "delay": delta})

        # Algorithm 1 Line 2: Cycle Detection Sanity Check
        if not nx.is_directed_acyclic_graph(G):
            # Infeasible cycle detected
            cycles = list(nx.simple_cycles(G))
            raise ValueError(f"Port Dependency Graph contains infeasible physical cycles: {cycles}")

        # Algorithm 1 Line 6: Topological Sort
        topo_order = list(nx.topological_sort(G))

        # Algorithm 1 Line 7-13: Critical Path Analysis (Longest-path start time sigma)
        sigma: Dict[str, float] = {}
        for p in topo_order:
            in_edges = list(G.in_edges(p, data=True))
            if not in_edges:
                sigma[p] = 0.0
            else:
                sigma[p] = max(sigma[q] + data.get("weight", 1e-6) for q, _, data in in_edges)

        # Algorithm 11: Dependency Level Assignment (BFS / Distance from sources)
        level_map: Dict[str, int] = {}
        sources = [n for n in nodes if G.in_degree(n) == 0]
        for s in sources:
            level_map[s] = 0

        for v in topo_order:
            if v not in level_map:
                in_nodes = list(G.predecessors(v))
                level_map[v] = max(level_map[u] for u in in_nodes) + 1

        max_level = max(level_map.values()) if level_map else 0
        levels = [[] for _ in range(max_level + 1)]
        for node, lvl in level_map.items():
            levels[lvl].append(node)

        dep_graph = DependencyGraph(
            nodes=nodes,
            edges=edges_data,
            levels=levels,
            schedule=sigma
        )

        return dep_graph, sigma, levels

    def _recommend_topologies(self, circuit_type: str, port_analyses: List[PortAnalysis]) -> List[Dict[str, Any]]:
        topologies = []
        ctype = circuit_type.upper()
        if "OTA" in ctype or "OPAMP" in ctype:
            topologies.append({"name": "Open-Loop AC Frequency Sweep", "feedback": "LC-servo open-loop", "analysis": "AC"})
            topologies.append({"name": "Unity-Gain Transient Follower", "feedback": "Negative unity-gain loop", "analysis": "TRAN"})
            topologies.append({"name": "Common-Mode Error Bridge", "feedback": "Resistive balance", "analysis": "AC/DC"})
        elif "BUCK" in ctype or "DCDC" in ctype:
            topologies.append({"name": "Closed-Loop DC Regulation", "feedback": "Resistor divider into FB", "analysis": "DC/TRAN"})
            topologies.append({"name": "Switching Waveform & Transient Step", "load": "Current step generator", "analysis": "TRAN"})
            topologies.append({"name": "Protection & Enable Shutdown", "control": "EN threshold sweep", "analysis": "DC"})
        elif "HSC" in ctype or "COMPARATOR" in ctype:
            topologies.append({"name": "Dynamic Overdrive Transient", "stimulus": "Fast edge differential pulse", "analysis": "TRAN"})
            topologies.append({"name": "DC Transfer & Offset", "stimulus": "Slow differential ramp", "analysis": "DC"})
        return topologies
