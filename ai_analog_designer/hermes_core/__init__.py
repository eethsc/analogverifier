"""
Hermes Agent Framework Integration for AnalogVerifier.
Wraps NousResearch Hermes Agent architecture, tuned and governed by Harness² (C1-C8).
"""

from .hermes_agent import HermesAgent, AgentTurnResult
from .hermes_tools import HermesAnalogTools
from .harness_hermes_tuner import HarnessHermesTuner
from .hermes_workflow import HermesAnalogWorkflowRunner
from .web_reference_search import WebCircuitReferenceSearcher

__all__ = [
    "HermesAgent",
    "AgentTurnResult",
    "HermesAnalogTools",
    "HarnessHermesTuner",
    "HermesAnalogWorkflowRunner",
    "WebCircuitReferenceSearcher",
]
