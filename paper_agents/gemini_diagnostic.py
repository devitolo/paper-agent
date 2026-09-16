"""One bounded fixed-prompt CLI probe; the Python probe does not access the DB."""
from __future__ import annotations

import argparse
import json
import re
import time

from .feedback import parse_json_object
from .gemini_process import run_gemini


def diagnose(model: str, timeout: int = 60) -> dict:
    report = {"diagnostic_version": 1, "output_format": "stream-json",
              "timeout_seconds": timeout, "probe_loads_project_paper_data": False,
              "cli_configuration": "inherited; context/hooks/extensions may load"}
    try:
        version = run_gemini(["gemini", "--version"], 10).strip()
        report["cli_version"] = version if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?", version) else "unrecognized (output withheld)"
    except RuntimeError as error:
        report.update(status="version_check_failed", error=str(error))
        return report
    metadata = {}
    started = time.monotonic()
    try:
        response = run_gemini(
            ["gemini", "--model", model, "--output-format", "stream-json", "-p",
             'Return exactly {"ok":true} as your final answer. Do not use tools or read files.'],
            timeout, stream_json=True, metadata=metadata,
        )
        matches = parse_json_object(response) == {"ok": True}
        report.update(status="succeeded" if matches else "unexpected_answer",
                      expected_answer=matches)
    except RuntimeError as error:
        report.update(status="failed", error=str(error))
    report.update(metadata)
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 180:
        parser.error("--timeout must be between 1 and 180 seconds")
    report = diagnose(args.model, args.timeout)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
