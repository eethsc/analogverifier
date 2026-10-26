from typing import Dict, Type, Optional
from .base_designer import BaseCircuitDesigner
from .cmos_ota_designer import CmosOtaDesigner
from .dcdc_buck_designer import DcdcBuckDesigner
from .comparator_designer import HighSpeedComparatorDesigner
from .bjt_opamp_designer import BjtOpampDesigner

class DesignerRegistry:
    """Registry and factory for analog circuit netlist designers."""
    
    _designers: Dict[str, Type[BaseCircuitDesigner]] = {
        "OTA": CmosOtaDesigner,
        "CMOS_OTA": CmosOtaDesigner,
        "DCDC_BUCK": DcdcBuckDesigner,
        "DCDC": DcdcBuckDesigner,
        "BUCK": DcdcBuckDesigner,
        "HSC": HighSpeedComparatorDesigner,
        "COMPARATOR": HighSpeedComparatorDesigner,
        "OPAMP": BjtOpampDesigner,
        "LM741": BjtOpampDesigner
    }

    @classmethod
    def get_designer(cls, circuit_type: str) -> BaseCircuitDesigner:
        norm_type = circuit_type.upper().strip()
        designer_cls = cls._designers.get(norm_type)
        if not designer_cls:
            available = list(cls._designers.keys())
            raise ValueError(f"Unknown circuit type '{circuit_type}'. Available types: {available}")
        return designer_cls()

    @classmethod
    def list_supported_types(cls):
        return list(cls._designers.keys())
