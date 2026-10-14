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
        spec_text = sections.get("electrical_specifications", "")[:14000]
        functional_text = sections.get("functional_description", "")[:12000]

        prompt = f"""You are a Principal Analog IC Hardware Architect and Knowledge Graph Engineer.
Analyze the following IC datasheet sections to extract a complete Hardware Knowledge Graph (HKG) and Port/Block Dependencies.

## DATASHEET CONTENT:

[OVERVIEW]
{overview_text}

[PIN CONFIGURATION]
{pin_text}

[ELECTRICAL SPECIFICATIONS]
{spec_text}

[FUNCTIONAL DESCRIPTION & ARCHITECTURE]
{functional_text}

## EXTRACTION INSTRUCTIONS:
Extract the following entities and relations into JSON:
1. "ic_device": {{"name": str, "category": str, "description": str}}
2. "pins": List of objects:
   {{
     "name": str (e.g. IN, OUT, GND, IN1_P, IN1_N, VCC, VEE),
     "pin_number": str,
     "direction": "INPUT"|"OUTPUT"|"POWER"|"GROUND"|"BIDIRECTIONAL",
     "role": "POWER"|"GROUND"|"BIAS"|"CONTROL"|"ENABLE"|"FEEDBACK"|"SIGNAL"|"OUTPUT",
     "description": str,
     "nominal_voltage": float or null,
     "voltage_range": {{"min": float or null, "max": float or null, "typ": float or null}},
     "settling_time_us": float or null (extract from turn-on, step response, or startup specs if applicable),
     "load_capacitance_uf": float or null
   }}
3. "functional_blocks": List of internal architectural sub-blocks identified in the functional description/block diagram:
   {{
     "name": str (e.g. "Bandgap_Core", "PTAT_Bias_Generator", "Error_Amplifier", "Output_Buffer", "Differential_Input_Pair"),
     "type": str,
     "description": str,
     "input_ports": [str],
     "output_ports": [str],
     "settling_time_us": float or null
   }}
4. "port_dependencies": List of causal physical flows between pins (and through internal stages):
   {{
     "source": str,
     "target": str,
     "dependency_type": "POWER_FEED"|"BIAS_FEED"|"CONTROL_GATE"|"SIGNAL_PATH"|"FEEDBACK_LOOP",
     "settling_delay_us": float (real settling time in microseconds, e.g. 120.0 for 120us, 2000.0 for 2ms),
     "condition": str (e.g. "VIN >= VOUT + 200mV with CL=1uF")
   }}
5. "electrical_parameters": List of ALL testable specifications (DO NOT OMIT ANY!):
   {{"symbol": str, "name": str, "min": float or null, "typ": float or null, "max": float or null, "unit": str, "test_conditions": str, "related_pins": [str], "category": str}}
   (Crucial: Include ALL timing specs: turnon settling time, startup time, slew rate, propagation delay, bandwidth, rise/fall times!)

OUTPUT JSON FORMAT:
{{
  "ic_device": {{ "name": "...", "category": "...", "description": "..." }},
  "pins": [ {{ "name": "...", "pin_number": "...", "direction": "...", "role": "...", "description": "...", "nominal_voltage": null, "voltage_range": {{ "min": null, "max": null, "typ": null }}, "settling_time_us": null, "load_capacitance_uf": null }} ],
  "functional_blocks": [ {{ "name": "...", "type": "...", "description": "...", "input_ports": [...], "output_ports": [...], "settling_time_us": null }} ],
  "port_dependencies": [ {{ "source": "...", "target": "...", "dependency_type": "...", "settling_delay_us": 120.0, "condition": "..." }} ],
  "electrical_parameters": [ {{ "symbol": "...", "name": "...", "min": null, "typ": null, "max": null, "unit": "...", "test_conditions": "...", "related_pins": [...] }} ]
}}
"""
        system_prompt = "You are a specialized analog semiconductor AI. Extract comprehensive, 100% faithful hardware entities, internal architectures, and physical port timing dependencies from datasheet text."
        
        extracted_json = self.llm.complete_json(prompt, system_prompt=system_prompt)
        return self._json_to_networkx_graph(extracted_json)

    def _json_to_networkx_graph(self, data: Dict[str, Any]) -> nx.MultiDiGraph:
        from .pdf_parser import PDFDatasheetParser
        G = nx.MultiDiGraph()
        ic_info = data.get("ic_device") or {"name": "DUT_IC", "category": "ANALOG"}
        if not isinstance(ic_info, dict):
            ic_info = {"name": str(ic_info), "category": "ANALOG"}
        ic_name = ic_info.get("name", "DUT_IC")
        G.add_node(ic_name, node_type="IC_DEVICE", data=ic_info)

        # Add Pins with disambiguated names
        pins = data.get("pins") or []
        for idx, pin in enumerate(pins):
            if not isinstance(pin, dict):
                continue
            raw_pname = pin.get("name") or f"PIN_{idx}"
            pname = PDFDatasheetParser.sanitize_pin_name(raw_pname)
            pin["name"] = pname
            G.add_node(pname, node_type="PIN", data=pin)
            G.add_edge(ic_name, pname, relation="HAS_PIN")

        # Add Functional Blocks
        blocks = data.get("functional_blocks") or []
        for idx, blk in enumerate(blocks):
            if isinstance(blk, dict):
                bname = blk.get("name") or f"BLOCK_{idx}"
            else:
                bname = str(blk)
                blk = {"name": bname}
            G.add_node(bname, node_type="FUNCTIONAL_BLOCK", data=blk)
            G.add_edge(ic_name, bname, relation="HAS_BLOCK")

            # Connect block to input/output ports
            for inp in blk.get("input_ports", []):
                inp_clean = PDFDatasheetParser.sanitize_pin_name(inp)
                if inp_clean in G:
                    G.add_edge(inp_clean, bname, relation="FEEDS_BLOCK")
            for outp in blk.get("output_ports", []):
                outp_clean = PDFDatasheetParser.sanitize_pin_name(outp)
                if outp_clean in G:
                    G.add_edge(bname, outp_clean, relation="DRIVES_PORT")

        # Add Port Dependencies directly
        for dep in data.get("port_dependencies") or []:
            if isinstance(dep, dict):
                src = PDFDatasheetParser.sanitize_pin_name(dep.get("source", ""))
                tgt = PDFDatasheetParser.sanitize_pin_name(dep.get("target", ""))
                if src in G and tgt in G and src != tgt:
                    raw_delay = dep.get("settling_delay_us")
                    try:
                        delay_us = float(raw_delay) if raw_delay is not None else 10.0
                    except (ValueError, TypeError):
                        delay_us = 10.0
                    G.add_edge(
                        src, tgt,
                        relation=dep.get("dependency_type", "DEPENDS_ON"),
                        settling_delay_us=delay_us,
                        condition=dep.get("condition") or ""
                    )

        # Add Electrical Parameters
        for idx, param in enumerate(data.get("electrical_parameters") or []):
            if not isinstance(param, dict):
                continue
            sym = param.get("symbol") or param.get("name") or f"PARAM_{idx}"
            param["symbol"] = sym
            G.add_node(sym, node_type="SPEC_PARAM", data=param)
            G.add_edge(ic_name, sym, relation="SPECIFIES")
            for rp in param.get("related_pins", []):
                rp_clean = PDFDatasheetParser.sanitize_pin_name(rp)
                if rp_clean in G:
                    G.add_edge(rp_clean, sym, relation="GOVERNS")

        # Store raw extraction metadata
        G.graph["raw_extraction"] = data
        return G
