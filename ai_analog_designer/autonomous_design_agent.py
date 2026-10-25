import os
import re
import json
import shutil
import logging
from typing import Dict, Any, List, Optional

from .openrouter_client import OpenRouterClient
from .pdf_to_graph import PDFDatasheetParser, HardwareKnowledgeGraphBuilder, PortDependencyGraphExtractor
from .circuit_synthesis import AICircuitArchitect, NetlistValidator
from .circuit_synthesis.harness2 import Harness2CircuitArchitect, HarnessTree
from .neuro_symbolic_verifier import TestbenchSynthesizer, ClosedLoopEvaluator
from .neuro_symbolic_verifier.dynamic_testbench_harness import DynamicTestbenchHarness, MathPostProcessor
from .neuro_symbolic_verifier.verification_flow import plan_for, verify, write_artifacts
from .visualization import ProcessVisualizer
from .tracing import ExecutionTracer

logger = logging.getLogger("AutonomousAnalogDesignAgent")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

class AutonomousAnalogDesignAgent:
    """
    Autonomous AI Analog IC Designer & Verifier.
    Integrates:
      1. ArXiv-grounded PDF-to-Knowledge-Graph extraction
      2. Port Dependency Graph (PDG) topological scheduling
      3. Google Research Harness² Recursive Synthesis Engine (NIM 550B + OpenRouter)
      4. Dynamic Testbench Harness with Fourier & Mathematical Post-Processing (NumPy/SciPy)
      5. Deterministic LTspice batch simulation & Neuro-symbolic SymPy verification
      6. Closed-loop simulation-guided contrastive repair loop
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        output_dir: str = "output_design",
        simulator_path: Optional[str] = None,
        max_iterations: int = 10,
        reuse_hkg_path: Optional[str] = None
    ):
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.max_iterations = max_iterations
        self.reuse_hkg_path = reuse_hkg_path

        self.llm = OpenRouterClient(api_key=api_key, default_model=model)
        self.pdf_parser = PDFDatasheetParser()
        self.kg_builder = HardwareKnowledgeGraphBuilder(llm_client=self.llm)
        self.pdg_extractor = PortDependencyGraphExtractor()
        self.circuit_architect = AICircuitArchitect(llm_client=self.llm)
        self.harness2_architect = Harness2CircuitArchitect(llm_client=self.llm)
        self.dynamic_harness = DynamicTestbenchHarness(llm_client=self.llm, simulator_path=simulator_path)
        self.netlist_validator = NetlistValidator()
        self.testbench_synthesizer = TestbenchSynthesizer(llm_client=self.llm)
        self.evaluator = ClosedLoopEvaluator(simulator_path=simulator_path)

    def extract_specs_from_prompt(self, user_prompt: str) -> Dict[str, Any]:
        """
        Uses LLM to parse natural language circuit design requests into structured HKG elements
        (IC metadata, pins with roles, electrical parameters with SymPy intervals).
        """
        sys_prompt = """You are an Expert Analog IC Specification & System Architect.
Convert the user's natural language circuit design request into a complete, structured JSON specification schema:
{
  "ic_device": {
    "name": "<Part or Circuit Name, e.g. CMOS_OPAMP_RAIL2RAIL>",
    "category": "<e.g. Operational Amplifier, Bandgap Reference, Comparator, Audio Amp, PLL, etc.>",
    "description": "<Detailed description of requirements>"
  },
  "pins": [
    {
      "name": "<Pin Name, e.g. VDD, GND, INP, INM, OUT, VREF, EN>",
      "role": "<POWER | GROUND | BIAS | SIGNAL | OUTPUT | CONTROL | FEEDBACK>",
      "direction": "<INPUT | OUTPUT | BIDIRECTIONAL | POWER | GROUND>",
      "description": "<brief role>"
    }
  ],
  "electrical_parameters": [
    {
      "symbol": "<e.g. VDD, VOUT, GAIN, BW, PM, IB, IQ, VOS, CMRR, PSRR, SLEW>",
      "name": "<Full parameter name>",
      "min": <float or null>,
      "typ": <float or null>,
      "max": <float or null>,
      "unit": "<e.g. V, mV, uA, mA, dB, MHz, deg, V/us, Ohm>",
      "test_conditions": "<e.g. VDD=3.3V, Temp=25C, RL=10k, CL=10pF>"
    }
  ]
}
Return ONLY valid JSON.
"""
        resp = self.llm.chat_completion(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": f"User Natural Language Requirement:\n{user_prompt}"}
            ],
            temperature=0.1
        )
        data = self.llm.parse_json_response(resp)
        if not data or not isinstance(data, dict):
            data = {
                "ic_device": {"name": "CUSTOM_ANALOG_IC", "category": "Custom Circuit", "description": user_prompt},
                "pins": [
                    {"name": "VDD", "role": "POWER", "direction": "INPUT", "description": "Positive Supply"},
                    {"name": "GND", "role": "GROUND", "direction": "INPUT", "description": "Ground"},
                    {"name": "INP", "role": "SIGNAL", "direction": "INPUT", "description": "Non-inverting Input"},
                    {"name": "INM", "role": "SIGNAL", "direction": "INPUT", "description": "Inverting Input"},
                    {"name": "OUT", "role": "OUTPUT", "direction": "OUTPUT", "description": "Output"}
                ],
                "electrical_parameters": [
                    {"symbol": "VDD", "name": "Supply Voltage", "min": 3.0, "typ": 3.3, "max": 3.6, "unit": "V", "test_conditions": "Nominal"},
                    {"symbol": "VOUT", "name": "Output Voltage", "min": 0.1, "typ": 1.65, "max": 3.2, "unit": "V", "test_conditions": "Open-loop"}
                ]
            }
        return data

    def _evaluate_netlist_unified(
        self,
        netlist_code: str,
        subckt_name: str,
        pins: List[Dict[str, Any]],
        specifications: List[Dict[str, Any]],
        pdg_schedule: Dict[str, float],
        tag: str
    ) -> Dict[str, Any]:
        """
        Unified evaluation pipeline combining deterministic fixtures (DC/AC small-signal)
        with LLM-driven dynamic testbenches and mathematical post-processing (Fourier THD+N, power, frequency).
        """
        val_res = self.netlist_validator.validate(
            netlist=netlist_code,
            expected_subckt_name=subckt_name,
            expected_pins=pins
        )
        cleaned_netlist = val_res.get("cleaned_netlist", netlist_code.strip())

        # 1. Deterministic verification
        sched_us = {k: v * 1e6 for k, v in pdg_schedule.items()}
        det_eval = verify(
            netlist=cleaned_netlist,
            plan=self.spec_plan,
            schedule_us=sched_us,
            out_dir=self.output_dir,
            dut_name=subckt_name,
            label=tag
        )

        # 2. Dynamic verification for complex specs (THD+N, power, frequency, rDS(on), etc.)
        dynamic_specs = self.dynamic_harness.identify_dynamic_specs(specifications)
        if dynamic_specs:
            tb_file = os.path.join(self.output_dir, f"{subckt_name}_{tag}.cir")
            with open(tb_file, "w", encoding="utf-8") as f:
                f.write(cleaned_netlist)

            dut_class = self.spec_plan.get("device_class", "Analog IC")
            dyn_tb = self.dynamic_harness.synthesize_dynamic_testbench(
                subckt_name=subckt_name,
                pins=pins,
                target_specs=dynamic_specs,
                pdg_schedule=pdg_schedule,
                netlist_path=tb_file,
                dut_category=dut_class
            )
            dyn_eval = self.dynamic_harness.execute_and_evaluate(
                testbench_deck=dyn_tb["content"],
                tb_name=f"{subckt_name}_{tag}_dyn",
                out_dir=self.output_dir,
                target_specs=dynamic_specs
            )

            # Unified metrics merging
            det_passed = det_eval.get("passed_specs", 0)
            det_failed = det_eval.get("failed_specs", 0)
            dyn_passed = dyn_eval.get("passed_count", 0)
            dyn_failed = dyn_eval.get("failed_count", 0)

            total_passed = det_passed + dyn_passed
            total_measured = det_passed + det_failed + dyn_passed + dyn_failed

            det_eval["passed_specs"] = total_passed
            det_eval["failed_specs"] = det_failed + dyn_failed
            if total_measured > 0:
                det_eval["pass_rate_percent"] = round(100.0 * total_passed / total_measured, 1)

            # Append dynamic evaluations to spec_evaluations list
            if "spec_evaluations" not in det_eval:
                det_eval["spec_evaluations"] = []
            det_eval["spec_evaluations"].extend(dyn_eval.get("spec_evaluations", []))

            # Merge feedback diagnostics
            if dyn_eval.get("diagnostic_feedback"):
                existing_fb = det_eval.get("diagnostic_feedback", "")
                det_eval["diagnostic_feedback"] = (existing_fb + "\n" + dyn_eval["diagnostic_feedback"]).strip()

            det_eval["fourier_data"] = dyn_eval.get("fourier_data")
            det_eval["simulation_errors"] = det_eval.get("simulation_errors", []) + dyn_eval.get("simulation_errors", [])

        return det_eval

    def run(
        self,
        datasheet_pdf_path: Optional[str] = None,
        user_prompt: Optional[str] = None,
        max_pages: int = 12,
        progress_callback: Optional[Any] = None,
        max_iterations: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Executes the entire end-to-end pipeline:
        Supports:
          1. Arbitrary PDF datasheet upload
          2. Natural language circuit specification requests (with or without PDF)
          3. Real-time progress updates via progress_callback
        """
        logger.info(f"=== Starting Autonomous Analog Design (PDF: {datasheet_pdf_path}, Prompt: {user_prompt}) ===")
        self.tracer = ExecutionTracer(target_name=os.path.basename(datasheet_pdf_path) if datasheet_pdf_path else "CUSTOM_CIRCUIT")

        # -------------------------------------------------------------
        # Phase 1: Knowledge Ingestion (PDF Parsing and/or User Prompt)
        # -------------------------------------------------------------
        self.tracer.start_span(1, "Phase 1: Knowledge Ingestion & HKG Construction", inputs={
            "datasheet_pdf_path": datasheet_pdf_path,
            "user_prompt": user_prompt,
            "max_pages": max_pages
        })
        if progress_callback:
            progress_callback(1, "Phase 1: Knowledge Ingestion", "Parsing datasheet and natural language requirements...")

        if datasheet_pdf_path and os.path.exists(datasheet_pdf_path):
            logger.info(f"Phase 1: Parsing PDF layout, tables, and text sections from: {datasheet_pdf_path}")
            self.pdf_parser.max_pages = max_pages
            parsed_doc = self.pdf_parser.parse_pdf(datasheet_pdf_path)
            
            if self.reuse_hkg_path and os.path.exists(self.reuse_hkg_path):
                logger.info(f"Reusing cached HKG extraction: {self.reuse_hkg_path} (no LLM calls in Phase 1)")
                with open(self.reuse_hkg_path, "r", encoding="utf-8") as f:
                    raw_extraction = json.load(f)
                hkg = self.kg_builder._json_to_networkx_graph(raw_extraction)
                from .pdf_to_graph.pdf_parser import PDFDatasheetParser
                hkg.graph["parsed_pages"] = PDFDatasheetParser.audit_pages(parsed_doc)
            else:
                logger.info("Constructing Hardware Knowledge Graph (HKG) from parsed document...")
                hkg = self.kg_builder.build_kg_from_parsed_pdf(parsed_doc)
            raw_extraction = hkg.graph.get("raw_extraction", {})
        elif user_prompt:
            logger.info("Phase 1: No PDF provided; extracting specifications directly from natural language prompt...")
            raw_extraction = self.extract_specs_from_prompt(user_prompt)
            # Create synthetic HKG
            from .pdf_to_graph.hardware_kg_builder import HardwareKnowledgeGraph
            hkg = HardwareKnowledgeGraph()
            hkg.graph["raw_extraction"] = raw_extraction
            hkg.graph["nodes"] = [raw_extraction.get("ic_device", {})]
        else:
            err = ValueError("Either datasheet_pdf_path or user_prompt must be provided.")
            self.tracer.record_error(err, remediation="Provide a valid datasheet PDF path or natural language prompt.")
            raise err

        ic_info = raw_extraction.get("ic_device", {"name": "DUT_IC", "category": "ANALOG", "description": ""})
        pins = raw_extraction.get("pins", [])
        specifications = raw_extraction.get("electrical_parameters", [])
        functional_blocks = raw_extraction.get("functional_blocks", [])

        # If user provided a natural language prompt along with PDF, reflect prompt in IC description
        if user_prompt and datasheet_pdf_path:
            ic_info["description"] = f"{ic_info.get('description', '')}\n[User Prompt Directives]: {user_prompt}"

        part_name = ic_info.get("name", "ANALOG_IC")
        self.tracer.target_name = part_name
        safe_part_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', part_name)
        subckt_name = re.sub(r'[^a-zA-Z0-9_]', '_', part_name).upper()

        logger.info(f"Target IC: {part_name} | Pins Extracted: {len(pins)} | Specs Extracted: {len(specifications)}")

        # Deterministic measurement plan (computed once; fixtures are rebuilt per iteration for each netlist)
        self.spec_plan = plan_for(ic_info, pins, specifications)
        _ps = self.spec_plan["summary"]
        logger.info(f"  [Measurement Plan] class={self.spec_plan['device_class']} electrical_rows={_ps['electrical_rows']} "
                    f"planned_fixtures={_ps['planned']} unsupported={_ps['unsupported_kind']} not_testable={_ps['not_testable']}")

        # Save KG snapshot
        kg_file = os.path.join(self.output_dir, f"{safe_part_name}_knowledge_graph.json")
        with open(kg_file, "w", encoding="utf-8") as f:
            json.dump(raw_extraction, f, indent=2)

        self.tracer.end_span(outputs={
            "part_name": part_name,
            "pins_count": len(pins),
            "specifications_count": len(specifications),
            "pins": [p.get("name") for p in pins[:10]]
        })

        # -------------------------------------------------------------
        # Phase 2: Port Dependency Graph (PDG) Scheduling
        # -------------------------------------------------------------
        self.tracer.start_span(2, "Phase 2: Port Dependency Graph (PDG) & Timing Schedule", inputs={
            "pins_count": len(pins)
        })
        if progress_callback:
            progress_callback(2, "Phase 2: Port Dependency Graph", f"Extracting PDG & topological settling sequence for {len(pins)} pins...")

        logger.info("Phase 2: Constructing Port Dependency Graph & Activation Schedule...")
        pdg, pdg_schedule, levels = self.pdg_extractor.extract_pdg(hkg)
        pdg_res = {"pdg": pdg, "schedule": pdg_schedule, "levels": levels}

        from .pdf_to_graph import PDGFidelityVerifier
        fidelity_audit = PDGFidelityVerifier.audit_pdg(hkg, pdg, pdg_schedule)
        pdg_res["fidelity_audit"] = fidelity_audit
        logger.info(f"  [PDG Fidelity Audit] Score: {fidelity_audit.get('fidelity_score_pct')}% | Status: {fidelity_audit.get('status')}")

        audit_file = os.path.join(self.output_dir, f"{safe_part_name}_PDG_FIDELITY_AUDIT.json")
        try:
            with open(audit_file, "w", encoding="utf-8") as f:
                json.dump(fidelity_audit, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write fidelity audit: {e}")

        for pin_name, act_time in pdg_schedule.items():
            logger.info(f"  [PDG Schedule] Pin {pin_name:10s} -> Activation Delay: {act_time*1e6:.2f} us")

        self.tracer.end_span(outputs={
            "pin_schedule_us": {k: round(v*1e6, 2) for k, v in pdg_schedule.items()},
            "levels_count": len(levels),
            "fidelity_score_pct": fidelity_audit.get("fidelity_score_pct"),
            "fidelity_status": fidelity_audit.get("status")
        })

        # -------------------------------------------------------------
        # Phase 3 & 4: Harness² AI Synthesis & Dynamic Verification Loop
        # -------------------------------------------------------------
        current_feedback = None
        design_iterations = []
        final_netlist_path = None
        final_evaluation = None
        total_loop_budget = max_iterations if max_iterations is not None else self.max_iterations
        best_score = None
        best_eval = None
        best_netlist_path = None

        # Bootstrap Harness² 8-component tree from persistent experience library
        dut_category = ic_info.get("category", "analog_ic")
        active_harness_tree = self.harness2_architect.bootstrap_for_device(
            category=dut_category,
            part_name=part_name
        )

        for iteration in range(1, total_loop_budget + 1):
            if progress_callback:
                progress_callback(3, f"Phase 3: Harness² Synthesis (Loop {iteration})", "Synthesizing convergent netlist via recursive Harness² meta-engine...")

            logger.info(f"--- Iteration {iteration}/{total_loop_budget}: Harness² Netlist Synthesis ---")
            self.tracer.start_span(3, f"Phase 3: Harness² Netlist Synthesis (Loop {iteration})", inputs={
                "iteration": iteration,
                "subckt_name": subckt_name,
                "user_prompt": user_prompt
            })

            # 3.1 Synthesis under Harness² (Iteration 1: Baseline; Iteration > 1: Contrastive Propose-Probe-Compose)
            if iteration == 1 or best_eval is None:
                synth_res = self.harness2_architect.synthesize_candidate(
                    tree=active_harness_tree,
                    ic_info=ic_info,
                    pins=pins,
                    specifications=specifications,
                    pdg_schedule={k: v * 1e-6 for k, v in pdg_schedule.items()},
                    measurement_plan=self.spec_plan,
                    user_prompt=user_prompt,
                    prior_feedback=current_feedback
                )
            else:
                def eval_probe(p_netlist: str, p_tag: str) -> Dict[str, Any]:
                    return self._evaluate_netlist_unified(
                        netlist_code=p_netlist,
                        subckt_name=subckt_name,
                        pins=pins,
                        specifications=specifications,
                        pdg_schedule=pdg_schedule,
                        tag=f"iter{iteration}_{p_tag}"
                    )

                logger.info(f"Iteration {iteration}: Launching Harness² Contrastive Propose–Probe–Compose cycle...")
                winning_res, winning_eval, composed_tree, decision_notes = self.harness2_architect.execute_contrastive_iteration(
                    ic_info=ic_info,
                    pins=pins,
                    specifications=specifications,
                    pdg_schedule={k: v * 1e-6 for k, v in pdg_schedule.items()},
                    base_eval=best_eval,
                    eval_probe_fn=eval_probe,
                    measurement_plan=self.spec_plan,
                    user_prompt=user_prompt,
                    iteration_index=iteration
                )
                active_harness_tree = composed_tree
                synth_res = winning_res
                logger.info(f"Iteration {iteration} Composer Decision: {decision_notes}")

            raw_netlist = synth_res.get("spice_netlist", "")
            subckt_name = synth_res.get("subcircuit_name", subckt_name)

            # 3.2 Validate and clean netlist
            val_res = self.netlist_validator.validate(
                netlist=raw_netlist,
                expected_subckt_name=subckt_name,
                expected_pins=pins
            )

            cleaned_netlist = val_res["cleaned_netlist"]
            netlist_filename = f"{subckt_name}_iter{iteration}.cir"
            netlist_path = os.path.join(self.output_dir, netlist_filename)
            with open(netlist_path, "w", encoding="utf-8") as f:
                f.write(cleaned_netlist)
            with open(netlist_path[:-4] + ".raw.cir", "w", encoding="utf-8") as f:   # untouched LLM output
                f.write(raw_netlist)

            final_netlist_path = netlist_path
            self.tracer.end_span(outputs={
                "netlist_path": netlist_path,
                "subcircuit_name": subckt_name,
                "netlist_line_count": len(cleaned_netlist.splitlines())
            })

            # 4. Phase 4 & 5: Unified Verification (Deterministic Fixtures + LLM Dynamic Testbench with Math Post-Processing)
            self.tracer.start_span(4, f"Phase 4: Unified Verification (Loop {iteration})", inputs={
                "netlist": netlist_filename
            })
            if progress_callback:
                progress_callback(4, f"Phase 4: Unified Verification (Loop {iteration})", "Executing deterministic fixtures and dynamic Fourier/math post-processing...")

            eval_res = self._evaluate_netlist_unified(
                netlist_code=cleaned_netlist,
                subckt_name=subckt_name,
                pins=pins,
                specifications=specifications,
                pdg_schedule=pdg_schedule,
                tag=f"iter{iteration}"
            )

            self.tracer.end_span(outputs={
                "pass_rate_percent": eval_res["pass_rate_percent"],
                "passed_specs": eval_res["passed_specs"],
                "failed_specs": eval_res["failed_specs"]
            })


            final_evaluation = eval_res
            pass_rate = eval_res["pass_rate_percent"]

            # Keep the best design seen so far (highest #passed, then coverage, then fewest failures)
            score = (eval_res.get("passed_specs", 0), eval_res.get("coverage_pct", 0.0), -eval_res.get("failed_specs", 0))
            if best_score is None or score > best_score:
                best_score, best_eval, best_netlist_path = score, eval_res, netlist_path
            self.tracer.end_span(outputs={
                "pass_rate_percent": pass_rate,
                "passed_specs": eval_res["passed_specs"],
                "failed_specs": eval_res["failed_specs"],
                "unmeasured_specs": eval_res["unmeasured_specs"],
                "simulation_errors": eval_res.get("simulation_errors", [])[:2]
            })

            self.tracer.start_span(6, f"Phase 6: Closed-Loop Diagnostics (Loop {iteration})", inputs={
                "pass_rate_percent": pass_rate
            })
            if progress_callback:
                progress_callback(6, f"Phase 6: Closed-Loop Diagnostics (Loop {iteration})", f"Pass Rate: {pass_rate:.1f}% ({eval_res['passed_specs']}/{eval_res['total_specs']}). Analyzing shortfalls...")
            logger.info(f"Iteration {iteration} Evaluation Result: Pass Rate = {pass_rate:.1f}% "
                        f"({eval_res['passed_specs']}/{eval_res['total_specs']} measured testable specs)")

            # Analyze shortfalls in this iteration
            shortfalls = []
            if eval_res.get("simulation_errors"):
                shortfalls.extend([f"Compiler/Simulation Error: {e}" for e in eval_res["simulation_errors"][:4]])
            failed_evals = [e for e in eval_res.get("spec_evaluations", []) if not e.get("passed")]
            for fe in failed_evals[:6]:
                sym = fe.get("symbol")
                meas = fe.get("measured")
                diag = fe.get("diagnostic", "Out of bounds")
                meas_str = f"{meas:.4g}" if meas is not None else "Unobserved"
                shortfalls.append(f"Param '{sym}' violated: {diag} (Measured: {meas_str})")

            # Analyze improvements made in this iteration
            improvements = []
            if iteration > 1 and len(design_iterations) > 0:
                prev_it = design_iterations[-1]
                delta_pass = pass_rate - prev_it["pass_rate_percent"]
                if delta_pass > 0:
                    improvements.append(f"Pass rate improved by +{delta_pass:.1f}% ({prev_it['passed_specs']} -> {eval_res['passed_specs']} specs passed).")
                    try:
                        cue = f"Unsatisfied specs on {part_name}: {[e['symbol'] for e in eval_res.get('spec_evaluations', []) if not e.get('passed')][:4]}"
                        lesson = synth_res.get("architecture_summary", "Evolved circuit topology and parameter sizing.")
                        evidence = f"Pass rate improved from {prev_it['pass_rate_percent']:.1f}% to {pass_rate:.1f}%."
                        self.harness2_architect.library.record_experience(
                            task_id=part_name,
                            category=dut_category,
                            cue=cue,
                            lesson=lesson,
                            evidence=evidence,
                            tree=active_harness_tree,
                            score_before=prev_it["pass_rate_percent"],
                            score_after=pass_rate
                        )
                    except Exception as le:
                        logger.warning(f"Could not record experience to library: {le}")
                if prev_it.get("simulation_errors") and not eval_res.get("simulation_errors"):
                    improvements.append("Resolved prior SPICE compiler / syntax errors completely.")
                prev_passed = set(prev_it.get("passed_symbols", []))
                curr_passed = set([e["symbol"] for e in eval_res.get("spec_evaluations", []) if e.get("passed")])
                newly_passed = curr_passed - prev_passed
                if newly_passed:
                    improvements.append(f"Newly satisfied specs: {', '.join(list(newly_passed)[:5])}.")
                if synth_res.get("architecture_summary"):
                    improvements.append(f"Topology refinement: {synth_res['architecture_summary'][:140]}...")
            else:
                improvements.append("Initial first-principles topology and Level-1 parameter sizing synthesized from scratch.")

            passed_syms = [e["symbol"] for e in eval_res.get("spec_evaluations", []) if e.get("passed")]

            design_iterations.append({
                "iteration": iteration,
                "netlist_path": netlist_path,
                "pass_rate_percent": pass_rate,
                "coverage_pct": eval_res.get("coverage_pct"),
                "passed_specs": eval_res["passed_specs"],
                "failed_specs": eval_res["failed_specs"],
                "unmeasured_specs": eval_res["unmeasured_specs"],
                "passed_symbols": passed_syms,
                "simulation_errors": eval_res.get("simulation_errors", []),
                "shortfalls": shortfalls,
                "improvements": improvements,
                "architecture_summary": synth_res.get("architecture_summary", ""),
                "design_rationale": synth_res.get("design_rationale", {})
            })

            self.tracer.end_span(outputs={
                "shortfalls_count": len(shortfalls),
                "improvements_count": len(improvements)
            })

            # Check convergence
            if eval_res["all_passed"] or pass_rate >= 100.0:
                logger.info("SUCCESS: 100% of datasheet specifications satisfied!")
                break

            # If not converged, feed diagnostics into next iteration
            current_feedback = eval_res["diagnostic_feedback"]
            logger.info(f"Refinement Feedback for next iteration:\n{current_feedback}")

        # Restore the best netlist found (the loop may end on a worse iteration)
        if best_netlist_path and best_netlist_path != final_netlist_path:
            best_copy = os.path.join(self.output_dir, f"{subckt_name}_BEST.cir")
            try:
                shutil.copyfile(best_netlist_path, best_copy)
                logger.info(f"Best iteration kept: {best_netlist_path} -> {best_copy}")
                final_netlist_path = best_copy
                final_evaluation = best_eval
            except Exception as be:
                logger.warning(f"Could not copy best netlist: {be}")

        if final_evaluation and "rows" in final_evaluation:
            art = write_artifacts(final_evaluation, self.output_dir, safe_part_name)
            logger.info(f"Coverage report: {art['coverage']}")

        # -------------------------------------------------------------
        # Phase 5: Generate Final Comprehensive Report & Traces
        # -------------------------------------------------------------
        report = self._generate_report(
            part_name=part_name,
            subckt_name=subckt_name,
            ic_info=ic_info,
            pins=pins,
            specifications=specifications,
            pdg_res=pdg_res,
            iterations=design_iterations,
            final_eval=final_evaluation,
            final_netlist_path=final_netlist_path
        )

        report_md_path = os.path.join(self.output_dir, f"{safe_part_name}_DESIGN_VERIFICATION_REPORT.md")
        with open(report_md_path, "w", encoding="utf-8") as f:
            f.write(report["markdown"])

        # Export Fine-Grained Step-by-Step Execution Traces
        try:
            trace_json_path = os.path.join(self.output_dir, f"{safe_part_name}_execution_trace.json")
            trace_html_path = os.path.join(self.output_dir, f"{safe_part_name}_TRACE_INSPECTOR.html")
            self.tracer.export_json(trace_json_path)
            self.tracer.export_html(trace_html_path)
            report["summary"]["trace_inspector_url"] = trace_html_path
            report["trace"] = self.tracer.to_dict()
            logger.info(f"Execution trace inspector generated at: {trace_html_path}")
        except Exception as te:
            logger.warning(f"Could not export execution trace: {te}")

        report_json_path = os.path.join(self.output_dir, f"{safe_part_name}_DESIGN_SUMMARY.json")
        with open(report_json_path, "w", encoding="utf-8") as f:
            json.dump(report["summary"], f, indent=2)

        # Generate Interactive Visual Dashboard for this IC
        try:
            dashboard_html_path = os.path.join(self.output_dir, f"{safe_part_name}_VISUAL_DASHBOARD.html")
            ic_data = ProcessVisualizer.load_ic_data_from_directory(self.output_dir)
            if ic_data:
                ProcessVisualizer.export_dashboard([ic_data], dashboard_html_path)
                logger.info(f"Visual process dashboard generated at: {dashboard_html_path}")
        except Exception as e:
            logger.warning(f"Could not generate visual dashboard: {e}")

        logger.info(f"Design completed! Final report saved to: {report_md_path}")
        return report

    def _generate_report(
        self,
        part_name: str,
        subckt_name: str,
        ic_info: Dict[str, Any],
        pins: List[Dict[str, Any]],
        specifications: List[Dict[str, Any]],
        pdg_res: Dict[str, Any],
        iterations: List[Dict[str, Any]],
        final_eval: Optional[Dict[str, Any]],
        final_netlist_path: Optional[str]
    ) -> Dict[str, Any]:
        """Compiles human-readable Markdown and structured JSON design reports."""
        pass_rate = final_eval["pass_rate_percent"] if final_eval else 0.0

        md_lines = [
            f"# Autonomous AI Analog Circuit Synthesis & Verification Report",
            f"**Target IC:** `{part_name}` | **Subcircuit:** `{subckt_name}`",
            f"**Overall Datasheet Specification Pass Rate (among measured specs):** `{pass_rate:.1f}%`",
            f"**Measurement Coverage (measured / testable specs):** `{(final_eval or {}).get('coverage_pct', 0.0)}%`",
            "",
            "## 1. Executive Summary",
            f"- **IC Category:** {ic_info.get('description', 'Analog Integrated Circuit')}",
            f"- **Total Datasheet Specifications Extracted:** {len(specifications)}",
            f"- **Design Iterations Required:** {len(iterations)}",
            f"- **Synthesized Netlist Location:** `{final_netlist_path}`",
            "",
            "## 2. Port Dependency Graph (PDG) Scheduling",
            "Topological activation sequence derived from physical pin roles:",
            "| Pin Name | Physical Role | Direction | Activation Time (us) |",
            "| :--- | :--- | :--- | :--- |"
        ]

        for p in pins:
            pname = p["name"]
            prole = p.get("role", "SIGNAL")
            pdir = p.get("direction", "INPUT")
            ptime = pdg_res["schedule"].get(pname, 0.0) * 1e6
            md_lines.append(f"| `{pname}` | `{prole}` | `{pdir}` | {ptime:.2f} us |")

        md_lines.extend([
            "",
            "### 2.1 Port Dependency Graph Topology",
            "```mermaid",
            "graph TD"
        ])
        for p in pins:
            pname = p["name"]
            prole = p.get("role", "SIGNAL")
            ptime = pdg_res["schedule"].get(pname, 0.0) * 1e6
            md_lines.append(f'    {pname}["{pname} ({prole})<br/>{ptime:.1f}us"]')
        pdg_graph = pdg_res.get("pdg")
        if pdg_graph and hasattr(pdg_graph, "edges"):
            for u, v, d in pdg_graph.edges(data=True):
                delay = d.get("settling_time", 0.0) * 1e6
                md_lines.append(f'    {u} -->|{delay:.1f}us| {v}')
        md_lines.append("```")

        md_lines.extend([
            "",
            "## 3. Specification Verification & SymPy Oracle Audit",
            "Detailed verification results evaluated against SymPy interval constraints:",
            "| Parameter | Min Target | Typ Target | Max Target | Measured Value | Unit | Status |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |"
        ])

        if final_eval:
            for ev in final_eval.get("spec_evaluations", []):
                sym = ev.get("symbol", "param")
                min_t = ev.get("min_target") if ev.get("min_target") is not None else ev.get("min")
                min_str = f"{min_t:.4g}" if min_t is not None else "-"
                typ_t = ev.get("typ_target") if ev.get("typ_target") is not None else ev.get("typ")
                typ_str = f"{typ_t:.4g}" if typ_t is not None else "-"
                max_t = ev.get("max_target") if ev.get("max_target") is not None else ev.get("max")
                max_str = f"{max_t:.4g}" if max_t is not None else "-"
                meas = ev.get("measured")
                meas_str = f"{meas:.4g}" if meas is not None else "N/A"
                unit = ev.get("unit", "")
                status = "✅ PASS" if ev.get("passed") else "❌ FAIL"
                md_lines.append(f"| `{sym}` | {min_str} | {typ_str} | {max_str} | **{meas_str}** | {unit} | {status} |")

        md_lines.extend([
            "",
            "## 4. Closed-Loop Iteration History & Root Cause Analysis",
            "| Iteration | Pass Rate | Passed Specs | Failed Specs | Architecture Notes |",
            "| :--- | :--- | :--- | :--- | :--- |"
        ])

        for it in iterations:
            md_lines.append(
                f"| Iteration {it['iteration']} | {it['pass_rate_percent']:.1f}% | {it['passed_specs']} | {it['failed_specs']} | {it.get('architecture_summary', '')[:80]}... |"
            )

        for it in iterations:
            i_num = it['iteration']
            md_lines.append(f"\n### 4.{i_num} Loop {i_num} Detailed Audit")
            md_lines.append(f"- **Pass Rate**: `{it['pass_rate_percent']:.1f}%` ({it['passed_specs']}/{it['passed_specs'] + it['failed_specs']} specs passed)")
            md_lines.append("\n**Shortfall Audit (미달 원인 분석):**")
            for sf in it.get("shortfalls", []):
                md_lines.append(f"- {sf}")
            if not it.get("shortfalls"):
                md_lines.append("- None (All specifications and compilation checks passed).")

            md_lines.append("\n**Improvement Actions (개선 및 수정 사항):**")
            for imp in it.get("improvements", []):
                md_lines.append(f"- {imp}")

        md_lines.extend([
            "",
            "## 5. Synthesized SPICE Netlist Architecture",
            f"Netlist file: `{final_netlist_path}`",
            "```spice"
        ])

        if final_netlist_path and os.path.exists(final_netlist_path):
            with open(final_netlist_path, "r", encoding="utf-8") as f:
                netlist_content = f.read()
            md_lines.append(netlist_content)
        md_lines.append("```")

        md_text = "\n".join(md_lines)

        summary = {
            "part_name": part_name,
            "subckt_name": subckt_name,
            "pass_rate_percent": pass_rate,
            "spec_count": len(specifications),
            "iterations": iterations,
            "final_netlist_path": final_netlist_path
        }

        return {"markdown": md_text, "summary": summary}
