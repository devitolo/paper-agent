"""Explicit synthetic export probe. No pipeline, database, source, or model calls."""
import json

from paper_agents import telemetry


@telemetry.pipeline_trace
def synthetic_trace():
    telemetry.attributes(synthetic=True)
    with telemetry.span("telemetry.smoke"):
        telemetry.attributes(outcome="ok")
    current = telemetry._current.get()
    return format(current.get_span_context().trace_id, "032x") if current is not None else None


def main():
    backend = telemetry._get_backend()
    if backend is None:
        print(json.dumps({"status": "disabled_or_unavailable"}))
        return 1
    trace_id = synthetic_trace()
    processor = backend.processor
    report = {"synthetic": True, "trace_id": trace_id,
              "pending_spans": processor.queue.unfinished_tasks,
              "dropped_spans": processor.dropped, "export_failures": processor.export_failures}
    report["status"] = "sent" if trace_id and not any(report[k] for k in ("pending_spans", "dropped_spans", "export_failures")) else "not_confirmed"
    print(json.dumps(report))
    return 0 if report["status"] == "sent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
