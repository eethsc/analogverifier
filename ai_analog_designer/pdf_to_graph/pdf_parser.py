import re
import os
import json
import base64
import time
import urllib.request
import urllib.error
import concurrent.futures as cf
import pymupdf
from typing import Dict, List, Any, Optional

DEFAULT_NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "")

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
    State-of-the-art layout-aware PDF datasheet parser powered by NVIDIA Nemotron-Parse.
    Converts PDF document pages into high-fidelity structured LaTeX tables and Markdown,
    eliminating heuristic pixel coordinate errors, duplicate words, and collapsed columns.
    """

    def __init__(
        self,
        max_pages: int = 30,
        api_key: Optional[str] = None,
        model: str = "nvidia/nemotron-parse",
        concurrency: int = 3,
        dpi: int = 150,
        use_nemotron: bool = True
    ):
        self.max_pages = max_pages
        self.api_key = api_key or os.getenv("NVIDIA_API_KEY") or DEFAULT_NVIDIA_API_KEY
        self.model = model
        self.concurrency = max(1, concurrency)
        self.dpi = dpi
        self.use_nemotron = use_nemotron

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

    def _call_nemotron_parse(self, b64_img: str, max_retries: int = 2) -> Dict[str, Any]:
        """Calls NVIDIA NIM nemotron-parse endpoint with image payload."""
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{b64_img}"}
                        }
                    ]
                }
            ],
            "max_tokens": 3000,
            "temperature": 0.0
        }

        req = urllib.request.Request(
            "https://integrate.api.nvidia.com/v1/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            }
        )

        last_err = None
        for attempt in range(max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                last_err = e
                # If 429 Too Many Requests, wait and retry
                if e.code == 429 and attempt < max_retries:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                break
            except Exception as e:
                last_err = e
                if attempt < max_retries:
                    time.sleep(1.0)
                    continue
                break
        raise last_err or RuntimeError("Failed to call Nemotron-Parse API.")

    def parse_page_with_nemotron(self, page_index_0based: int, doc: pymupdf.Document) -> Dict[str, Any]:
        """Renders single page to image and parses it via NVIDIA Nemotron-Parse."""
        page_num = page_index_0based + 1
        page = doc[page_index_0based]
        
        # 1. Fallback raw text directly from PyMuPDF
        raw_text = page.get_text()

        if not self.use_nemotron or not self.api_key:
            classes = self.classify_page(raw_text)
            return {
                "page_num": page_num,
                "text": raw_text,
                "layout_text": raw_text,
                "classes": classes,
                "blocks": []
            }

        try:
            # 2. Render to PNG image
            pix = page.get_pixmap(dpi=self.dpi)
            b64_img = base64.b64encode(pix.tobytes("png")).decode("utf-8")

            # 3. Request Nemotron-Parse
            res_data = self._call_nemotron_parse(b64_img)
            choice = res_data["choices"][0]
            msg = choice.get("message", {})

            # 4. Extract structured blocks from tool_calls arguments
            blocks = []
            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args_str = fn.get("arguments", "")
                    try:
                        args = json.loads(args_str)
                        if isinstance(args, list):
                            for sub in args:
                                if isinstance(sub, list):
                                    blocks.extend(sub)
                                else:
                                    blocks.append(sub)
                        elif isinstance(args, dict):
                            blocks.append(args)
                    except Exception:
                        pass

            # 5. Build clean, structured layout text (Markdown + LaTeX tables)
            md_lines = []
            for b in blocks:
                b_type = b.get("type", "text")
                b_text = b.get("text", "")
                if b_type in ["header", "title"]:
                    md_lines.append(f"\n### {b_text}\n")
                elif b_type == "table":
                    md_lines.append(f"\n{b_text}\n")
                elif b_type == "figure":
                    md_lines.append(f"\n[Figure: {b_text}]\n")
                else:
                    md_lines.append(b_text)

            structured_text = "\n".join(md_lines).strip()
            final_text = structured_text if structured_text else raw_text
            classes = self.classify_page(final_text or raw_text)

            return {
                "page_num": page_num,
                "text": final_text,
                "layout_text": final_text,
                "classes": classes,
                "blocks": blocks
            }
        except Exception as e:
            # Graceful fallback to raw text if remote call fails
            classes = self.classify_page(raw_text)
            return {
                "page_num": page_num,
                "text": raw_text,
                "layout_text": raw_text,
                "classes": classes,
                "blocks": [],
                "error": str(e)
            }

    @staticmethod
    def classify_page(text: str) -> List[str]:
        lower = text.lower()
        classes = [c for c, kws in PAGE_CLASS_KEYWORDS.items() if any(k in lower for k in kws)]
        toc_hits = len(re.findall(r'\.{5,}\s*\d+', text))
        if toc_hits >= 6:
            return ["toc"]
        return classes

    def parse_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """
        Parses all PDF pages concurrently using NVIDIA Nemotron-Parse.
        Returns unified dictionary with page-level provenance.
        """
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"Datasheet PDF not found at {pdf_path}")

        doc = pymupdf.open(pdf_path)
        total_pages = len(doc)
        pages_to_process = min(total_pages, self.max_pages)

        pages: Dict[int, Dict[str, Any]] = {}
        tables_found = []

        # Concurrently parse pages through Nemotron-Parse
        with cf.ThreadPoolExecutor(max_workers=self.concurrency) as executor:
            future_to_idx = {
                executor.submit(self.parse_page_with_nemotron, p_idx, doc): p_idx + 1
                for p_idx in range(pages_to_process)
            }
            for future in cf.as_completed(future_to_idx):
                p_num = future_to_idx[future]
                try:
                    res = future.result()
                    pages[p_num] = {
                        "text": res["text"],
                        "layout_text": res["layout_text"],
                        "classes": res["classes"],
                        "blocks": res.get("blocks", [])
                    }
                except Exception as e:
                    raw_text = doc[p_num - 1].get_text()
                    pages[p_num] = {
                        "text": raw_text,
                        "layout_text": raw_text,
                        "classes": self.classify_page(raw_text),
                        "blocks": [],
                        "error": str(e)
                    }

        # Also collect basic tables if detected by pymupdf
        for p_idx in range(pages_to_process):
            try:
                page_tables = doc[p_idx].find_tables()
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
            "parser_engine": "nvidia/nemotron-parse" if self.use_nemotron else "pymupdf_fallback"
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
        pages = {int(k): v for k, v in parsed["pages"].items()}
        want = {
            "pins": ["pin_configuration"],
            "specs": ["abs_max", "recommended", "electrical", "esd", "thermal"],
            "functional": ["functional"],
        }[purpose]
        sel = [pn for pn in sorted(pages) if any(c in pages[pn]["classes"] for c in want)]
        if purpose == "specs":
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
        """Page texts used by the provenance audit."""
        return {pn: v["text"] + "\n" + v.get("layout_text", "") for pn, v in (parsed.get("pages") or {}).items()}

    @staticmethod
    def page_tagged_text(parsed: Dict[str, Any], page_numbers: List[int], budget_chars: int = 60000,
                          layout: bool = False) -> str:
        out, used = [], 0
        for pn in page_numbers:
            pg = parsed["pages"].get(pn) or parsed["pages"].get(str(pn))
            t = pg.get("layout_text") if layout and pg.get("layout_text") else pg["text"]
            chunk = f"\n===== PAGE {pn} =====\n{t}"
            if used + len(chunk) > budget_chars:
                chunk = chunk[: max(0, budget_chars - used)]
            out.append(chunk)
            used += len(chunk)
            if used >= budget_chars:
                break
        return "".join(out)
