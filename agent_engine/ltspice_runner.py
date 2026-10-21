import os
import subprocess
import glob
import re

_CANDIDATES = [
    os.environ.get("LTSPICE_PATH", ""),
    r"D:\Program Files\LTspice\LTspice.exe",
    r"C:\Program Files\LTspice\LTspice.exe",
    r"C:\Program Files\ADI\LTspice\LTspice.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\ADI\LTspice\LTspice.exe"),
    r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe",
]
LTSPICE_EXE = next((p for p in _CANDIDATES if p and os.path.exists(p)), _CANDIDATES[1])

class LTspiceRunner:
    def __init__(self, simulator_path=None):
        self.simulator_path = simulator_path or LTSPICE_EXE
        if not os.path.exists(self.simulator_path):
            raise FileNotFoundError(f"LTspice not found at {self.simulator_path}")

    def run_netlist(self, netlist_path, timeout=60):
        """Runs LTspice in batch mode and parses measurement results."""
        netlist_path = os.path.abspath(netlist_path)
        workdir = os.path.dirname(netlist_path)
        base_name = os.path.splitext(os.path.basename(netlist_path))[0]
        log_file = os.path.join(workdir, f"{base_name}.log")

        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except Exception:
                pass

        # Run without -ascii to prevent crash on .noise analyses
        cmd = [self.simulator_path, "-b", netlist_path]
        try:
            proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise TimeoutError(f"LTspice timed out after {timeout} seconds on {netlist_path}")

        if not os.path.exists(log_file):
            candidates = glob.glob(os.path.join(workdir, f"{base_name}*.log"))
            if candidates:
                log_file = candidates[0]
            else:
                raise RuntimeError(f"Log file not generated for {netlist_path}. LTspice output: {proc.stdout} {proc.stderr}")

        return self.parse_log(log_file)

    def parse_log(self, log_path):
        """Extracts all measurement (.meas) values from the LTspice log file."""
        results = {}
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        for line in lines:
            line_str = line.strip()

            # Ignore non-meas lines
            line_lower = line_str.lower()
            if any(line_lower.startswith(k) for k in ["direct newton", "solver", "maximum", "total elapsed", "tnom", "temp", "method", "asciirawfile", "gmin", "vernier", "starting", "source step"]):
                continue

            # Case 1: Complex AC measurement:
            # e.g.: "avol_at_1hz: V(out)=(105.53dB,-11.31°) at 1"
            m_ac = re.match(r"^([a-zA-Z0-9_]+)\s*:\s*.*=\s*\(\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)dB\s*,\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)[°\w]*\)", line_str, re.IGNORECASE)
            if m_ac:
                var_name = m_ac.group(1).lower()
                results[f"{var_name}_db"] = float(m_ac.group(2))
                results[f"{var_name}_phase"] = float(m_ac.group(3))
                results[var_name] = float(m_ac.group(2))
                continue

            # Case 2: Measurement finding target AT coordinate (WHEN / TRIG-TARG):
            # e.g.: "gbw: mag(V(out))=1.0 AT 829241.65123"
            # e.g.: "t_r1: V(out)=-5.0 AT 1.54609531534e-05"
            # e.g.: "vicr_pos: V(out)=13.0 AT 12.9996887593"
            m_at = re.match(r"^([a-zA-Z0-9_]+)\s*:\s*.*=\s*[-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s+AT\s+([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)$", line_str)
            if m_at:
                var_name = m_at.group(1).lower()
                results[var_name] = float(m_at.group(2))
                continue

            # Case 3: Standard measurement with "= <value> [at/from ...]":
            # e.g.: "isc_pos_ma: I(V_short1) * 1e3=30.6201696683 at 1"
            # e.g.: "e_noise_1k: V(inoise)=1.82682171612e-08 at 1000"
            # e.g.: "vom_pos: MAX(V(out))=12.9653360653 FROM -15 TO 15"
            # e.g.: "sr_pos: 10.0 / ((t_r2 - t_r1) * 1e6)=0.481910216843"
            m_val = re.match(r"^([a-zA-Z0-9_]+)\s*:\s*.*=\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)(?:\s+(?:at|from)\s+.*)?$", line_str, re.IGNORECASE)
            if m_val:
                var_name = m_val.group(1).lower()
                results[var_name] = float(m_val.group(2))
                continue

            # Case 4: Plain "name=val"
            m_direct = re.match(r"^([a-zA-Z0-9_]+)\s*[=:]\s*([-+]?[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?)$", line_str)
            if m_direct:
                var_name = m_direct.group(1).lower()
                results[var_name] = float(m_direct.group(2))

        return results
