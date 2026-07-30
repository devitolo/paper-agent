from __future__ import annotations

import html
import json
import mimetypes
import sqlite3
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from paper_agents.db import DEFAULT_DB_PATH, connect_db, init_db

FEEDBACK_STATUSES = [
    ("interested", "Interested"),
    ("read_later", "Read later"),
    ("not_interested", "Not interested"),
    ("reviewed", "Reviewed"),
]

FILTERS = [
    ("needs_review", "Needs review"),
    ("all", "All selected"),
    ("interested", "Interested"),
    ("read_later", "Read later"),
    ("reviewed", "Reviewed"),
    ("not_interested", "Not interested"),
]

SORTS = [
    ("latest", "Latest"),
    ("score", "Score"),
]

VIEWS = [
    ("full", "Full"),
    ("compact", "Condensed"),
]


def run_review_ui(host: str = "127.0.0.1", port: int = 8000, db_path: Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    server = ThreadingHTTPServer((host, port), make_handler(db_path))
    print(f"review UI running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nreview UI stopped")
    finally:
        server.server_close()


def make_handler(db_path: Path) -> type[BaseHTTPRequestHandler]:
    class ReviewHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/":
                params = urllib.parse.parse_qs(parsed.query)
                self.respond_html(
                    render_review_queue(
                        db_path,
                        saved=params.get("saved", [None])[0] == "1",
                        filter_value=params.get("filter", ["needs_review"])[0],
                        sort_value=params.get("sort", ["latest"])[0],
                        view_value=params.get("view", ["full"])[0],
                    )
                )
                return
            if parsed.path.startswith("/artifact/"):
                self.serve_artifact(db_path, parsed.path.removeprefix("/artifact/"))
                return
            self.send_error(HTTPStatus.NOT_FOUND, "Not found")

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/feedback":
                self.send_error(HTTPStatus.NOT_FOUND, "Not found")
                return

            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8")
            form = urllib.parse.parse_qs(body)
            try:
                paper_id = int(form.get("paper_id", [""])[0])
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid paper id")
                return

            status = form.get("status", [""])[0]
            notes = form.get("notes", [""])[0].strip()
            if status not in {value for value, _ in FEEDBACK_STATUSES}:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid feedback status")
                return

            save_feedback(db_path, paper_id=paper_id, status=status, notes=notes)
            return_to = form.get("return_to", ["/"])[0]
            redirect_to = add_query_param(return_to, "saved", "1")
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", redirect_to)
            self.end_headers()

        def serve_artifact(self, db_path: Path, artifact_id_text: str) -> None:
            try:
                artifact_id = int(artifact_id_text)
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid artifact id")
                return

            artifact = load_artifact(db_path, artifact_id)
            if not artifact:
                self.send_error(HTTPStatus.NOT_FOUND, "Artifact not found")
                return

            artifact_path = Path(artifact["path"])
            if not artifact_path.exists() or not artifact_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Artifact file not found")
                return

            content_type = mimetypes.guess_type(str(artifact_path))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(artifact_path.stat().st_size))
            self.end_headers()
            with artifact_path.open("rb") as handle:
                self.wfile.write(handle.read())

        def respond_html(self, content: str) -> None:
            data = content.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ReviewHandler


def render_review_queue(
    db_path: Path,
    *,
    saved: bool = False,
    filter_value: str = "needs_review",
    sort_value: str = "latest",
    view_value: str = "full",
) -> str:
    filter_value = normalize_choice(filter_value, FILTERS, "needs_review")
    sort_value = normalize_choice(sort_value, SORTS, "latest")
    view_value = normalize_choice(view_value, VIEWS, "full")
    cards = load_review_cards(db_path, filter_value=filter_value, sort_value=sort_value)
    saved_banner = '<div class="banner">Feedback saved.</div>' if saved else ""
    request_path = build_queue_href(filter_value, sort_value, view_value)
    card_html = "\n".join(render_card(card, view_value=view_value, return_to=request_path) for card in cards)
    if not card_html:
        card_html = '<section class="empty">No selected papers are waiting in the registry yet.</section>'

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Project Paper Review Queue</title>
  <style>{page_css()}</style>
</head>
<body>
  <main>
    <header class="topbar">
      <div>
        <h1>Project Paper Review Queue</h1>
        <p>{len(cards)} papers | {escape(selected_label(FILTERS, filter_value))} | sorted by {escape(selected_label(SORTS, sort_value)).lower()}</p>
      </div>
      <nav aria-label="Queue filters" class="control-group">
        {render_tabs(FILTERS, "filter", filter_value, filter_value, sort_value, view_value)}
      </nav>
      <nav aria-label="Sort order" class="control-group">
        {render_tabs(SORTS, "sort", sort_value, filter_value, sort_value, view_value)}
      </nav>
      <nav aria-label="View mode" class="control-group">
        {render_tabs(VIEWS, "view", view_value, filter_value, sort_value, view_value)}
      </nav>
    </header>
    {saved_banner}
    <div class="cards">{card_html}</div>
    <script>
      document.querySelectorAll("[data-copy-text]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          const target = document.getElementById(button.dataset.copyText);
          if (!target) return;
          await navigator.clipboard.writeText(target.value);
          button.textContent = "Copied";
          setTimeout(() => {{ button.textContent = "Copy prompt"; }}, 1400);
        }});
      }});
    </script>
  </main>
</body>
</html>"""


def render_card(card: dict[str, Any], *, view_value: str, return_to: str) -> str:
    tags = "".join(f'<span class="tag">{escape(keyword)}</span>' for keyword in card["matched_keywords"][:6])
    links = render_artifact_links(card["artifacts"])
    feedback_buttons = "".join(
        f'<button type="submit" name="status" value="{value}" class="{button_class(card, value)}">{label}</button>'
        for value, label in FEEDBACK_STATUSES
    )
    notes = escape(card.get("feedback_notes") or "")
    feedback_status = card.get("feedback_status")
    feedback_label = f'<span class="feedback-state">Current: {escape(feedback_status)}</span>' if feedback_status else ""
    summary = card["summary"]
    source_link = render_source_link(card)
    copy_id = f"copy-{card['id']}"
    copy_prompt = build_copy_prompt(card)
    compact_class = " compact" if view_value == "compact" else ""
    summary_html = render_summary(summary, compact=view_value == "compact")

    return f"""<article class="paper-card{compact_class}">
  <div class="card-head">
    <div>
      <h2>{escape(card["title"])}</h2>
      <p>{escape(card.get("published") or "date unknown")} | {escape(card["source"])} | {escape(card["source_id"])} | {source_link}</p>
    </div>
    <div class="score"><strong>{card["score"]:.1f}</strong><span>score</span></div>
  </div>
  <div class="tags">{tags}</div>
  {summary_html}
  <div class="links">{links}</div>
  <details class="copy-box">
    <summary>Copy prompt/link</summary>
    <textarea id="{copy_id}" readonly>{escape(copy_prompt)}</textarea>
    <button type="button" class="secondary" data-copy-text="{copy_id}">Copy prompt</button>
  </details>
  <form method="post" action="/feedback" class="feedback-form">
    <input type="hidden" name="paper_id" value="{card['id']}">
    <input type="hidden" name="return_to" value="{escape(return_to)}">
    <div class="feedback-row">{feedback_buttons}{feedback_label}</div>
    <label>Notes<textarea name="notes">{notes}</textarea></label>
    <button type="submit" name="status" value="{feedback_status or 'read_later'}" class="secondary">Save notes</button>
  </form>
</article>"""


def render_artifact_links(artifacts: dict[str, dict[str, Any]]) -> str:
    labels = {
        "pdf": "Open PDF",
        "triage_summary": "Open summary",
        "chatgpt_review": "Open review",
    }
    links = []
    for artifact_type, label in labels.items():
        artifact = artifacts.get(artifact_type)
        if artifact:
            links.append(f'<a href="/artifact/{artifact["id"]}" target="_blank" rel="noreferrer">{label}</a>')
    if not artifacts.get("chatgpt_review"):
        links.append('<span class="missing">No ChatGPT review yet</span>')
    return "".join(links)



def render_source_link(card: dict[str, Any]) -> str:
    url = card.get("url")
    if not url:
        return "source link unavailable"
    label = "arXiv" if card.get("source") == "arxiv" else "source"
    return f'<a class="source-link" href="{escape(url)}" target="_blank" rel="noreferrer">{label}</a>'


def render_summary(summary: dict[str, Any], *, compact: bool) -> str:
    problem = escape(summary.get("research_problem") or "Not extracted yet.")
    if compact:
        return f'<div class="compact-summary"><strong>Problem:</strong> {problem}</div>'
    return f"""<div class="summary-grid">
    <section><h3>Problem</h3><p>{problem}</p></section>
    <section><h3>Why it matters</h3><p>{escape(summary.get("why_it_matters") or "Not extracted yet.")}</p></section>
    <section><h3>Approach</h3><p>{escape(summary.get("approach") or "Not extracted yet.")}</p></section>
  </div>"""


def build_copy_prompt(card: dict[str, Any]) -> str:
    summary = card["summary"]
    return "\n".join(
        [
            "Please help me review this paper.",
            "",
            f"Title: {card['title']}",
            f"Date: {card.get('published') or summary.get('paper_date') or 'unknown'}",
            f"Link: {card.get('url') or 'unknown'}",
            f"Score: {card['score']:.1f}",
            "",
            f"Problem: {summary.get('research_problem') or 'Not extracted yet.'}",
            f"Why it matters: {summary.get('why_it_matters') or 'Not extracted yet.'}",
            f"Approach: {summary.get('approach') or 'Not extracted yet.'}",
            "",
            "Give me a listening-friendly, section-by-section summary and tell me whether this is worth reading further.",
        ]
    )

def button_class(card: dict[str, Any], status: str) -> str:
    return "primary" if card.get("feedback_status") == status else "secondary"


def load_review_cards(db_path: Path, *, filter_value: str, sort_value: str) -> list[dict[str, Any]]:
    init_db(db_path)
    where_clause = ""
    params: tuple[Any, ...] = ()
    if filter_value == "needs_review":
        where_clause = "WHERE latest_feedback.status IS NULL"
    elif filter_value != "all":
        where_clause = "WHERE latest_feedback.status = ?"
        params = (filter_value,)

    order_clause = (
        "ORDER BY latest_recommendation.score DESC, latest_recommendation.curator_run_id DESC, latest_recommendation.recommendation_order ASC"
        if sort_value == "score"
        else "ORDER BY latest_recommendation.curator_run_id DESC, latest_recommendation.recommendation_order ASC"
    )

    with connect_db(db_path) as connection:
        rows = connection.execute(
            f"""
            WITH latest_recommendation AS (
                SELECT
                    recommendations.id AS recommendation_id,
                    recommendations.paper_id,
                    recommendations.curator_run_id,
                    recommendations.recommendation_order,
                    recommendations.rationale,
                    curator_evaluations.score,
                    curator_evaluations.matched_signals_json,
                    ROW_NUMBER() OVER (
                        PARTITION BY recommendations.paper_id
                        ORDER BY recommendations.curator_run_id DESC, recommendations.recommendation_order ASC
                    ) AS row_number
                FROM recommendations
                LEFT JOIN curator_evaluations
                  ON curator_evaluations.curator_run_id = recommendations.curator_run_id
                 AND curator_evaluations.paper_id = recommendations.paper_id
            ), latest_feedback AS (
                SELECT
                    paper_id,
                    status,
                    notes,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id DESC) AS row_number
                FROM feedback
            ), primary_source AS (
                SELECT
                    paper_id,
                    source,
                    source_id,
                    url,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id ASC) AS row_number
                FROM paper_sources
            )
            SELECT
                papers.id,
                primary_source.source,
                primary_source.source_id,
                papers.title,
                papers.published,
                primary_source.url,
                latest_recommendation.score,
                latest_recommendation.matched_signals_json,
                latest_recommendation.rationale,
                latest_feedback.status,
                latest_feedback.notes
            FROM papers
            JOIN latest_recommendation
              ON latest_recommendation.paper_id = papers.id
             AND latest_recommendation.row_number = 1
            LEFT JOIN primary_source ON primary_source.paper_id = papers.id AND primary_source.row_number = 1
            LEFT JOIN latest_feedback ON latest_feedback.paper_id = papers.id AND latest_feedback.row_number = 1
            {where_clause}
            {order_clause}
            LIMIT 50
            """,
            params,
        ).fetchall()

        cards = []
        for row in rows:
            paper_id = row[0]
            artifacts = load_artifacts_for_paper(connection, paper_id)
            cards.append(
                {
                    "id": paper_id,
                    "source": row[1] or "unknown",
                    "source_id": row[2] or "unknown",
                    "title": row[3],
                    "published": row[4],
                    "url": row[5],
                    "score": float(row[6] or 0),
                    "matched_keywords": decode_json(row[7], []),
                    "ranking_reason": row[8],
                    "feedback_status": row[9],
                    "feedback_notes": row[10],
                    "artifacts": artifacts,
                    "summary": load_summary(artifacts.get("triage_summary")),
                }
            )
    return cards


def render_tabs(
    choices: list[tuple[str, str]],
    param: str,
    current_value: str,
    filter_value: str,
    sort_value: str,
    view_value: str,
) -> str:
    links = []
    for value, label in choices:
        next_filter = value if param == "filter" else filter_value
        next_sort = value if param == "sort" else sort_value
        next_view = value if param == "view" else view_value
        active = " active" if value == current_value else ""
        links.append(
            f'<a class="tab{active}" href="{build_queue_href(next_filter, next_sort, next_view)}">{escape(label)}</a>'
        )
    return "".join(links)


def build_queue_href(filter_value: str, sort_value: str, view_value: str) -> str:
    return "/?" + urllib.parse.urlencode({"filter": filter_value, "sort": sort_value, "view": view_value})


def add_query_param(path: str, key: str, value: str) -> str:
    parsed = urllib.parse.urlparse(path or "/")
    query = urllib.parse.parse_qs(parsed.query)
    query[key] = [value]
    return urllib.parse.urlunparse(("", "", parsed.path or "/", "", urllib.parse.urlencode(query, doseq=True), ""))


def normalize_choice(value: str, choices: list[tuple[str, str]], default: str) -> str:
    allowed = {choice for choice, _ in choices}
    return value if value in allowed else default


def selected_label(choices: list[tuple[str, str]], value: str) -> str:
    return dict(choices).get(value, value)


def load_artifacts_for_paper(connection: sqlite3.Connection, paper_id: int) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, artifact_type, path, model, metadata_json
        FROM artifacts
        WHERE paper_id = ?
        ORDER BY id DESC
        """,
        (paper_id,),
    ).fetchall()
    artifacts: dict[str, dict[str, Any]] = {}
    for row in rows:
        artifacts.setdefault(
            row[1],
            {
                "id": row[0],
                "artifact_type": row[1],
                "path": row[2],
                "model": row[3],
                "metadata": decode_json(row[4], {}),
            },
        )
    return artifacts


def load_artifact(db_path: Path, artifact_id: int) -> dict[str, Any] | None:
    init_db(db_path)
    with connect_db(db_path) as connection:
        row = connection.execute(
            "SELECT id, artifact_type, path, model, metadata_json FROM artifacts WHERE id = ?",
            (artifact_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "artifact_type": row[1],
        "path": row[2],
        "model": row[3],
        "metadata": decode_json(row[4], {}),
    }


def load_summary(artifact: dict[str, Any] | None) -> dict[str, Any]:
    if not artifact:
        return {}
    path = Path(artifact["path"])
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data.get("merged", {}) if isinstance(data, dict) else {}


def save_feedback(db_path: Path, *, paper_id: int, status: str, notes: str) -> None:
    init_db(db_path)
    with connect_db(db_path) as connection:
        connection.execute(
            "INSERT INTO feedback (paper_id, status, notes) VALUES (?, ?, ?)",
            (paper_id, status, notes),
        )


def decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def page_css() -> str:
    return """
:root { color-scheme: light dark; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #1f2328; }
main { max-width: 1120px; margin: 0 auto; padding: 24px; }
.topbar { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; border-bottom: 1px solid #d8dee4; padding-bottom: 14px; margin-bottom: 16px; }
h1 { margin: 0 0 4px; font-size: 24px; font-weight: 600; }
h2 { margin: 0 0 5px; font-size: 18px; font-weight: 600; }
h3 { margin: 0 0 6px; font-size: 13px; font-weight: 600; color: #57606a; }
p { margin: 0; }
.topbar p, .card-head p { color: #57606a; }
nav, .tags, .links, .feedback-row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.control-group { justify-content: flex-end; }
.source-link { color: #0969da; text-decoration: none; }
.source-link:hover { text-decoration: underline; }
.tab, .links a, button, .missing { border: 1px solid #d8dee4; border-radius: 6px; padding: 7px 10px; background: #ffffff; color: #24292f; text-decoration: none; font: inherit; }
.tab.active, button.primary { background: #1f6feb; color: #ffffff; border-color: #1f6feb; }
button.secondary { background: #f6f8fa; }
.banner { padding: 10px 12px; border: 1px solid #2da44e; background: #dafbe1; border-radius: 6px; margin-bottom: 12px; }
.cards { display: grid; gap: 14px; }
.paper-card, .empty { background: #ffffff; border: 1px solid #d8dee4; border-radius: 8px; padding: 16px; }
.card-head { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: start; }
.score { min-width: 64px; text-align: center; border: 1px solid #d8dee4; border-radius: 6px; padding: 8px; background: #f6f8fa; }
.score strong { display: block; font-size: 18px; }
.score span, .feedback-state { color: #57606a; font-size: 13px; }
.tag { border: 1px solid #d8dee4; color: #57606a; border-radius: 999px; padding: 3px 8px; font-size: 13px; }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin: 14px 0; }
.summary-grid section { min-width: 0; }
.compact-summary { margin: 10px 0; color: #57606a; line-height: 1.4; }
.links { margin-bottom: 12px; }
.copy-box { border-top: 1px solid #d8dee4; padding-top: 10px; margin-bottom: 12px; }
.copy-box summary { cursor: pointer; color: #57606a; font-size: 13px; margin-bottom: 8px; }
.copy-box textarea { min-height: 132px; margin-bottom: 8px; }
.paper-card.compact { padding: 12px; }
.paper-card.compact .tags, .paper-card.compact .links, .paper-card.compact .feedback-form { margin-top: 8px; }
.feedback-form { display: grid; gap: 10px; border-top: 1px solid #d8dee4; padding-top: 12px; }
label { display: grid; gap: 6px; color: #57606a; font-size: 13px; }
textarea { width: 100%; min-height: 48px; resize: vertical; border: 1px solid #d8dee4; border-radius: 6px; padding: 8px; font: inherit; color: #1f2328; background: #ffffff; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .topbar, .feedback-form { border-color: #30363d; }
  .topbar p, .card-head p, h3, .score span, .feedback-state, .tag, label, .compact-summary, .copy-box summary { color: #8b949e; }
  .source-link { color: #58a6ff; }
  .tab, .links a, button, .missing, .paper-card, .empty, textarea, .copy-box { background: #161b22; color: #e6edf3; border-color: #30363d; }
  button.secondary, .score { background: #21262d; }
  .banner { background: #0f2a1a; border-color: #238636; }
}
@media (max-width: 720px) {
  main { padding: 14px; }
  .topbar, .card-head { display: grid; grid-template-columns: 1fr; }
  .control-group { justify-content: flex-start; }
  .summary-grid { grid-template-columns: 1fr; }
  .score { width: fit-content; text-align: left; }
}
"""
