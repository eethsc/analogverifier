import re
from typing import Dict, Any, List, Tuple

class NetlistValidator:
    """
    Validates SPICE netlist syntax, pin compatibility, model completeness, and topological sanity.
    Provides automated minor syntax repair (e.g., missing .ends, mid-line comment sanitization,
    duplicate instance deduplication, default models) and detailed diagnostics for LLM re-prompting.
    """

    STANDARD_MODELS = """
* Default Physical Device Models for AI Analog Synthesis
.model NMOS NMOS (LEVEL=1 VTO=0.7 KP=120u GAMMA=0.4 PHI=0.6 LAMBDA=0.02 TOX=10n CJ=2e-4 CJSW=1e-10 CGDO=2e-10 CGSO=2e-10)
.model PMOS PMOS (LEVEL=1 VTO=-0.7 KP=40u GAMMA=0.4 PHI=0.6 LAMBDA=0.02 TOX=10n CJ=2e-4 CJSW=1e-10 CGDO=2e-10 CGSO=2e-10)
.model QNPN NPN (IS=1e-14 BF=150 VAF=100 IKF=0.1 ISE=1e-14 NE=1.5 BR=1 VAR=100 TF=0.3n TR=10n CJE=2p CJC=1p VJC=0.75 MJC=0.33)
.model QPNP PNP (IS=1e-14 BF=80 VAF=80 IKF=0.1 ISE=1e-14 NE=1.5 BR=1 VAR=80 TF=0.5n TR=20n CJE=2p CJC=1.5p VJC=0.75 MJC=0.33)
.model D1N4148 D (IS=2.52n RS=0.568 N=1.752 CJO=4p VJ=0.5 M=0.333 BV=100 IBV=100u TT=4n)
.model D_ESD D (IS=1e-15 RS=10 CJO=1p BV=50)
"""

    def __init__(self):
        pass

    def _join_multiline_braces(self, text: str) -> str:
        """Flattens multiline { ... } expressions into single lines for SPICE compliance."""
        lines = text.splitlines()
        out = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if '{' in line and '}' not in line:
                joined = line
                i += 1
                while i < len(lines):
                    nxt = lines[i].strip()
                    if ';' in nxt:
                        nxt = nxt[:nxt.find(';')].strip()
                    joined += ' ' + nxt
                    if '}' in nxt:
                        break
                    i += 1
                out.append(joined)
            else:
                out.append(line)
            i += 1
        return '\n'.join(out)

    def sanitize_spice_syntax(self, code: str) -> str:
        """Sanitizes common SPICE / LTspice syntax errors and deduplicates instances per subcircuit."""
        # Wrap any accidental prose before the first .subckt or after the last .ends with comment *
        idx = code.lower().find(".subckt")
        if idx != -1:
            prefix = code[:idx]
            clean_prefix = "\n".join(["* " + l if l.strip() and not l.strip().startswith(("*", ";")) else l for l in prefix.splitlines()])
            ends_idx = code.lower().rfind(".ends")
            if ends_idx != -1:
                ends_line_end = code.find("\n", ends_idx)
                if ends_line_end != -1:
                    body = code[idx:ends_line_end]
                    suffix = code[ends_line_end:]
                    clean_suffix = "\n".join(["* " + l if l.strip() and not l.strip().startswith(("*", ";", ".")) else l for l in suffix.splitlines()])
                    code = clean_prefix + "\n" + body + "\n" + clean_suffix
                else:
                    code = clean_prefix + "\n" + code[idx:]
            else:
                code = clean_prefix + "\n" + code[idx:]

        flattened_code = self._join_multiline_braces(code)
        # Clean stray continuation operators left inside braces/parens
        flattened_code = re.sub(r'([\(\{])\s*\+', r'\1', flattened_code)
        flattened_code = re.sub(r'\+\s*([\)\}])', r'\1', flattened_code)
        lines = flattened_code.splitlines()
        clean_lines = []

        subckt_scopes = []
        scope_instances = set()
        scope_v_pairs = set()
        v_decouple_counter = 1

        for line in lines:
            line_str = line.strip()
            if not line_str:
                clean_lines.append("")
                continue

            # Full line comment
            if line_str.startswith("*") or line_str.startswith(";"):
                clean_lines.append(line)
                continue

            # Drop orphaned single brackets or semicolons
            if line_str in [")", "}", "]", ";"]:
                continue

            # Track subckt context; if nested .subckt is encountered, close the prior one with .ends
            if line_str.lower().startswith(".subckt"):
                if subckt_scopes:
                    clean_lines.append(".ends")
                    scope_instances = subckt_scopes.pop()
                subckt_scopes.append(scope_instances)
                scope_instances = set()
                scope_v_pairs = set()
            elif line_str.lower().startswith(".ends"):
                if subckt_scopes:
                    scope_instances = subckt_scopes.pop()
                else:
                    scope_instances = set()
                scope_v_pairs = set()

            # In LTspice, .node is invalid and .global/.temp/.tran/.ac/.op inside .subckt are illegal
            if line_str.lower().startswith(".node "):
                clean_lines.append(f"* Removed invalid .node directive: {line_str}")
                continue
            if len(subckt_scopes) > 0 and line_str.lower().startswith((".global", ".temp", ".tran", ".ac", ".op", ".dc")):
                clean_lines.append(f"* Removed subckt directive: {line_str}")
                continue

            tokens = line_str.split()
            first_tok = tokens[0] if tokens else ""

            # Remove invalid independent voltage model: e.g. .model VREF V (V=2.5)
            if line_str.lower().startswith(".model") and len(tokens) >= 3 and tokens[2].upper() == "V":
                clean_lines.append(f"* Removed invalid V model: {line_str}")
                continue

            # Check for incomplete / truncated device lines (e.g. bare "C_OUTR")
            if tokens and first_tok[0].upper() in "RCLMQ" and not first_tok.startswith("."):
                f_char = first_tok[0].upper()
                if f_char in "RCL" and len(tokens) < 4:
                    clean_lines.append(f"* Truncated element removed: {line_str}")
                    continue
                elif f_char == "M" and len(tokens) < 6:
                    clean_lines.append(f"* Truncated element removed: {line_str}")
                    continue
                elif f_char == "Q" and len(tokens) < 5:
                    clean_lines.append(f"* Truncated element removed: {line_str}")
                    continue

            # For behavioral sources (B, E, G): strip invalid parameters TD=... and TRISTATE=...
            if tokens and first_tok[0].upper() in "BEG" and not first_tok.startswith("."):
                line = re.sub(r'\bTD\s*=\s*\{?[^}\s]+\}?', '', line, flags=re.IGNORECASE)
                line = re.sub(r'\bTRISTATE\s*=\s*\{?[^}\s]+\}?', '', line, flags=re.IGNORECASE)
                line_str = line.strip()
                tokens = line_str.split()

            # Break self-referential algebraic loops in B-sources that cause singular matrix in LTspice
            # e.g.: B1 X 0 V = { cond ? new_val : V(X) } -> causes singular matrix on node X
            if tokens and first_tok[0].upper() == "B" and len(tokens) >= 3 and not first_tok.startswith("."):
                out_node = tokens[1]
                self_v_pattern = re.compile(r'\bV\s*\(\s*' + re.escape(out_node) + r'\s*\)', re.IGNORECASE)
                if self_v_pattern.search(line):
                    line = self_v_pattern.sub('0', line)
                    clean_lines.append(f".ic V({out_node})=0")
                    line_str = line.strip()
                    tokens = line_str.split()

            # Prevent parallel voltage sources driving identical nodes (over-defined circuit matrix)
            if tokens and first_tok[0].upper() in "BV" and len(tokens) >= 3 and not first_tok.startswith("."):
                is_v = True
                if first_tok[0].upper() == "B":
                    is_v = any(term in line_str.upper() for term in [" V =", " V=", " V "])
                if is_v:
                    n1 = tokens[1].upper()
                    n2 = tokens[2].upper()
                    pair = (n1, n2)
                    if pair in scope_v_pairs and n1 not in ["0", "GND", "AGND"]:
                        new_node = f"{tokens[1]}_dec_{v_decouple_counter}"
                        v_decouple_counter += 1
                        line = re.sub(r'^\s*(\S+\s+)' + re.escape(tokens[1]), r'\g<1>' + new_node, line)
                        clean_lines.append(f"R_{new_node} {new_node} {tokens[1]} 1 ; Parallel source decoupling")
                        line_str = line.strip()
                        tokens = line_str.split()
                    else:
                        scope_v_pairs.add(pair)

            # For pulse sources: replace dynamic V(node) inside PULSE with static typical voltage
            if "PULSE(" in line_str.upper():
                line = re.sub(r'PULSE\s*\(\s*([0-9\.\-]+)\s+V\([^)]+\)', r'PULSE(\1 3.3', line, flags=re.IGNORECASE)
                line_str = line.strip()
                tokens = line_str.split()

            # For MOSFETs (M...), remove parentheses around parameters e.g. (W=10u L=0.5u) -> W=10u L=0.5u
            if tokens and first_tok.upper().startswith("M") and not first_tok.startswith("."):
                line = re.sub(r'\(([\w\.\s=+-]+)\)', r'\1', line)
                line_str = line.strip()
                tokens = line_str.split()

            # For BJTs (Q...), strip invalid MOSFET sizing parameters W=... L=...
            if tokens and tokens[0].upper().startswith("Q") and not tokens[0].startswith("."):
                line_str = re.sub(r'\b[WL]=\S+', '', line_str, flags=re.IGNORECASE).strip()
                line = re.sub(r'\b[WL]=\S+', '', line, flags=re.IGNORECASE).strip()

            # In LTspice expressions, logical AND is & (not &&) and logical OR is | (not ||)
            if "&&" in line:
                line = line.replace("&&", "&")
                line_str = line.strip()
            if "||" in line:
                line = line.replace("||", "|")
                line_str = line.strip()

            # Map time integration / differentiation to LTspice functions idt(...) and ddt(...)
            if "integ(" in line.lower() or "diff(" in line.lower():
                line = re.sub(r'\bINTEG\s*\(', 'idt(', line, flags=re.IGNORECASE)
                line = re.sub(r'\bDIFF\s*\(', 'ddt(', line, flags=re.IGNORECASE)
                line_str = line.strip()

            # Sanitize mid-line * comments to ; comments (strictly for non-expression device lines)
            is_expr_line = line_str.lower().startswith((".param", ".meas", ".func", "b", "e", "g"))
            if not is_expr_line and " *" in line:
                parts = []
                inside_braces = False
                inside_quotes = False
                inside_parens = 0
                i = 0
                while i < len(line):
                    ch = line[i]
                    if ch in ("'", '"'):
                        inside_quotes = not inside_quotes
                    elif ch == '{':
                        inside_braces = True
                    elif ch == '}':
                        inside_braces = False
                    elif ch == '(':
                        inside_parens += 1
                    elif ch == ')':
                        inside_parens = max(0, inside_parens - 1)
                    
                    if not inside_quotes and not inside_braces and inside_parens == 0 and line[i:i+2] == " *":
                        rest = line[i+2:].strip()
                        if rest and not any(op in rest for op in ["=", "+", "-", "/", "(", ")"]) and not rest[0].isdigit():
                            parts.append(" ; " + rest)
                            break
                    parts.append(ch)
                    i += 1
                line = "".join(parts)
                line_str = line.strip()

            # Wrap bare parameter names or unbraced math on R, C, L devices in curly braces { ... }
            tokens = line_str.split()
            if len(tokens) >= 4 and tokens[0][0].upper() in "RCL" and not tokens[0].startswith("."):
                val_tok = tokens[3]
                if not val_tok.startswith("{"):
                    if any(op in val_tok for op in ["/", "*", "+"]):
                        tokens[3] = f"{{{val_tok}}}"
                        line = " ".join(tokens)
                        line_str = line.strip()
                    elif not val_tok[0].isdigit() and val_tok[0] not in "+-":
                        tokens[3] = f"{{{val_tok}}}"
                        line = " ".join(tokens)
                        line_str = line.strip()

            # In-line procedural assignments inside B-sources: e.g. { a = 1 b = 2 c }
            first_tok = tokens[0] if tokens else ""
            if first_tok and first_tok[0].upper() in "BEG" and "{" in line and "}" in line:
                m_b = re.search(r'^(.*?=\s*\{\s*)(.*?)(\s*\}\s*)$', line)
                if m_b:
                    prefix, inner, suffix = m_b.group(1), m_b.group(2), m_b.group(3)
                    clean_eq = re.sub(r'[=<>]=', '', inner)
                    if '=' in clean_eq:
                        pattern = r'([a-zA-Z0-9_]+)\s*=\s*(.*?)(?=[a-zA-Z0-9_]+\s*=(?!=)|$)'
                        matches = re.findall(pattern, inner)
                        if len(matches) > 1:
                            subst = {}
                            last_expr = ''
                            for var, expr in matches:
                                expr = expr.strip()
                                expr = re.sub(r'\s+' + re.escape(var) + r'$', '', expr)
                                for v_name, v_val in subst.items():
                                    expr = re.sub(r'\b' + re.escape(v_name) + r'\b', f'({v_val})', expr)
                                subst[var] = expr
                                last_expr = expr
                            line = f"{prefix}{last_expr}{suffix}"
                            line_str = line.strip()

            # Disambiguate duplicate instance names (e.g. M_OUT_N defined twice in same subckt)
            tokens = line_str.split()
            first_tok = tokens[0] if tokens else ""
            if first_tok and first_tok[0].upper() in "RCLMQDVIXEGFHSW" and not first_tok.startswith("."):
                inst_name = first_tok.upper()
                if inst_name in scope_instances:
                    # Deduplicate name
                    counter = 2
                    new_name = f"{first_tok}_{counter}"
                    while new_name.upper() in scope_instances:
                        counter += 1
                        new_name = f"{first_tok}_{counter}"
                    # Replace first token in line preserving indentation
                    line = re.sub(r'^\s*' + re.escape(first_tok), new_name, line)
                    scope_instances.add(new_name.upper())
                else:
                    scope_instances.add(inst_name)

            clean_lines.append(line)

        # Hoist all .model definitions to the global scope before the first .subckt
        # This prevents "Node or model name expected" when helper subcircuits are defined
        full_text = "\n".join(clean_lines)
        model_pattern = re.compile(r'^\s*(\.model\s+[^\n]+)', re.MULTILINE | re.IGNORECASE)
        found_models = model_pattern.findall(full_text)

        standard_models = [
            ".model NMOS NMOS (LEVEL=1 VTO=0.7 KP=100u GAMMA=0.4 PHI=0.7 LAMBDA=0.01)",
            ".model PMOS PMOS (LEVEL=1 VTO=-0.7 KP=40u GAMMA=0.5 PHI=0.8 LAMBDA=0.01)",
            ".model D_PROT D (IS=1e-15 N=1 RS=10)",
            ".model S_MODEL VSWITCH (RON=10 ROFF=1G VTH=0.5 VH=0.1)"
        ]

        all_models = list(found_models)
        for sm in standard_models:
            m_type = sm.split()[1].upper()
            if not any(f" {m_type} " in m.upper() for m in found_models):
                all_models.insert(0, sm)

        # Remove duplicate models preserving order
        unique_models = []
        seen_model_names = set()
        for m in all_models:
            m_parts = m.strip().split()
            if len(m_parts) >= 2:
                name = m_parts[1].upper()
                if name not in seen_model_names:
                    seen_model_names.add(name)
                    unique_models.append(m.strip())

        idx_first_sub = full_text.lower().find(".subckt")
        if idx_first_sub != -1 and unique_models:
            header = "* --- Globally Hoisted Models for Multi-Subcircuit Visibility ---\n" + "\n".join(unique_models) + "\n\n"
            full_text = full_text[:idx_first_sub] + header + full_text[idx_first_sub:]

        return full_text

    def validate(
        self,
        netlist: str,
        expected_subckt_name: str,
        expected_pins: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Validates the generated SPICE netlist against device pin specifications and SPICE syntax rules.
        """
        errors = []
        warnings = []
        cleaned_netlist = self.sanitize_spice_syntax(netlist.strip())

        # 1. Check .subckt header
        subckt_match = re.search(r'^\s*\.subckt\s+([a-zA-Z0-9_]+)\s+(.+)$', cleaned_netlist, re.MULTILINE | re.IGNORECASE)
        if not subckt_match:
            errors.append(f"Missing .subckt directive in netlist for expected subcircuit '{expected_subckt_name}'.")
            declared_name = expected_subckt_name
            declared_pins = []
        else:
            declared_name = subckt_match.group(1)
            declared_pins = subckt_match.group(2).split()

            # Verify pin presence
            expected_pin_names = [p["name"] for p in expected_pins]
            expected_pin_names_lower = [p["name"].lower() for p in expected_pins]
            declared_pins_lower = [p.lower() for p in declared_pins]

            missing_pins = []
            for ep, ep_lower in zip(expected_pin_names, expected_pin_names_lower):
                if ep_lower not in declared_pins_lower:
                    missing_pins.append(ep)

            if missing_pins:
                warnings.append(f"Pins expected from datasheet missing in .subckt: {missing_pins}")

        # 2. Check .ends directive
        ends_match = re.search(r'^\s*\.ends(\s+[a-zA-Z0-9_]+)?\s*$', cleaned_netlist, re.MULTILINE | re.IGNORECASE)
        if not ends_match:
            warnings.append("Missing .ends directive. Auto-appending .ends.")
            cleaned_netlist += f"\n.ends {declared_name}\n"

        # 3. Check for undefined device models
        used_models = set()
        defined_models = set()

        for line in cleaned_netlist.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith("*") or line_str.startswith(";"):
                continue

            # Check .model definition
            model_def_match = re.match(r'^\.model\s+([a-zA-Z0-9_]+)\s+([a-zA-Z0-9_]+)', line_str, re.IGNORECASE)
            if model_def_match:
                defined_models.add(model_def_match.group(1).upper())
                continue

            # Check MOSFET: M... d g s b model_name [w=...] [l=...]
            mos_match = re.match(r'^M[a-zA-Z0-9_]+\s+\S+\s+\S+\s+\S+\s+\S+\s+([a-zA-Z0-9_]+)', line_str, re.IGNORECASE)
            if mos_match:
                used_models.add(mos_match.group(1).upper())
                continue

            # Check BJT: Q... c b e [s] model_name
            bjt_match = re.match(r'^Q[a-zA-Z0-9_]+\s+\S+\s+\S+\s+\S+\s+([a-zA-Z0-9_]+)', line_str, re.IGNORECASE)
            if bjt_match:
                used_models.add(bjt_match.group(1).upper())
                continue

            # Check Diode: D... a c model_name
            dio_match = re.match(r'^D[a-zA-Z0-9_]+\s+\S+\s+\S+\s+([a-zA-Z0-9_]+)', line_str, re.IGNORECASE)
            if dio_match:
                used_models.add(dio_match.group(1).upper())
                continue

        missing_models = [m for m in used_models if m not in defined_models]
        if missing_models:
            warnings.append(f"Models referenced but not defined in netlist: {missing_models}. Auto-injecting standard physical models.")
            cleaned_netlist = self.STANDARD_MODELS + "\n" + cleaned_netlist

        # 4. Check for invalid characters or ngspice directives incompatible with LTspice
        if ".control" in cleaned_netlist.lower():
            errors.append("Netlist contains .control block which is invalid inside an LTspice subcircuit.")

        is_valid = len(errors) == 0

        return {
            "is_valid": is_valid,
            "errors": errors,
            "warnings": warnings,
            "cleaned_netlist": cleaned_netlist,
            "subckt_name": declared_name,
            "pins": declared_pins if subckt_match else [p["name"] for p in expected_pins]
        }
