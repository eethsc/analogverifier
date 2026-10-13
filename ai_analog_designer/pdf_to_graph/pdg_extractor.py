import networkx as nx
from typing import Dict, List, Any, Tuple

class PortDependencyGraphExtractor:
    """
    Extracts the Port Dependency Graph (PDG) from the Hardware Knowledge Graph (HKG)
    strictly adhering to the AnalogVerifier methodology (Section 4.3, Appendix D).
    
    Precedence Rule (Eq. 3):
    POWER < BIAS < CONTROL < SIGNAL
    """
    ROLE_PRECEDENCE = {
        "GROUND": 0,
        "POWER": 0,
        "BIAS": 1,
        "ENABLE": 2,
        "CONTROL": 2,
        "SIGNAL": 3,
        "INPUT": 3,
        "FEEDBACK": 4,
        "OUTPUT": 4
    }

    DEFAULT_DELTAS = {
        "POWER_TO_BIAS": 10.0e-6,
        "BIAS_TO_CONTROL": 5.0e-6,
        "CONTROL_TO_SIGNAL": 2.0e-6,
        "SIGNAL_TO_MEASURE": 1.0e-6
    }

    def __init__(self, delta_multiplier: float = 1.0):
        self.delta_multiplier = delta_multiplier

    def extract_pdg(self, hkg: nx.MultiDiGraph) -> Tuple[nx.DiGraph, Dict[str, float], List[List[str]]]:
        """
        Constructs PDG DAG G = (P, E, delta) from pins in HKG.
        Synthesizes time-optimal stimulus schedule sigma(p) via Algorithm 1.
        """
        # Find all pin nodes in HKG
        pins = []
        for n, d in hkg.nodes(data=True):
            if d.get("node_type") == "PIN":
                pin_data = d.get("data", {})
                role = pin_data.get("role", "SIGNAL").upper()
                pins.append((n, role, pin_data))

        # Fallback if node_type wasn't tagged
        if not pins:
            raw_data = hkg.graph.get("raw_extraction", {})
            for p in raw_data.get("pins", []):
                pname = p["name"]
                role = p.get("role", "SIGNAL").upper()
                pins.append((pname, role, p))

        pdg = nx.DiGraph()
        for pname, role, pdata in pins:
            pdg.add_node(pname, role=role, data=pdata)

        # Inject physical dependency edges
        pin_names = [p[0] for p in pins]
        for u, role_u, _ in pins:
            for v, role_v, _ in pins:
                if u == v:
                    continue
                rank_u = self.ROLE_PRECEDENCE.get(role_u, 3)
                rank_v = self.ROLE_PRECEDENCE.get(role_v, 3)

                if rank_u < rank_v:
                    if rank_u == 0 and rank_v == 1:
                        delta = self.DEFAULT_DELTAS["POWER_TO_BIAS"]
                    elif rank_u <= 1 and rank_v == 2:
                        delta = self.DEFAULT_DELTAS["BIAS_TO_CONTROL"]
                    elif rank_u <= 2 and rank_v == 3:
                        delta = self.DEFAULT_DELTAS["CONTROL_TO_SIGNAL"]
                    else:
                        delta = self.DEFAULT_DELTAS["SIGNAL_TO_MEASURE"]

                    delta *= self.delta_multiplier
                    pdg.add_edge(u, v, weight=delta)

        # Sanity check: cycle detection (Algorithm 1 Line 2)
        if not nx.is_directed_acyclic_graph(pdg):
            # Break cycles if any feedback loop exists by removing back-edges
            cycles = list(nx.simple_cycles(pdg))
            for c in cycles:
                # Remove feedback to maintain strict DAG for forward scheduling
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

        # Dependency level assignment (Algorithm 11)
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
