
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "evaluation"))

from scorer import load_cases, score_report


def get_boundary_case():
    cases = load_cases(PROJECT_ROOT / "evaluation" / "cases.json")
    return next(c for c in cases if c["case_id"] == "off-by-one-total")


def correct_boundary_evidence():
    return [
        {
            "tool": "read_file",
            "result": (
                "FILE: calculator.py\n"
                "1 | def calculate_total(numbers):\n"
                "2 |     total = 0\n"
                "3 |     for i in range(len(numbers) - 1):\n"
                "4 |         total += numbers[i]\n"
                "5 |     return total\n"
            ),
        },
        {
            "tool": "read_file",
            "result": (
                "FILE: main.py\n"
                "1 | from calculator import calculate_total\n"
                "2 |\n"
                "3 | prices = [100, 200, 300]\n"
                "4 | print(calculate_total(prices))\n"
            ),
        },
    ]


def correct_boundary_answer():
    return (
        "The off-by-one error is caused by "
        "`range(len(numbers) - 1)`. This range skips the last "
        "element because its stop index is exclusive. The loop "
        "should process every list element, for example by using "
        "`range(len(numbers))`."
    )


def test_manifest_contains_three_cases():
    cases = load_cases(PROJECT_ROOT / "evaluation" / "cases.json")

    assert len(cases) == 3
    assert len({case["case_id"] for case in cases}) == 3


def test_correct_boundary_report_passes():
    case = get_boundary_case()

    report = {
        "answer": correct_boundary_answer(),
        "evidence": [
            {
                "tool": "read_file",
                "result": (
                    "FILE: calculator.py\n"
                    "3 | for i in range(len(numbers) - 1):\n"
                    "4 |     total += numbers[i]\n"
                ),
            },
            {
                "tool": "read_file",
                "result": "FILE: main.py\n3 | prices = [100, 200, 300]\n",
            },
        ],
    }

    result = score_report(case, report)

    assert result["passed"] is True
    assert result["evidence_coverage"] == 1.0

    root_cause = result["root_cause_validation"]
    assert root_cause["applicable"] is True
    assert root_cause["passed"] is True


def test_missing_evidence_fails():
    case = get_boundary_case()

    report = {
        "answer": correct_boundary_answer(),
        "evidence": [],
    }

    result = score_report(case, report)

    assert result["passed"] is False
    assert result["evidence_coverage"] == 0.0


def test_filename_and_line_in_separate_evidence_entries_do_not_match():
    case = get_boundary_case()

    report = {
        "answer": correct_boundary_answer(),
        "evidence": [
            {"result": "FILE: calculator.py\n2 | total = 0\n"},
            {"result": "FILE: unrelated.py\n3 | unrelated source\n"},
        ],
    }

    result = score_report(case, report)

    assert result["evidence_coverage"] == 0.0
    assert result["passed"] is False


def test_wrong_source_lines_fail_even_when_keywords_match():
    case = get_boundary_case()

    report = {
        "answer": correct_boundary_answer(),
        "evidence": [
            {
                "tool": "read_file",
                "result": (
                    "FILE: calculator.py\n"
                    "1 | def calculate_total(numbers):\n"
                    "2 |     total = 0\n"
                ),
            },
            {
                "tool": "read_file",
                "result": (
                    "FILE: main.py\n"
                    "1 | from calculator import calculate_total\n"
                ),
            },
        ],
    }

    result = score_report(case, report)

    assert result["term_coverage"] == 1.0
    assert result["evidence_coverage"] == 0.0
    assert result["passed"] is False


def test_wrong_diagnosis_fails_even_with_correct_evidence():
    case = get_boundary_case()

    report = {
        "answer": (
            "The off-by-one issue is not a bug. The range correctly "
            "processes every element and never skips the last element."
        ),
        "evidence": correct_boundary_evidence(),
    }

    result = score_report(case, report)

    assert result["evidence_coverage"] == 1.0
    assert result["contradictory_diagnosis"] is True
    assert result["passed"] is False


def test_structured_root_cause_accepts_correct_diagnosis():
    case = get_boundary_case()

    report = {
        "answer": correct_boundary_answer(),
        "evidence": correct_boundary_evidence(),
    }

    result = score_report(case, report)

    assert result["root_cause_validation"]["applicable"] is True
    assert result["root_cause_validation"]["passed"] is True
    assert result["passed"] is True


def test_structured_root_cause_rejects_missing_faulty_expression():
    case = get_boundary_case()

    report = {
        "answer": (
            "This off-by-one bug uses range and skips the last element. "
            "The loop should process every list element."
        ),
        "evidence": correct_boundary_evidence(),
    }

    result = score_report(case, report)

    assert (
        result["root_cause_validation"]["checks"]["faulty_expression"]
        is False
    )
    assert result["passed"] is False


def test_structured_root_cause_rejects_wrong_correct_behavior():
    case = get_boundary_case()

    report = {
        "answer": (
            "The off-by-one bug is caused by "
            "`range(len(numbers) - 1)`, which skips the last element. "
            "The loop should continue to skip it."
        ),
        "evidence": correct_boundary_evidence(),
    }

    result = score_report(case, report)

    assert (
        result["root_cause_validation"]["checks"]["correct_behavior"]
        is False
    )
    assert result["passed"] is False
