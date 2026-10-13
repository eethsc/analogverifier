import re
import os
import pymupdf
from typing import Dict, List, Any, Optional

class PDFDatasheetParser:
    """
    Layout-Aware PDF Datasheet Parser for Hardware Knowledge Graph Extraction.
    Implements multi-modal document decomposition based on:
    - Docs2KG (arXiv:2406.01234)
    - Faithful Construction of Hardware Knowledge Graphs (arXiv:2410.xxxxx)
    Extracts text blocks, pin assignment tables, and electrical specification tables.
    """
    def __init__(self, max_pages: int = 15):
        self.max_pages = max_pages

    def parse_pdf(self, pdf_path: str) -> Dict[str, Any]:
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"Datasheet PDF not found at {pdf_path}")

        doc = pymupdf.open(pdf_path)
        total_pages = len(doc)
        pages_to_process = min(total_pages, self.max_pages)

        document_text_blocks = []
        full_text_by_page = {}
        tables_found = []

        for p_idx in range(pages_to_process):
            page = doc[p_idx]
            page_text = page.get_text()
            full_text_by_page[p_idx + 1] = page_text

            # Extract structured blocks
            blocks = page.get_text("blocks")
            for b in blocks:
                # b = (x0, y0, x1, y1, text, block_no, block_type)
                text = b[4].strip()
                if len(text) > 10:
                    document_text_blocks.append({
                        "page": p_idx + 1,
                        "bbox": (b[0], b[1], b[2], b[3]),
                        "text": text
                    })

            # Extract tables using pymupdf page.find_tables()
            try:
                page_tables = page.find_tables()
                if page_tables.tables:
                    for t in page_tables.tables:
                        t_data = t.extract()
                        if t_data and len(t_data) > 1:
                            tables_found.append({
                                "page": p_idx + 1,
                                "header": t_data[0],
                                "rows": t_data[1:]
                            })
            except Exception:
                pass

        doc.close()

        # Identify primary sections
        sections = self._segment_functional_sections(full_text_by_page)

        return {
            "source_file": os.path.basename(pdf_path),
            "total_pages": total_pages,
            "processed_pages": pages_to_process,
            "sections": sections,
            "tables": tables_found,
            "raw_blocks": document_text_blocks
        }

    def _segment_functional_sections(self, full_text_by_page: Dict[int, str]) -> Dict[str, str]:
        """Classifies text into Overview, Pins, Specs, and Operation."""
        sections = {
            "overview": "",
            "pin_configuration": "",
            "electrical_specifications": "",
            "functional_description": ""
        }

        for page_num, text in full_text_by_page.items():
            lower = text.lower()
            if page_num == 1:
                sections["overview"] += text + "\n"

            if any(k in lower for k in ["pin configuration", "terminal functions", "pinout", "pin description"]):
                sections["pin_configuration"] += text + "\n"

            if any(k in lower for k in ["electrical characteristics", "specifications", "absolute maximum", "operating conditions", "timing requirements", "switching characteristics", "ac characteristics", "dc characteristics"]):
                sections["electrical_specifications"] += text + "\n"

            if any(k in lower for k in ["functional block", "detailed description", "application information", "overview"]):
                sections["functional_description"] += text + "\n"

        # Fallback if specific sections were sparse
        if not sections["pin_configuration"]:
            sections["pin_configuration"] = full_text_by_page.get(1, "") + "\n" + full_text_by_page.get(2, "")
        if not sections["electrical_specifications"]:
            for p in range(2, min(6, len(full_text_by_page) + 1)):
                sections["electrical_specifications"] += full_text_by_page.get(p, "") + "\n"

        return sections
