import re
import json
from typing import Dict, List, Any, Optional
from .data_models import DocumentBlock, DocumentDatabase

class Stage0DocumentParser:
    """
    Stage 0: Document Parsing and Block-Level Semantic Analysis (Paper Appendix A)
    Decomposes unstructured or semi-structured IC datasheets into typed blocks:
    - 12 Block taxonomy: section_header, feature_list, pin_definition, electrical_characteristics,
      absolute_max_ratings, application_info, equation, table, text_paragraph, figure_reference,
      timing_diagram, other.
    """
    def __init__(self):
        self.block_counter = 0

    def parse_from_json_spec(self, json_data: Dict[str, Any], doc_id: str = "SPEC_001") -> DocumentDatabase:
        """Parses structured JSON specification files into typed DocumentBlocks."""
        blocks: List[DocumentBlock] = []

        # 1. Product title and feature block
        circuit_name = json_data.get("circuit_name") or json_data.get("device", "Unknown Circuit")
        doc_info = json_data.get("datasheet_doc", "Datasheet")
        features = json_data.get("key_features", [
            f"Precision analog circuit device: {circuit_name}",
            f"Referenced datasheet: {doc_info}"
        ])
        b_hdr = DocumentBlock(
            block_id=f"blk_{self._next_id()}",
            page_number=1,
            raw_text=f"Product Overview: {circuit_name} ({doc_info})",
            block_type="section_header",
            confidence=0.99,
            metadata={"product_name": circuit_name, "doc_ref": doc_info}
        )
        blocks.append(b_hdr)

        b_feat = DocumentBlock(
            block_id=f"blk_{self._next_id()}",
            page_number=1,
            raw_text="\n".join(features),
            block_type="feature_list",
            confidence=0.95,
            metadata={"features": features}
        )
        blocks.append(b_feat)

        # 2. Pin definitions block
        pins_data = json_data.get("pins", [])
        if pins_data:
            extracted_pins = []
            for p in pins_data:
                extracted_pins.append({
                    "pin_name": p.get("pin_name", ""),
                    "pin_number": p.get("pin_number", {}),
                    "pin_type": p.get("pin_type", "ANALOG"),
                    "direction": p.get("direction", "INPUT"),
                    "description": p.get("description", ""),
                    "electrical_constraints": p.get("electrical_constraints", {})
                })
            b_pin = DocumentBlock(
                block_id=f"blk_{self._next_id()}",
                page_number=2,
                raw_text=f"Pin Definitions for {circuit_name}",
                block_type="pin_definition",
                confidence=0.98,
                extracted_pins=extracted_pins
            )
            blocks.append(b_pin)

        # 3. Operating conditions
        test_cond = json_data.get("test_conditions", {})
        if test_cond:
            b_app = DocumentBlock(
                block_id=f"blk_{self._next_id()}",
                page_number=2,
                raw_text=f"Operating conditions: {json.dumps(test_cond)}",
                block_type="application_info",
                confidence=0.96,
                metadata={"test_conditions": test_cond}
            )
            blocks.append(b_app)

        # 4. Electrical characteristics table
        specs = json_data.get("specifications", {})
        extracted_params = []
        if isinstance(specs, dict):
            for sym, sdata in specs.items():
                extracted_params.append({
                    "symbol": sym,
                    "name": sdata.get("name", sym),
                    "min_value": sdata.get("min"),
                    "typ_value": sdata.get("typ"),
                    "max_value": sdata.get("max"),
                    "unit": sdata.get("unit", ""),
                    "description": sdata.get("description", ""),
                    "category": sdata.get("category", "General"),
                    "is_testable": True,
                    "test_conditions": [f"Standard operating conditions: {k}={v}" for k, v in test_cond.items()]
                })
        elif isinstance(specs, list):
            for sdata in specs:
                extracted_params.append({
                    "symbol": sdata.get("meas_key") or sdata.get("id", "PARAM"),
                    "name": sdata.get("name", ""),
                    "min_value": sdata.get("min"),
                    "typ_value": sdata.get("typ"),
                    "max_value": sdata.get("max"),
                    "unit": sdata.get("unit", ""),
                    "description": sdata.get("description", ""),
                    "category": sdata.get("category", "General"),
                    "is_testable": True,
                    "test_conditions": [f"Standard operating conditions: {k}={v}" for k, v in test_cond.items()]
                })

        b_elec = DocumentBlock(
            block_id=f"blk_{self._next_id()}",
            page_number=3,
            raw_text=f"Electrical Characteristics: {len(extracted_params)} parameters",
            block_type="electrical_characteristics",
            confidence=0.98,
            extracted_parameters=extracted_params
        )
        blocks.append(b_elec)

        # 5. Design equations if available
        eqs = json_data.get("equations", [])
        if eqs:
            extracted_eqs = []
            for eq in eqs:
                extracted_eqs.append({
                    "equation_id": eq.get("equation_id", "EQ_001"),
                    "raw_form": eq.get("raw_form", ""),
                    "sympy_form": eq.get("sympy_form", ""),
                    "latex_form": eq.get("latex_form", ""),
                    "description": eq.get("description", ""),
                    "output_variable": eq.get("output_variable", ""),
                    "input_variables": eq.get("input_variables", [])
                })
            b_eq = DocumentBlock(
                block_id=f"blk_{self._next_id()}",
                page_number=4,
                raw_text=f"Design Equations: {len(extracted_eqs)} equations",
                block_type="equation",
                confidence=0.95,
                extracted_equations=extracted_eqs
            )
            blocks.append(b_eq)

        return DocumentDatabase(
            document_id=doc_id,
            blocks=blocks,
            metadata={"source_type": "JSON", "circuit_name": circuit_name}
        )

    def parse_from_text(self, text_content: str, doc_id: str = "DOC_TEXT_001") -> DocumentDatabase:
        """Parses text-based datasheet into typed blocks using layout and structural markers."""
        blocks: List[DocumentBlock] = []
        raw_sections = re.split(r'\n(?=[0-9]+\.\s+[A-Z]|\bTable\s+[0-9]+:|\bPin\s+Assignment|\bElectrical\s+Characteristics)', text_content)

        page_num = 1
        for sec in raw_sections:
            sec_clean = sec.strip()
            if not sec_clean:
                continue

            # Classify block type
            lower_sec = sec_clean.lower()
            if "pin assignment" in lower_sec or "pin definition" in lower_sec:
                b_type = "pin_definition"
            elif "electrical characteristics" in lower_sec or "parameter" in lower_sec and "|" in lower_sec:
                b_type = "electrical_characteristics"
            elif "equation" in lower_sec or "=" in lower_sec and ("/" in lower_sec or "*" in lower_sec):
                b_type = "equation"
            elif "features" in lower_sec or "key features" in lower_sec:
                b_type = "feature_list"
            elif lower_sec.startswith("table"):
                b_type = "table"
            elif any(lower_sec.startswith(f"{i}.") for i in range(1, 20)):
                b_type = "section_header"
            else:
                b_type = "text_paragraph"

            blk = DocumentBlock(
                block_id=f"blk_{self._next_id()}",
                page_number=page_num,
                raw_text=sec_clean,
                block_type=b_type,
                confidence=0.92
            )
            blocks.append(blk)
            if len(sec_clean) > 800:
                page_num += 1

        return DocumentDatabase(document_id=doc_id, blocks=blocks, metadata={"source_type": "TEXT"})

    def _next_id(self) -> int:
        self.block_counter += 1
        return self.block_counter
