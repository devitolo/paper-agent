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
                self.respond_html(render_review_queue(db_path, status=params.get("status", [None])[0]))
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
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/?status=saved")
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


def render_review_queue(db_path: Path, status: str | None = None) -> str:
    cards = load_review_cards(db_path)
    saved_banner = '<div class="banner">Feedback saved.</div>' if status == "saved" else ""
    card_html = "\n".join(render_card(card) for card in cards)
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
        <p>{len(cards)} selected papers from SQLite</p>
      </div>
      <nav aria-label="Queue filters">
        <a class="tab active" href="/">Needs review</a>
        <a class="tab" href="/">All selected</a>
        <a class="tab" href="/">Read later</a>
      </nav>
    </header>
    {saved_banner}
    <div class="cards">{card_html}</div>
  </main>
</body>
</html>"""


def render_card(card: dict[str, Any]) -> str:
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

    return f"""<article class="paper-card">
  <div class="card-head">
    <div>
      <h2>{escape(card["title"])}</h2>
      <p>{escape(card.get("published") or "date unknown")} | {escape(card["source"])} | {escape(card["source_id"])}</p>
    </div>
    <div class="score"><strong>{card["score"]:.1f}</strong><span>score</span></div>
  </div>
  <div class="tags">{tags}</div>
  <div class="summary-grid">
    <section><h3>Problem</h3><p>{escape(summary.get("research_problem") or "Not extracted yet.")}</p></section>
    <section><h3>Why it matters</h3><p>{escape(summary.get("why_it_matters") or "Not extracted yet.")}</p></section>
    <section><h3>Approach</h3><p>{escape(summary.get("approach") or "Not extracted yet.")}</p></section>
  </div>
  <div class="links">{links}</div>
  <form method="post" action="/feedback" class="feedback-form">
    <input type="hidden" name="paper_id" value="{card['id']}">
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


def button_class(card: dict[str, Any], status: str) -> str:
    return "primary" if card.get("feedback_status") == status else "secondary"


def load_review_cards(db_path: Path) -> list[dict[str, Any]]:
    init_db(db_path)
    with connect_db(db_path) as connection:
        rows = connection.execute(
            """
            WITH latest AS (
                SELECT
                    paper_id,
                    score,
                    selected,
                    matched_keywords_json,
                    ranking_reason,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY run_id DESC) AS row_number
                FROM scout_candidates
            ), latest_feedback AS (
                SELECT
                    paper_id,
                    status,
                    notes,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id DESC) AS row_number
                FROM feedback
            )
            SELECT
                papers.id,
                papers.source,
                papers.source_id,
                papers.title,
                papers.published,
                papers.url,
                latest.score,
                latest.matched_keywords_json,
                latest.ranking_reason,
                latest_feedback.status,
                latest_feedback.notes
            FROM papers
            JOIN latest ON latest.paper_id = papers.id AND latest.row_number = 1 AND latest.selected = 1
            LEFT JOIN latest_feedback ON latest_feedback.paper_id = papers.id AND latest_feedback.row_number = 1
            ORDER BY papers.last_seen_at DESC, papers.id DESC
            LIMIT 50
            """
        ).fetchall()

        cards = []
        for row in rows:
            paper_id = row[0]
            artifacts = load_artifacts_for_paper(connection, paper_id)
            cards.append(
                {
                    "id": paper_id,
                    "source": row[1],
                    "source_id": row[2],
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
.links { margin-bottom: 12px; }
.feedback-form { display: grid; gap: 10px; border-top: 1px solid #d8dee4; padding-top: 12px; }
label { display: grid; gap: 6px; color: #57606a; font-size: 13px; }
textarea { width: 100%; min-height: 48px; resize: vertical; border: 1px solid #d8dee4; border-radius: 6px; padding: 8px; font: inherit; color: #1f2328; background: #ffffff; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .topbar, .feedback-form { border-color: #30363d; }
  .topbar p, .card-head p, h3, .score span, .feedback-state, .tag, label { color: #8b949e; }
  .tab, .links a, button, .missing, .paper-card, .empty, textarea { background: #161b22; color: #e6edf3; border-color: #30363d; }
  button.secondary, .score { background: #21262d; }
  .banner { background: #0f2a1a; border-color: #238636; }
}
@media (max-width: 720px) {
  main { padding: 14px; }
  .topbar, .card-head { display: grid; grid-template-columns: 1fr; }
  .summary-grid { grid-template-columns: 1fr; }
  .score { width: fit-content; text-align: left; }
}
"""
