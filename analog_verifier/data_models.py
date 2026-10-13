from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Union

# ==============================================================================
# Stage 0: Document Parsing Models (Appendix A)
# ==============================================================================
@dataclass
class DocumentBlock:
    block_id: str
    page_number: int
    raw_text: str
    block_type: str  # section_header, feature_list, pin_definition, electrical_characteristics, etc.
    confidence: float = 1.0
    extracted_parameters: List[Dict[str, Any]] = field(default_factory=list)
    extracted_equations: List[Dict[str, Any]] = field(default_factory=list)
    extracted_pins: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class DocumentDatabase:
    document_id: str
    blocks: List[DocumentBlock]
    metadata: Dict[str, Any] = field(default_factory=dict)

# ==============================================================================
# Stage 1: Circuit Information Extraction Models (Appendix B)
# ==============================================================================
@dataclass
class PinDefinition:
    pin_name: str
    pin_number: Dict[str, str] = field(default_factory=dict)
    pin_type: str = "ANALOG"  # POWER | GROUND | ANALOG | DIGITAL | MIXED
    direction: str = "INPUT"  # INPUT | OUTPUT | BIDIRECTIONAL | POWER | GROUND
    description: str = ""
    electrical_constraints: Dict[str, Any] = field(default_factory=dict)
    inferred_role: str = "SIGNAL"  # STIMULUS | OUTPUT | CONTROL | FEEDBACK | REFERENCE | BIAS | POWER | GROUND
    source_block: str = "Stage1"

@dataclass
class ElectricalParameter:
    symbol: str
    name: str
    description: str
    min_value: Optional[float] = None
    typ_value: Optional[Union[float, List[float]]] = None
    max_value: Optional[float] = None
    unit: str = ""
    test_conditions: List[str] = field(default_factory=list)
    category: str = "VOLTAGE"  # VOLTAGE | CURRENT | RESISTANCE | TIMING | FREQUENCY | POWER | GAIN | etc.
    is_testable: bool = True
    related_pins: List[str] = field(default_factory=list)
    test_type_hint: str = "DC_STATIC"
    source_block: str = "Stage1"

@dataclass
class EquationDefinition:
    equation_id: str
    raw_form: str
    sympy_form: str
    latex_form: str = ""
    description: str = ""
    output_variable: str = ""
    input_variables: List[Dict[str, Any]] = field(default_factory=list)
    application: str = ""
    validity_conditions: str = ""
    source_block: str = "Stage1"

@dataclass
class CircuitInformation:
    circuit_name: str
    circuit_type: str
    circuit_description: str
    key_features: List[str] = field(default_factory=list)
    typical_applications: List[str] = field(default_factory=list)
    pins: List[PinDefinition] = field(default_factory=list)
    parameters: List[ElectricalParameter] = field(default_factory=list)
    equations: List[EquationDefinition] = field(default_factory=list)
    operating_conditions: Dict[str, Any] = field(default_factory=dict)
    absolute_max_ratings: Dict[str, Any] = field(default_factory=dict)
    extraction_confidence: float = 0.95
    extraction_notes: List[str] = field(default_factory=list)

# ==============================================================================
# Stage 2: Test Task Extraction Models (Appendix C)
# ==============================================================================
@dataclass
class MathEvidence:
    parameter_symbol: str
    constraint_expression: str
    pass_criteria: str
    test_conditions: List[str] = field(default_factory=list)
    provenance: str = "SPEC"

@dataclass
class TestTask:
    task_id: str
    task_name: str
    test_type: str  # DC_STATIC | DC_SWEEP | AC_GAIN | AC_BANDWIDTH | AC_PHASE | TRANSIENT_TIMING | TRANSIENT_LOAD | PROTECTION | NOISE
    primary_parameter: str
    math_evidences: List[MathEvidence] = field(default_factory=list)
    stimulus_ports: List[str] = field(default_factory=list)
    measurement_ports: List[str] = field(default_factory=list)
    control_ports: List[str] = field(default_factory=list)
    priority: str = "HIGH"  # HIGH | MEDIUM | LOW
    parameter_category: str = "SPEC_MEASURABLE"  # SPEC_MEASURABLE | DERIVED_VARIABLE | DESIGN_CONSTRAINT | META_INFO
    test_domain: str = "DC"  # DC | AC | TRANSIENT | MIXED
    reasoning: str = ""

@dataclass
class TaskExtractionResult:
    document_id: str
    circuit_name: str
    circuit_type: str
    tasks: List[TestTask]
    total_parameters: int
    testable_parameters: int
    generated_tasks: int
    processing_stats: Dict[str, Any] = field(default_factory=dict)

# ==============================================================================
# Stage 2.5: Cross Validation Models (Appendix E)
# ==============================================================================
@dataclass
class ValidationIssue:
    issue_id: str
    category: str  # PORT_EXISTENCE | PARAMETER_REFERENCE | EVIDENCE_REFERENCE | VARIABLE_RESOLUTION | CONDITION_COMPLETENESS | DATA_INTEGRITY
    severity: str  # error | warning | info
    message: str
    context: Dict[str, Any] = field(default_factory=dict)
    suggestion: str = ""
    affected_items: List[str] = field(default_factory=list)

@dataclass
class ValidationReport:
    validation_passed: bool
    total_checks: int
    passed_checks: int
    failed_checks: int
    error_count: int
    warning_count: int
    info_count: int
    issues: List[ValidationIssue] = field(default_factory=list)

# ==============================================================================
# Stage 3: Port Dependency & Graph Scheduling Models (Appendix D)
# ==============================================================================
@dataclass
class PortAnalysis:
    port_name: str
    pin_names: List[str]
    inferred_role: str  # POWER | GROUND | BIAS | CONTROL | ENABLE | SIGNAL | OUTPUT | FEEDBACK
    role_confidence: float
    role_reasoning: str
    signal_type: str  # ANALOG | DIGITAL | MIXED | POWER
    signal_domain: str = "voltage"
    typical_range: Dict[str, Any] = field(default_factory=dict)
    testability: float = 0.95
    recommended_stimulus: str = ""
    recommended_measurement: str = ""

@dataclass
class SignalPath:
    path_id: str
    path_type: str  # MAIN_SIGNAL | FEEDBACK | BIAS | CONTROL
    source_port: str
    intermediate_ports: List[str]
    sink_port: str
    is_critical: bool = True
    latency_critical: bool = False
    path_reasoning: str = ""

@dataclass
class DependencyGraph:
    nodes: List[str]
    edges: List[Dict[str, Any]]  # [{"from": u, "to": v, "delay": delta}]
    levels: List[List[str]] = field(default_factory=list)
    schedule: Dict[str, float] = field(default_factory=dict)  # sigma(p) start times

@dataclass
class Stage3Result:
    circuit_name: str
    circuit_type: str
    port_analyses: List[PortAnalysis]
    signal_paths: List[SignalPath]
    dependency_graph: DependencyGraph
    schedule: Dict[str, float]
    port_mapping: Dict[str, List[str]] = field(default_factory=dict)
    test_topologies: List[Dict[str, Any]] = field(default_factory=list)

# ==============================================================================
# Stage 4: SymPy Oracle & Tolerance Models (Appendix F)
# ==============================================================================
@dataclass
class SymPyFormula:
    formula_id: str
    formula_name: str
    sympy_expr: str
    latex_expr: str
    human_readable: str
    output_variable: str
    input_variables: List[Dict[str, Any]]
    source: str = "SPEC"  # SPEC | PHYSICS | DERIVED | EMPIRICAL
    validity_conditions: List[str] = field(default_factory=list)
    is_verified: bool = True

@dataclass
class TestVector:
    vector_id: str
    vector_type: str  # NOMINAL | MIN_SPEC | MAX_SPEC | CORNER_LL | CORNER_HH | STRESS | TYPICAL
    input_values: Dict[str, Any]
    expected_outputs: Dict[str, Any]
    pass_criteria: Dict[str, Any]  # criterion_type: RANGE|ABSOLUTE|PERCENTAGE, pass_expression
    generation_reasoning: str = ""

@dataclass
class VerificationModel:
    primary_check: str
    tolerance_model: str  # ABSOLUTE | PERCENTAGE | ASYMMETRIC
    tolerance_value: float = 0.05
    guard_band_factor: float = 0.90
    guard_band_enabled: bool = True

@dataclass
class OracleResult:
    task_id: str
    parameter_symbol: str
    formulas: List[SymPyFormula]
    test_vectors: List[TestVector]
    verification_model: VerificationModel
    confidence: float = 0.95
    generation_notes: List[str] = field(default_factory=list)

# ==============================================================================
# Stage 5 & Repair: SPICE Testbench & Simulation Models (Appendix G)
# ==============================================================================
@dataclass
class TestbenchCode:
    testbench_id: str
    for_task_id: str
    parameter_symbol: str
    language: str = "spice"
    code: str = ""
    header: str = ""
    dut_instantiation: str = ""
    stimulus_block: str = ""
    measurement_block: str = ""
    verdict_block: str = ""
    filename: str = ""
    filepath: Optional[str] = None

@dataclass
class SimulationMeasurement:
    measurement_name: str
    measured_value: Optional[float]
    unit: str
    expected_value: Optional[float] = None
    pass_condition: str = ""
    passed: bool = False
    error_percentage: Optional[float] = None

@dataclass
class TestbenchExecutionResult:
    testbench_id: str
    status: str  # PASS | SYNTAX_ERROR | CONVERGENCE_ERROR | ASSERTION_FAIL | UNKNOWN
    raw_measurements: Dict[str, float]
    measurements: List[SimulationMeasurement]
    log_file: str
    error_details: str = ""
    execution_time: float = 0.0

@dataclass
class ClosedLoopRepairReport:
    initial_testbench_id: str
    final_status: str  # PASS | DESIGN_VIOLATION | MANUAL_REVIEW
    total_iterations: int
    iteration_history: List[Dict[str, Any]]
    final_testbench_code: str
    message: str
