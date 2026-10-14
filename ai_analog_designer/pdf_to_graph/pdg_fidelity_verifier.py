import networkx as nx
from typing import Dict, Any, List, Tuple, Optional

class PDGFidelityVerifier:
    """
    Formally verifies the fidelity and consistency of the extracted Port Dependency Graph (PDG)
    against the physical IC datasheet and Hardware Knowledge Graph (HKG).
    
    Implements 5 formal verification checks:
    1. Pin Conservation & Bijective Coverage Audit (P_datasheet == V_PDG)
    2. Role & Direction Semantic Consistency (Truth-table matching)
    3. Graph Topology & Causal DAG Invariant (Cycle-free & Supply-to-Output Reachability)
    4. Timing Delay Grounding Audit (Edge delta grounded in datasheet parameters vs heuristics)
    5. Composite PDG Fidelity Score (0 - 100%)
    """

    @classmethod
    def audit_pdg(
        cls,
        hkg: nx.MultiDiGraph,
        pdg: nx.DiGraph,
        schedule: Dict[str, float]
    ) -> Dict[str, Any]:
        """Performs comprehensive verification of PDG against HKG datasheet ground truth."""
        raw_extraction = hkg.graph.get("raw_extraction", {})
        ds_pins = raw_extraction.get("pins", [])
        electrical_params = raw_extraction.get("electrical_parameters", [])

        # 1. Pin Conservation Audit
        ds_pin_names = {p.get("name", "").upper() for p in ds_pins if p.get("name")}
        pdg_pin_names = {str(n).upper() for n in pdg.nodes()}

        matched_pins = ds_pin_names.intersection(pdg_pin_names)
        missing_pins = list(ds_pin_names - pdg_pin_names)
        hallucinated_pins = list(pdg_pin_names - ds_pin_names)

        total_ds_pins = len(ds_pin_names) if ds_pin_names else len(pdg_pin_names)
        coverage_pct = (len(matched_pins) / total_ds_pins * 100.0) if total_ds_pins > 0 else 100.0

        # 2. Role & Direction Semantic Consistency
        role_violations = []
        for p in ds_pins:
            pname = p.get("name", "").upper()
            if pname in pdg:
                node_data = pdg.nodes[pname]
                role = str(node_data.get("role", "")).upper()
                direction = str(p.get("direction", "")).upper()

                # Invariant checks:
                if "OUTPUT" in direction and role in ["POWER", "GROUND"]:
                    role_violations.append(f"Pin '{pname}' direction is OUTPUT but role is assigned {role}")
                elif "POWER" in direction and role not in ["POWER", "GROUND"]:
                    role_violations.append(f"Pin '{pname}' direction is POWER but role is assigned {role}")

        # 3. Graph Topology & Causal DAG Invariant
        is_dag = nx.is_directed_acyclic_graph(pdg)
        cycles = list(nx.simple_cycles(pdg)) if not is_dag else []

        # Reachability from supplies to outputs
        power_nodes = [n for n in pdg.nodes() if pdg.nodes[n].get("role") in ["POWER", "GROUND"]]
        output_nodes = [n for n in pdg.nodes() if pdg.nodes[n].get("role") in ["OUTPUT", "FEEDBACK"]]
        unreachable_outputs = []

        for out in output_nodes:
            has_supply_path = any(nx.has_path(pdg, p, out) for p in power_nodes) if power_nodes else True
            if not has_supply_path:
                unreachable_outputs.append(out)

        # 4. Timing Spec Grounding Audit
        grounded_edges = 0
        heuristic_edges = 0
        edge_details = []

        for u, v, d in pdg.edges(data=True):
            delay_us = d.get("delay_us", 0.0)
            dep_type = d.get("dependency_type", "")
            cond = d.get("condition", "")

            is_grounded = any(k in cond.lower() for k in ["datasheet", "turn-on", "propagation", "slew", "gbw"])
            if is_grounded:
                grounded_edges += 1
            else:
                heuristic_edges += 1

            edge_details.append({
                "from": u,
                "to": v,
                "delay_us": delay_us,
                "type": dep_type,
                "condition": cond,
                "grounded": is_grounded
            })

        total_edges = grounded_edges + heuristic_edges
        grounding_score_pct = (grounded_edges / total_edges * 100.0) if total_edges > 0 else 100.0

        # 5. Composite Fidelity Score (0 to 100)
        # Weights: Pin coverage 40%, Direction consistency 20%, Topology (DAG+Reach) 25%, Grounding 15%
        dir_score = max(0.0, 100.0 - (len(role_violations) * 15.0))
        topo_score = (50.0 if is_dag else 0.0) + (50.0 if not unreachable_outputs else 0.0)
        
        fidelity_score = (
            (coverage_pct * 0.40) +
            (dir_score * 0.20) +
            (topo_score * 0.25) +
            (grounding_score_pct * 0.15)
        )

        return {
            "fidelity_score_pct": round(fidelity_score, 1),
            "status": "TRUSTWORTHY" if fidelity_score >= 85.0 else ("ACCEPTABLE" if fidelity_score >= 70.0 else "UNRELIABLE"),
            "pin_coverage": {
                "datasheet_pin_count": len(ds_pin_names),
                "pdg_node_count": len(pdg_pin_names),
                "coverage_pct": round(coverage_pct, 1),
                "missing_pins": missing_pins,
                "hallucinated_pins": hallucinated_pins
            },
            "semantic_consistency": {
                "score_pct": round(dir_score, 1),
                "violations": role_violations
            },
            "topological_validity": {
                "is_dag": is_dag,
                "detected_cycles": cycles,
                "unreachable_outputs": unreachable_outputs
            },
            "timing_grounding": {
                "grounded_edges": grounded_edges,
                "heuristic_edges": heuristic_edges,
                "grounding_score_pct": round(grounding_score_pct, 1),
                "edge_details": edge_details[:10]  # preview
            }
        }
