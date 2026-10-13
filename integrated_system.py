import os
import sys
import json
import time
from typing import Dict, List, Any, Optional

from circuit_designer import (
    DesignerRegistry, BaseCircuitDesigner, CircuitDesignResult
)
from analog_verifier import (
    Stage0DocumentParser,
    Stage1CircuitExtractor,
    Stage2TaskDecomposer,
    Stage25CrossValidator,
    Stage3GraphScheduler,
    Stage4SymPyOracle,
    Stage5TestbenchGenerator,
    SimulatorInterface,
    ClosedLoopRepairEngine,
    ValidationReport,
    ClosedLoopRepairReport
)

class IntegratedCircuitDesignVerifier:
    """
    End-to-End System combining:
    1. Direct Circuit Netlist Design & Sizing Engine (First-Principles Analog Synthesis)
    2. AnalogVerifier Neuro-Symbolic Framework (ICML 2026 Paper Implementation)
       - Stage 0: Block-level semantic parsing
       - Stage 1: Structured circuit information extraction
       - Stage 2: Verification task decomposition
       - Stage 2.5: Cross-validation quality gate
       - Stage 3: Port Dependency Graph (PDG) & schedule synthesis
       - Stage 4: SymPy numerical-symbolic oracle generation
       - Stage 5: SPICE testbench synthesis
       - Closed-Loop Simulation-Grounded Repair (Algorithm 2)
    """
    def __init__(self, simulator_path: Optional[str] = None):
        self.stage0 = Stage0DocumentParser()
        self.stage1 = Stage1CircuitExtractor()
        self.stage2 = Stage2TaskDecomposer()
        self.stage2_5 = Stage25CrossValidator(strict_mode=False)
        self.stage3 = Stage3GraphScheduler()
        self.stage4 = Stage4SymPyOracle()
        self.stage5 = Stage5TestbenchGenerator()
        self.sim_interface = SimulatorInterface(simulator_path=simulator_path)
        self.repair_engine = ClosedLoopRepairEngine(simulator_interface=self.sim_interface)

    def run_full_design_and_verification(
        self,
        circuit_type: str,
        specifications: Dict[str, Any],
        doc_id: str = "SPEC_BENCHMARK"
    ) -> Dict[str, Any]:
        """
        Executes end-to-end flow:
        Design Netlist -> Parse Spec -> Decompose Tasks -> Cross-Validate ->
        PDG Schedule -> SymPy Oracle -> Synthesize Testbenches -> Closed-Loop Repair Simulation.
        """
        start_time = time.time()
        print("=" * 80)
        print(f"[*] Starting Integrated Analog Design & Neuro-Symbolic Verification")
        print(f"[*] Circuit Type: {circuit_type}")
        print("=" * 80)

        # -------------------------------------------------------------
        # Phase 1: Direct Circuit Netlist Design & Sizing
        # -------------------------------------------------------------
        print("\n--- [Phase 1] Direct Circuit Netlist Design & Sizing ---")
        designer = DesignerRegistry.get_designer(circuit_type)
        design_result: CircuitDesignResult = designer.design(specifications)
        saved_netlist_path = designer.save_netlist(design_result)
        print(f"[+] Designed subcircuit '{design_result.subcircuit_name}'")
        print(f"[+] Sized {len(design_result.components)} components and {len(design_result.ports)} ports")
        print(f"[+] Netlist successfully written to: {saved_netlist_path}")
        print(f"[+] Predicted metrics: {design_result.predicted_metrics}")

        # Format spec input for Stage 0
        spec_doc = {
            "circuit_name": design_result.circuit_name,
            "datasheet_doc": f"Automated Specification Sheet for {design_result.circuit_name}",
            "key_features": design_result.design_notes,
            "pins": [
                {
                    "pin_name": p.name,
                    "pin_type": "POWER" if p.role in ["POWER", "GROUND"] else "ANALOG",
                    "direction": p.direction,
                    "description": p.description,
                    "electrical_constraints": {"operating_voltage_range": p.voltage_range}
                }
                for p in design_result.ports
            ],
            "specifications": {
                k: {
                    "name": k.replace("_", " "),
                    "unit": self._infer_unit(k),
                    "typ": v,
                    "min": round(v * 0.8, 4) if isinstance(v, (int, float)) and v > 0 else None,
                    "max": round(v * 1.2, 4) if isinstance(v, (int, float)) and v > 0 else None,
                    "description": f"Target parameter {k}"
                }
                for k, v in design_result.predicted_metrics.items()
            }
        }

        # -------------------------------------------------------------
        # Phase 2: Context-Aware Task Serialization (Stages 0 - 2)
        # -------------------------------------------------------------
        print("\n--- [Phase 2] Context-Aware Task Serialization (Stages 0-2) ---")
        doc_db = self.stage0.parse_from_json_spec(spec_doc, doc_id=doc_id)
        print(f"[Stage 0] Segmented specification into {len(doc_db.blocks)} typed blocks.")

        circuit_info = self.stage1.extract(doc_db, default_circuit_type=design_result.circuit_type)
        print(f"[Stage 1] Extracted CircuitInformation: {len(circuit_info.parameters)} parameters, {len(circuit_info.pins)} pins (Confidence: {circuit_info.extraction_confidence}).")

        task_result = self.stage2.decompose(circuit_info, doc_id=doc_id)
        print(f"[Stage 2] Decomposed into {len(task_result.tasks)} atomic verification tasks (Domains: {task_result.processing_stats['domains']}).")

        # -------------------------------------------------------------
        # Phase 3: Stage 2.5 Cross Validation
        # -------------------------------------------------------------
        print("\n--- [Phase 3] Stage 2.5 Cross Validation Quality Gate ---")
        val_report: ValidationReport = self.stage2_5.validate(circuit_info, task_result)
        print(f"[Stage 2.5] Cross-validation Passed: {val_report.validation_passed} (Passed: {val_report.passed_checks}/{val_report.total_checks}, Errors: {val_report.error_count}, Warnings: {val_report.warning_count})")

        # -------------------------------------------------------------
        # Phase 4: Stage 3 Graph-Symbolic Scheduling (PDG)
        # -------------------------------------------------------------
        print("\n--- [Phase 4] Stage 3 Graph-Symbolic Scheduling (PDG) ---")
        stage3_result = self.stage3.analyze_and_schedule(circuit_info, task_result)
        dep_graph = stage3_result.dependency_graph
        print(f"[Stage 3] PDG constructed: {len(dep_graph.nodes)} nodes, {len(dep_graph.edges)} precedence edges.")
        print(f"[Stage 3] Dependency Levels: {dep_graph.levels}")
        print(f"[Stage 3] Correct-by-Construction Stimulus Schedule sigma(p):")
        for port, t_start in stage3_result.schedule.items():
            print(f"    - Port {port:6s} -> activation time: {t_start*1e6:8.2f} us")

        # -------------------------------------------------------------
        # Phase 5: Stage 4 Numerical-Symbolic Grounding (SymPy Oracle)
        # -------------------------------------------------------------
        print("\n--- [Phase 5] Stage 4 Numerical-Symbolic Grounding (SymPy Oracle) ---")
        oracles = self.stage4.generate_oracles(circuit_info, task_result, stage3_result)
        print(f"[Stage 4] Generated {len(oracles)} SymPy-grounded verification oracles.")
        for o in oracles[:3]:
            f_expr = o.formulas[0].sympy_expr if o.formulas else "N/A"
            print(f"    - Task {o.task_id} ({o.parameter_symbol}): Formula '{f_expr}', Check: '{o.verification_model.primary_check}'")

        # -------------------------------------------------------------
        # Phase 6: Stage 5 SPICE Testbench Synthesis
        # -------------------------------------------------------------
        print("\n--- [Phase 6] Stage 5 SPICE Testbench Generation ---")
        testbenches = self.stage5.generate_all(
            circuit_info=circuit_info,
            task_result=task_result,
            stage3_result=stage3_result,
            oracles=oracles,
            dut_subcircuit_file=saved_netlist_path,
            dut_subcircuit_name=design_result.subcircuit_name
        )
        print(f"[Stage 5] Synthesized {len(testbenches)} executable SPICE testbench netlists in '{self.stage5.output_dir}'.")

        # -------------------------------------------------------------
        # Phase 7: Simulation Execution & Closed-Loop Repair Loop (Algorithm 2)
        # -------------------------------------------------------------
        print("\n--- [Phase 7] Closed-Loop Simulation-Grounded Repair Loop (Algorithm 2) ---")
        repair_reports: List[ClosedLoopRepairReport] = []
        oracle_map = {o.task_id: o for o in oracles}

        pass_count = 0
        violation_count = 0

        for tb in testbenches:
            matching_task_id = tb.for_task_id
            oracle = oracle_map.get(matching_task_id)
            print(f"[*] Simulating & validating {tb.filename}...", end="", flush=True)
            report = self.repair_engine.run_repair_loop(
                testbench=tb,
                circuit_info=circuit_info,
                stage3_result=stage3_result,
                oracle=oracle
            )
            repair_reports.append(report)
            if report.final_status == "PASS":
                pass_count += 1
                print(f" [PASS] (Iterations: {report.total_iterations})")
            elif report.final_status == "DESIGN_VIOLATION":
                violation_count += 1
                print(f" [DESIGN VIOLATION] (Hardware bug confirmed by Grounded Oracle)")
            else:
                print(f" [{report.final_status}]")

        total_elapsed = time.time() - start_time
        pass_rate = (pass_count / len(testbenches)) * 100.0 if testbenches else 0.0

        print("\n" + "=" * 80)
        print(f"[*] FINAL VERIFICATION SUMMARY")
        print(f"    - Circuit: {design_result.circuit_name} ({circuit_type})")
        print(f"    - Testbenches Generated: {len(testbenches)}")
        print(f"    - Functional Pass Rate: {pass_rate:.1f}% ({pass_count}/{len(testbenches)})")
        print(f"    - Design Violations: {violation_count}")
        print(f"    - Total Flow Time: {total_elapsed:.2f} seconds")
        print("=" * 80)

        return {
            "design_result": design_result,
            "circuit_info": circuit_info,
            "task_result": task_result,
            "validation_report": val_report,
            "stage3_result": stage3_result,
            "oracles": oracles,
            "testbenches": testbenches,
            "repair_reports": repair_reports,
            "pass_rate": pass_rate,
            "total_elapsed": total_elapsed
        }

    def _infer_unit(self, key: str) -> str:
        k = key.lower()
        if "db" in k: return "dB"
        if "hz" in k: return "Hz"
        if "deg" in k: return "deg"
        if "us" in k or "ns" in k or "s" in k: return "s"
        if "ma" in k: return "mA"
        if "ua" in k: return "uA"
        if "mv" in k: return "mV"
        if "v" in k: return "V"
        if "pct" in k: return "%"
        if "mw" in k: return "mW"
        return ""
