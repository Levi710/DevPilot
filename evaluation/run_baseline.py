import json
from pathlib import Path

from scorer import compute_metrics, load_cases, score_report


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
    metrics = compute_metrics(results)

    print("\nDEVPILOT BASELINE EVALUATION (UNAMBIGUOUS METRIC BREAKDOWN)")
    print("=" * 65)

    for result in results:
        status = "PASS" if result["analysis_passed"] else "FAIL"
        print(f"\n[{status}] {result['case_id']} ({result.get('category', 'general')})")
        print(f"  Analysis Pass:            {result['analysis_passed']}")
        print(f"  Term coverage:            {result['term_coverage']:.0%}")
        print(f"  Evidence coverage:        {result['evidence_coverage']:.0%}")
        print(f"  Reproduction confirmed:   {result.get('reproduction_confirmed', False)}")
        print(f"  Fix verified:             {result.get('fix_verified', False)}")
        print(f"  Regression suite passed:  {result.get('regression_suite_passed')}")
        print(f"  Verification status:      {result.get('verification_status')}")

        if result["missing_terms"]:
            print("  Missing terms:", ", ".join(result["missing_terms"]))

        if result["missing_evidence"]:
            print("  Missing evidence:", result["missing_evidence"])

    print("\n" + "=" * 65)
    print("INDEPENDENT METRIC SUMMARY:")
    print(f"  Total Cases:                  {metrics['total_cases']}")
    print(f"  Analysis Quality Pass Rate:   {metrics['analysis_passed']}/{metrics['total_cases']} ({metrics['analysis_pass_rate']:.0%})")
    print(f"  Reproduction Confirmed Rate:  {metrics['reproduction_confirmed']}/{metrics['total_cases']} ({metrics['reproduction_confirmed_rate']:.0%})")
    print(f"  Fix Verification Rate:        {metrics['fix_verified']}/{metrics['total_cases']} ({metrics['fix_verified_rate']:.0%})")
    print(f"  Regressions Caught:           {metrics['regressions_caught']}")
    print(f"  False Positives Rejected:     {metrics['false_positives_rejected']}")
    print(f"  Timeouts Enforced:            {metrics['timeouts_enforced']}")
    print(f"  Dependencies Classified:      {metrics['dependencies_classified']}")
    print("=" * 65)
    print("Note: Explanation quality and empirical sandbox verification are reported independently.")

    output = {
        "total_cases": len(results),
        "passed": metrics["analysis_passed"],
        "metrics": metrics,
        "results": results,
    }

    output_path = root / "baseline_results.json"
    with open(output_path, "w", encoding="utf-8") as file:
        json.dump(output, file, indent=2)

    print(f"Detailed results saved to: {output_path}")


if __name__ == "__main__":
    main()
