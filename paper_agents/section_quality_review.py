"""Local-only blinded review UI for the section-quality experiment."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


FIELDS = ("research_problem", "why_it_matters", "approach")
DECISION_VALUES = {"current", "replacement", "neither", "too_similar"}
REVIEW_VERSION = "section-quality-pairwise-v2"
DEFAULT_TASK_LIMIT = 8


def normalized_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def readable_candidate(value: Any, field: str) -> bool:
    text = normalized_text(value)
    words = text.split()
    limit = 36 if field == "approach" else 30
    return (
        6 <= len(words) <= limit
        and text.casefold() not in {"not extracted yet.", "not stated", "n/a"}
        and text.count(";") <= 1
    )


def text_similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left.casefold(), right.casefold()).ratio()


def candidate_readability_key(candidate: dict[str, Any], field: str) -> tuple[Any, ...]:
    text = normalized_text(candidate.get("text"))
    words = text.split()
    target = 24 if field == "approach" else 18
    return (abs(len(words) - target), len(words), candidate["method"])


def build_review_bundle(
    baseline_payload: dict[str, Any],
    abstract_payload: dict[str, Any],
    field_payload: dict[str, Any],
    *,
    task_limit: int = DEFAULT_TASK_LIMIT,
    preferred_task_ids: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    baseline = {int(row["paper_id"]): row for row in baseline_payload["results"]}
    abstract = {int(row["paper_id"]): row for row in abstract_payload["results"]}
    field_specific = {int(row["paper_id"]): row for row in field_payload["results"]}
    if set(baseline) != set(abstract) or set(baseline) != set(field_specific):
        raise ValueError("review inputs do not contain the same papers")
    eligible: dict[str, list[dict[str, Any]]] = {field: [] for field in FIELDS}
    for paper_id in sorted(baseline):
        base = baseline[paper_id]
        for field in FIELDS:
            variants = [
                {
                    "method": "abstract_only",
                    "text": abstract[paper_id]["minilm_qwen"].get(field),
                    "evidence": abstract[paper_id]["minilm_qwen"].get(f"{field}_evidence"),
                    "source_type": abstract[paper_id]["source_types"][field],
                },
                {
                    "method": "field_specific",
                    "text": field_specific[paper_id]["minilm_qwen"].get(field),
                    "evidence": field_specific[paper_id]["minilm_qwen"].get(f"{field}_evidence"),
                    "source_type": field_specific[paper_id]["source_types"][field],
                },
            ]
            current = normalized_text(base["new_fields"].get(field))
            readable = [variant for variant in variants if readable_candidate(variant["text"], field)]
            if not readable_candidate(current, field) or not readable:
                continue
            proposed = min(readable, key=lambda row: candidate_readability_key(row, field))
            replacement = normalized_text(proposed["text"])
            if text_similarity(current, replacement) >= 0.88:
                continue
            task_id = f"{paper_id}:{field}"
            eligible[field].append({
                "task_id": task_id,
                "paper_id": paper_id,
                "title": base["title"],
                "field": field,
                "current": current,
                "replacement": replacement,
                "replacement_method": proposed["method"],
                "selection_hash": hashlib.sha256(f"{REVIEW_VERSION}:{task_id}".encode()).hexdigest(),
            })
    all_eligible = {row["task_id"]: row for rows in eligible.values() for row in rows}
    if preferred_task_ids is not None:
        tasks = [all_eligible[task_id] for task_id in preferred_task_ids if task_id in all_eligible][:task_limit]
    else:
        tasks = []
    for rows in eligible.values():
        rows.sort(key=lambda row: row["selection_hash"])
    while preferred_task_ids is None and len(tasks) < task_limit and any(eligible.values()):
        for field in FIELDS:
            if eligible[field] and len(tasks) < task_limit:
                tasks.append(eligible[field].pop(0))
    mapping = {task["task_id"]: task.pop("replacement_method") for task in tasks}
    for task in tasks:
        task.pop("selection_hash")
    return {
        "review_version": REVIEW_VERSION,
        "tasks": tasks,
        "method_mapping": mapping,
        "selection": {
            "task_limit": task_limit,
            "comparison": "current_vs_one_readable_replacement",
            "discarded_prior_review": True,
        },
    }


def public_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    return {"review_version": bundle["review_version"], "tasks": bundle["tasks"]}


def validate_decision(bundle: dict[str, Any], payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("decision must be an object")
    task_id = payload.get("task_id")
    valid_ids = {task["task_id"] for task in bundle["tasks"]}
    if task_id not in valid_ids:
        raise ValueError("unknown task")
    decision = payload.get("decision")
    if decision not in DECISION_VALUES:
        raise ValueError("invalid decision")
    return task_id, decision


def save_decisions(path: Path, bundle: dict[str, Any], decisions: dict[str, str]) -> None:
    payload = {
        "review_version": bundle["review_version"],
        "method_mapping": bundle["method_mapping"],
        "decisions": decisions,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def render_page() -> str:
    return r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Section Quality Pairwise Review</title>
<style>
:root{color-scheme:dark;--bg:#08111d;--card:#111d2c;--border:#293a50;--text:#e8eef7;--muted:#9caabd;--accent:#36b7ef;--good:#39c978}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,sans-serif}.wrap{max-width:1050px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:18px}h1{margin:0;font-size:25px}.muted{color:var(--muted)}.progress{font-weight:700}.panel,.variant{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:20px}.head{display:flex;justify-content:space-between;gap:12px;align-items:start}.field{color:var(--accent);font-weight:800;text-transform:uppercase;letter-spacing:.07em}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}.variant h3{margin:0 0 12px}.text{font-size:18px;line-height:1.55}.choices{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:18px}.choice{border:1px solid #40536b;background:#142235;color:var(--text);border-radius:10px;padding:12px;cursor:pointer;font-weight:750}.choice:hover{border-color:var(--accent)}.choice.selected{background:var(--accent);border-color:var(--accent);color:#03111a}.nav{display:flex;justify-content:space-between;gap:10px;margin-top:14px}.nav button,.export{border:1px solid #40536b;background:#142235;color:var(--text);border-radius:9px;padding:10px 15px;cursor:pointer}.saved{color:var(--good);min-height:22px;margin-top:9px}@media(max-width:760px){.grid,.choices{grid-template-columns:1fr}.top{align-items:start;flex-direction:column}}
</style></head><body><main class="wrap"><div class="top"><div><h1>Section Quality Pairwise Review</h1><div class="muted">Read two short versions and choose which one would better help you decide whether to read the paper.</div></div><div><div id="progress" class="progress"></div><button id="export" class="export">Export decisions</button></div></div><section id="app" class="panel"></section></main>
<script>
let bundle,decisions={},tasks=[],index=0;
const fieldNames={research_problem:'Problem',why_it_matters:'Why It Matters',approach:'Approach'};
async function init(){bundle=await (await fetch('/api')).json();decisions=bundle.decisions||{};tasks=bundle.tasks;let first=tasks.findIndex(t=>!decisions[t.task_id]);index=first<0?0:first;render()}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function completedCount(){return tasks.filter(t=>decisions[t.task_id]).length}
function choice(value,label){return `<button class="choice" data-decision="${value}">${label}</button>`}
function render(){if(!tasks.length){document.querySelector('#progress').textContent='No readable comparisons';document.querySelector('#app').innerHTML='<p>No comparisons passed the readability and difference checks.</p>';return}let t=tasks[index],done=completedCount();document.querySelector('#progress').textContent=`${done} of ${tasks.length} comparisons completed`;document.querySelector('#app').innerHTML=`<div class="head"><div><div class="field">${fieldNames[t.field]}</div><h2>${esc(t.title)}</h2><div class="muted">Comparison ${index+1} of ${tasks.length}</div></div></div><div class="grid"><article class="variant"><h3>Current production</h3><div class="text">${esc(t.current)}</div></article><article class="variant"><h3>Proposed replacement</h3><div class="text">${esc(t.replacement)}</div></article></div><div class="choices">${choice('current','Current better')}${choice('replacement','Replacement better')}${choice('neither','Neither useful')}${choice('too_similar','Too similar')}</div><div id="saved" class="saved"></div><div class="nav"><button id="prev">Previous</button><button id="next">Next unfinished</button></div>`;document.querySelectorAll('.choice').forEach(b=>{b.classList.toggle('selected',decisions[t.task_id]===b.dataset.decision);b.onclick=()=>decide(t,b)});document.querySelector('#prev').onclick=()=>{index=Math.max(0,index-1);render()};document.querySelector('#next').onclick=nextUnfinished}
async function decide(t,b){let decision=b.dataset.decision;let response=await fetch('/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:t.task_id,decision})});if(response.ok){decisions[t.task_id]=decision;document.querySelector('#saved').textContent='Saved';setTimeout(nextUnfinished,250)}}
function nextUnfinished(){let start=index;for(let n=1;n<=tasks.length;n++){let j=(start+n)%tasks.length;if(!decisions[tasks[j].task_id]){index=j;render();return}}index=Math.min(tasks.length-1,index+1);render()}
document.querySelector('#export').onclick=()=>location.href='/export';init();
</script></body></html>'''


def make_handler(bundle: dict[str, Any], decisions_path: Path):
    decisions_payload = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}
    decisions = (
        {task_id: decision for task_id, decision in decisions_payload.get("decisions", {}).items()
         if decision in DECISION_VALUES}
        if decisions_payload.get("review_version") == bundle["review_version"] else {}
    )

    class Handler(BaseHTTPRequestHandler):
        def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/":
                data = render_page().encode()
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            elif self.path == "/api":
                self.send_json({**public_bundle(bundle), "decisions": decisions})
            elif self.path == "/export":
                payload = {"review_version": bundle["review_version"], "method_mapping": bundle["method_mapping"], "decisions": decisions}
                data = json.dumps(payload, indent=2, ensure_ascii=False).encode()
                self.send_response(HTTPStatus.OK); self.send_header("Content-Type", "application/json"); self.send_header("Content-Disposition", 'attachment; filename="section-quality-ratings.json"'); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
            else: self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if self.path != "/decision": self.send_error(HTTPStatus.NOT_FOUND); return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 10000: raise ValueError("invalid request size")
                task_id, decision = validate_decision(bundle, json.loads(self.rfile.read(length)))
                decisions[task_id] = decision
                save_decisions(decisions_path, bundle, decisions)
                self.send_json({"status": "saved", "completed": len(decisions)})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local blinded section-quality review UI")
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text())
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(bundle, args.decisions))
    print(f"Section quality review: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
