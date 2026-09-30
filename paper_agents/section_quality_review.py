"""Local-only blinded review UI for the section-quality experiment."""
from __future__ import annotations

import argparse
import hashlib
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


FIELDS = ("research_problem", "why_it_matters", "approach")
USEFUL_VALUES = {"yes", "partial", "no"}
USEFUL_RATING_KEYS = {"current_useful", "A_useful", "B_useful"}


def build_review_bundle(
    baseline_payload: dict[str, Any],
    abstract_payload: dict[str, Any],
    field_payload: dict[str, Any],
) -> dict[str, Any]:
    baseline = {int(row["paper_id"]): row for row in baseline_payload["results"]}
    abstract = {int(row["paper_id"]): row for row in abstract_payload["results"]}
    field_specific = {int(row["paper_id"]): row for row in field_payload["results"]}
    if set(baseline) != set(abstract) or set(baseline) != set(field_specific):
        raise ValueError("review inputs do not contain the same papers")
    tasks = []
    mapping = {}
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
            digest = hashlib.sha256(f"section-quality-20-v1:{paper_id}:{field}".encode()).digest()
            if digest[0] % 2:
                variants.reverse()
            task_id = f"{paper_id}:{field}"
            labeled = []
            mapping[task_id] = {}
            for label, variant in zip(("A", "B"), variants):
                mapping[task_id][label] = variant["method"]
                labeled.append({"label": label, **{k: v for k, v in variant.items() if k != "method"}})
            tasks.append(
                {
                    "task_id": task_id,
                    "paper_id": paper_id,
                    "title": base["title"],
                    "field": field,
                    "current": base["new_fields"].get(field),
                    "prior_signal": (base.get("original_signals") or {}).get(field),
                    "candidates": labeled,
                }
            )
    return {
        "review_version": "section-quality-20-v1",
        "tasks": tasks,
        "method_mapping": mapping,
    }


def public_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    return {"review_version": bundle["review_version"], "tasks": bundle["tasks"]}


def validate_decision(bundle: dict[str, Any], payload: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(payload, dict):
        raise ValueError("decision must be an object")
    task_id = payload.get("task_id")
    valid_ids = {task["task_id"] for task in bundle["tasks"]}
    if task_id not in valid_ids:
        raise ValueError("unknown task")
    ratings = payload.get("ratings")
    if not isinstance(ratings, dict):
        raise ValueError("ratings must be an object")
    if set(ratings) != USEFUL_RATING_KEYS:
        raise ValueError("ratings are incomplete")
    for key, value in ratings.items():
        if value not in USEFUL_VALUES:
            raise ValueError(f"invalid rating for {key}")
    return task_id, ratings


def save_decisions(path: Path, bundle: dict[str, Any], decisions: dict[str, Any]) -> None:
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
<title>Section Quality Review</title>
<style>
:root{color-scheme:dark;--bg:#08111d;--card:#111d2c;--border:#293a50;--text:#e8eef7;--muted:#9caabd;--accent:#36b7ef;--good:#39c978}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif}.wrap{max-width:1180px;margin:auto;padding:28px}.top{display:flex;justify-content:space-between;gap:20px;align-items:end;margin-bottom:18px}h1{margin:0;font-size:25px}.muted{color:var(--muted)}.progress{font-weight:700}.panel,.variant{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px}.head{display:flex;justify-content:space-between;gap:12px;align-items:start}.field{color:var(--accent);font-weight:800;text-transform:uppercase;letter-spacing:.07em}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}.current{grid-column:1/-1}.variant h3{margin:0 0 9px}.text{font-size:16px;min-height:48px}.evidence{margin-top:12px;color:#c5d2e3}.badge{display:inline-block;padding:2px 8px;border-radius:999px;background:#1d3147;color:#b9dff2;font-size:12px}.ratings{display:grid;gap:8px;margin-top:14px}.rating-row{display:flex;gap:7px;align-items:center;flex-wrap:wrap}.rating-row strong{min-width:92px}.rating{border:1px solid #40536b;background:#0d1724;color:var(--text);border-radius:8px;padding:7px 11px;cursor:pointer}.rating.selected{background:var(--accent);border-color:var(--accent);color:#03111a}.nav{display:flex;justify-content:space-between;gap:10px;margin-top:16px}.nav button,.export{border:1px solid #40536b;background:#142235;color:var(--text);border-radius:9px;padding:10px 15px;cursor:pointer}.saved{color:var(--good);min-height:22px;margin-top:9px}@media(max-width:760px){.grid{grid-template-columns:1fr}.top{align-items:start;flex-direction:column}}
</style></head><body><main class="wrap"><div class="top"><div><h1>Section Quality Review</h1><div class="muted">Compare the three versions. For each one, choose whether it gives you enough useful information.</div></div><div><div id="progress" class="progress"></div><button id="export" class="export">Export ratings</button></div></div><section id="app" class="panel"></section></main>
<script>
let bundle,decisions={},tasks=[],index=0;
const fieldNames={research_problem:'Problem',why_it_matters:'Why It Matters',approach:'Approach'};
async function init(){bundle=await (await fetch('/api')).json();decisions=bundle.decisions||{};tasks=bundle.tasks;index=Math.max(0,tasks.findIndex(t=>!complete(decisions[t.task_id])));render()}
function buttons(task,key,values){let chosen=(decisions[task.task_id]||{})[key];return values.map(v=>`<button class="rating ${chosen===v?'selected':''}" data-key="${key}" data-value="${v}">${v[0].toUpperCase()+v.slice(1)}</button>`).join('')}
function candidate(task,c){return `<article class="variant"><div class="head"><h3>Candidate ${c.label}</h3><span class="badge">${c.source_type==='source_abstract'?'Abstract':'Full text'}</span></div><div class="text">${esc(c.text||'No text')}</div><div class="ratings"><div class="rating-row"><strong>Useful</strong>${buttons(task,c.label+'_useful',['yes','partial','no'])}</div></div></article>`}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function complete(d){return ['current_useful','A_useful','B_useful'].every(k=>d&&d[k])}
function completedCount(){return tasks.filter(t=>complete(decisions[t.task_id])).length}
function render(){let t=tasks[index],done=completedCount();document.querySelector('#progress').textContent=`${done} of ${tasks.length} fields completed`;document.querySelector('#app').innerHTML=`<div class="head"><div><div class="field">${fieldNames[t.field]}</div><h2>${esc(t.title)}</h2><div class="muted">Paper ${Math.floor(index/3)+1} of ${tasks.length/3} · Field ${index+1} of ${tasks.length}</div></div><span class="badge">Prior signal: ${t.prior_signal}</span></div><div class="grid"><article class="variant current"><h3>Current production text</h3><div class="text">${esc(t.current||'No text')}</div><div class="ratings"><div class="rating-row"><strong>Useful</strong>${buttons(t,'current_useful',['yes','partial','no'])}</div></div></article>${t.candidates.map(c=>candidate(t,c)).join('')}</div><div id="saved" class="saved"></div><div class="nav"><button id="prev">Previous</button><button id="next">Next unfinished</button></div>`;document.querySelectorAll('.rating').forEach(b=>b.onclick=()=>rate(t,b));document.querySelector('#prev').onclick=()=>{index=Math.max(0,index-1);render()};document.querySelector('#next').onclick=nextUnfinished}
async function rate(t,b){let old=decisions[t.task_id]||{},d={current_useful:old.current_useful,A_useful:old.A_useful,B_useful:old.B_useful};d[b.dataset.key]=b.dataset.value;decisions[t.task_id]=d;document.querySelectorAll(`[data-key="${b.dataset.key}"]`).forEach(x=>x.classList.toggle('selected',x===b));if(complete(d)){let response=await fetch('/decision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:t.task_id,ratings:d})});if(response.ok){document.querySelector('#saved').textContent='Saved';document.querySelector('#progress').textContent=`${completedCount()} of ${tasks.length} fields completed`}}}
function nextUnfinished(){let start=index;for(let n=1;n<=tasks.length;n++){let j=(start+n)%tasks.length;if(!complete(decisions[tasks[j].task_id])){index=j;render();return}}index=Math.min(tasks.length-1,index+1);render()}
document.querySelector('#export').onclick=()=>location.href='/export';init();
</script></body></html>'''


def make_handler(bundle: dict[str, Any], decisions_path: Path):
    decisions_payload = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}
    decisions = {
        task_id: {key: value for key, value in ratings.items() if key in USEFUL_RATING_KEYS}
        for task_id, ratings in decisions_payload.get("decisions", {}).items()
    }

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
                task_id, ratings = validate_decision(bundle, json.loads(self.rfile.read(length)))
                decisions[task_id] = ratings
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
