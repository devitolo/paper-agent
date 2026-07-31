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
from paper_agents.feedback import ProfileProvider, apply_feedback_to_profile, ingest_feedback_blob

ASSET_DIR = Path(__file__).with_name("assets")
LOGO_ASSETS = {"logo_light.png", "logo_dark.png"}

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
                        profile_apply_failed=params.get("profile_apply_failed", [None])[0] == "1",
                        filter_value=params.get("filter", ["needs_review"])[0],
                        sort_value=params.get("sort", ["latest"])[0],
                        view_value=params.get("view", ["full"])[0],
                    )
                )
                return
            if parsed.path.startswith("/artifact/"):
                self.serve_artifact(db_path, parsed.path.removeprefix("/artifact/"))
                return
            if parsed.path.startswith("/assets/"):
                self.serve_asset(parsed.path.removeprefix("/assets/"))
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
            feedback_content = form.get("notes", [""])[0]
            notes = feedback_content.strip()
            recommendation_id = parse_optional_int(form.get("recommendation_id", [""])[0])
            if status not in {value for value, _ in FEEDBACK_STATUSES}:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid feedback status")
                return

            result = save_feedback(
                db_path,
                paper_id=paper_id,
                status=status,
                notes=notes,
                feedback_content=feedback_content,
                recommendation_id=recommendation_id,
                source="review_queue_ui",
            )
            return_to = form.get("return_to", ["/"])[0]
            redirect_to = add_query_param(return_to, "saved", "1")
            if result.get("profile_apply_error"):
                redirect_to = add_query_param(redirect_to, "profile_apply_failed", "1")
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

        def serve_asset(self, asset_name: str) -> None:
            if asset_name not in LOGO_ASSETS:
                self.send_error(HTTPStatus.NOT_FOUND, "Asset not found")
                return

            asset_path = ASSET_DIR / asset_name
            if not asset_path.exists() or not asset_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Asset file not found")
                return

            content_type = mimetypes.guess_type(str(asset_path))[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(asset_path.stat().st_size))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            with asset_path.open("rb") as handle:
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
    profile_apply_failed: bool = False,
    filter_value: str = "needs_review",
    sort_value: str = "latest",
    view_value: str = "full",
) -> str:
    filter_value = normalize_choice(filter_value, FILTERS, "needs_review")
    sort_value = normalize_choice(sort_value, SORTS, "latest")
    view_value = normalize_choice(view_value, VIEWS, "full")
    cards = load_review_cards(db_path, filter_value=filter_value, sort_value=sort_value)
    banners = []
    if saved:
        banners.append('<div class="banner">Feedback saved.</div>')
    if profile_apply_failed:
        banners.append('<div class="banner warning">Profile auto-apply failed. Feedback was saved; run feedback apply manually when ready.</div>')
    saved_banner = "".join(banners)
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
        <h1 class="brand-title"><picture><source srcset="/assets/logo_dark.png" media="(prefers-color-scheme: dark)"><img src="/assets/logo_light.png" alt="" class="brand-logo"></picture><span>Project Paper Review Queue</span></h1>
        <p>{len(cards)} papers | {escape(selected_label(FILTERS, filter_value))} | sorted by {escape(selected_label(SORTS, sort_value)).lower()}</p>
      </div>
      <form method="get" action="/" class="queue-controls">
        {render_select(FILTERS, "filter", filter_value, "Status")}
        {render_select(SORTS, "sort", sort_value, "Sort")}
        {render_select(VIEWS, "view", view_value, "View")}
        <button type="submit" class="secondary">Apply</button>
      </form>
    </header>
    {saved_banner}
    <div class="cards">{card_html}</div>
    <script>
      document.querySelectorAll("[data-copy-value]").forEach((button) => {{
        const originalText = button.textContent;
        button.addEventListener("click", async () => {{
          await navigator.clipboard.writeText(button.dataset.copyValue);
          button.textContent = "Copied";
          setTimeout(() => {{ button.textContent = originalText; }}, 1400);
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
    source_controls = render_source_controls(card)
    compact_class = " compact" if view_value == "compact" else ""
    summary_html = render_summary(summary, compact=view_value == "compact")

    return f"""<article class="paper-card{compact_class}">
  <form method="post" action="/feedback" class="paper-form">
    <input type="hidden" name="paper_id" value="{card['id']}">
    <input type="hidden" name="recommendation_id" value="{card['recommendation_id']}">
    <input type="hidden" name="return_to" value="{escape(return_to)}">
    <div class="paper-main">
      <div class="card-head">
        <div>
          <h2>{escape(card["title"])}</h2>
          <p>{escape(card.get("published") or "date unknown")} | {escape(card["source"])} | {escape(card["source_id"])} | {source_controls}</p>
        </div>
      </div>
      <div class="tags">{tags}</div>
      {summary_html}
      <div class="links">{links}</div>
      <label>Feedback<textarea name="notes">{notes}</textarea></label>
      <button type="submit" name="status" value="{feedback_status or 'read_later'}" class="secondary save-feedback">Save feedback</button>
    </div>
    <div class="action-rail">
      <div class="score"><strong>{card["score"]:.1f}</strong></div>
      <div class="feedback-actions">{feedback_buttons}</div>
      {feedback_label}
    </div>
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
    return "".join(links)



def render_source_controls(card: dict[str, Any]) -> str:
    url = card.get("url")
    if not url:
        return "source link unavailable"
    label = "arXiv" if card.get("source") == "arxiv" else "source"
    escaped_url = escape(url)
    return (
        f'<a class="source-link" href="{escaped_url}" target="_blank" rel="noreferrer">{label}</a>'
        f'<button type="button" class="copy-url" data-copy-value="{escaped_url}" aria-label="Copy paper URL">Copy</button>'
    )


def render_summary(summary: dict[str, Any], *, compact: bool) -> str:
    problem = escape(summary.get("research_problem") or "Not extracted yet.")
    if compact:
        return f'<div class="compact-summary"><strong>Problem:</strong> {problem}</div>'
    return f"""<div class="summary-grid">
    <section><h3>Problem</h3><p>{problem}</p></section>
    <section><h3>Why it matters</h3><p>{escape(summary.get("why_it_matters") or "Not extracted yet.")}</p></section>
    <section><h3>Approach</h3><p>{escape(summary.get("approach") or "Not extracted yet.")}</p></section>
  </div>"""


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
                latest_recommendation.recommendation_id,
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
                    "recommendation_id": row[1],
                    "source": row[2] or "unknown",
                    "source_id": row[3] or "unknown",
                    "title": row[4],
                    "published": row[5],
                    "url": row[6],
                    "score": float(row[7] or 0),
                    "matched_keywords": decode_json(row[8], []),
                    "ranking_reason": row[9],
                    "feedback_status": row[10],
                    "feedback_notes": row[11],
                    "artifacts": artifacts,
                    "summary": load_summary(artifacts.get("triage_summary")),
                }
            )
    return cards


def render_select(
    choices: list[tuple[str, str]],
    name: str,
    current_value: str,
    label: str,
) -> str:
    options = []
    for value, option_label in choices:
        selected = " selected" if value == current_value else ""
        options.append(f'<option value="{escape(value)}"{selected}>{escape(option_label)}</option>')
    return (
        f'<label class="control-label">{escape(label)}'
        f'<select name="{escape(name)}" onchange="this.form.submit()">{"".join(options)}</select>'
        "</label>"
    )


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


def save_feedback(
    db_path: Path,
    *,
    paper_id: int,
    status: str,
    notes: str,
    feedback_content: str | None = None,
    recommendation_id: int | None = None,
    source: str = "review_queue_ui",
    profile_provider_fn: ProfileProvider | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    ingest_output = None
    with connect_db(db_path) as connection:
        connection.execute(
            "INSERT INTO feedback (paper_id, status, notes) VALUES (?, ?, ?)",
            (paper_id, status, notes),
        )
        if feedback_content and feedback_content.strip():
            ingest_output = ingest_feedback_blob(
                connection,
                paper_id=paper_id,
                recommendation_id=recommendation_id,
                content=feedback_content,
                source=source,
                status=status,
            )
    result: dict[str, Any] = {
        "feedback_saved": True,
        "feedback_ingested": ingest_output is not None,
        "ingest": ingest_output,
        "profile_apply": None,
        "profile_apply_error": None,
    }
    if ingest_output is None:
        return result

    try:
        with connect_db(db_path) as connection:
            result["profile_apply"] = apply_feedback_to_profile(
                connection,
                structured_feedback_ids=[ingest_output["structured_feedback_id"]],
                dry_run=False,
                provider_fn=profile_provider_fn,
            )
        if result["profile_apply"] and result["profile_apply"].get("status") == "failed":
            result["profile_apply_error"] = result["profile_apply"].get("error") or "Profile auto-apply failed."
    except RuntimeError as error:
        result["profile_apply_error"] = str(error)
        print(f"feedback profile auto-apply failed: {error}")
    return result


def decode_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def parse_optional_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def page_css() -> str:
    return """
:root { color-scheme: light dark; }
body { margin: 0; font: 13px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f6f7f9; color: #1f2328; }
main { max-width: 1180px; margin: 0 auto; padding: 14px; }
.topbar { display: grid; grid-template-columns: minmax(260px, 1fr) auto; gap: 12px; align-items: end; border-bottom: 1px solid #d8dee4; padding-bottom: 8px; margin-bottom: 10px; }
h1 { margin: 0 0 2px; font-size: 18px; font-weight: 650; }
.brand-title { display: flex; gap: 10px; align-items: center; }
.brand-logo { display: block; width: 42px; height: 42px; border-radius: 9px; }
h2 { margin: 0 0 3px; font-size: 15px; font-weight: 650; line-height: 1.25; }
h3 { margin: 0 0 3px; font-size: 12px; font-weight: 650; color: #57606a; }
p { margin: 0; }
.topbar p, .card-head p { color: #57606a; font-size: 12px; }
.queue-controls { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: end; }
.control-label { display: grid; gap: 2px; color: #57606a; font-size: 11px; }
select, button { border: 1px solid #d8dee4; border-radius: 5px; padding: 4px 7px; background: #ffffff; color: #24292f; font: inherit; min-height: 28px; }
button { cursor: pointer; }
.source-link { color: #0969da; text-decoration: none; }
.source-link:hover { text-decoration: underline; }
.copy-url { display: inline-flex; align-items: center; min-height: 22px; margin-left: 6px; padding: 1px 6px; font-size: 12px; }
.links a { border: 1px solid #d8dee4; border-radius: 5px; padding: 3px 7px; background: #ffffff; color: #24292f; text-decoration: none; }
button.primary { background: #1f6feb; color: #ffffff; border-color: #1f6feb; }
button.secondary { background: #f6f8fa; }
.banner { padding: 7px 9px; border: 1px solid #2da44e; background: #dafbe1; border-radius: 5px; margin-bottom: 8px; }
.banner.warning { border-color: #bf8700; background: #fff8c5; }
.cards { display: grid; gap: 8px; }
.paper-card, .empty { background: #ffffff; border: 1px solid #d8dee4; border-radius: 6px; padding: 10px; }
.paper-form { display: grid; grid-template-columns: minmax(0, 1fr) 116px; gap: 12px; align-items: start; }
.paper-main { min-width: 0; }
.card-head { display: grid; gap: 4px; align-items: start; }
.score { text-align: center; border: 1px solid #d8dee4; border-radius: 5px; padding: 5px 6px; background: #f6f8fa; }
.score strong { display: block; font-size: 17px; line-height: 1; }
.feedback-state { color: #57606a; font-size: 11px; text-align: center; }
.tags, .links { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
.tag { border: 1px solid #d8dee4; color: #57606a; border-radius: 999px; padding: 1px 6px; font-size: 11px; }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 8px 0; }
.summary-grid section { min-width: 0; }
.summary-grid p, .compact-summary { color: #3f4650; font-size: 12px; }
.compact-summary { margin: 6px 0; line-height: 1.35; }
.links { margin-bottom: 7px; }
.paper-card.compact { padding: 8px 10px; }
.paper-card.compact .tags, .paper-card.compact .links { margin-top: 5px; }
.action-rail { display: grid; gap: 6px; }
.feedback-actions { display: grid; gap: 5px; }
.feedback-actions button { width: 100%; min-height: 26px; padding: 3px 6px; text-align: left; }
label { display: grid; gap: 3px; color: #57606a; font-size: 12px; }
textarea { box-sizing: border-box; width: 100%; min-height: 42px; resize: vertical; border: 1px solid #d8dee4; border-radius: 5px; padding: 6px; font: inherit; color: #1f2328; background: #ffffff; }
.save-feedback { margin-top: 5px; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .topbar { border-color: #30363d; }
  .topbar p, .card-head p, h3, .feedback-state, .tag, label, .compact-summary { color: #8b949e; }
  .summary-grid p { color: #c9d1d9; }
  .source-link { color: #58a6ff; }
  .links a, button, select, .paper-card, .empty, textarea { background: #161b22; color: #e6edf3; border-color: #30363d; }
  button.secondary, .score { background: #21262d; }
  .banner { background: #0f2a1a; border-color: #238636; }
  .banner.warning { background: #2d2300; border-color: #9e6a03; }
}
@media (max-width: 720px) {
  main { padding: 10px; }
  .topbar, .paper-form { grid-template-columns: 1fr; }
  .queue-controls { justify-content: flex-start; }
  .summary-grid { grid-template-columns: 1fr; }
  .action-rail { grid-template-columns: 64px 1fr; align-items: start; }
  .feedback-actions { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .feedback-state { text-align: left; grid-column: 1 / -1; }
}
"""
