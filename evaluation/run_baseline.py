import json
from pathlib import Path

from scorer import load_cases, score_report


def main():
    root = Path(__file__).resolve().parent
    cases = load_cases(root / "cases.json")

    with open(root / "baseline.json", encoding="utf-8-sig") as file:
        baseline = json.load(file)

    reports = {
        item["case_id"]: item["report"]
        for item in baseline["reports"]
    }

    expected_ids = {case["case_id"] for case in cases}
    actual_ids = set(reports)

    if expected_ids != actual_ids:
        print("ERROR: Baseline case IDs do not match cases.json.")
        print("Missing:", sorted(expected_ids - actual_ids))
        print("Unexpected:", sorted(actual_ids - expected_ids))
        raise SystemExit(1)

    results = [score_report(case, reports[case["case_id"]]) for case in cases]

    print("\nDEVPILOT BASELINE EVALUATION")
    print("=" * 60)

    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"\n[{status}] {result['case_id']}")
        print(f"  Root-cause term coverage: {result['term_coverage']:.0%}")
        print(f"  Evidence coverage:        {result['evidence_coverage']:.0%}")

        if result["missing_terms"]:
            print("  Missing terms:", ", ".join(result["missing_terms"]))

        if result["missing_evidence"]:
            print("  Missing evidence:", result["missing_evidence"])

    passed = sum(result["passed"] for result in results)
    print("\n" + "=" * 60)
    print(f"Cases passed: {passed}/{len(results)}")
    print("Note: keyword coverage is a heuristic, not semantic proof.")

    output = {
        "total_cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
    }

    output_path = root / "baseline_results.json"
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(output, file, indent=2)

    print(f"Detailed results saved to: {output_path}")


if __name__ == "__main__":
    main()
