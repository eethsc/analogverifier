import os
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional

@dataclass
class CircuitPort:
    name: str
    pin_number: int
    role: str  # POWER, GROUND, INPUT, OUTPUT, BIAS, CONTROL, ENABLE, FEEDBACK
    direction: str  # INPUT, OUTPUT, BIDIRECTIONAL, POWER, GROUND
    description: str
    nominal_voltage: float = 0.0
    voltage_range: Dict[str, float] = field(default_factory=dict)

@dataclass
class ComponentSizing:
    component_name: str
    component_type: str  # MOSFET, BJT, RESISTOR, CAPACITOR, INDUCTOR
    parameters: Dict[str, Any]
    design_rationale: str

@dataclass
class CircuitDesignResult:
    circuit_name: str
    circuit_type: str
    subcircuit_name: str
    ports: List[CircuitPort]
    components: List[ComponentSizing]
    design_parameters: Dict[str, Any]
    predicted_metrics: Dict[str, Any]
    netlist_code: str
    netlist_path: Optional[str] = None
    design_notes: List[str] = field(default_factory=list)

class BaseCircuitDesigner:
    """Base class for automated analog circuit netlist design and sizing."""
    
    def __init__(self, name: str, circuit_type: str):
        self.name = name
        self.circuit_type = circuit_type

    def design(self, target_specs: Dict[str, Any]) -> CircuitDesignResult:
        """Sizes circuit components from specifications and generates SPICE netlist."""
        raise NotImplementedError

    def save_netlist(self, result: CircuitDesignResult, output_dir: str = "generated_netlists") -> str:
        """Saves the generated netlist to a file."""
        os.makedirs(output_dir, exist_ok=True)
        filename = f"{result.subcircuit_name.lower()}.sub"
        filepath = os.path.join(output_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(result.netlist_code)
        result.netlist_path = os.path.abspath(filepath)
        return result.netlist_path
