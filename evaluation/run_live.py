
import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


EVALUATION_DIR = Path(__file__).resolve().parent
API_BASE_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT_SECONDS = 180
OUTPUT_PATH = EVALUATION_DIR / "live_results.json"

if str(EVALUATION_DIR) not in sys.path:
    sys.path.insert(0, str(EVALUATION_DIR))

from scorer import load_cases, score_report


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def save_results(results, started_at):
    """Persist completed cases immediately."""

    passed = sum(item.get("passed", False) for item in results)

    output = {
        "evaluation_type": "live",
        "api_base_url": API_BASE_URL,
        "started_at": started_at,
        "updated_at": timestamp(),
        "total_cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "results": results,
        "note": (
            "Keyword and evidence scoring is heuristic, "
            "not proof of semantic correctness."
        ),
    }

    OUTPUT_PATH.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def request_json(method, endpoint, payload=None):
    url = f"{API_BASE_URL}{endpoint}"
    data = None

    headers = {"Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        url=url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urlopen(
            request,
            timeout=REQUEST_TIMEOUT_SECONDS,
        ) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)

    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"API returned HTTP {exc.code}: {detail}"
        ) from exc

    except (URLError, socket.timeout, TimeoutError) as exc:
        raise RuntimeError(
            f"Request to {url} failed or timed out after "
            f"{REQUEST_TIMEOUT_SECONDS} seconds: {exc}"
        ) from exc

    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"API returned invalid JSON for {endpoint}."
        ) from exc


def extract_report(record):
    """Extract answer and evidence from an API response."""

    candidates = [record]

    for key in ("report", "result", "investigation"):
        nested = record.get(key) if isinstance(record, dict) else None
        if isinstance(nested, dict):
            candidates.insert(0, nested)

    for candidate in candidates:
        answer = candidate.get("answer")
        evidence = candidate.get("evidence")

        if isinstance(answer, str) and isinstance(evidence, list):
            return {
                "answer": answer,
                "evidence": evidence,
            }

    raise ValueError(
        "API response did not contain an answer string "
        "and evidence list."
    )


def main():
    cases = load_cases(EVALUATION_DIR / "cases.json")

    if not cases:
        raise SystemExit("ERROR: No benchmark cases found.")

    started_at = timestamp()
    results = []

    print("\nDEVPILOT LIVE EVALUATION", flush=True)
    print("=" * 65, flush=True)
    print(f"Cases: {len(cases)}", flush=True)
    print(f"API: {API_BASE_URL}", flush=True)
    print(
        f"Request timeout: {REQUEST_TIMEOUT_SECONDS} seconds",
        flush=True,
    )

    try:
        health = request_json("GET", "/api/health")
        print(
            f"Health check: {health.get('status', 'unknown')}",
            flush=True,
        )

        repository_response = request_json(
            "GET",
            "/api/repositories",
        )

        connected_ids = {
            item.get("repository_id")
            for item in repository_response.get("repositories", [])
            if item.get("status") == "connected"
        }

    except RuntimeError as exc:
        print(f"ERROR: {exc}", flush=True)
        raise SystemExit(1) from exc

    for index, case in enumerate(cases, start=1):
        case_id = case["case_id"]
        repository_id = case["repository_id"]

        print(
            f"\n[{index}/{len(cases)}] {case_id}",
            flush=True,
        )
        print(f"Repository: {repository_id}", flush=True)
        print(f"Task: {case['description']}", flush=True)

        entry = {
            "case_id": case_id,
            "repository_id": repository_id,
            "started_at": timestamp(),
            "passed": False,
        }

        results.append(entry)

        if repository_id not in connected_ids:
            entry["error"] = "Repository is not connected."
            entry["finished_at"] = timestamp()
            save_results(results, started_at)
            print(f"ERROR: {entry['error']}", flush=True)
            continue

        try:
            print("Waiting for investigation response...", flush=True)

            saved_record = request_json(
                "POST",
                "/api/investigations",
                {
                    "repository_id": repository_id,
                    "description": case["description"],
                },
            )

            entry["investigation_id"] = saved_record.get("id")
            report = extract_report(saved_record)
            entry["report"] = report

            scored = score_report(case, report)
            entry["score"] = scored
            entry["passed"] = bool(scored["passed"])

            status = "PASS" if entry["passed"] else "FAIL"
            print(f"[{status}]", flush=True)
            print(
                f"Root-cause term coverage: "
                f"{scored['term_coverage']:.0%}",
                flush=True,
            )
            print(
                f"Evidence coverage: "
                f"{scored['evidence_coverage']:.0%}",
                flush=True,
            )

            if scored.get("missing_terms"):
                print(
                    "Missing terms: "
                    + ", ".join(scored["missing_terms"]),
                    flush=True,
                )

            if scored.get("missing_evidence"):
                print(
                    f"Missing evidence: {scored['missing_evidence']}",
                    flush=True,
                )

        except RuntimeError as exc:
            entry["error"] = str(exc)
            entry["timed_out_or_failed"] = True
            print(f"ERROR: {exc}", flush=True)

            entry["finished_at"] = timestamp()
            save_results(results, started_at)

            # Do not start another potentially expensive request.
            print(
                "Stopping evaluation to avoid overlapping "
                "or duplicate investigations.",
                flush=True,
            )
            break

        except Exception as exc:
            entry["error"] = (
                f"{type(exc).__name__}: {exc}"
            )
            print(f"ERROR: {entry['error']}", flush=True)

        entry["finished_at"] = timestamp()
        save_results(results, started_at)

        print(
            f"Progress saved to: {OUTPUT_PATH}",
            flush=True,
        )

        if index < len(cases):
            time.sleep(1)

    passed = sum(item.get("passed", False) for item in results)

    print("\n" + "=" * 65, flush=True)
    print(
        f"Completed cases: {len(results)}/{len(cases)}",
        flush=True,
    )
    print(
        f"Passed: {passed} | Failed or incomplete: "
        f"{len(results) - passed}",
        flush=True,
    )
    print(f"Results: {OUTPUT_PATH}", flush=True)

    if len(results) != len(cases) or passed != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
