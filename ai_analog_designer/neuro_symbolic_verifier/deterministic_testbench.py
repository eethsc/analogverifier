"""
Deterministic testbench library.

For every PLANNED spec item (see spec_planner.build_plan) a small, fixed LTspice deck is generated.
Each deck contains exactly one DUT instance, a fixture for ONE measurement kind under ONE set of
conditions, and `.meas` directives with fixed names. A Python extractor turns the raw `.meas` values
into the metric in its canonical unit. No LLM is involved, so a missing measurement is a verifier bug,
not a prompt failure.

Fixture conventions
  * V- (or GND / ANODE) is the global node 0.  V+ = VS.  Mid-supply node "mid" = VS/2.
  * Transient decks ramp the supply with a PWL over t_ramp (from the PDG schedule) and measure after settling.
  * Unused op-amp / comparator channels are parked as followers at mid.
"""
import os
import re
import math
import glob
import subprocess
import concurrent.futures as cf
from typing import Dict, Any, List, Optional, Tuple

from agent_engine.ltspice_runner import LTSPICE_EXE
from .units import spec_limits_in, fmt


# ----------------------------------------------------------------------------- DUT instantiation
def subckt_header(netlist: str, dut_name: Optional[str] = None) -> Tuple[str, List[str]]:
    """Returns (name, pins) of the DUT subcircuit: the one called `dut_name`, else the LAST .subckt in the
    file (helper/vendor subcircuits are defined before the top-level wrapper)."""
    heads = re.findall(r"^\s*\.subckt\s+(\S+)\s+(.+?)\s*$", netlist, re.M | re.I)
    if not heads:
        raise ValueError("netlist has no .subckt header")
    if dut_name:
        for n, p in heads:
            if n.lower() == dut_name.lower():
                return n, p.split()
    n, p = heads[-1]
    return n, p.split()


def instantiate(netlist: str, node_of: Dict[str, str], inst: str = "XDUT", dut_name: Optional[str] = None) -> str:
    """Builds the X-line, mapping subckt header pins to fixture nodes via sanitized pin names."""
    name, pins = subckt_header(netlist, dut_name)
    def key(p):
        return re.sub(r"[^A-Z0-9]", "", p.upper().replace("+", "P").replace("-", "N"))
    lut = {key(k): v for k, v in node_of.items()}
    nodes = []
    for p in pins:
        n = node_of.get(p) or lut.get(key(p))
        nodes.append(n if n else f"nc_{key(p)}")
    return f"{inst} {' '.join(nodes)} {name}"


# ----------------------------------------------------------------------------- deck assembly
class Deck:
    def __init__(self, key: str, kind: str, title: str, lines: List[str], analysis: str, meas: List[str],
                 extract, fixture: Dict[str, Any]):
        self.key, self.kind, self.title = key, kind, title
        self.lines, self.analysis, self.meas = lines, analysis, meas
        self.extract, self.fixture = extract, fixture

    def text(self, netlist: str) -> str:
        return "\n".join([f"* {self.title}", ".temp 25", ".options plotwinsize=0 numdgt=7",
                          netlist.strip(), "", *self.lines, "", self.analysis, *self.meas, ".end", ""])


def _r(v: float) -> str:
    return f"{v:.6g}"


def _ramp(vs: float, t_ramp: float) -> str:
    return f"PWL(0 0 {_r(t_ramp)} {_r(vs)})"


class FixtureContext:
    def __init__(self, netlist: str, plan: Dict[str, Any], schedule_us: Optional[Dict[str, float]] = None,
                 dut_name: Optional[str] = None):
        self.netlist = netlist
        self.dut_name = dut_name
        # Symmetric-supply fixtures (V- = -VS/2, mid = 0) for macromodels written for +/-15 V rails.
        self.split = os.environ.get("TB_SPLIT_SUPPLY") == "1"
        self.plan = plan
        self.pm = plan["pin_map"]
        self.cls = plan["device_class"]
        sched = schedule_us or {}
        self.t_ramp = min(max(5e-6, 1e-6 * max(sched.values(), default=10.0)), 200e-6)
        ramp = self.t_ramp if os.environ.get("TB_RAMP_SUPPLY") == "1" else 0.0
        self.t_settle = ramp + 100e-6
        self.t_end = self.t_settle + 50e-6

    # -------- helpers
    def park_unused(self, used_channel: Dict[str, str], node_of: Dict[str, str], lines: List[str], mid: str = "mid"):
        for i, ch in enumerate(self.pm.get("channels", [])):
            if ch is used_channel:
                continue
            node_of[ch["inp"]] = mid
            node_of[ch["inn"]] = f"park{i}"
            node_of[ch["out"]] = f"park{i}"
            lines.append(f"Rpark{i} park{i} {mid} 100k")

    def base_nodes(self, vs: float) -> Dict[str, str]:
        n = {}
        if self.pm.get("vpos"):
            n[self.pm["vpos"]] = "vp"
        if self.pm.get("vneg"):
            n[self.pm["vneg"]] = "vn" if self.split else "0"
        return n

    def _rails(self, vs: float, ac: bool) -> List[str]:
        if self.split:
            return [f"VPOS vp 0 {_r(vs / 2)}" + (" AC 1" if ac else ""),
                    f"VNEG vn 0 {_r(-vs / 2)}", "VMID mid 0 0"]
        return [f"VPOS vp 0 {_r(vs)}" + (" AC 1" if ac else ""), f"VMID mid 0 {_r(vs / 2)}"]

    def supply_tran(self, vs: float) -> List[str]:
        # Supply ramping (PDG start-up sequencing) is opt-in: several vendor macromodels (e.g. TI LM393) do not
        # converge through a 0->VS ramp in transient, so spec measurements use a DC supply by default.
        if os.environ.get("TB_RAMP_SUPPLY") == "1":
            if self.split:
                return [f"VPOS vp 0 {_ramp(vs / 2, self.t_ramp)}", f"VNEG vn 0 {_ramp(-vs / 2, self.t_ramp)}", "VMID mid 0 0"]
            return [f"VPOS vp 0 {_ramp(vs, self.t_ramp)}", f"VMID mid 0 {_ramp(vs / 2, self.t_ramp)}"]
        return self._rails(vs, False)

    def supply_dc(self, vs: float, ac_on_vp: bool = False) -> List[str]:
        return self._rails(vs, ac_on_vp)

    def tran(self) -> str:
        return f".tran 0 {_r(self.t_end)} 0 {_r(self.t_end / 2000)}"

    def win(self) -> str:
        return f"FROM {_r(self.t_settle)} TO {_r(self.t_end)}"

    def ch(self) -> Dict[str, str]:
        chans = self.pm.get("channels") or []
        if not chans:
            raise ValueError(f"no complete input/output channel found for class {self.cls}: {self.pm}")
        return chans[0]


# ============================================================================ OP-AMP / INAMP
def opamp_dc(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    rl = fx.get("rl") or 10e3
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + ["VSP mid inp 0", "VSN out inn 0", f"RL out mid {_r(rl)}", "CL out mid 20p"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    w = ctx.win()
    meas = [f".meas tran m_out AVG V(out) {w}", f".meas tran m_mid AVG V(mid) {w}",
            f".meas tran m_isp AVG I(VSP) {w}", f".meas tran m_isn AVG I(VSN) {w}",
            f".meas tran m_ipos AVG I(VPOS) {w}"]
    nch = max(1, len(ctx.pm.get("channels") or [1]))

    def extract(r):
        out = {}
        if "m_out" in r and "m_mid" in r:
            out["vos"] = r["m_out"] - r["m_mid"]
        ibp = r.get("m_isp")           # current into IN+ (VSP: mid -> inp, I flows mid->inp = into pin)
        ibn = r.get("m_isn")           # current into IN- (VSN: out -> inn)
        if ibp is not None and ibn is not None:
            out["ib"] = (ibp + ibn) / 2
            out["ios"] = abs(ibp - ibn)
        if "m_ipos" in r:
            out["iq_total"] = -r["m_ipos"]
            out["iq"] = -r["m_ipos"] / nch
        return out
    return Deck(key, "opamp_dc", f"follower DC: VOS/IB/IOS/IQ VS={vs}", lines, ctx.tran(), meas, extract, fx)


def opamp_openloop(ctx: FixtureContext, key: str, fx: Dict[str, Any], cm: bool = False) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    rl = fx.get("rl") or 10e3
    cl = fx.get("cl") or 20e-12
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_dc(vs)
    if cm:
        lines += ["VCM cmn mid AC 1", "VIN inp cmn 0", "LFB out inn 1G", "CFB inn cmn 1G"]
    else:
        lines += ["VIN inp mid AC 1", "LFB out inn 1G", "CFB inn mid 1G"]
    lines += [f"RL out mid {_r(rl)}", f"CL out mid {_r(cl)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [".meas ac m_a0 FIND V(out) AT 0.1",
            ".meas ac m_ugf WHEN mag(V(out))=1 FALL=1",
            ".meas ac m_ph FIND V(out) WHEN mag(V(out))=1 FALL=1"]

    def extract(r):
        out = {}
        if cm:
            if "m_a0_db" in r:
                out["acm_db"] = r["m_a0_db"]
            return out
        if "m_a0_db" in r:
            out["aol_db"] = r["m_a0_db"]
        if "m_ugf" in r:
            out["gbw"] = r["m_ugf"]
        if "m_ph_phase" in r:
            p = r["m_ph_phase"]
            # open-loop phase of a stable amp sits between -90 and -180 at UGF; unwrap 0..360 reports
            if p > 0:
                p -= 360
            out["pm"] = 180 + p
        return out
    return Deck(key, "opamp_cm" if cm else "opamp_ol", f"open-loop AC {'CM' if cm else 'DM'} VS={vs}", lines,
                ".ac dec 20 0.1 1G", meas, extract, fx)


def opamp_psrr(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "mid", ch["inn"]: "out", ch["out"]: "out"})
    lines = ctx.supply_dc(vs, ac_on_vp=True) + ["RL out mid 10k"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    f = fx.get("freq") or 10.0
    meas = [f".meas ac m_ps FIND V(out) AT {_r(f)}"]

    def extract(r):
        return {"psrr_db": -r["m_ps_db"]} if "m_ps_db" in r else {}
    return Deck(key, "opamp_psrr", f"PSRR follower VS={vs}", lines, ".ac dec 20 1 10Meg", meas, extract, fx)


def opamp_sr(ctx: FixtureContext, key: str, fx: Dict[str, Any], sr_hint: float = 1.0) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    rl = fx.get("rl") or 10e3
    cl = fx.get("cl") or 20e-12
    amp = min(max(vs / 4, 0.5), 5.0)
    t_sw = max(amp * 2 / max(sr_hint, 0.05) * 1e-6 * 10, 5e-6)   # 10x expected edge time
    t0 = ctx.t_settle
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "out", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [
        f"VIN inp mid PULSE({_r(-amp)} {_r(amp)} {_r(t0)} 10n 10n {_r(t_sw)} {_r(2 * t_sw)})",
        f"RL out mid {_r(rl)}", f"CL out mid {_r(cl)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    lo, hi = -amp * 0.6, amp * 0.6          # 20% / 80% of the 2*amp swing
    t_end = t0 + 2 * t_sw + 1e-6
    meas = [f".meas tran m_tr1 WHEN V(out,mid)={_r(lo)} RISE=1 TD={_r(t0)}",
            f".meas tran m_tr2 WHEN V(out,mid)={_r(hi)} RISE=1 TD={_r(t0)}",
            f".meas tran m_tf1 WHEN V(out,mid)={_r(hi)} FALL=1 TD={_r(t0 + t_sw)}",
            f".meas tran m_tf2 WHEN V(out,mid)={_r(lo)} FALL=1 TD={_r(t0 + t_sw)}"]

    def extract(r):
        out = {}
        if r.get("m_tr1") is not None and r.get("m_tr2") is not None and r["m_tr2"] > r["m_tr1"]:
            out["sr_rise"] = (hi - lo) / (r["m_tr2"] - r["m_tr1"]) * 1e-6
        if r.get("m_tf1") is not None and r.get("m_tf2") is not None and r["m_tf2"] > r["m_tf1"]:
            out["sr_fall"] = (hi - lo) / (r["m_tf2"] - r["m_tf1"]) * 1e-6
        if out:
            out["sr"] = min(out.values())
        return out
    return Deck(key, "opamp_sr", f"slew rate follower VS={vs}", lines,
                f".tran 0 {_r(t_end)} 0 {_r(t_sw / 2000)}", meas, extract, fx)


def opamp_swing(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    rail = fx.get("rail") or "pos"
    iout = fx.get("iout")
    rl = fx.get("rl")
    if iout:
        rl = max(1.0, (vs / 2) / abs(iout))
    rl = rl or 10e3
    ovd = 0.2 if rail == "pos" else -0.2
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "mid", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [f"VIN inp mid {_r(ovd)}", f"RL out mid {_r(rl)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    w = ctx.win()
    meas = [f".meas tran m_out AVG V(out) {w}", f".meas tran m_vp AVG V(vp) {w}"]
    if ctx.split:
        meas.append(f".meas tran m_vn AVG V(vn) {w}")

    def extract(r):
        if "m_out" not in r:
            return {}
        vn = r.get("m_vn", 0.0)
        if rail == "pos":
            return {"voh": r["m_out"], "vswing": r["m_vp"] - r["m_out"], "headroom_pos": r["m_vp"] - r["m_out"]}
        return {"vol": r["m_out"], "vswing": r["m_out"] - vn, "headroom_neg": r["m_out"] - vn}
    return Deck(key, "opamp_swing", f"output swing {rail} rail RL={rl:.4g} VS={vs}", lines, ctx.tran(), meas, extract, fx)


def opamp_iout(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    """Output current at a forced output voltage (datasheet: VO = V+ or V-, VID = +-1 V)."""
    vs, ch = fx["vs"], ctx.ch()
    at = fx.get("vo_at") or "pos"
    off = fx.get("vo_offset") or 0.0
    vp, vn = (vs / 2, -vs / 2) if ctx.split else (vs, 0.0)
    vforce = (vp if at == "pos" else vn + off)
    vid = 1.0 if at == "pos" else -1.0
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "mid", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [f"VIN inp mid {_r(vid)}", f"VFORCE out 0 {_r(vforce)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_i AVG I(VFORCE) {ctx.win()}"]

    def extract(r):
        return {"iout": abs(r["m_i"])} if "m_i" in r else {}
    return Deck(key, "opamp_iout", f"output current at VO={at} rail VS={vs}", lines, ctx.tran(), meas, extract, fx)


def opamp_isc(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    rail = fx.get("rail") or "pos"
    ovd = 1.0 if rail == "pos" else -1.0
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "mid", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [f"VIN inp mid {_r(ovd)}", "VSHORT out mid 0"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_isc AVG I(VSHORT) {ctx.win()}"]

    def extract(r):
        return {"isc": abs(r["m_isc"]), "iout": abs(r["m_isc"])} if "m_isc" in r else {}
    return Deck(key, "opamp_isc", f"short-circuit current {rail} VS={vs}", lines, ctx.tran(), meas, extract, fx)


def opamp_noise(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "out", ch["out"]: "out"})
    lines = ctx.supply_dc(vs) + ["VIN inp mid 0", "RL out mid 10k"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    f = fx.get("freq") or 1000.0
    meas = [f".meas noise m_en FIND V(inoise) AT {_r(f)}"]

    def extract(r):
        return {"en": r["m_en"] * 1e9} if r.get("m_en") is not None else {}
    return Deck(key, "opamp_noise", f"input noise density VS={vs}", lines, ".noise V(out) VIN dec 10 1 1Meg", meas, extract, fx)


# ============================================================================ COMPARATOR
def _cmp_pullup(fx, vs):
    return fx.get("rl") or 5.1e3


def cmp_vos(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    vref = 1.4 if vs >= 3 else vs / 2
    t0, span, tr = ctx.t_settle, 0.05, 400e-6
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [f"VREF inn 0 {_r(vref)}",
                                   f"VIN inp 0 PWL(0 {_r(vref - span)} {_r(t0)} {_r(vref - span)} {_r(t0 + tr)} {_r(vref + span)})",
                                   f"RPU out vp {_r(_cmp_pullup(fx, vs))}", "CL out 0 15p"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_vin FIND V(inp,inn) WHEN V(out)={_r(vs / 2)} RISE=1 TD={_r(t0)}"]

    def extract(r):
        return {"vos": r["m_vin"]} if r.get("m_vin") is not None else {}
    return Deck(key, "cmp_vos", f"comparator VOS ramp VS={vs}", lines,
                f".tran 0 {_r(t0 + tr + 10e-6)} 0 {_r(tr / 40000)}", meas, extract, fx)


def cmp_ib(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    vcm = 1.4 if vs >= 3 else vs / 2
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [f"VCM cm 0 {_r(vcm)}", "VSP cm inp 0", "VSN cm inn 0",
                                   f"RPU out vp {_r(_cmp_pullup(fx, vs))}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    w = ctx.win()
    meas = [f".meas tran m_isp AVG I(VSP) {w}", f".meas tran m_isn AVG I(VSN) {w}", f".meas tran m_ipos AVG I(VPOS) {w}"]

    def extract(r):
        out = {}
        if "m_isp" in r and "m_isn" in r:
            out["ib"] = (r["m_isp"] + r["m_isn"]) / 2
            out["ios"] = abs(r["m_isp"] - r["m_isn"])
        return out
    return Deck(key, "cmp_ib", f"comparator IB/IOS VS={vs}", lines, ctx.tran(), meas, extract, fx)


def cmp_avd(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx.get("vs") or 15.0, ctx.ch()
    rl = fx.get("rl") or 15e3
    vref = 1.4
    t0, span, tr = ctx.t_settle, 0.02, 300e-6
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [f"VREF inn 0 {_r(vref)}",
                                   f"VIN inp 0 PWL(0 {_r(vref + span)} {_r(t0)} {_r(vref + span)} {_r(t0 + tr)} {_r(vref - span)})",
                                   f"RPU out vp {_r(rl)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    vlo, vhi = 1.4, min(11.4, vs - 1.0)
    meas = [f".meas tran m_v1 FIND V(inp,inn) WHEN V(out)={_r(vhi)} FALL=1 TD={_r(t0)}",
            f".meas tran m_v2 FIND V(inp,inn) WHEN V(out)={_r(vlo)} FALL=1 TD={_r(t0)}"]

    def extract(r):
        if r.get("m_v1") is None or r.get("m_v2") is None:
            return {}
        d = abs(r["m_v1"] - r["m_v2"])
        return {"avd_db": 20 * math.log10((vhi - vlo) / d)} if d > 0 else {"avd_db": 200.0}
    return Deck(key, "cmp_avd", f"comparator AVD VS={vs}", lines, f".tran 0 {_r(t0 + tr + 10e-6)} 0 {_r(tr / 30000)}", meas, extract, fx)


def cmp_vol(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    iol = fx.get("iout") or 4e-3
    vid = fx.get("vid") or -1.0
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + ["VCM inn 0 1.4", f"VID inp inn {_r(-abs(vid))}", f"ISINK vp out {_r(abs(iol))}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_vol AVG V(out) {ctx.win()}"]

    def extract(r):
        return {"vol": r["m_vol"]} if "m_vol" in r else {}
    return Deck(key, "cmp_vol", f"comparator VOL @IOL={iol:.3g} VS={vs}", lines, ctx.tran(), meas, extract, fx)


def cmp_iol(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    vo = fx.get("vo") or 1.5
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + ["VCM inn 0 1.4", "VID inp inn -1", f"VFORCE out 0 {_r(vo)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_i AVG I(VFORCE) {ctx.win()}"]

    def extract(r):
        return {"iol": -r["m_i"]} if "m_i" in r else {}
    return Deck(key, "cmp_iol", f"comparator IOL @VOL={vo} VS={vs}", lines, ctx.tran(), meas, extract, fx)


def cmp_ioh(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    vo = fx.get("vo") or vs
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + ["VCM inn 0 1.4", "VID inp inn 1", f"VFORCE out 0 {_r(vo)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_i AVG I(VFORCE) {ctx.win()}"]

    def extract(r):
        return {"ioh": abs(r["m_i"])} if "m_i" in r else {}
    return Deck(key, "cmp_ioh", f"comparator IOH @VOH={vo} VS={vs}", lines, ctx.tran(), meas, extract, fx)


def cmp_icc(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + ["VCM inn 0 1.4", "VID inp inn -1"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_ipos AVG I(VPOS) {ctx.win()}"]

    def extract(r):
        return {"iq": -r["m_ipos"], "iq_total": -r["m_ipos"]} if "m_ipos" in r else {}
    return Deck(key, "cmp_icc", f"comparator ICC VS={vs}", lines, ctx.tran(), meas, extract, fx)


def cmp_tresp(ctx: FixtureContext, key: str, fx: Dict[str, Any]) -> Deck:
    vs, ch = fx["vs"], ctx.ch()
    rpu = fx.get("rl") or 5.1e3
    cl = fx.get("cl") or 15e-12
    step, ovd = 0.1, 0.005
    t0 = ctx.t_settle
    n = ctx.base_nodes(vs); n.update({ch["inp"]: "inp", ch["inn"]: "inn", ch["out"]: "out"})
    lines = ctx.supply_tran(vs) + [f"VREF inn 0 {_r(1.4 + step - ovd)}",
                                   f"VIN inp 0 PULSE(1.4 {_r(1.4 + step)} {_r(t0)} 10n 10n 20u 40u)",
                                   f"RPU out vp {_r(rpu)}", f"CL out 0 {_r(cl)}"]
    ctx.park_unused(ch, n, lines)
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_t WHEN V(out)={_r(vs / 2)} RISE=1 TD={_r(t0)}"]

    def extract(r):
        return {"tresp": r["m_t"] - t0} if r.get("m_t") is not None else {}
    return Deck(key, "cmp_tresp", f"comparator response time VS={vs}", lines, f".tran 0 {_r(t0 + 25e-6)} 0 5n", meas, extract, fx)


# ============================================================================ 555 TIMER
def _t(ctx: FixtureContext) -> Dict[str, str]:
    t = ctx.pm.get("timer") or {}
    for k in ("trig", "thres", "out"):
        if k not in t:
            raise ValueError(f"timer pin '{k}' not found in pin map {t}")
    return t


def timer_nodes(ctx, vs):
    t = _t(ctx)
    n = ctx.base_nodes(vs)
    n.update({t["trig"]: "trig", t["thres"]: "thres", t["out"]: "out"})
    if "reset" in t:
        n[t["reset"]] = "rst"
    if "cont" in t:
        n[t["cont"]] = "cont"
    if "disch" in t:
        n[t["disch"]] = "disch"
    return n, t


def timer_common(ctx, vs, rst="vp"):
    lines = ctx.supply_tran(vs)
    lines.append(f"RRST rst {rst} 1" if rst == "vp" else f"VRST rst 0 {rst}")
    lines.append("CCONT cont 0 10n")
    return lines


def t555_vth(ctx, key, fx):
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    t0, tr = ctx.t_settle, 500e-6
    lines = timer_common(ctx, vs) + [
        f"VTRIG trig 0 PWL(0 {_r(vs)} {_r(t0)} {_r(vs)} {_r(t0 + 1e-6)} 0 {_r(t0 + 5e-6)} 0 {_r(t0 + 6e-6)} {_r(vs)})",
        f"VTHR thres 0 PWL(0 0 {_r(t0 + 10e-6)} 0 {_r(t0 + 10e-6 + tr)} {_r(vs)})",
        "RL out 0 10k", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [f".meas tran m_v FIND V(thres) WHEN V(out)={_r(vs / 2)} FALL=1 TD={_r(t0 + 8e-6)}"]
    return Deck(key, "t555_vth", f"555 VTH ramp VS={vs}", lines, f".tran 0 {_r(t0 + tr + 20e-6)} 0 {_r(tr / 5000)}", meas,
                lambda r: {"vth": r["m_v"]} if r.get("m_v") is not None else {}, fx)


def t555_vtrig(ctx, key, fx):
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    t0, tr = ctx.t_settle, 500e-6
    lines = timer_common(ctx, vs) + [
        f"VTHR thres 0 PWL(0 0 {_r(t0)} 0 {_r(t0 + 1e-6)} {_r(vs)} {_r(t0 + 5e-6)} {_r(vs)} {_r(t0 + 6e-6)} 0)",
        f"VTRIG trig 0 PWL(0 {_r(vs)} {_r(t0 + 10e-6)} {_r(vs)} {_r(t0 + 10e-6 + tr)} 0)",
        "RL out 0 10k", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [f".meas tran m_v FIND V(trig) WHEN V(out)={_r(vs / 2)} RISE=1 TD={_r(t0 + 8e-6)}"]
    return Deck(key, "t555_vtrig", f"555 VTRIG ramp VS={vs}", lines, f".tran 0 {_r(t0 + tr + 20e-6)} 0 {_r(tr / 5000)}", meas,
                lambda r: {"vtrig": r["m_v"]} if r.get("m_v") is not None else {}, fx)


def t555_vreset(ctx, key, fx):
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    t0, tr = ctx.t_settle, 500e-6
    lines = ctx.supply_tran(vs) + ["CCONT cont 0 10n", "VTHR thres 0 0", "VTRIG trig 0 0",
                                   f"VRST rst 0 PWL(0 {_r(vs)} {_r(t0)} {_r(vs)} {_r(t0 + tr)} 0)",
                                   "RL out 0 10k", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [f".meas tran m_v FIND V(rst) WHEN V(out)={_r(vs / 2)} FALL=1 TD={_r(t0)}"]
    return Deck(key, "t555_vreset", f"555 VRESET ramp VS={vs}", lines, f".tran 0 {_r(t0 + tr + 20e-6)} 0 {_r(tr / 5000)}", meas,
                lambda r: {"vreset": r["m_v"]} if r.get("m_v") is not None else {}, fx)


def t555_vcont(ctx, key, fx):
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    lines = ctx.supply_tran(vs) + ["RRST rst vp 1", "VTHR thres 0 0", "VTRIG trig vp 0", "RCONT cont 0 1G",
                                   "RL out 0 10k", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [f".meas tran m_v AVG V(cont) {ctx.win()}"]
    return Deck(key, "t555_vcont", f"555 VCONT VS={vs}", lines, ctx.tran(), meas,
                lambda r: {"vcont": r["m_v"]} if "m_v" in r else {}, fx)


def t555_state(ctx, key, fx, state, kind):
    """Output forced LOW (thres=VCC) or HIGH (trig=0); VOL/VOH under load current, ICC."""
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    io = fx.get("iout")
    lines = timer_common(ctx, vs)
    if state == "low":
        lines += [f"VTHR thres 0 {_r(vs)}", f"VTRIG trig 0 {_r(vs)}"]
    else:
        lines += ["VTHR thres 0 0", "VTRIG trig 0 0"]
    if kind in ("vol555", "voh555") and io:
        rl = max(0.5, (vs / 2) / abs(io))
        lines.append(f"RL out {'vp' if state == 'low' else '0'} {_r(rl)}")
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    w = ctx.win()
    meas = [f".meas tran m_out AVG V(out) {w}", f".meas tran m_ipos AVG I(VPOS) {w}"]

    def extract(r):
        out = {}
        if "m_out" in r:
            out["vol555" if state == "low" else "voh555"] = r["m_out"]
        if "m_ipos" in r:
            out["iq"] = -r["m_ipos"]
        return out
    return Deck(key, f"t555_{state}", f"555 output {state} VS={vs} IO={io}", lines, ctx.tran(), meas, extract, fx)


def t555_pin_current(ctx, key, fx, kind):
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    lines = ctx.supply_tran(vs) + ["CCONT cont 0 10n", "RL out 0 10k"]
    if kind == "ith":
        lines += ["RRST rst vp 1", "VTRIG trig vp 0", f"VTHR thres_s 0 {_r(vs * 2 / 3 + 0.3)}", "VSENSE thres_s thres 0"]
        src = "VSENSE"
    elif kind == "itrig":
        lines += ["RRST rst vp 1", "VTHR thres 0 0", "VTRIG trig_s 0 0", "VSENSE trig_s trig 0"]
        src = "VSENSE"
    else:  # ireset
        lvl = fx.get("rst_level", vs)
        lines += ["VTHR thres 0 0", "VTRIG trig vp 0", f"VRST rst_s 0 {_r(lvl)}", "VSENSE rst_s rst 0"]
        src = "VSENSE"
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    meas = [f".meas tran m_i AVG I({src}) {ctx.win()}"]
    return Deck(key, f"t555_{kind}", f"555 {kind} VS={vs}", lines, ctx.tran(), meas,
                lambda r: {kind: r["m_i"]} if "m_i" in r else {}, fx)


def t555_disch(ctx, key, fx, kind):
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    if "disch" not in t:
        raise ValueError("no DISCH pin")
    io = fx.get("iout") or 8e-3
    lines = timer_common(ctx, vs) + ["RL out 0 10k"]
    if kind == "vdisch_on":
        lines += [f"VTHR thres 0 {_r(vs)}", f"VTRIG trig 0 {_r(vs)}", f"IDIS vp disch {_r(io)}"]
        meas = [f".meas tran m_v AVG V(disch) {ctx.win()}"]
        ex = lambda r: {"vdisch_on": r["m_v"]} if "m_v" in r else {}
    else:
        lines += ["VTHR thres 0 0", "VTRIG trig 0 0", f"VDIS disch 0 {_r(vs)}"]
        meas = [f".meas tran m_i AVG I(VDIS) {ctx.win()}"]
        ex = lambda r: {"idisch_off": abs(r["m_i"])} if "m_i" in r else {}
    lines.append(instantiate(ctx.netlist, n, dut_name=ctx.dut_name))
    return Deck(key, f"t555_{kind}", f"555 {kind} VS={vs}", lines, ctx.tran(), meas, ex, fx)


def t555_edges(ctx, key, fx):
    vs = fx["vs"]; n, t = timer_nodes(ctx, vs)
    cl = fx.get("cl") or 15e-12
    t0 = ctx.t_settle
    # Output is first forced LOW (THRES high), released at t0+20u, then TRIG is pulled low at t0+30u so the
    # output makes a clean LOW->HIGH edge; THRES is raised again at t0+60u for the HIGH->LOW edge.
    lines = timer_common(ctx, vs) + [
        f"VTRIG trig 0 PWL(0 {_r(vs)} {_r(t0 + 30e-6)} {_r(vs)} {_r(t0 + 30.01e-6)} 0 {_r(t0 + 34e-6)} 0 {_r(t0 + 34.01e-6)} {_r(vs)})",
        f"VTHR thres 0 PWL(0 {_r(vs)} {_r(t0 + 20e-6)} {_r(vs)} {_r(t0 + 20.01e-6)} 0 {_r(t0 + 60e-6)} 0 {_r(t0 + 60.01e-6)} {_r(vs)})",
        f"CL out 0 {_r(cl)}", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    lo, hi = 0.2 * vs, 0.8 * vs
    meas = [f".meas tran m_r1 WHEN V(out)={_r(lo)} RISE=1 TD={_r(t0 + 30e-6)}", f".meas tran m_r2 WHEN V(out)={_r(hi)} RISE=1 TD={_r(t0 + 30e-6)}",
            f".meas tran m_f1 WHEN V(out)={_r(hi)} FALL=1 TD={_r(t0 + 60e-6)}", f".meas tran m_f2 WHEN V(out)={_r(lo)} FALL=1 TD={_r(t0 + 60e-6)}"]

    def extract(r):
        out = {}
        if r.get("m_r1") is not None and r.get("m_r2") is not None:
            out["tr555"] = r["m_r2"] - r["m_r1"]
        if r.get("m_f1") is not None and r.get("m_f2") is not None:
            out["tf555"] = r["m_f2"] - r["m_f1"]
        return out
    return Deck(key, "t555_edges", f"555 rise/fall VS={vs}", lines, f".tran 0 {_r(t0 + 80e-6)} 0 5n", meas, extract, fx)


# ============================================================================ SHUNT REFERENCE (TL431)
def _s(ctx):
    s = ctx.pm.get("shunt") or {}
    if not {"cathode", "ref", "anode"} <= set(s):
        raise ValueError(f"shunt pins incomplete: {s}")
    return s


def shunt_vref(ctx, key, fx):
    s = _s(ctx); n = {s["cathode"]: "k", s["ref"]: "k", s["anode"]: "0"}
    ika = fx.get("iout") or 10e-3
    lines = [f"IKA k 0 DC {_r(-ika)}" , "VDC vsrc 0 20", f"RK vsrc k {_r((20 - 2.5) / ika)}"]
    lines = ["VDC vsrc 0 20", f"RK vsrc k {_r((20 - 2.5) / ika)}", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas tran m_v AVG V(k) FROM 150u TO 200u"]
    return Deck(key, "shunt_vref", f"TL431 Vref IKA={ika}", lines, ".tran 0 200u 0 100n", meas,
                lambda r: {"vref": r["m_v"]} if "m_v" in r else {}, fx)


def shunt_dvref(ctx, key, fx):
    s = _s(ctx); n = {s["cathode"]: "k", s["ref"]: "ref", s["anode"]: "0"}
    vka = fx.get("vka") or 10.0
    r1 = 10e3
    r2 = r1 * 2.495 / (vka - 2.495)
    lines = ["VDC vsrc 0 40", f"RK vsrc k {_r((40 - vka) / 10e-3)}", f"R1 k ref {_r(r1)}", f"R2 ref 0 {_r(r2)}",
             instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas tran m_vref AVG V(ref) FROM 150u TO 200u", ".meas tran m_vka AVG V(k) FROM 150u TO 200u"]
    return Deck(key, "shunt_dvref", f"TL431 Vref at VKA={vka}", lines, ".tran 0 200u 0 100n", meas,
                lambda r: {"vref_at_vka": r["m_vref"], "vka": r["m_vka"]} if "m_vref" in r else {}, fx)


def shunt_iref(ctx, key, fx):
    s = _s(ctx); n = {s["cathode"]: "k", s["ref"]: "ref", s["anode"]: "0"}
    lines = ["VDC vsrc 0 20", f"RK vsrc k {_r((20 - 5) / 10e-3)}", "R1 k r1 10k", "VSENSE r1 ref 0", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas tran m_i AVG I(VSENSE) FROM 150u TO 200u"]
    return Deck(key, "shunt_iref", "TL431 Iref", lines, ".tran 0 200u 0 100n", meas,
                lambda r: {"iref": abs(r["m_i"])} if "m_i" in r else {}, fx)


def shunt_ioff(ctx, key, fx):
    s = _s(ctx); n = {s["cathode"]: "k", s["ref"]: "0", s["anode"]: "0"}
    lines = ["VKA k 0 36", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas tran m_i AVG I(VKA) FROM 150u TO 200u"]
    return Deck(key, "shunt_ioff", "TL431 Ioff", lines, ".tran 0 200u 0 100n", meas,
                lambda r: {"ioff": abs(r["m_i"])} if "m_i" in r else {}, fx)


def shunt_imin(ctx, key, fx):
    s = _s(ctx); n = {s["cathode"]: "k", s["ref"]: "k", s["anode"]: "0"}
    lines = ["IKA 0 k PWL(0 0 1m 2m)", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas tran m_v2m FIND V(k) AT 0.99m", ".meas tran m_t WHEN V(k)=2.45 FALL=1"]

    def extract(r):
        return {"imin": r["m_t"] * 2.0} if r.get("m_t") is not None else {}   # I = 2 A/s * t
    return Deck(key, "shunt_imin", "TL431 Imin", lines, ".tran 0 1m 0 200n", meas, extract, fx)


def shunt_zka(ctx, key, fx):
    s = _s(ctx); n = {s["cathode"]: "k", s["ref"]: "k", s["anode"]: "0"}
    lines = ["IKA 0 k DC 10m AC 1m", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas ac m_z FIND V(k) AT 1k"]
    return Deck(key, "shunt_zka", "TL431 |ZKA|", lines, ".ac dec 10 10 100k", meas,
                lambda r: {"zka": r["m_z_mag"] / 1e-3} if "m_z_mag" in r else {}, fx)


# ============================================================================ LDO (LM317)
def _l(ctx):
    l = ctx.pm.get("ldo") or {}
    if not {"in", "out", "adj"} <= set(l):
        raise ValueError(f"ldo pins incomplete: {l}")
    return l


def ldo_base(ctx, fx, vin=None, iload=10e-3, extra=None):
    l = _l(ctx); n = {l["in"]: "vin", l["out"]: "out", l["adj"]: "adj"}
    vout = 5.0
    r1, r2 = 240.0, 720.0
    vin = vin or (vout + 5.0)
    lines = [f"VIN vin 0 {_r(vin)}", f"R1 out adj_s {_r(r1)}", "VSENSE adj_s adj 0", f"R2 adj 0 {_r(r2)}",
             f"ILOAD out 0 {_r(iload)}", "CIN vin 0 100n", "COUT out 0 1u"] + (extra or []) + [instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    return lines


def ldo_dc(ctx, key, fx):
    lines = ldo_base(ctx, fx)
    meas = [".meas tran m_vo AVG V(out) FROM 1.5m TO 2m", ".meas tran m_vadj AVG V(adj) FROM 1.5m TO 2m",
            ".meas tran m_iadj AVG I(VSENSE) FROM 1.5m TO 2m"]

    def extract(r):
        o = {}
        if "m_vo" in r and "m_vadj" in r:
            o["vref_ldo"] = r["m_vo"] - r["m_vadj"]
        if "m_iadj" in r:
            o["iadj"] = abs(r["m_iadj"])
        return o
    return Deck(key, "ldo_dc", "LM317 VREF/IADJ", lines, ".tran 0 2m 0 1u", meas, extract, fx)


def ldo_line(ctx, key, fx):
    lines = ldo_base(ctx, fx)
    lines = [l if not l.startswith("VIN ") else "VIN vin 0 8" for l in lines]
    meas = [".meas dc m_v1 FIND V(out) AT 8", ".meas dc m_v2 FIND V(out) AT 45"]

    def extract(r):
        if "m_v1" in r and "m_v2" in r and r["m_v1"]:
            return {"line_reg": abs(r["m_v2"] - r["m_v1"]) / r["m_v1"] * 100 / (45 - 8)}
        return {}
    return Deck(key, "ldo_line", "LM317 line regulation", lines, ".dc VIN 8 45 1", meas, extract, fx)


def ldo_load(ctx, key, fx):
    lines = ldo_base(ctx, fx, vin=10.0)
    meas = [".meas dc m_v1 FIND V(out) AT 10m", ".meas dc m_v2 FIND V(out) AT 1.5"]

    def extract(r):
        if "m_v1" in r and "m_v2" in r:
            return {"load_reg": abs(r["m_v1"] - r["m_v2"]), "load_reg_pct": abs(r["m_v1"] - r["m_v2"]) / r["m_v1"] * 100}
        return {}
    return Deck(key, "ldo_load", "LM317 load regulation", lines, ".dc ILOAD 10m 1.5 10m", meas, extract, fx)


def ldo_ilim(ctx, key, fx):
    l = _l(ctx); n = {l["in"]: "vin", l["out"]: "out", l["adj"]: "0"}
    vdiff = 15.0
    lines = [f"VIN vin 0 {_r(vdiff)}", "VSHORT out 0 0", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas tran m_i AVG I(VSHORT) FROM 1.5m TO 2m"]
    return Deck(key, "ldo_ilim", "LM317 current limit", lines, ".tran 0 2m 0 1u", meas,
                lambda r: {"ilim": abs(r["m_i"])} if "m_i" in r else {}, fx)


def ldo_psrr(ctx, key, fx):
    l = _l(ctx); n = {l["in"]: "vin", l["out"]: "out", l["adj"]: "adj"}
    r1, r2 = 240.0, 1680.0   # VOUT = 10 V
    lines = ["VIN vin 0 15 AC 1", f"R1 out adj {_r(r1)}", f"R2 adj 0 {_r(r2)}", "CADJ adj 0 10u", "ILOAD out 0 10m",
             "COUT out 0 1u", instantiate(ctx.netlist, n, dut_name=ctx.dut_name)]
    meas = [".meas ac m_v FIND V(out) AT 120"]
    return Deck(key, "ldo_psrr", "LM317 ripple rejection 120Hz", lines, ".ac dec 10 10 10k", meas,
                lambda r: {"psrr_ldo_db": -r["m_v_db"]} if "m_v_db" in r else {}, fx)


# ============================================================================ dispatcher
def build_decks(ctx: FixtureContext) -> Tuple[Dict[str, Deck], Dict[str, str]]:
    """Returns ({fixture_key: Deck}, {fixture_key: error}) for all PLANNED items of the plan."""
    decks: Dict[str, Deck] = {}
    errors: Dict[str, str] = {}
    sr_hint = 1.0
    for it in ctx.plan["items"]:
        if it.get("kind") == "sr" and it.get("typ"):
            sr_hint = float(it["typ"])
    for it in ctx.plan["items"]:
        if it["status"] != "PLANNED":
            continue
        key, kind, fx = it["fixture_key"], it["kind"], it["fixture"]
        if key in decks or key in errors:
            continue
        try:
            if ctx.cls == "comparator" and kind == "vos":
                d = cmp_vos(ctx, key, fx)
            elif ctx.cls == "comparator" and kind in ("ib", "ios"):
                d = cmp_ib(ctx, key, fx)
            elif ctx.cls == "comparator" and kind == "iq":
                d = cmp_icc(ctx, key, fx)
            elif ctx.cls == "timer" and kind == "iq":
                d = t555_state(ctx, key, fx, fx.get("state") or "low", kind)
            elif kind in ("vos", "ib", "ios", "iq") and ctx.cls in ("opamp", "inamp", "audio_amp"):
                d = opamp_dc(ctx, key, fx)
            elif kind in ("aol_db", "gbw", "pm"):
                d = opamp_openloop(ctx, key, fx)
            elif kind == "cmrr_db":
                d = opamp_openloop(ctx, key, fx, cm=True)
            elif kind == "psrr_db":
                d = opamp_psrr(ctx, key, fx)
            elif kind == "sr":
                d = opamp_sr(ctx, key, fx, sr_hint)
            elif kind == "vswing":
                d = opamp_swing(ctx, key, fx)
            elif kind in ("isc", "iout"):
                d = opamp_iout(ctx, key, fx) if (kind == "iout" and fx.get("vo_at")) else opamp_isc(ctx, key, fx)
            elif kind == "en":
                d = opamp_noise(ctx, key, fx)
            elif kind == "vos" and ctx.cls == "comparator":
                d = cmp_vos(ctx, key, fx)
            elif kind in ("ib", "ios") and ctx.cls == "comparator":
                d = cmp_ib(ctx, key, fx)
            elif kind == "avd_db":
                d = cmp_avd(ctx, key, fx)
            elif kind == "vol" and ctx.cls == "comparator":
                d = cmp_vol(ctx, key, fx)
            elif kind == "iol":
                d = cmp_iol(ctx, key, fx)
            elif kind == "ioh":
                d = cmp_ioh(ctx, key, fx)
            elif kind == "iq" and ctx.cls == "comparator":
                d = cmp_icc(ctx, key, fx)
            elif kind == "tresp":
                d = cmp_tresp(ctx, key, fx)
            elif kind == "vth":
                d = t555_vth(ctx, key, fx)
            elif kind == "vtrig":
                d = t555_vtrig(ctx, key, fx)
            elif kind == "vreset":
                d = t555_vreset(ctx, key, fx)
            elif kind == "vcont":
                d = t555_vcont(ctx, key, fx)
            elif kind in ("vol555", "voh555") or (kind == "iq" and ctx.cls == "timer"):
                state = fx.get("state") or ("low" if kind == "vol555" else "high" if kind == "voh555" else "low")
                d = t555_state(ctx, key, fx, state, kind)
            elif kind in ("ith", "itrig", "ireset"):
                d = t555_pin_current(ctx, key, fx, kind)
            elif kind in ("vdisch_on", "idisch_off"):
                d = t555_disch(ctx, key, fx, kind)
            elif kind in ("tr555", "tf555"):
                d = t555_edges(ctx, key, fx)
            elif kind == "vref":
                d = shunt_vref(ctx, key, fx)
            elif kind == "dvref_dvka":
                d = shunt_dvref(ctx, key, fx)
            elif kind == "iref":
                d = shunt_iref(ctx, key, fx)
            elif kind == "ioff":
                d = shunt_ioff(ctx, key, fx)
            elif kind == "imin":
                d = shunt_imin(ctx, key, fx)
            elif kind == "zka":
                d = shunt_zka(ctx, key, fx)
            elif kind in ("vref_ldo", "iadj"):
                d = ldo_dc(ctx, key, fx)
            elif kind == "line_reg":
                d = ldo_line(ctx, key, fx)
            elif kind == "load_reg":
                d = ldo_load(ctx, key, fx)
            elif kind == "ilim":
                d = ldo_ilim(ctx, key, fx)
            elif kind == "psrr_ldo_db":
                d = ldo_psrr(ctx, key, fx)
            else:
                errors[key] = f"no fixture implemented for kind '{kind}' (class {ctx.cls})"
                continue
            decks[key] = d
        except Exception as e:
            errors[key] = f"{type(e).__name__}: {e}"
    return decks, errors


# ============================================================================ LTspice execution
_NUM = r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?"


def parse_meas_log(path: str) -> Dict[str, float]:
    res: Dict[str, float] = {}
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()
    for line in text.splitlines():
        s = line.strip()
        m = re.match(rf"^([a-zA-Z_][\w]*)\s*:\s*.*?=\s*\(\s*({_NUM})dB\s*,\s*({_NUM})°?\s*\)(?:\s+at\s+({_NUM}))?", s, re.I)
        if m:
            name = m.group(1).lower()
            db, ph = float(m.group(2)), float(m.group(3))
            res[name + "_db"] = db
            res[name + "_mag"] = 10 ** (db / 20)
            res[name + "_phase"] = ph
            res[name] = db
            if m.group(4):
                res[name + "_at"] = float(m.group(4))
            continue
        m = re.match(rf"^([a-zA-Z_][\w]*)\s*:\s*.*?=\s*({_NUM})\s+AT\s+({_NUM})\s*$", s)
        if m:
            res[m.group(1).lower()] = float(m.group(3))       # WHEN-type: the x coordinate is the result
            res[m.group(1).lower() + "_value"] = float(m.group(2))
            continue
        m = re.match(rf"^([a-zA-Z_][\w]*)\s*:\s*.*?=\s*({_NUM})(?:\s+(?:at|from)\s+.*)?$", s, re.I)
        if m:
            res[m.group(1).lower()] = float(m.group(2))
            continue
        m = re.match(rf"^([a-zA-Z_][\w]*)\s*=\s*({_NUM})\s*$", s)
        if m and not s.lower().startswith(("tnom", "temp")):
            res[m.group(1).lower()] = float(m.group(2))
    return res


_ERR_PATTERNS = re.compile(
    r"(?i)(fatal error|^\s*error\b|singular matrix|unknown subcircuit|can't find definition|could not find|"
    r"time step too small|expected .{0,60} here|unknown parameter|undefined|voltage not found|current not found|"
    r"syntax error|iteration limit reached|simulation failed|is not a valid)")


def ltspice_errors(log_text: str) -> List[str]:
    """Extracts LTspice error lines together with the offending netlist line that follows them."""
    out: List[str] = []
    lines = log_text.splitlines()
    for i, ln in enumerate(lines):
        low = ln.strip().lower()
        if not low or low.startswith(("warning", "gmin", "source step", "changing tseed")):
            continue
        if _ERR_PATTERNS.search(ln):
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            ctx = nxt if nxt and not nxt.startswith("^") else ""
            msg = re.sub(r"^.*?\.cir\(\d+\):\s*", "", ln.strip())
            out.append((msg + (f" | {ctx}" if ctx else ""))[:240])
    return list(dict.fromkeys(out))


def compile_check(netlist: str, plan: Dict[str, Any], out_dir: str, dut_name: Optional[str] = None,
                  vs: Optional[float] = None) -> Dict[str, Any]:
    """Single .op of the DUT with every pin tied to ground through 1 GOhm and the supply applied.
    Fails fast on syntax / compile / operating-point problems so 50+ fixtures are not run on a broken netlist."""
    os.makedirs(out_dir, exist_ok=True)
    name, pins = subckt_header(netlist, dut_name)
    pm = plan.get("pin_map", {})
    vs = vs or plan.get("vs_default") or 5.0
    nodes = [f"n_{re.sub(r'[^A-Za-z0-9_]', '_', p.replace('+', 'p').replace('-', 'n'))}" for p in pins]
    lines = [f"* compile check for {name}", ".temp 25", netlist.strip(), ""]
    lines += [f"R_{idx}_{n} {n} 0 1G" for idx, n in enumerate(nodes, 1)]
    for p, n in zip(pins, nodes):
        if pm.get("vpos") and p == pm["vpos"]:
            lines.append(f"VPOS {n} 0 {_r(vs)}")
        if pm.get("vneg") and p == pm["vneg"]:
            lines.append(f"VNEG {n} 0 0")
    lines += [f"XDUT {' '.join(nodes)} {name}", ".op", ".end", ""]
    path = os.path.abspath(os.path.join(out_dir, "compile_check.cir"))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log = path[:-4] + ".log"
    if os.path.exists(log):
        os.remove(log)
    try:
        subprocess.run([LTSPICE_EXE, "-b", path], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "errors": ["compile check timed out after 60 s (operating point does not converge)"], "path": path}
    if not os.path.exists(log):
        return {"ok": False, "errors": ["LTspice produced no log for the compile check"], "path": path}
    with open(log, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read()
    errs = ltspice_errors(txt)
    return {"ok": not errs, "errors": errs[:12], "path": path}


def run_deck(deck: Deck, netlist: str, out_dir: str, timeout: Optional[int] = None) -> Dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"tb_{deck.key}.cir")
    with open(path, "w", encoding="utf-8") as f:
        f.write(deck.text(netlist))
    log = path[:-4] + ".log"
    for ext in (".log", ".raw", ".op.raw"):
        try:
            os.remove(path[:-4] + ext)
        except OSError:
            pass
    rec = {"key": deck.key, "kind": deck.kind, "title": deck.title, "deck_path": path, "raw": {}, "metrics": {}, "error": None}
    if timeout is None:
        timeout = 60 if ".tran" not in deck.analysis else 120
    path = os.path.abspath(path)
    log = path[:-4] + ".log"
    try:
        subprocess.run([LTSPICE_EXE, "-b", path], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        rec["error"] = f"LTspice timeout after {timeout}s"
        return rec
    if not os.path.exists(log):
        rec["error"] = "LTspice produced no log"
        return rec
    rec["raw"] = parse_meas_log(log)
    with open(log, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read()
    fatal = ltspice_errors(txt)
    if fatal:
        rec["error"] = "; ".join(x.strip() for x in fatal[:3])
    try:
        rec["metrics"] = deck.extract(rec["raw"])
    except Exception as e:
        rec["error"] = (rec["error"] or "") + f" extract: {e}"
    return rec


def run_all(decks: Dict[str, Deck], netlist: str, out_dir: str, workers: int = 4) -> Dict[str, Dict[str, Any]]:
    results = {}
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(run_deck, d, netlist, out_dir): k for k, d in decks.items()}
        for f in cf.as_completed(futs):
            results[futs[f]] = f.result()
    # derived metrics that need two decks (CMRR = AOL - ACM)
    aol = next((r["metrics"]["aol_db"] for r in results.values() if "aol_db" in r["metrics"]), None)
    for r in results.values():
        if "acm_db" in r["metrics"] and aol is not None:
            r["metrics"]["cmrr_db"] = aol - r["metrics"]["acm_db"]
    vref = next((r["metrics"]["vref"] for r in results.values() if "vref" in r["metrics"]), None)
    for r in results.values():
        if "vref_at_vka" in r["metrics"] and vref is not None and r["metrics"].get("vka"):
            r["metrics"]["dvref_dvka"] = (r["metrics"]["vref_at_vka"] - vref) / (r["metrics"]["vka"] - vref)
    return results


# ============================================================================ evaluation
def _tol(unit: str, typ: float) -> float:
    if unit == "dB":
        return 6.0
    if unit == "deg":
        return 15.0
    return abs(typ) * 0.30


def evaluate(plan: Dict[str, Any], results: Dict[str, Dict[str, Any]], deck_errors: Dict[str, str]) -> Dict[str, Any]:
    rows = []
    for it in plan["items"]:
        row = {k: it.get(k) for k in ("spec_index", "symbol", "name", "category", "min", "typ", "max", "unit",
                                      "test_conditions", "kind", "status", "note")}
        if it["status"] != "PLANNED":
            row["result"] = it["status"]
            rows.append(row)
            continue
        key, kind, unit = it["fixture_key"], it["kind"], it["canonical_unit"]
        lim = spec_limits_in(unit, it)
        row.update({"fixture_key": key, "canonical_unit": unit, "limits_canonical": lim})
        r = results.get(key)
        if key in deck_errors:
            row["result"] = "NO_FIXTURE"; row["diagnostic"] = deck_errors[key]; rows.append(row); continue
        if r is None:
            row["result"] = "NOT_RUN"; rows.append(row); continue
        val = r["metrics"].get(kind)
        if kind == "load_reg" and it.get("unit", "").startswith("%"):
            val = r["metrics"].get("load_reg_pct")
        if val is None:
            row["result"] = "SIM_ERROR" if r.get("error") else "NOT_OBSERVED"
            row["diagnostic"] = r.get("error") or f"fixture '{r['kind']}' produced no value for '{kind}' (raw: {list(r['raw'])[:6]})"
            rows.append(row); continue
        if it["magnitude"]:
            val = abs(val)
            lo = min([abs(x) for x in (lim["min"], lim["max"]) if x is not None], default=None) if lim["min"] is not None and lim["max"] is not None else None
            hi = max([abs(x) for x in (lim["min"], lim["max"]) if x is not None], default=None)
            if lim["min"] is not None and lim["max"] is None:   # e.g. IO min -20 mA  => |IO| >= 20 mA
                lo, hi = abs(lim["min"]), None
            if lim["min"] is None and lim["max"] is not None:
                lo, hi = None, abs(lim["max"])
            typ = abs(lim["typ"]) if lim["typ"] is not None else None
        else:
            lo, hi, typ = lim["min"], lim["max"], lim["typ"]
        row["measured"] = val
        if lo is None and hi is None and typ is None:
            row["result"] = "NO_NUMERIC_LIMIT"; rows.append(row); continue
        if lo is None and hi is None:
            tol = _tol(unit, typ)
            ok = abs(val - typ) <= tol
            row["result"] = "PASS_TYP" if ok else "FAIL_TYP"
            row["residual"] = val - typ
            row["diagnostic"] = f"{kind}: measured {fmt(val, unit)} vs typ {fmt(typ, unit)} (±{fmt(tol, unit)} band)"
            rows.append(row); continue
        ok = (lo is None or val >= lo) and (hi is None or val <= hi)
        row["result"] = "PASS" if ok else "FAIL"
        if not ok:
            row["residual"] = (lo - val) if (lo is not None and val < lo) else (val - hi)
            bound = f"min {fmt(lo, unit)}" if (lo is not None and val < lo) else f"max {fmt(hi, unit)}"
            row["diagnostic"] = f"{kind}: measured {fmt(val, unit)} violates {bound}"
        else:
            row["diagnostic"] = f"{kind}: measured {fmt(val, unit)} within [{fmt(lo, unit)}, {fmt(hi, unit)}]"
        rows.append(row)

    def cnt(*st):
        return sum(r["result"] in st for r in rows)
    planned = cnt("PASS", "FAIL", "PASS_TYP", "FAIL_TYP", "SIM_ERROR", "NOT_OBSERVED", "NO_FIXTURE", "NOT_RUN", "NO_NUMERIC_LIMIT")
    measured = cnt("PASS", "FAIL", "PASS_TYP", "FAIL_TYP")
    hard = cnt("PASS", "FAIL")
    summary = {
        "total_rows": len(rows),
        "electrical_rows": sum(str(r["category"]).upper() == "ELECTRICAL" for r in rows),
        "planned": planned,
        "measured": measured,
        "coverage_pct": round(100 * measured / planned, 1) if planned else 0.0,
        "pass_hard": cnt("PASS"), "fail_hard": cnt("FAIL"),
        "pass_typ": cnt("PASS_TYP"), "fail_typ": cnt("FAIL_TYP"),
        "pass_rate_measured_pct": round(100 * cnt("PASS", "PASS_TYP") / measured, 1) if measured else 0.0,
        "pass_rate_hard_pct": round(100 * cnt("PASS") / hard, 1) if hard else None,
        "sim_errors": cnt("SIM_ERROR"), "not_observed": cnt("NOT_OBSERVED"), "no_fixture": cnt("NO_FIXTURE"),
        "unsupported_kind": cnt("UNSUPPORTED_KIND"),
        "not_testable": sum(r["result"].startswith("NOT_TESTABLE") for r in rows),
    }
    return {"summary": summary, "rows": rows}


def feedback_text(evaluation: Dict[str, Any], max_items: int = 25) -> str:
    """Compact, actionable feedback for the netlist synthesiser."""
    lines = []
    for r in evaluation["rows"]:
        if r["result"] in ("FAIL", "FAIL_TYP"):
            lines.append(f"- [{r['result']}] {r['symbol']} ({r['name']}): {r.get('diagnostic')} | conditions: {r.get('test_conditions')}")
    for r in evaluation["rows"]:
        if r["result"] == "SIM_ERROR":
            lines.append(f"- [SIM_ERROR] {r['symbol']}: {str(r.get('diagnostic'))[:200]}")
    s = evaluation.get("summary", {})
    if not lines:
        if not s.get("measured"):
            return ("NO specification could be measured: every fixture failed to produce a value, i.e. the netlist "
                    "does not simulate. Fix SPICE syntax/topology first (see compile errors).")
        return "All measured specifications passed."
    if s.get("planned"):
        lines.insert(0, f"Coverage {s.get('measured')}/{s.get('planned')} testable specs measured; "
                        f"{s.get('fail_hard', 0) + s.get('fail_typ', 0)} failing:")
    return "\n".join(lines[:max_items])
