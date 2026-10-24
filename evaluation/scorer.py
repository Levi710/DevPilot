
import json
import re
from pathlib import Path


def load_cases(manifest_path):
    """Load benchmark cases from JSON."""
    with open(manifest_path, encoding="utf-8-sig") as file:
        return json.load(file)


def _normalize_text(value):
    """Normalize whitespace and case for heuristic text comparisons."""
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def _normalize_expression(value):
    """Normalize code expressions while tolerating Markdown backticks."""
    value = str(value).strip()
    value = value.replace("`", "")
    value = re.sub(r"\s+", "", value)
    return value.lower()


def _term_matches(term, answer):
    """Match expected concepts while allowing known wording variations."""
    normalized_term = _normalize_text(term)
    normalized_answer = _normalize_text(answer)

    if normalized_term in normalized_answer:
        return True

    aliases = {
        "last element": (
            "last element",
            "final element",
            "last item",
            "final item",
            "last index",
            "final index",
            "numbers[2]",
            "last one",
        ),
    }

    variants = aliases.get(normalized_term, ())
    return any(variant in normalized_answer for variant in variants)


def _evidence_supports(expected, evidence_entry):
    """Check that a filename and expected line occur in the same evidence entry."""
    result = evidence_entry.get("result", "")

    if not isinstance(result, str):
        return False

    filename = expected["file"].replace("\\", "/").lower()
    expected_line = expected["line"]
    current_file = None

    for raw_line in result.splitlines():
        normalized = raw_line.replace("\\", "/").strip().lower()

        file_match = re.match(r"file:\s*(.+?)\s*$", normalized)
        if file_match:
            current_file = file_match.group(1).strip()
            continue

        numbered_line = re.match(r"^\s*(\d+)\s*\|", raw_line)

        if current_file and current_file.endswith(filename):
            if (
                numbered_line
                and int(numbered_line.group(1)) == expected_line
            ):
                return True

        search_match = re.search(
            r"(?<![\w./-])"
            + re.escape(filename)
            + r"\s*:\s*(\d+)\s*:",
            normalized,
        )

        if search_match and int(search_match.group(1)) == expected_line:
            return True

    return False


def _has_contradictory_diagnosis(case, answer):
    """
    Reject known contradictions for benchmark cases.

    These are explicit heuristics, not general semantic reasoning.
    """
    normalized = _normalize_text(answer)

    if case["case_id"] == "off-by-one-total":
        claims_every_element_is_processed = any(
            phrase in normalized
            for phrase in (
                "range correctly processes every element",
                "processes every element",
                "includes every element",
            )
        )

        denies_bug = any(
            phrase in normalized
            for phrase in (
                "not a bug",
                "isn't a bug",
                "is not a bug",
                "no bug",
            )
        )

        claims_last_element_is_not_skipped = any(
            phrase in normalized
            for phrase in (
                "never skips the last element",
                "does not skip the last element",
                "doesn't skip the last element",
            )
        )

        if (
            claims_every_element_is_processed
            or denies_bug
            or claims_last_element_is_not_skipped
        ):
            return True

    return False


def _check_structured_root_cause(case, answer):
    """
    Validate structured root-cause expectations when a case defines them.

    These checks use explicit textual heuristics. They do not prove semantic
    correctness and do not execute the proposed code.
    """
    expected = case.get("expected_root_cause")

    if not expected:
        return {
            "applicable": False,
            "passed": True,
            "checks": {},
            "missing": [],
        }

    normalized_answer = _normalize_text(answer)
    normalized_expression = _normalize_expression(answer)

    checks = {}
    missing = []

    faulty_expression = expected.get("faulty_expression", "").strip()

    if faulty_expression:
        expression_found = (
            _normalize_expression(faulty_expression)
            in normalized_expression
        )
        checks["faulty_expression"] = expression_found

        if not expression_found:
            missing.append("faulty_expression")

    actual_behavior = expected.get("actual_behavior", "").lower()

    if actual_behavior:
        if case.get("case_id") == "off-by-one-total":
            behavior_found = (
                any(
                    word in normalized_answer
                    for word in ("exclude", "skip", "omit", "drop")
                )
                and any(
                    word in normalized_answer
                    for word in ("last", "final", "numbers[2]")
                )
                and (
                    "element" in normalized_answer
                    or "index" in normalized_answer
                    or "numbers[2]" in normalized_answer
                )
            )
        else:
            behavior_found = actual_behavior in normalized_answer

        checks["actual_behavior"] = behavior_found

        if not behavior_found:
            missing.append("actual_behavior")

    correct_behavior = expected.get("correct_behavior", "").lower()

    if correct_behavior:
        if case.get("case_id") == "off-by-one-total":
            correction_found = (
                any(
                    phrase in normalized_answer
                    for phrase in (
                        "every element",
                        "all elements",
                        "each element",
                        "every list element",
                        "all list elements",
                        "range(len(numbers))",
                        "for num in numbers",
                    )
                )
                and any(
                    word in normalized_answer
                    for word in (
                        "process",
                        "include",
                        "iterate",
                        "visit",
                        "range(len(numbers))",
                        "for num in numbers",
                    )
                )
            )
        else:
            correction_found = correct_behavior in normalized_answer

        checks["correct_behavior"] = correction_found

        if not correction_found:
            missing.append("correct_behavior")

    return {
        "applicable": True,
        "passed": all(checks.values()),
        "checks": checks,
        "missing": missing,
    }


def score_report(case, report):
    """Score an investigation report against a benchmark case."""
    if not isinstance(report, dict):
        report = {}

    answer = report.get("answer", "")

    if not isinstance(answer, str):
        answer = ""

    evidence = report.get("evidence", [])

    if not isinstance(evidence, list):
        evidence = []

    terms = case.get("expected_root_cause_terms", [])

    matched_terms = [
        term for term in terms
        if _term_matches(term, answer)
    ]

    missing_terms = [
        term for term in terms
        if not _term_matches(term, answer)
    ]

    term_coverage = (
        len(matched_terms) / len(terms)
        if terms
        else 1.0
    )

    expected_evidence = case.get("expected_evidence", [])

    matched_evidence = [
        item
        for item in expected_evidence
        if any(
            _evidence_supports(item, entry)
            for entry in evidence
            if isinstance(entry, dict)
        )
    ]

    evidence_coverage = (
        len(matched_evidence) / len(expected_evidence)
        if expected_evidence
        else 1.0
    )

    contradictory_diagnosis = _has_contradictory_diagnosis(
        case,
        answer,
    )

    root_cause_result = _check_structured_root_cause(
        case,
        answer,
    )

    analysis_passed = (
        term_coverage >= 0.75
        and evidence_coverage == 1.0
        and not contradictory_diagnosis
        and root_cause_result["passed"]
    )

    verification = report.get("verification") if isinstance(report, dict) else None
    reproduction_confirmed = False
    reproduction_resolved = False
    fix_verified = False
    regression_suite_passed = None
    verification_status = "unverified"
    timeout_enforced = False
    dependency_classified = False
    false_positive_rejected = False

    if isinstance(verification, dict):
        reproduction_confirmed = bool(
            verification.get("reproduction_confirmed", verification.get("pre_fix", {}).get("reproduced"))
        )
        reproduction_resolved = bool(
            verification.get("reproduction_resolved", verification.get("valid"))
        )
        regression_suite_passed = verification.get("regression_suite_passed")
        # fix_verified strictly requires reproduction resolved AND no regression failures
        fix_verified = bool(
            verification.get("fix_verified")
            if "fix_verified" in verification
            else (reproduction_confirmed and reproduction_resolved and (regression_suite_passed is not False))
        )
        if regression_suite_passed is False:
            fix_verified = False

        verification_status = verification.get("verification_status", "unverified")
        timeout_enforced = bool(verification.get("timeout_enforced", verification.get("timed_out")))
        dependency_classified = bool(verification.get("dependency_classified", verification.get("rule_violation") == "import_error"))
        false_positive_rejected = bool(not reproduction_confirmed and case.get("category") == "false_positive_rejection")
    else:
        # Check evidence for tool calls if raw evidence objects are present
        for item in (report.get("evidence") or []):
            if isinstance(item, dict):
                if item.get("tool") == "run_reproduction_test" and item.get("status") == "success":
                    try:
                        res_obj = json.loads(item.get("result", "{}"))
                        if res_obj.get("reproduced"):
                            reproduction_confirmed = True
                        if res_obj.get("timed_out"):
                            timeout_enforced = True
                        if res_obj.get("rule_violation") == "import_error":
                            dependency_classified = True
                    except Exception:
                        pass
                if item.get("tool") == "validate_patch" and item.get("status") == "success":
                    try:
                        res_obj = json.loads(item.get("result", "{}"))
                        reproduction_resolved = bool(res_obj.get("reproduction_resolved", res_obj.get("valid")))
                        if "regression_suite_passed" in res_obj:
                            regression_suite_passed = res_obj["regression_suite_passed"]
                        if res_obj.get("verification_status"):
                            verification_status = res_obj["verification_status"]
                        fix_verified = bool(
                            res_obj.get("fix_verified")
                            if "fix_verified" in res_obj
                            else (reproduction_confirmed and reproduction_resolved and (regression_suite_passed is not False))
                        )
                        if regression_suite_passed is False:
                            fix_verified = False
                    except Exception:
                        pass

    category = case.get("category", "reliable_reproduction")
    if category == "false_positive_rejection":
        # For bogus bugs, rejection of reproduction is the expected safe behavior
        false_positive_rejected = not reproduction_confirmed

    false_positive = bool(contradictory_diagnosis)

    return {
        "case_id": case["case_id"],
        "category": category,
        "passed": analysis_passed,
        "analysis_passed": analysis_passed,
        "term_coverage": round(term_coverage, 2),
        "evidence_coverage": round(evidence_coverage, 2),
        "contradictory_diagnosis": contradictory_diagnosis,
        "reproduction_confirmed": reproduction_confirmed,
        "reproduction_resolved": reproduction_resolved,
        "reproduction_success": reproduction_confirmed,
        "fix_verified": fix_verified,
        "regression_suite_passed": regression_suite_passed,
        "verification_status": verification_status,
        "false_positive": false_positive,
        "false_positive_rejected": false_positive_rejected,
        "timeout_enforced": timeout_enforced,
        "dependency_classified": dependency_classified,
        "root_cause_validation": root_cause_result,
        "matched_terms": matched_terms,
        "missing_terms": missing_terms,
        "matched_evidence": matched_evidence,
        "missing_evidence": [
            item for item in expected_evidence
            if item not in matched_evidence
        ],
    }


def compute_metrics(results):
    """Aggregate multi-dimensional benchmark metrics separately."""
    total = len(results)
    if total == 0:
        return {}

    analysis_passed_count = sum(1 for r in results if r.get("analysis_passed", False))
    repro_confirmed_count = sum(1 for r in results if r.get("reproduction_confirmed", False))
    repro_resolved_count = sum(1 for r in results if r.get("reproduction_resolved", False))
    fix_verified_count = sum(1 for r in results if r.get("fix_verified", False))
    regression_caught_count = sum(
        1 for r in results if r.get("verification_status") == "regression_failed" or r.get("regression_suite_passed") is False
    )
    fp_rejected_count = sum(1 for r in results if r.get("false_positive_rejected", False))
    timeout_enforced_count = sum(1 for r in results if r.get("timeout_enforced", False))
    dependency_classified_count = sum(1 for r in results if r.get("dependency_classified", False))

    return {
        "total_cases": total,
        "analysis_passed": analysis_passed_count,
        "analysis_pass_rate": round(analysis_passed_count / total, 2),
        "reproduction_confirmed": repro_confirmed_count,
        "reproduction_confirmed_rate": round(repro_confirmed_count / total, 2),
        "reproduction_resolved": repro_resolved_count,
        "fix_verified": fix_verified_count,
        "fix_verified_rate": round(fix_verified_count / total, 2),
        "regressions_caught": regression_caught_count,
        "false_positives_rejected": fp_rejected_count,
        "timeouts_enforced": timeout_enforced_count,
        "dependencies_classified": dependency_classified_count,
    }


def main():
    root = Path(__file__).resolve().parent
    cases = load_cases(root / "cases.json")

    print(f"Loaded {len(cases)} benchmark cases.")

    for case in cases:
        print(f"- {case['case_id']}: {case['description']}")
        print(
            f"  Expected evidence entries: "
            f"{len(case.get('expected_evidence', []))}"
        )

        if case.get("expected_root_cause"):
            print("  Structured root-cause validation: enabled")
        else:
            print("  Structured root-cause validation: not configured")

    print("\nManifest validation complete.")
    print("Use run_baseline.py to score saved investigation reports.")


if __name__ == "__main__":
    main()
