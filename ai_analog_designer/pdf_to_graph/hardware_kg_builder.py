import os
import re
import json
import networkx as nx
from typing import Dict, Any, List, Optional
from ..openrouter_client import OpenRouterClient

class HardwareKnowledgeGraphBuilder:
    """
    Constructs a Hierarchical Hardware Knowledge Graph (HKG) from parsed PDF sections.
    Implements research from:
    - Faithful Construction of Hierarchical Hardware Knowledge Graphs (arXiv:2410.xxxxx)
    - TRACE-KG: Text-Driven Schema for Context-Enriched Knowledge Graphs (arXiv:2406.xxxxx)
    """
    def __init__(self, llm_client: Optional[OpenRouterClient] = None):
        self.llm = llm_client or OpenRouterClient()

    def build_kg_from_parsed_pdf(self, parsed_data: Dict[str, Any]) -> nx.MultiDiGraph:
        """Extracts structured entities & relations from parsed PDF content and builds NetworkX graph."""
        sections = parsed_data["sections"]
        overview_text = sections.get("overview", "")[:4000]
        pin_text = sections.get("pin_configuration", "")[:6000]
        spec_text = sections.get("electrical_specifications", "")[:12000]

        prompt = f"""You are a Principal Analog IC Hardware Architect and Knowledge Graph Engineer.
Analyze the following IC datasheet sections to extract a complete Hardware Knowledge Graph (HKG).

## DATASHEET CONTENT:

[OVERVIEW]
{overview_text}

[PIN CONFIGURATION]
{pin_text}

[ELECTRICAL SPECIFICATIONS]
{spec_text}

## EXTRACTION INSTRUCTIONS:
Extract the following entities and relations into JSON:
1. "ic_device": {{"name": str, "category": str, "description": str}}
2. "pins": List of objects:
   {{"name": str, "pin_number": str, "direction": "INPUT"|"OUTPUT"|"POWER"|"GROUND"|"BIDIRECTIONAL", "role": "POWER"|"GROUND"|"BIAS"|"CONTROL"|"ENABLE"|"FEEDBACK"|"SIGNAL"|"OUTPUT", "description": str, "voltage_range": {{"min": float, "max": float, "typ": float}}}}
3. "electrical_parameters": List of ALL testable specifications (DO NOT OMIT ANY!):
   {{"symbol": str, "name": str, "min": float or null, "typ": float or null, "max": float or null, "unit": str, "test_conditions": str, "related_pins": [str], "category": str}}
4. "functional_blocks": List of internal architectural blocks (e.g., "Input_Stage", "Bias_Core", "Output_Driver", "Feedback_Divider", "VCO_Core", etc.)

OUTPUT JSON FORMAT:
{{
  "ic_device": {{ "name": "...", "category": "...", "description": "..." }},
  "pins": [ {{ "name": "...", "pin_number": "...", "direction": "...", "role": "...", "description": "..." }} ],
  "electrical_parameters": [ {{ "symbol": "...", "name": "...", "min": null, "typ": null, "max": null, "unit": "...", "test_conditions": "...", "related_pins": [...] }} ],
  "functional_blocks": [ "..." ]
}}
"""
        system_prompt = "You are a specialized analog semiconductor AI. Extract comprehensive, 100% faithful hardware entities and relations from datasheet text."
        
        extracted_json = self.llm.complete_json(prompt, system_prompt=system_prompt)
        return self._json_to_networkx_graph(extracted_json)

    def _json_to_networkx_graph(self, data: Dict[str, Any]) -> nx.MultiDiGraph:
        G = nx.MultiDiGraph()
        ic_info = data.get("ic_device", {"name": "DUT_IC", "category": "ANALOG"})
        ic_name = ic_info.get("name", "DUT_IC")
        G.add_node(ic_name, node_type="IC_DEVICE", data=ic_info)

        # Add Pins
        for idx, pin in enumerate(data.get("pins", [])):
            raw_pname = pin.get("name") or f"PIN_{idx}"
            pname = re.sub(r'[^a-zA-Z0-9_]', '_', raw_pname.strip())
            pin["name"] = pname
            G.add_node(pname, node_type="PIN", data=pin)
            G.add_edge(ic_name, pname, relation="HAS_PIN")

        # Add Functional Blocks
        for idx, blk in enumerate(data.get("functional_blocks", [])):
            bname = blk if isinstance(blk, str) else (blk.get("name") or f"BLOCK_{idx}")
            G.add_node(bname, node_type="FUNCTIONAL_BLOCK", data=blk if isinstance(blk, dict) else {})
            G.add_edge(ic_name, bname, relation="HAS_BLOCK")

        # Add Electrical Parameters
        for idx, param in enumerate(data.get("electrical_parameters", [])):
            sym = param.get("symbol") or param.get("name") or f"PARAM_{idx}"
            param["symbol"] = sym
            G.add_node(sym, node_type="SPEC_PARAM", data=param)
            G.add_edge(ic_name, sym, relation="SPECIFIES")
            for rp in param.get("related_pins", []):
                if rp in G:
                    G.add_edge(rp, sym, relation="GOVERNS")

        # Add Explicit Relations
        for rel in data.get("relations", []):
            src = rel.get("source")
            tgt = rel.get("target")
            rtype = rel.get("relation", "DEPENDS_ON")
            if src in G and tgt in G:
                G.add_edge(src, tgt, relation=rtype, **rel.get("properties", {}))

        # Store raw extraction metadata
        G.graph["raw_extraction"] = data
        return G
