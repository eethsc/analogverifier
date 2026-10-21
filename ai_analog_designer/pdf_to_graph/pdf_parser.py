import re
import os
import pymupdf
from typing import Dict, List, Any, Optional


PAGE_CLASS_KEYWORDS = {
    "pin_configuration": ["pin configuration", "pin functions", "terminal functions", "pinout", "pin description",
                          "pin assignments", "pin attributes"],
    "abs_max": ["absolute maximum ratings"],
    "esd": ["esd ratings", "electrostatic discharge"],
    "recommended": ["recommended operating conditions"],
    "thermal": ["thermal information", "thermal characteristics"],
    "electrical": ["electrical characteristics", "switching characteristics", "timing requirements",
                   "dc characteristics", "ac characteristics", "operating characteristics",
                   "dynamic characteristics", "performance characteristics"],
    "typical_characteristics": ["typical characteristics"],
    "functional": ["functional block diagram", "detailed description", "feature description",
                   "device functional modes", "theory of operation", "block diagram"],
    "application": ["application information", "typical application"],
}


class PDFDatasheetParser:
    """
    Layout-aware PDF datasheet parser.

    Keeps page-level provenance so that every entity extracted by the LLM can be
    traced (and machine-verified) back to the page it came from.
    """

    def __init__(self, max_pages: int = 30):
        self.max_pages = max_pages

    @staticmethod
    def sanitize_pin_name(raw_name: str) -> str:
        """Sanitizes pin names preserving polarities (+/-) and disambiguating ports."""
        if not raw_name:
            return "PIN"
        name = str(raw_name).strip()
        name = name.replace("+", "_P").replace("-", "_N").replace("−", "_N").replace("/", "_").replace("#", "_B")
        name = re.sub(r'[^a-zA-Z0-9_]', '_', name)
        name = re.sub(r'_+', '_', name).strip('_')
        return name or "PIN"

    @staticmethod
    def column_tagged_text(page) -> str:
        """Re-renders a spec-table page line-by-line from word coordinates and tags every number that sits
        under a MIN / TYP / MAX (/ NOM) column header, e.g. 'Iref Reference input current ... 2{TYP} 4{MAX} uA'.
        Plain PyMuPDF text loses column positions, which makes LLMs put a lone typ value into the min slot."""
        words = page.get_text("words")  # x0, y0, x1, y1, word, block, line, wno
        if not words:
            return page.get_text()
        rows: List[List[tuple]] = []
        for w in sorted(words, key=lambda w: (round((w[1] + w[3]) / 2), w[0])):
            yc = (w[1] + w[3]) / 2
            if rows and abs(((rows[-1][0][1] + rows[-1][0][3]) / 2) - yc) <= 3.0:
                rows[-1].append(w)
            else:
                rows.append([w])
        header_cols: Dict[str, float] = {}
        out = []
        num_re = re.compile(r'^[±\-−–+]?\d*\.?\d+$')
        for row in rows:
            row.sort(key=lambda w: w[0])
            toks = [w[4].upper() for w in row]
            if sum(t in ("MIN", "TYP", "MAX", "NOM") for t in toks) >= 2:
                header_cols = {w[4].upper(): (w[0] + w[2]) / 2 for w in row if w[4].upper() in ("MIN", "TYP", "MAX", "NOM")}
                out.append(" ".join(w[4] for w in row) + "   <<COLUMN HEADER>>")
                continue
            parts = []
            prev_x1 = None
            for w in row:
                txt = w[4]
                if prev_x1 is not None and w[0] - prev_x1 > 12:
                    parts.append("|")
                if header_cols and num_re.match(txt):
                    xc = (w[0] + w[2]) / 2
                    col, dist = min(((c, abs(x - xc)) for c, x in header_cols.items()), key=lambda t: t[1])
                    if dist < 28:
                        txt = f"{txt}{{{col}}}"
                parts.append(txt)
                prev_x1 = w[2]
            out.append(" ".join(parts))
        return "\n".join(out)

    @staticmethod
    def classify_page(text: str) -> List[str]:
        lower = text.lower()
        classes = [c for c, kws in PAGE_CLASS_KEYWORDS.items() if any(k in lower for k in kws)]
        # Table-of-contents pages mention every heading; detect and demote them.
        toc_hits = len(re.findall(r'\.{5,}\s*\d+', text))
        if toc_hits >= 6:
            return ["toc"]
        return classes

    def parse_pdf(self, pdf_path: str) -> Dict[str, Any]:
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"Datasheet PDF not found at {pdf_path}")

        doc = pymupdf.open(pdf_path)
        total_pages = len(doc)
        pages_to_process = min(total_pages, self.max_pages)

        pages: Dict[int, Dict[str, Any]] = {}
        tables_found = []
        for p_idx in range(pages_to_process):
            page = doc[p_idx]
            text = page.get_text()
            classes = self.classify_page(text)
            entry = {"text": text, "classes": classes}
            if any(c in classes for c in ("electrical", "abs_max", "recommended", "thermal", "esd")):
                entry["layout_text"] = self.column_tagged_text(page)
            pages[p_idx + 1] = entry
            try:
                page_tables = page.find_tables()
                for t in page_tables.tables:
                    t_data = t.extract()
                    if t_data and len(t_data) > 1:
                        tables_found.append({"page": p_idx + 1, "header": t_data[0], "rows": t_data[1:]})
            except Exception:
                pass
        doc.close()

        sections = self._segment_functional_sections(pages)
        return {
            "source_file": os.path.basename(pdf_path),
            "source_path": os.path.abspath(pdf_path),
            "total_pages": total_pages,
            "processed_pages": pages_to_process,
            "pages": pages,
            "sections": sections,
            "tables": tables_found,
        }

    def _segment_functional_sections(self, pages: Dict[int, Dict[str, Any]]) -> Dict[str, str]:
        """Legacy flat sections (kept for backwards compatibility)."""
        sec = {"overview": "", "pin_configuration": "", "electrical_specifications": "", "functional_description": ""}
        for pn in sorted(pages):
            text, cls = pages[pn]["text"], pages[pn]["classes"]
            if pn == 1:
                sec["overview"] += text + "\n"
            if "pin_configuration" in cls:
                sec["pin_configuration"] += text + "\n"
            if any(c in cls for c in ("abs_max", "recommended", "electrical", "esd", "thermal")):
                sec["electrical_specifications"] += text + "\n"
            if "functional" in cls:
                sec["functional_description"] += text + "\n"
        return sec

    @staticmethod
    def select_pages(parsed: Dict[str, Any], purpose: str) -> List[int]:
        """Selects the page numbers relevant for a given extraction purpose."""
        pages = parsed["pages"]
        want = {
            "pins": ["pin_configuration"],
            "specs": ["abs_max", "recommended", "electrical", "esd", "thermal"],
            "functional": ["functional"],
        }[purpose]
        sel = [pn for pn in sorted(pages) if any(c in pages[pn]["classes"] for c in want)]
        if purpose == "specs":
            # follow multi-page continuation of electrical tables
            extra = []
            for pn in sel:
                nxt = pn + 1
                if nxt in pages and nxt not in sel and "electrical" in pages[pn]["classes"]:
                    t = pages[nxt]["text"].lower()
                    if ("(continued)" in t or ("parameter" in t and ("min" in t and "max" in t))) \
                            and "typical characteristics" not in t[:400]:
                        extra.append(nxt)
            sel = sorted(set(sel + extra))
        if not sel:
            sel = [pn for pn in sorted(pages)][:4 if purpose == "pins" else 10]
        if purpose == "pins" and 1 not in sel:
            sel = [1] + sel
        return sel

    @staticmethod
    def audit_pages(parsed: Dict[str, Any]) -> Dict[int, str]:
        """Page texts used by the provenance audit (plain text + column-tagged layout text)."""
        return {pn: v["text"] + "\n" + v.get("layout_text", "") for pn, v in (parsed.get("pages") or {}).items()}

    @staticmethod
    def page_tagged_text(parsed: Dict[str, Any], page_numbers: List[int], budget_chars: int = 60000,
                         layout: bool = False) -> str:
        out, used = [], 0
        for pn in page_numbers:
            pg = parsed["pages"][pn]
            t = pg.get("layout_text") if layout and pg.get("layout_text") else pg["text"]
            chunk = f"\n===== PAGE {pn} =====\n{t}"
            if used + len(chunk) > budget_chars:
                chunk = chunk[: max(0, budget_chars - used)]
            out.append(chunk)
            used += len(chunk)
            if used >= budget_chars:
                break
        return "".join(out)
