import re
from typing import Dict, List, Any, Tuple, Optional
from .data_models import (
    CircuitInformation, TaskExtractionResult,
    ValidationReport, ValidationIssue
)

class Stage25CrossValidator:
    """
    Stage 2.5: Cross Validation Pipeline (Paper Appendix E, Algorithms 12 & 13)
    Verifies semantic consistency between Circuit Information, Task Decomposition, and Port Analysis:
    - Phase 1: Port Existence Validation (with Algorithm 13 Fuzzy Port Resolution)
    - Phase 2: Parameter Reference Validation
    - Phase 3: Evidence Reference Validation
    - Phase 4: Condition Completeness Check
    - Phase 5: Data Integrity Check
    - Phase 6: Aggregate Results & Strict/Lenient Gate Decision
    """
    def __init__(self, strict_mode: bool = False):
        self.strict_mode = strict_mode
        self.issue_counter = 0

    def validate(
        self,
        circuit_info: CircuitInformation,
        task_result: TaskExtractionResult,
        known_ports: Optional[List[str]] = None
    ) -> ValidationReport:
        issues: List[ValidationIssue] = []

        if known_ports is None:
            known_ports = [p.pin_name for p in circuit_info.pins]

        total_checks = 0
        passed_checks = 0

        # Phase 1: Port Existence Validation
        for t in task_result.tasks:
            all_task_ports = t.stimulus_ports + t.measurement_ports + t.control_ports
            for p_name in all_task_ports:
                total_checks += 1
                matched_port, confidence = self._fuzzy_port_match(p_name, known_ports)
                if matched_port is None:
                    issue = self._create_issue(
                        category="PORT_EXISTENCE",
                        severity="warning",
                        message=f"Port '{p_name}' in task '{t.task_id}' not found in known pins {known_ports}.",
                        context={"task_id": t.task_id, "query_port": p_name},
                        suggestion=f"Map '{p_name}' to closest valid pin or external node.",
                        affected_items=[p_name]
                    )
                    issues.append(issue)
                else:
                    passed_checks += 1
                    # If fuzzy matched with high confidence, auto-heal
                    if matched_port != p_name and confidence >= 0.8:
                        if p_name in t.stimulus_ports:
                            t.stimulus_ports = [matched_port if x == p_name else x for x in t.stimulus_ports]
                        if p_name in t.measurement_ports:
                            t.measurement_ports = [matched_port if x == p_name else x for x in t.measurement_ports]
                        if p_name in t.control_ports:
                            t.control_ports = [matched_port if x == p_name else x for x in t.control_ports]

        # Phase 2: Parameter Reference Validation
        param_symbols = {p.symbol for p in circuit_info.parameters}
        for t in task_result.tasks:
            total_checks += 1
            if not t.primary_parameter:
                issues.append(self._create_issue(
                    category="PARAMETER_REFERENCE",
                    severity="error",
                    message=f"Task '{t.task_id}' has no primary parameter defined.",
                    context={"task_id": t.task_id}
                ))
            elif t.primary_parameter not in param_symbols:
                # Check normalized match
                norm_prim = re.sub(r'[^a-zA-Z0-9]', '_', t.primary_parameter).lower()
                matched = any(re.sub(r'[^a-zA-Z0-9]', '_', s).lower() == norm_prim for s in param_symbols)
                if not matched:
                    issues.append(self._create_issue(
                        category="PARAMETER_REFERENCE",
                        severity="error",
                        message=f"Primary parameter '{t.primary_parameter}' not in circuit parameter registry.",
                        context={"task_id": t.task_id, "primary_parameter": t.primary_parameter}
                    ))
                else:
                    passed_checks += 1
            else:
                passed_checks += 1

        # Phase 3: Evidence Reference Validation
        for t in task_result.tasks:
            total_checks += 1
            if not t.math_evidences:
                issues.append(self._create_issue(
                    category="EVIDENCE_REFERENCE",
                    severity="warning",
                    message=f"Task '{t.task_id}' ({t.task_name}) lacks mathematical evidence / bound constraints.",
                    context={"task_id": t.task_id}
                ))
            else:
                passed_checks += 1

        # Phase 4: Condition Completeness Check
        for t in task_result.tasks:
            for ev in t.math_evidences:
                total_checks += 1
                if not ev.test_conditions:
                    issues.append(self._create_issue(
                        category="CONDITION_COMPLETENESS",
                        severity="info",
                        message=f"Evidence for '{ev.parameter_symbol}' in task '{t.task_id}' has no explicit test conditions.",
                        context={"task_id": t.task_id}
                    ))
                else:
                    passed_checks += 1

        # Phase 5: Data Integrity Check
        seen_task_ids = set()
        for t in task_result.tasks:
            total_checks += 1
            if t.task_id in seen_task_ids:
                issues.append(self._create_issue(
                    category="DATA_INTEGRITY",
                    severity="error",
                    message=f"Duplicate task identifier detected: '{t.task_id}'",
                    context={"task_id": t.task_id}
                ))
            else:
                seen_task_ids.add(t.task_id)
                passed_checks += 1

        # Phase 6: Aggregate Results
        error_count = len([i for i in issues if i.severity == "error"])
        warning_count = len([i for i in issues if i.severity == "warning"])
        info_count = len([i for i in issues if i.severity == "info"])
        failed_checks = error_count + warning_count

        if self.strict_mode:
            validation_passed = (error_count == 0 and warning_count == 0)
        else:
            validation_passed = (error_count == 0)

        return ValidationReport(
            validation_passed=validation_passed,
            total_checks=total_checks,
            passed_checks=passed_checks,
            failed_checks=failed_checks,
            error_count=error_count,
            warning_count=warning_count,
            info_count=info_count,
            issues=issues
        )

    def _fuzzy_port_match(self, query: str, known_ports: List[str]) -> Tuple[Optional[str], float]:
        """Algorithm 13: Fuzzy Port Name Resolution."""
        if not query or not known_ports:
            return None, 0.0

        # Exact match
        if query in known_ports:
            return query, 1.0

        q_norm = self._normalize_name(query)
        # Normalized match
        for p in known_ports:
            if self._normalize_name(p) == q_norm:
                return p, 0.95

        # Substring match
        for p in known_ports:
            p_norm = self._normalize_name(p)
            if p_norm in q_norm or q_norm in p_norm:
                return p, 0.80

        # Functional aliases match (e.g. VDD <-> IN, OUT <-> SW, FB <-> VOUT)
        aliases = {
            "vcc": ["vdd", "vin", "in"],
            "vee": ["vss", "gnd"],
            "inp": ["in+", "1", "in_p"],
            "inm": ["in-", "2", "in_n"],
            "sw": ["out", "lx"],
            "out": ["sw", "5", "vout"]
        }
        for alias_key, alias_vals in aliases.items():
            if q_norm == alias_key:
                for p in known_ports:
                    if self._normalize_name(p) in alias_vals:
                        return p, 0.75

        return None, 0.0

    def _normalize_name(self, s: str) -> str:
        return re.sub(r'[^a-zA-Z0-9]', '', s).lower()

    def _create_issue(
        self,
        category: str,
        severity: str,
        message: str,
        context: Dict[str, Any],
        suggestion: str = "",
        affected_items: Optional[List[str]] = None
    ) -> ValidationIssue:
        self.issue_counter += 1
        return ValidationIssue(
            issue_id=f"V{self.issue_counter:03d}",
            category=category,
            severity=severity,
            message=message,
            context=context,
            suggestion=suggestion,
            affected_items=affected_items or []
        )
