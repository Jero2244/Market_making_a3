"""Exclusive offline retrospective analysis; never edits original live evidence."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

from market_making.validation.validate_live import observation_acceptance, summarize, write_json


def reanalyze(original, output):
    # Only this blocked exploratory evidence can be assessed observationally.
    report = json.loads((original / "report.json").read_text(encoding="utf-8"))
    if (report.get("mode") != "exploratory-demo"
            or report["stages"]["session"]["status"] != "BLOCKED"):
        raise ValueError("Expected original blocked exploratory evidence")
    if output.resolve() == original.resolve() or original.resolve() in output.resolve().parents:
        raise ValueError("Retrospective output must be separate from original artifacts")
    provenance = {p.name: sha256(p.read_bytes()).hexdigest()
                  for p in sorted(original.iterdir()) if p.is_file()}
    events = [json.loads(line) for line in (original / "stream.jsonl").read_text(encoding="utf-8").splitlines()]
    identity = (report["selected"]["marketId"], report["selected"]["symbol"])
    summary = summarize(events, identity, expected_duration=report["duration_seconds"])
    # The original final duration marker supports normal recorder completion;
    # absent completion must never become a retrospective transport PASS.
    acceptance = observation_acceptance(summary)
    result = {
        "analysis_kind": "Retrospective offline reanalysis with corrected observational criteria; NOT a new credentialed run",
        "analyzed_at_utc": datetime.now(timezone.utc).isoformat(),
        "original_directory": original.as_posix(),
        "original_file_sha256": provenance,
        "original_started_at_utc": report["started_at_utc"],
        "original_ended_at_utc": report["ended_at_utc"],
        "original_stages": report["stages"],
        "original_exit_code": None,
        "original_inferred_exit_equivalent": 2,
        "original_exit_code_basis": "Process exit status not independently read; equivalent 2 inferred from original blocked exploratory stages",
        "corrected_criteria": "Selected Md with at least one nonempty requested entry; ordered one-local-close/retry/new-connection/resubscription/new-Md; duration deadline; no terminal rejection. Entry completeness is independent.",
        "retrospective_observational_stages": {
            "session": {"status": "BLOCKED", "reason": "No retrospective strict calendar/demo-session verification"},
            "stream": {"status": "PASS" if acceptance["stream_pass"] else "BLOCKED", "scope": "Original demo transport/data observations only"},
            "recovery": {"status": "PASS" if acceptance["recovery_pass"] else "BLOCKED", "scope": "Original induced-local-close transport observations only"},
        },
        "aggregate_exit_equivalent": 2,
        "observations": acceptance,
        "summary": summary,
        "no_new_network_activity": True,
        "limitations": [
            "No issued-token registry is recoverable offline; original retention protections are not independently revalidated against unknown issued tokens.",
            "LA null and TV zero are explicit availability limitations, not evidence of transport failure or positive trade activity.",
            "No strict-session, production-liquidity, per-entry update-semantics or genuine exchange-outage PASS is implied.",
        ],
    }
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "retrospective_analysis.json", result)
    if provenance != {p.name: sha256(p.read_bytes()).hexdigest()
                      for p in sorted(original.iterdir()) if p.is_file()}:
        raise ValueError("Original artifacts changed during offline reanalysis")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = reanalyze(args.original_dir, args.output_dir)
    for name, item in result["retrospective_observational_stages"].items():
        print(f"retrospective {name}: {item['status']}")
    print("New credentialed runs: 0; strict aggregate exit equivalent: 2")


if __name__ == "__main__":
    main()
