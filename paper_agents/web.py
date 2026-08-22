from __future__ import annotations

import html
import json
import mimetypes
import sqlite3
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from paper_agents.db import DEFAULT_DB_PATH, connect_db, health_summary, init_db
from paper_agents.feedback import ProfileProvider, apply_feedback_to_profile, ingest_feedback_blob
from paper_agents.topic_inventory import scout_topic_inventory
from paper_agents.topics import (
    CADENCES,
    DEFAULT_TOPIC_CONFIG_PATH,
    PRIORITIES,
    TopicEntry,
    create_topic_from_fast_path,
    load_topic_config_or_seed,
    save_topic_config,
    update_topic_from_form,
)

ASSET_DIR = Path(__file__).with_name("assets")
LOGO_ASSETS = {"logo_light.png", "logo_dark.png"}

FEEDBACK_STATUSES = [
    ("interested", "Interested"),
    ("read_later", "Read later"),
    ("not_interested", "Not interested"),
    ("reviewed", "Reviewed"),
]

FILTERS = [
    ("all", "All"),
    ("has_feedback", "Scored"),
    ("needs_review", "Needs review"),
    ("interested", "Interested"),
    ("read_later", "Read later"),
    ("reviewed", "Reviewed"),
    ("not_interested", "Not interested"),
]

SOURCE_FILTER_ALL = "all"

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
                        profile_apply_queued=params.get("profile_apply_queued", [None])[0] == "1",
                        profile_apply_failed=params.get("profile_apply_failed", [None])[0] == "1",
                        filter_value=params.get("filter", ["needs_review"])[0],
                        source_value=params.get("source", [SOURCE_FILTER_ALL])[0],
                        sort_value=params.get("sort", ["latest"])[0],
                        view_value=params.get("view", ["full"])[0],
                    )
                )
                return
            if parsed.path == "/health":
                params = urllib.parse.parse_qs(parsed.query)
                self.respond_html(
                    render_health_page(
                        db_path,
                        days=parse_days(params.get("days", ["21"])[0]),
                        source_value=params.get("source", [SOURCE_FILTER_ALL])[0],
                    )
                )
                return
            if parsed.path == "/topics":
                params = urllib.parse.parse_qs(parsed.query)
                self.respond_html(
                    render_topics_page(
                        saved=params.get("saved", [None])[0] == "1",
                        error=params.get("error", [None])[0],
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
            if parsed.path == "/topics":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                form = urllib.parse.parse_qs(body)
                try:
                    save_topics_form(form)
                    redirect_to = "/topics?saved=1"
                except ValueError as error:
                    redirect_to = "/topics?" + urllib.parse.urlencode({"error": str(error)})
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", redirect_to)
                self.end_headers()
                return

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

            status = form.get("status", [""])[-1]
            action = form.get("action", ["status"])[-1]
            submitted_notes = form.get("notes", [""])[0]
            notes = submitted_notes.strip()
            feedback_content = submitted_notes if action == "feedback" else ""
            recommendation_id = parse_optional_int(form.get("recommendation_id", [""])[0])
            if status not in {value for value, _ in FEEDBACK_STATUSES}:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid feedback status")
                return
            if action not in {"status", "feedback"}:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid feedback action")
                return

            result = save_feedback(
                db_path,
                paper_id=paper_id,
                status=status,
                notes=notes,
                feedback_content=feedback_content,
                recommendation_id=recommendation_id,
                source="review_queue_ui",
                profile_apply_mode="background",
            )
            return_to = form.get("return_to", ["/"])[0]
            redirect_to = add_query_param(return_to, "saved", "1")
            if result.get("profile_apply_queued"):
                redirect_to = add_query_param(redirect_to, "profile_apply_queued", "1")
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
    profile_apply_queued: bool = False,
    profile_apply_failed: bool = False,
    filter_value: str = "needs_review",
    source_value: str = SOURCE_FILTER_ALL,
    sort_value: str = "latest",
    view_value: str = "full",
) -> str:
    filter_value = normalize_choice(filter_value, FILTERS, "needs_review")
    source_choices = load_source_filter_choices(db_path)
    source_value = normalize_choice(source_value, source_choices, SOURCE_FILTER_ALL)
    sort_value = normalize_choice(sort_value, SORTS, "latest")
    view_value = normalize_choice(view_value, VIEWS, "full")
    cards = load_review_cards(db_path, filter_value=filter_value, source_value=source_value, sort_value=sort_value)
    banners = []
    if saved:
        banners.append('<div class="banner">Feedback saved.</div>')
    if profile_apply_queued:
        banners.append('<div class="banner">Profile update queued.</div>')
    if profile_apply_failed:
        banners.append('<div class="banner warning">Profile auto-apply failed. Feedback was saved; run feedback apply manually when ready.</div>')
    saved_banner = "".join(banners)
    request_path = build_queue_href(filter_value, source_value, sort_value, view_value)
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
        <p>{len(cards)} papers | {escape(selected_label(FILTERS, filter_value))} | {escape(selected_label(source_choices, source_value))} | sorted by {escape(selected_label(SORTS, sort_value)).lower()}</p>
      </div>
      <form method="get" action="/" class="queue-controls">
        {render_select(FILTERS, "filter", filter_value, "Status")}
        {render_select(source_choices, "source", source_value, "Source")}
        {render_select(SORTS, "sort", sort_value, "Sort")}
        {render_select(VIEWS, "view", view_value, "View")}
        <button type="submit" class="secondary">Apply</button>
        <a class="secondary-link" href="/topics">Topics</a>
        <a class="secondary-link" href="/health">Health</a>
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
      document.querySelectorAll(".paper-form").forEach((form) => {{
        form.addEventListener("submit", (event) => {{
          const submitter = event.submitter;
          if (!submitter || submitter.dataset.quickStatus !== "1") {{
            return;
          }}
          const originalText = submitter.textContent;
          submitter.textContent = "Saving...";
          const notice = form.querySelector(".submit-state");
          const timer = setTimeout(() => {{
            submitter.textContent = originalText;
            if (notice) {{
              notice.textContent = "Still waiting. Refresh and try again if this does not complete.";
            }}
          }}, 10000);
          window.addEventListener("pagehide", () => clearTimeout(timer), {{ once: true }});
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
        f'<button type="submit" name="status" value="{value}" class="{button_class(card, value)}" data-quick-status="1">{label}</button>'
        for value, label in FEEDBACK_STATUSES
    )
    notes = escape(card.get("feedback_notes") or "")
    feedback_status = card.get("feedback_status")
    feedback_label = f'<span class="feedback-state">Current: {escape(feedback_status)}</span>' if feedback_status else ""
    user_score_html = render_user_score(card.get("user_score"))
    feedback_meta_html = render_feedback_meta(card)
    summary = card["summary"]
    source_controls = render_source_controls(card)
    source_badge = f'<span class="source-badge {source_badge_class(card["source"])}">{escape(card["source_label"])}</span>'
    compact_class = " compact" if view_value == "compact" else ""
    summary_html = render_summary(summary, compact=view_value == "compact")

    return f"""<article class="paper-card{compact_class}">
  <form method="post" action="/feedback" class="paper-form">
    <input type="hidden" name="paper_id" value="{card['id']}">
    <input type="hidden" name="recommendation_id" value="{escape(card['recommendation_id'] or '')}">
    <input type="hidden" name="return_to" value="{escape(return_to)}">
    <input type="hidden" name="status" value="{feedback_status or 'read_later'}">
    <div class="paper-main">
      <div class="card-head">
        <div>
          <h2>{escape(card["title"])}</h2>
          <p>{source_badge} {escape(card.get("published") or "date unknown")} | {escape(card["source_id"])} | {source_controls}</p>
        </div>
      </div>
      <div class="tags">{tags}</div>
      {summary_html}
      <div class="links">{links}</div>
      <label>Feedback<textarea name="notes">{notes}</textarea></label>
      <button type="submit" name="action" value="feedback" class="secondary save-feedback">Save feedback</button>
    </div>
    <div class="action-rail">
      {user_score_html}
      {feedback_meta_html}
      <div class="score {'score-secondary' if card.get('user_score') is not None else ''}"><span>System</span><strong>{card["score"]:.1f}</strong></div>
      <div class="feedback-actions">{feedback_buttons}</div>
      {feedback_label}
      <span class="submit-state" aria-live="polite"></span>
    </div>
  </form>
</article>"""


def render_user_score(score: float | None) -> str:
    if score is None:
        return ""
    return f'<div class="user-score"><span>Your score</span><strong>{format_user_score(score)}/5</strong></div>'


def render_feedback_meta(card: dict[str, Any]) -> str:
    if not card.get("has_feedback"):
        return ""
    parts = []
    if card.get("feedback_decision"):
        parts.append(f"Decision: {escape(card['feedback_decision'])}")
    if card.get("feedback_received_at"):
        parts.append(f"Feedback: {escape(card['feedback_received_at'])}")
    if not parts:
        parts.append("Feedback saved")
    return f'<div class="feedback-meta">{"<br>".join(parts)}</div>'


def format_user_score(score: float) -> str:
    return f"{float(score):.2f}".rstrip("0").rstrip(".")


def render_health_page(db_path: Path, *, days: int = 21, source_value: str = SOURCE_FILTER_ALL) -> str:
    days = days if days in {7, 21, 30, 90} else 21
    summary = health_summary(db_path, days=days, source=source_value)
    source_choices = [(SOURCE_FILTER_ALL, "All sources")]
    source_choices.extend((source, source_display_name(source)) for source in summary["available_sources"])
    source_value = normalize_choice(summary["range"]["source"], source_choices, SOURCE_FILTER_ALL)
    warning_html = render_warnings(summary["warnings"])
    cards = [
        ("Latest Run", f"{summary['top']['latest_run_time'] or 'none'}", f"{summary['top']['latest_run_age_hours'] or 0}h old"),
        ("Workflow", summary["top"]["latest_workflow_state"] or "none", "latest state"),
        ("Last Rec Day", summary["top"]["last_successful_recommendation_day"] or "none", "successful recommendation"),
        ("Queue", str(summary["top"]["papers_waiting_in_queue"]), "papers waiting"),
        ("Feedback", str(summary["top"]["unapplied_structured_feedback_count"]), "unapplied structured rows"),
        ("Profile Failures", str(summary["top"]["recent_profile_apply_failures_count"]), "last 7 days"),
        ("Zero Eligible", summary["top"]["latest_zero_eligible_source"] or "none", "latest Scout source"),
    ]
    card_html = "".join(
        f'<section class="health-card"><h2>{escape(title)}</h2><strong>{escape(value)}</strong><span>{escape(detail)}</span></section>'
        for title, value, detail in cards
    )
    graph_html = render_health_graphs(summary)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Project Paper Health</title>
  <style>{page_css()}</style>
</head>
<body>
  <main>
    <header class="topbar">
      <div>
        <h1 class="brand-title"><picture><source srcset="/assets/logo_dark.png" media="(prefers-color-scheme: dark)"><img src="/assets/logo_light.png" alt="" class="brand-logo"></picture><span>Project Paper Health</span></h1>
        <p>{escape(summary['db']['path'])} | integrity {escape(summary['db']['integrity'])} | {format_bytes(summary['db']['size_bytes'])}</p>
      </div>
      <form method="get" action="/health" class="queue-controls">
        {render_select([("7", "7 days"), ("21", "21 days"), ("30", "30 days"), ("90", "90 days")], "days", str(days), "Range")}
        {render_select(source_choices, "source", source_value, "Source")}
        <a class="secondary-link" href="/topics">Topics</a>
        <a class="secondary-link" href="/">Review queue</a>
      </form>
    </header>
    {warning_html}
    <div class="health-cards">{card_html}</div>
    {graph_html}
    <section class="health-section">
      <h2>Daily Funnel</h2>
      {render_health_table("Workflow cycles", summary["daily"]["cycles"], ["day", "state", "cycle_count"])}
      {render_health_table("Scout", summary["daily"]["scout"], ["day", "source", "run_count", "candidate_count", "eligible_count", "excluded_count"])}
      {render_health_table("Curator", summary["daily"]["curator"], ["day", "source", "evaluation_count", "quality_met_count", "recommendation_count"])}
      {render_health_table("Reviewer", summary["daily"]["reviewer"], ["day", "source", "recommendation_count", "pdf_count", "triage_summary_count"])}
      {render_health_table("Feedback/Profile", summary["daily"]["feedback"], ["day", "raw_feedback_count", "structured_feedback_count", "profile_version_count", "apply_failure_count"])}
    </section>
    <section class="health-section">
      <h2>Source Breakdown</h2>
      {render_health_table("Scout by source", summary["source_breakdown"]["funnel"], ["source", "candidate_count", "eligible_count", "excluded_count"])}
      {render_health_table("Recommendations by source", summary["source_breakdown"]["recommendations"], ["source", "recommendation_count"])}
      {render_health_table("Exclusion reasons", summary["source_breakdown"]["exclusion_reasons"], ["source", "reason", "count"])}
    </section>
    <section class="health-section">
      <h2>Artifact Health</h2>
      {render_key_values(summary["artifact_health"])}
    </section>
  </main>
</body>
</html>"""


def render_topics_page(*, saved: bool = False, error: str | None = None, config_path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> str:
    inventory = scout_topic_inventory(config_path=config_path)
    topics = load_topic_config_or_seed(config_path)
    tabs = "".join(
        f'<a class="topic-tab source-badge {source_badge_class(item["source"])}" href="#{escape(item["source"])}">{escape(item["label"])}</a>'
        for item in inventory
    )
    sections = "".join(render_topic_source_section(item) for item in inventory)
    topic_rows = "".join(render_topic_edit_row(topic) for topic in topics)
    total_topics = len(topics)
    banner = ""
    if saved:
        banner = '<div class="banner">Topic config saved. Future scheduled runs will use the updated topic rotation.</div>'
    if error:
        banner = f'<div class="banner warning">Topic config was not saved: {escape(error)}</div>'
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Project Paper Scout Topics</title>
  <style>{page_css()}</style>
</head>
<body>
  <main>
    <header class="topbar">
      <div>
        <h1 class="brand-title"><picture><source srcset="/assets/logo_dark.png" media="(prefers-color-scheme: dark)"><img src="/assets/logo_light.png" alt="" class="brand-logo"></picture><span>Project Paper Scout Topics</span></h1>
        <p>{len(inventory)} sources | {total_topics} configured topics | editable file-backed config</p>
      </div>
      <nav class="queue-controls" aria-label="Primary">
        <a class="secondary-link" href="/">Review queue</a>
        <a class="secondary-link" href="/health">Health</a>
      </nav>
    </header>
    {banner}
    <section class="topic-note">
      <strong>Future runs only.</strong> Add or edit Scout topics here; scheduled jobs pick enabled topics from <code>config/topics.yaml</code> on their next run. This page does not run Scout or change existing recommendations.
    </section>
    {render_topic_add_form()}
    <nav class="topic-tabs" aria-label="Scout topic sources">{tabs}</nav>
    <div class="topic-sections">{sections}</div>
    <section class="topic-source">
      <div class="topic-source-head">
        <h2>All Topics</h2>
        <span>Edit config rows</span>
      </div>
      <div class="topic-table">{topic_rows}</div>
    </section>
  </main>
</body>
</html>"""


def render_topic_source_section(item: dict[str, Any]) -> str:
    active_topics = "".join(f"<li>{escape(topic)}</li>" for topic in item["active_topics"])
    topics = "".join(
        f"<li>{escape(topic.label)} <span>{escape(topic.cadence)} / {escape(topic.priority)} / {'enabled' if topic.enabled else 'disabled'}</span></li>"
        for topic in item["topics"]
    )
    return f"""<section class="topic-source" id="{escape(item["source"])}">
      <div class="topic-source-head">
        <h2><span class="source-badge {source_badge_class(item["source"])}">{escape(item["label"])}</span></h2>
        <span>{escape(item["schedule"])}</span>
      </div>
      <p>{escape(item["description"])}</p>
      <div class="topic-columns">
        <div>
          <h3>Active schedule topic</h3>
          <ol class="topic-list active-topic-list">{active_topics}</ol>
        </div>
        <div>
          <h3>Configured topics</h3>
          <ol class="topic-list">{topics}</ol>
        </div>
      </div>
      <p class="topic-note-text">{escape(item["notes"])}</p>
    </section>"""


def render_topic_add_form() -> str:
    return f"""<section class="topic-source topic-editor">
      <div class="topic-source-head">
        <h2>Add Topic</h2>
        <span>Fast path requires one field</span>
      </div>
      <form method="post" action="/topics" class="topic-add-form">
        <input type="hidden" name="action" value="add">
        <label>Topic<input name="topic_text" placeholder="Datalake operations" required></label>
        <details>
          <summary>Advanced</summary>
          <div class="topic-form-grid">
            <label>Search query<input name="query" placeholder="datalake operations reliability observability production engineering"></label>
            {render_source_checkboxes(["arxiv", "semantic_scholar", "openalex"])}
            {render_topic_select("cadence", CADENCES, "daily", "Cadence")}
            {render_topic_select("priority", PRIORITIES, "normal", "Priority")}
            <label class="inline-check"><input type="checkbox" name="enabled" value="1" checked> Enabled</label>
          </div>
        </details>
        <button type="submit" class="primary">Add topic</button>
      </form>
    </section>"""


def render_topic_edit_row(topic: TopicEntry) -> str:
    enabled_label = "enabled" if topic.enabled else "disabled"
    return f"""<form method="post" action="/topics" class="topic-row">
      <input type="hidden" name="action" value="update">
      <input type="hidden" name="topic_id" value="{escape(topic.id)}">
      <label>Label<input name="label" value="{escape(topic.label)}" required></label>
      <label>Query<input name="query" value="{escape(topic.query)}" required></label>
      <div>{render_source_checkboxes(topic.sources)}</div>
      {render_topic_select("cadence", CADENCES, topic.cadence, "Cadence")}
      {render_topic_select("priority", PRIORITIES, topic.priority, "Priority")}
      <label class="inline-check"><input type="checkbox" name="enabled" value="1" {'checked' if topic.enabled else ''}> {enabled_label}</label>
      <button type="submit" class="secondary">Save</button>
    </form>"""


def render_source_checkboxes(selected_sources: list[str]) -> str:
    selected = set(selected_sources)
    boxes = []
    for source in ["arxiv", "openalex", "semantic_scholar"]:
        boxes.append(
            f'<label class="inline-check"><input type="checkbox" name="sources" value="{escape(source)}" '
            f'{"checked" if source in selected else ""}> {escape(source_display_name(source))}</label>'
        )
    return '<fieldset class="source-checks"><legend>Sources</legend>' + "".join(boxes) + "</fieldset>"


def render_topic_select(name: str, choices: tuple[str, ...], selected_value: str, label: str) -> str:
    options = "".join(
        f'<option value="{escape(choice)}"{" selected" if choice == selected_value else ""}>{escape(choice.title())}</option>'
        for choice in choices
    )
    return f'<label>{escape(label)}<select name="{escape(name)}">{options}</select></label>'


def save_topics_form(form: dict[str, list[str]], *, config_path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> None:
    topics = load_topic_config_or_seed(config_path)
    action = form.get("action", [""])[0]
    if action == "add":
        topic = create_topic_from_fast_path(
            form.get("topic_text", [""])[0],
            existing_topics=topics,
            query=form.get("query", [""])[0] or None,
            sources=form.get("sources") or ["arxiv", "semantic_scholar", "openalex"],
            cadence=form.get("cadence", ["daily"])[0],
            priority=form.get("priority", ["normal"])[0],
            enabled=form.get("enabled", [""])[0] == "1",
        )
        topics.append(topic)
        save_topic_config(topics, config_path)
        return
    if action == "update":
        topic_id = form.get("topic_id", [""])[0]
        for index, topic in enumerate(topics):
            if topic.id != topic_id:
                continue
            topics[index] = update_topic_from_form(
                topic,
                label=form.get("label", [""])[0],
                query=form.get("query", [""])[0],
                sources=form.get("sources", []),
                cadence=form.get("cadence", ["daily"])[0],
                priority=form.get("priority", ["normal"])[0],
                enabled=form.get("enabled", [""])[0] == "1",
            )
            save_topic_config(topics, config_path)
            return
        raise ValueError(f"Unknown topic id: {topic_id}")
    raise ValueError("Unknown topic action")


def render_warnings(warnings: list[dict[str, str]]) -> str:
    if not warnings:
        return '<div class="banner">No health warnings.</div>'
    items = "".join(
        f'<li class="{escape(warning["level"])}"><strong>{escape(warning["level"])}</strong> {escape(warning["message"])}</li>'
        for warning in warnings
    )
    return f'<section class="health-warnings"><h2>Warnings</h2><ul>{items}</ul></section>'


def render_health_graphs(summary: dict[str, Any]) -> str:
    daily_rows = sorted(daily_funnel_rows(summary), key=lambda row: row["day"])
    source_rows = source_funnel_rows(summary)
    feedback_rows = feedback_activity_rows(summary)
    return f"""<section class="health-graphs">
      <div class="health-graph health-graph-wide">
        <div class="graph-head"><h2>Daily Trend</h2><span>Candidates -> eligible -> recommendations</span></div>
        {render_daily_trend_svg(daily_rows)}
      </div>
      <div class="health-graph">
        <div class="graph-head"><h2>Source Breakdown</h2><span>Candidates, eligible, recommendations</span></div>
        {render_source_breakdown_svg(source_rows)}
      </div>
      <div class="health-graph">
        <div class="graph-head"><h2>Recommendation Gap</h2><span>Eligible papers without recommendations</span></div>
        {render_recommendation_gap_svg(daily_rows)}
      </div>
      <div class="health-graph">
        <div class="graph-head"><h2>Feedback/Profile Activity</h2><span>Feedback submissions and profile updates</span></div>
        {render_feedback_activity_svg(feedback_rows)}
      </div>
    </section>"""


def daily_funnel_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = {}
    for row in summary["daily"]["scout"]:
        day = str(row["day"])
        bucket = by_day.setdefault(day, {"day": day, "candidate_count": 0, "eligible_count": 0, "recommendation_count": 0})
        bucket["candidate_count"] += int(row["candidate_count"] or 0)
        bucket["eligible_count"] += int(row["eligible_count"] or 0)
    for row in summary["daily"]["curator"]:
        day = str(row["day"])
        bucket = by_day.setdefault(day, {"day": day, "candidate_count": 0, "eligible_count": 0, "recommendation_count": 0})
        bucket["recommendation_count"] += int(row["recommendation_count"] or 0)
    return [by_day[day] for day in sorted(by_day.keys(), reverse=True)]


def source_funnel_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    by_source = {
        str(row["source"]): {
            "source": str(row["source"]),
            "candidate_count": int(row["candidate_count"] or 0),
            "eligible_count": int(row["eligible_count"] or 0),
            "recommendation_count": 0,
        }
        for row in summary["source_breakdown"]["funnel"]
    }
    for row in summary["source_breakdown"]["recommendations"]:
        source = str(row["source"])
        bucket = by_source.setdefault(source, {"source": source, "candidate_count": 0, "eligible_count": 0, "recommendation_count": 0})
        bucket["recommendation_count"] += int(row["recommendation_count"] or 0)
    return sorted(by_source.values(), key=lambda item: (item["candidate_count"], item["eligible_count"], item["recommendation_count"]), reverse=True)


def feedback_activity_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(
        [
            {
                "day": str(row["day"]),
                "raw_feedback_count": int(row["raw_feedback_count"] or 0),
                "profile_version_count": int(row["profile_version_count"] or 0),
            }
            for row in summary["daily"]["feedback"]
        ],
        key=lambda row: row["day"],
    )


def recent_chart_rows(rows: list[dict[str, Any]], limit: int = 21) -> list[dict[str, Any]]:
    return rows[-limit:] if len(rows) > limit else rows


def render_chart_legend(items: list[tuple[str, str]]) -> str:
    return '<div class="chart-legend">' + "".join(
        f'<span class="legend-item"><span class="legend-swatch {escape(class_name)}"></span>{escape(label)}</span>'
        for label, class_name in items
    ) + "</div>"


def render_daily_trend_svg(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty graph-empty">No daily funnel data in this range.</div>'
    rows = recent_chart_rows(rows)
    series = [
        ("Candidates", "candidate_count", "chart-candidates"),
        ("Eligible", "eligible_count", "chart-eligible"),
        ("Recommendations", "recommendation_count", "chart-recommendations"),
    ]
    max_value = max(max(int(row[key] or 0) for _, key, _ in series) for row in rows) or 1
    width, height = 760, 250
    left, right, top, bottom = 46, 16, 24, 36
    plot_width = width - left - right
    plot_height = height - top - bottom

    def x_at(index: int) -> float:
        if len(rows) == 1:
            return left + plot_width / 2
        return left + (plot_width * index / (len(rows) - 1))

    def y_at(value: int) -> float:
        return top + plot_height - (plot_height * value / max_value)

    grid = render_svg_grid(width, height, left, right, top, bottom, max_value)
    labels = render_svg_day_labels(rows, left, plot_width, height - 12)
    paths = []
    points = []
    for label, key, class_name in series:
        coords = [(x_at(index), y_at(int(row[key] or 0))) for index, row in enumerate(rows)]
        point_text = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
        paths.append(f'<polyline class="chart-line {class_name}" points="{point_text}" />')
        for (x, y), row in zip(coords, rows):
            value = int(row[key] or 0)
            points.append(
                f'<circle class="chart-point {class_name}" cx="{x:.1f}" cy="{y:.1f}" r="3">'
                f"<title>{escape(label)} {escape(row['day'])}: {value}</title></circle>"
            )
    legend = render_chart_legend([("Candidates", "chart-candidates"), ("Eligible", "chart-eligible"), ("Recommendations", "chart-recommendations")])
    return f"""{legend}<svg class="health-svg daily-trend-svg" viewBox="0 0 {width} {height}" role="img" aria-label="Daily funnel trend">
      {grid}
      <line class="chart-axis" x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" />
      <line class="chart-axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" />
      {''.join(paths)}
      {''.join(points)}
      {labels}
    </svg>"""


def render_source_breakdown_svg(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty graph-empty">No source data in this range.</div>'
    rows = rows[:8]
    return render_grouped_bar_svg(
        rows,
        [
            ("Candidates", "candidate_count", "chart-candidates"),
            ("Eligible", "eligible_count", "chart-eligible"),
            ("Recommendations", "recommendation_count", "chart-recommendations"),
        ],
        lambda row: source_display_name(row["source"]),
        "Source funnel breakdown",
        "source-breakdown-svg",
    )


def render_recommendation_gap_svg(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="empty graph-empty">No daily funnel data in this range.</div>'
    gap_rows = [
        {**row, "gap_count": max(0, int(row["eligible_count"] or 0) - int(row["recommendation_count"] or 0))}
        for row in recent_chart_rows(rows)
    ]
    return render_single_bar_svg(
        gap_rows,
        "gap_count",
        "chart-gap",
        lambda row: row["day"],
        "Eligible papers without recommendations",
        "recommendation-gap-svg",
        zero_message="No eligible-to-recommendation gaps in this range.",
    )


def render_feedback_activity_svg(rows: list[dict[str, Any]]) -> str:
    rows = recent_chart_rows(rows)
    if not rows or not any((row["raw_feedback_count"] or row["profile_version_count"]) for row in rows):
        return '<div class="empty graph-empty">No feedback/profile activity in this range.</div>'
    return render_grouped_bar_svg(
        rows,
        [
            ("Feedback", "raw_feedback_count", "chart-feedback"),
            ("Profile", "profile_version_count", "chart-profile"),
        ],
        lambda row: row["day"],
        "Feedback and profile activity",
        "feedback-activity-svg",
    )


def render_grouped_bar_svg(
    rows: list[dict[str, Any]],
    series: list[tuple[str, str, str]],
    label_for_row: Callable[[dict[str, Any]], str],
    aria_label: str,
    class_name: str,
) -> str:
    max_value = max(max(int(row[key] or 0) for _, key, _ in series) for row in rows) or 1
    width, height = 760, 250
    left, right, top, bottom = 46, 16, 24, 44
    plot_width = width - left - right
    plot_height = height - top - bottom
    group_width = plot_width / max(1, len(rows))
    bar_width = min(18, max(5, (group_width - 10) / max(1, len(series))))
    grid = render_svg_grid(width, height, left, right, top, bottom, max_value)
    bars = []
    labels = []
    for row_index, row in enumerate(rows):
        center = left + group_width * row_index + group_width / 2
        start = center - (bar_width * len(series) + 2 * (len(series) - 1)) / 2
        row_label = label_for_row(row)
        for series_index, (series_label, key, series_class) in enumerate(series):
            value = int(row[key] or 0)
            bar_height = plot_height * value / max_value
            x = start + series_index * (bar_width + 2)
            y = top + plot_height - bar_height
            bars.append(
                f'<rect class="chart-bar {series_class}" x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}">'
                f"<title>{escape(series_label)} {escape(row_label)}: {value}</title></rect>"
            )
        if should_render_axis_label(row_index, len(rows)):
            labels.append(f'<text class="chart-label" x="{center:.1f}" y="{height - 14}" text-anchor="middle">{escape(short_chart_label(row_label))}</text>')
    legend = render_chart_legend([(label, class_name) for label, _, class_name in series])
    return f"""{legend}<svg class="health-svg {escape(class_name)}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(aria_label)}">
      {grid}
      <line class="chart-axis" x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" />
      <line class="chart-axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" />
      {''.join(bars)}
      {''.join(labels)}
    </svg>"""


def render_single_bar_svg(
    rows: list[dict[str, Any]],
    value_key: str,
    series_class: str,
    label_for_row: Callable[[dict[str, Any]], str],
    aria_label: str,
    class_name: str,
    *,
    zero_message: str,
) -> str:
    max_value = max(int(row[value_key] or 0) for row in rows) if rows else 0
    if not rows:
        return f'<div class="empty graph-empty">{escape(zero_message)}</div>'
    has_values = max_value > 0
    max_value = max_value or 1
    width, height = 760, 230
    left, right, top, bottom = 46, 16, 24, 38
    plot_width = width - left - right
    plot_height = height - top - bottom
    bar_width = max(4, min(18, (plot_width / max(1, len(rows))) * 0.55))
    grid = render_svg_grid(width, height, left, right, top, bottom, max_value)
    bars = []
    labels = []
    for index, row in enumerate(rows):
        value = int(row[value_key] or 0)
        center = left + (plot_width * (index + 0.5) / len(rows))
        bar_height = plot_height * value / max_value
        x = center - bar_width / 2
        y = top + plot_height - bar_height
        row_label = label_for_row(row)
        bars.append(
            f'<rect class="chart-bar {escape(series_class)}" x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" height="{bar_height:.1f}">'
            f"<title>{escape(row_label)}: {value}</title></rect>"
        )
        if should_render_axis_label(index, len(rows)):
            labels.append(f'<text class="chart-label" x="{center:.1f}" y="{height - 12}" text-anchor="middle">{escape(short_chart_label(row_label))}</text>')
    zero_note = "" if has_values else f'<text class="chart-label chart-empty-label" x="{left + plot_width / 2:.1f}" y="{top + 22}" text-anchor="middle">{escape(zero_message)}</text>'
    return f"""<svg class="health-svg {escape(class_name)}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(aria_label)}">
      {grid}
      <line class="chart-axis" x1="{left}" y1="{height - bottom}" x2="{width - right}" y2="{height - bottom}" />
      <line class="chart-axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}" />
      {zero_note}
      {''.join(bars)}
      {''.join(labels)}
    </svg>"""


def render_svg_grid(width: int, height: int, left: int, right: int, top: int, bottom: int, max_value: int) -> str:
    plot_height = height - top - bottom
    rows = []
    for index in range(5):
        value = round(max_value * (4 - index) / 4)
        y = top + plot_height * index / 4
        rows.append(f'<line class="chart-grid" x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" />')
        label = value if max_value >= 4 or index in {0, 4} else ""
        rows.append(f'<text class="chart-label chart-y-label" x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
    return "".join(rows)


def render_svg_day_labels(rows: list[dict[str, Any]], left: int, plot_width: int, y: int) -> str:
    labels = []
    for index, row in enumerate(rows):
        if not should_render_axis_label(index, len(rows)):
            continue
        x = left + (plot_width / 2 if len(rows) == 1 else plot_width * index / (len(rows) - 1))
        labels.append(f'<text class="chart-label" x="{x:.1f}" y="{y}" text-anchor="middle">{escape(short_chart_label(row["day"]))}</text>')
    return "".join(labels)


def should_render_axis_label(index: int, count: int) -> bool:
    if count <= 7:
        return True
    step = max(1, count // 6)
    return index == 0 or index == count - 1 or index % step == 0


def short_chart_label(label: str) -> str:
    label = str(label)
    if len(label) == 10 and label[4] == "-" and label[7] == "-":
        return label[5:]
    return label if len(label) <= 13 else f"{label[:12]}..."


def render_health_table(title: str, rows: list[dict[str, Any]], columns: list[str]) -> str:
    headers = "".join(f"<th>{escape(column.replace('_', ' ').title())}</th>" for column in columns)
    if rows:
        body = "".join(
            "<tr>" + "".join(f"<td>{escape(row.get(column) if row.get(column) is not None else 0)}</td>" for column in columns) + "</tr>"
            for row in rows
        )
    else:
        body = f'<tr><td colspan="{len(columns)}">No rows in this range.</td></tr>'
    return f"""<div class="health-table">
      <h3>{escape(title)}</h3>
      <table><thead><tr>{headers}</tr></thead><tbody>{body}</tbody></table>
    </div>"""


def render_key_values(values: dict[str, Any]) -> str:
    items = "".join(f"<dt>{escape(key.replace('_', ' ').title())}</dt><dd>{escape(value)}</dd>" for key, value in values.items())
    return f'<dl class="health-kv">{items}</dl>'


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
    has_extracted_summary = any(summary.get(key) for key in ["research_problem", "why_it_matters", "approach"])
    source_abstract = summary.get("source_abstract")
    if not has_extracted_summary and source_abstract:
        abstract = escape(source_abstract)
        if compact:
            return f'<div class="compact-summary"><strong>Source Abstract:</strong> {abstract}</div>'
        return f"""<div class="source-summary">
    <section><h3>Source Abstract</h3><p>{abstract}</p></section>
  </div>"""

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


def load_review_cards(db_path: Path, *, filter_value: str, source_value: str, sort_value: str) -> list[dict[str, Any]]:
    init_db(db_path)
    where_clauses = []
    params: list[Any] = []
    if filter_value == "needs_review":
        where_clauses.append("latest_recommendation.paper_id IS NOT NULL")
        where_clauses.append("latest_feedback.status IS NULL")
    elif filter_value == "has_feedback":
        where_clauses.append("(latest_structured_feedback.paper_id IS NOT NULL OR latest_raw_feedback.paper_id IS NOT NULL)")
    elif filter_value != "all":
        where_clauses.append("latest_recommendation.paper_id IS NOT NULL")
        where_clauses.append("latest_feedback.status = ?")
        params.append(filter_value)
    else:
        where_clauses.append("latest_recommendation.paper_id IS NOT NULL")
    if source_value != SOURCE_FILTER_ALL:
        where_clauses.append("EXISTS (SELECT 1 FROM paper_sources source_filter WHERE source_filter.paper_id = papers.id AND source_filter.source = ?)")
        params.append(source_value)
    where_clause = "WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    if sort_value == "score":
        order_clause = "ORDER BY latest_recommendation.score DESC NULLS LAST, latest_feedback_received_at DESC, papers.id DESC"
    elif filter_value == "has_feedback":
        order_clause = "ORDER BY latest_feedback_received_at DESC, latest_recommendation.curator_run_id DESC, papers.id DESC"
    else:
        order_clause = "ORDER BY latest_recommendation.curator_run_id DESC, latest_recommendation.recommendation_order ASC"

    connection = connect_db(db_path)
    try:
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
            ), latest_structured_feedback AS (
                SELECT
                    paper_id,
                    decision,
                    score,
                    created_at,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id DESC) AS row_number
                FROM structured_feedback
            ), latest_raw_feedback AS (
                SELECT
                    paper_id,
                    received_at,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id DESC) AS row_number
                FROM raw_feedback
            ), primary_source AS (
                SELECT
                    paper_id,
                    source,
                    source_id,
                    url,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id ASC) AS row_number
                FROM paper_sources
            ), source_rollup AS (
                SELECT
                    paper_id,
                    GROUP_CONCAT(source, ',') AS sources
                FROM (
                    SELECT DISTINCT paper_id, source
                    FROM paper_sources
                    ORDER BY paper_id, source
                )
                GROUP BY paper_id
            )
            SELECT
                papers.id,
                latest_recommendation.recommendation_id,
                primary_source.source,
                primary_source.source_id,
                papers.title,
                papers.published,
                papers.abstract,
                primary_source.url,
                latest_recommendation.score,
                latest_recommendation.matched_signals_json,
                latest_recommendation.rationale,
                latest_feedback.status,
                latest_feedback.notes,
                source_rollup.sources,
                latest_structured_feedback.score,
                latest_structured_feedback.decision,
                latest_structured_feedback.created_at,
                latest_raw_feedback.received_at,
                COALESCE(latest_structured_feedback.created_at, latest_raw_feedback.received_at) AS latest_feedback_received_at
            FROM papers
            LEFT JOIN latest_recommendation
              ON latest_recommendation.paper_id = papers.id
             AND latest_recommendation.row_number = 1
            LEFT JOIN primary_source ON primary_source.paper_id = papers.id AND primary_source.row_number = 1
            LEFT JOIN source_rollup ON source_rollup.paper_id = papers.id
            LEFT JOIN latest_feedback ON latest_feedback.paper_id = papers.id AND latest_feedback.row_number = 1
            LEFT JOIN latest_structured_feedback ON latest_structured_feedback.paper_id = papers.id AND latest_structured_feedback.row_number = 1
            LEFT JOIN latest_raw_feedback ON latest_raw_feedback.paper_id = papers.id AND latest_raw_feedback.row_number = 1
            {where_clause}
            {order_clause}
            LIMIT 50
            """,
            tuple(params),
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
                    "source_label": source_label(row[2] or "unknown", parse_sources(row[13])),
                    "user_score": row[14],
                    "feedback_decision": row[15],
                    "feedback_received_at": row[17] or row[16],
                    "has_feedback": bool(row[15] is not None or row[16] is not None or row[17] is not None),
                    "title": row[4],
                    "published": row[5],
                    "url": row[7],
                    "score": float(row[8] or 0),
                    "matched_keywords": decode_json(row[9], []),
                    "ranking_reason": row[10],
                    "feedback_status": row[11],
                    "feedback_notes": row[12],
                    "artifacts": artifacts,
                    "summary": with_source_abstract(load_summary(artifacts.get("triage_summary")), row[6]),
                }
            )
    finally:
        connection.close()
    return cards


def with_source_abstract(summary: dict[str, Any], abstract: str | None) -> dict[str, Any]:
    merged = dict(summary)
    if abstract:
        merged["source_abstract"] = abstract
    return merged


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


def build_queue_href(filter_value: str, source_value: str, sort_value: str, view_value: str) -> str:
    return "/?" + urllib.parse.urlencode({"filter": filter_value, "source": source_value, "sort": sort_value, "view": view_value})


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


def load_source_filter_choices(db_path: Path) -> list[tuple[str, str]]:
    init_db(db_path)
    connection = connect_db(db_path)
    try:
        rows = connection.execute(
            """
            SELECT DISTINCT paper_sources.source
            FROM paper_sources
            WHERE paper_sources.source IS NOT NULL
              AND (
                EXISTS (SELECT 1 FROM recommendations WHERE recommendations.paper_id = paper_sources.paper_id)
                OR EXISTS (SELECT 1 FROM structured_feedback WHERE structured_feedback.paper_id = paper_sources.paper_id)
                OR EXISTS (SELECT 1 FROM raw_feedback WHERE raw_feedback.paper_id = paper_sources.paper_id)
              )
            ORDER BY paper_sources.source
            """
        ).fetchall()
    finally:
        connection.close()
    choices = [(SOURCE_FILTER_ALL, "All sources")]
    choices.extend((row[0], source_display_name(row[0])) for row in rows if row[0])
    return choices


def parse_sources(value: str | None) -> list[str]:
    if not value:
        return []
    return [source for source in value.split(",") if source]


def source_label(primary_source: str, sources: list[str]) -> str:
    unique = sorted(set(sources))
    if len(unique) > 1:
        return f"{source_display_name(primary_source)} +{len(unique) - 1}"
    return source_display_name(primary_source)


def source_badge_class(source: str) -> str:
    normalized = source.replace("_", "-").lower()
    if normalized not in {"arxiv", "openalex", "semantic-scholar"}:
        normalized = "unknown"
    return f"source-badge-{normalized}"


def source_display_name(source: str) -> str:
    labels = {
        "arxiv": "arXiv",
        "semantic_scholar": "Semantic Scholar",
        "openalex": "OpenAlex",
        "unknown": "Unknown",
    }
    return labels.get(source, source.replace("_", " ").title())


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
    profile_apply_mode: str = "sync",
) -> dict[str, Any]:
    if profile_apply_mode not in {"sync", "background"}:
        raise ValueError(f"Unknown profile apply mode: {profile_apply_mode}")
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
        "profile_apply_queued": False,
    }
    if ingest_output is None:
        return result

    structured_feedback_id = ingest_output["structured_feedback_id"]
    if profile_apply_mode == "background":
        start_profile_apply_worker(
            db_path,
            structured_feedback_ids=[structured_feedback_id],
            profile_provider_fn=profile_provider_fn,
        )
        result["profile_apply_queued"] = True
        return result

    try:
        with connect_db(db_path) as connection:
            result["profile_apply"] = apply_feedback_to_profile(
                connection,
                structured_feedback_ids=[structured_feedback_id],
                dry_run=False,
                provider_fn=profile_provider_fn,
            )
        if result["profile_apply"] and result["profile_apply"].get("status") == "failed":
            result["profile_apply_error"] = result["profile_apply"].get("error") or "Profile auto-apply failed."
    except RuntimeError as error:
        result["profile_apply_error"] = str(error)
        print(f"feedback profile auto-apply failed: {error}")
    return result


def start_profile_apply_worker(
    db_path: Path,
    *,
    structured_feedback_ids: list[int],
    profile_provider_fn: ProfileProvider | None = None,
) -> threading.Thread:
    worker = threading.Thread(
        target=run_profile_apply_worker,
        kwargs={
            "db_path": db_path,
            "structured_feedback_ids": structured_feedback_ids,
            "profile_provider_fn": profile_provider_fn,
        },
        daemon=True,
        name="paper-agent-profile-apply",
    )
    worker.start()
    return worker


def run_profile_apply_worker(
    *,
    db_path: Path,
    structured_feedback_ids: list[int],
    profile_provider_fn: ProfileProvider | None = None,
) -> None:
    try:
        with connect_db(db_path) as connection:
            apply_feedback_to_profile(
                connection,
                structured_feedback_ids=structured_feedback_ids,
                dry_run=False,
                provider_fn=profile_provider_fn,
            )
    except RuntimeError as error:
        print(f"feedback profile auto-apply failed: {error}")


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


def parse_days(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 21


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ["B", "KiB", "MiB", "GiB"]:
        if size < 1024 or unit == "GiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{value} B"


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
select, button, input { border: 1px solid #d8dee4; border-radius: 5px; padding: 4px 7px; background: #ffffff; color: #24292f; font: inherit; min-height: 28px; box-sizing: border-box; }
button { cursor: pointer; }
code { font: 12px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.secondary-link { display: inline-flex; align-items: center; min-height: 28px; border: 1px solid #d8dee4; border-radius: 5px; padding: 0 8px; color: #24292f; background: #f6f8fa; text-decoration: none; }
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
.score span, .user-score span { display: block; color: #57606a; font-size: 10px; line-height: 1; margin-bottom: 3px; text-transform: uppercase; }
.score-secondary { background: transparent; }
.score-secondary strong { font-size: 13px; }
.user-score { text-align: center; border: 1px solid #1a7f37; border-radius: 5px; padding: 6px; background: #dafbe1; color: #116329; }
.user-score strong { display: block; font-size: 18px; line-height: 1; }
.feedback-state { color: #57606a; font-size: 11px; text-align: center; }
.feedback-meta { border: 1px solid #d8dee4; border-radius: 5px; padding: 5px 6px; color: #57606a; background: #ffffff; font-size: 11px; line-height: 1.3; overflow-wrap: anywhere; }
.tags, .links { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
.tag { border: 1px solid #d8dee4; color: #57606a; border-radius: 999px; padding: 1px 6px; font-size: 11px; }
.source-badge { border-radius: 999px; padding: 1px 7px; font-size: 11px; font-weight: 650; }
.source-badge::before { margin-right: 4px; }
.source-badge-arxiv { color: #8a4600; background: #fff1d6; border: 1px solid #d4a72c; }
.source-badge-arxiv::before { content: "A"; }
.source-badge-openalex { color: #0969da; background: #ddf4ff; border: 1px solid #54aeef; }
.source-badge-openalex::before { content: "O"; }
.source-badge-semantic-scholar { color: #8250df; background: #fbefff; border: 1px solid #d8b9ff; }
.source-badge-semantic-scholar::before { content: "S"; }
.source-badge-unknown { color: #57606a; background: #f6f8fa; border: 1px solid #d8dee4; }
.source-badge-unknown::before { content: "?"; }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 8px 0; }
.summary-grid section { min-width: 0; }
.summary-grid p, .source-summary p, .compact-summary { color: #3f4650; font-size: 12px; }
.source-summary { margin: 8px 0; }
.source-summary p { max-height: 88px; overflow: auto; }
.compact-summary { margin: 6px 0; line-height: 1.35; }
.links { margin-bottom: 7px; }
.paper-card.compact { padding: 8px 10px; }
.paper-card.compact .tags, .paper-card.compact .links { margin-top: 5px; }
.action-rail { display: grid; gap: 6px; }
.feedback-actions { display: grid; gap: 5px; }
.feedback-actions button { width: 100%; min-height: 26px; padding: 3px 6px; text-align: left; }
.submit-state { color: #57606a; font-size: 11px; line-height: 1.25; }
label { display: grid; gap: 3px; color: #57606a; font-size: 12px; }
textarea { box-sizing: border-box; width: 100%; min-height: 42px; resize: vertical; border: 1px solid #d8dee4; border-radius: 5px; padding: 6px; font: inherit; color: #1f2328; background: #ffffff; }
.save-feedback { margin-top: 5px; }
.health-cards { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 8px; margin: 8px 0; }
.health-card { border: 1px solid #d8dee4; background: #ffffff; border-radius: 6px; padding: 8px; min-width: 0; }
.health-card h2 { color: #57606a; font-size: 11px; margin-bottom: 5px; text-transform: uppercase; }
.health-card strong { display: block; font-size: 15px; line-height: 1.2; overflow-wrap: anywhere; }
.health-card span { color: #57606a; font-size: 11px; }
.health-graphs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 10px 0 12px; }
.health-graph { border: 1px solid #d8dee4; background: #ffffff; border-radius: 6px; padding: 9px; min-width: 0; }
.health-graph-wide { grid-column: 1 / -1; }
.graph-head { display: flex; justify-content: space-between; gap: 8px; align-items: baseline; margin-bottom: 8px; }
.graph-head h2 { margin: 0; font-size: 14px; }
.graph-head span { color: #57606a; font-size: 11px; }
.chart-legend { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 5px; color: #57606a; font-size: 11px; }
.legend-item { display: inline-flex; gap: 4px; align-items: center; white-space: nowrap; }
.legend-swatch { width: 9px; height: 9px; border-radius: 999px; display: inline-block; }
.health-svg { display: block; width: 100%; height: auto; overflow: visible; }
.chart-axis { stroke: #8c959f; stroke-width: 1; }
.chart-grid { stroke: #d8dee4; stroke-width: 1; opacity: 0.8; }
.chart-label { fill: #57606a; font-size: 11px; }
.chart-line { fill: none; stroke-width: 2.6; stroke-linecap: round; stroke-linejoin: round; }
.chart-point { stroke: #ffffff; stroke-width: 1.5; }
.chart-bar { rx: 3px; ry: 3px; }
.chart-candidates { stroke: #0969da; fill: #0969da; background: #0969da; }
.chart-eligible { stroke: #1a7f37; fill: #1a7f37; background: #1a7f37; }
.chart-recommendations { stroke: #9a6700; fill: #9a6700; background: #9a6700; }
.chart-gap { stroke: #cf222e; fill: #cf222e; background: #cf222e; }
.chart-feedback { stroke: #8250df; fill: #8250df; background: #8250df; }
.chart-profile { stroke: #bf3989; fill: #bf3989; background: #bf3989; }
.graph-empty { padding: 8px; font-size: 12px; }
.health-warnings { border: 1px solid #bf8700; background: #fff8c5; border-radius: 6px; padding: 8px; margin-bottom: 8px; }
.health-warnings h2, .health-section h2 { margin: 0 0 6px; font-size: 14px; }
.health-warnings ul { margin: 0; padding-left: 18px; }
.health-warnings li { margin: 2px 0; }
.health-warnings li.critical strong { color: #cf222e; }
.health-section { margin-top: 12px; }
.health-table { margin: 8px 0 12px; overflow-x: auto; }
.health-table table { width: 100%; border-collapse: collapse; background: #ffffff; border: 1px solid #d8dee4; border-radius: 6px; overflow: hidden; }
.health-table th, .health-table td { padding: 5px 7px; border-bottom: 1px solid #d8dee4; text-align: left; white-space: nowrap; }
.health-table th { color: #57606a; background: #f6f8fa; font-size: 11px; font-weight: 650; }
.health-table td { font-size: 12px; }
.health-kv { display: grid; grid-template-columns: 220px minmax(0, 1fr); gap: 4px 10px; border: 1px solid #d8dee4; background: #ffffff; border-radius: 6px; padding: 8px; }
.health-kv dt { color: #57606a; }
.health-kv dd { margin: 0; }
.topic-note { border: 1px solid #d8dee4; background: #ffffff; border-radius: 6px; padding: 8px 9px; margin-bottom: 10px; color: #57606a; }
.topic-note strong { color: #1f2328; }
.topic-tabs { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 10px; }
.topic-tab { text-decoration: none; }
.topic-sections { display: grid; gap: 10px; }
.topic-source { border: 1px solid #d8dee4; background: #ffffff; border-radius: 6px; padding: 10px; scroll-margin-top: 10px; }
.topic-source-head { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 6px; }
.topic-source-head h2 { margin: 0; }
.topic-source-head > span, .topic-source > p, .topic-note-text { color: #57606a; font-size: 12px; }
.topic-columns { display: grid; grid-template-columns: minmax(180px, 0.38fr) minmax(0, 1fr); gap: 12px; margin-top: 8px; }
.topic-list { margin: 0; padding-left: 22px; columns: 2; column-gap: 28px; }
.active-topic-list { columns: 1; }
.topic-list li { break-inside: avoid; margin: 0 0 3px; padding-left: 2px; font-size: 12px; }
.topic-list li span { color: #57606a; font-size: 11px; }
.topic-note-text { margin-top: 8px; }
.topic-add-form { display: grid; gap: 8px; }
.topic-add-form details { border: 1px solid #d8dee4; border-radius: 5px; padding: 6px 8px; }
.topic-add-form summary { cursor: pointer; color: #57606a; font-size: 12px; }
.topic-form-grid { display: grid; grid-template-columns: minmax(220px, 1fr) minmax(180px, 0.7fr) repeat(2, minmax(120px, 0.4fr)) minmax(96px, 0.3fr); gap: 8px; align-items: end; margin-top: 8px; }
.topic-table { display: grid; gap: 6px; }
.topic-row { display: grid; grid-template-columns: minmax(150px, 0.8fr) minmax(260px, 1.3fr) minmax(180px, 0.8fr) 100px 100px 92px 62px; gap: 6px; align-items: end; border-top: 1px solid #d8dee4; padding-top: 6px; }
.topic-row:first-child { border-top: 0; padding-top: 0; }
.source-checks { display: flex; gap: 6px; flex-wrap: wrap; margin: 0; padding: 0; border: 0; min-width: 0; }
.source-checks legend { color: #57606a; font-size: 12px; padding: 0; margin-bottom: 3px; }
.inline-check { display: inline-flex; grid-auto-flow: column; gap: 4px; align-items: center; color: #57606a; font-size: 12px; }
.inline-check input { min-height: auto; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .topbar { border-color: #30363d; }
  .topbar p, .card-head p, h3, .feedback-state, .feedback-meta, .submit-state, .tag, label, .compact-summary, .score span, .user-score span, .health-card h2, .health-card span, .graph-head span, .chart-legend, .health-table th, .health-kv dt, .topic-source-head > span, .topic-source > p, .topic-note, .topic-note-text { color: #8b949e; }
  .summary-grid p, .source-summary p { color: #c9d1d9; }
  .source-link { color: #58a6ff; }
  .links a, button, select, input, .secondary-link, .paper-card, .empty, textarea, .feedback-meta, .health-card, .health-graph, .health-table table, .health-kv, .topic-note, .topic-source, .topic-add-form details { background: #161b22; color: #e6edf3; border-color: #30363d; }
  .topic-note strong { color: #e6edf3; }
  button.secondary, .score { background: #21262d; }
  .score-secondary { background: transparent; }
  .user-score { background: #0f2a1a; color: #7ee787; border-color: #238636; }
  .source-badge-arxiv { color: #f0b72f; background: #2d2300; border-color: #9e6a03; }
  .source-badge-openalex { color: #79c0ff; background: #0d263f; border-color: #1f6feb; }
  .source-badge-semantic-scholar { color: #d8b9ff; background: #2a163f; border-color: #8250df; }
  .source-badge-unknown { color: #8b949e; background: #21262d; border-color: #30363d; }
  .chart-axis { stroke: #8b949e; }
  .chart-grid { stroke: #30363d; opacity: 1; }
  .chart-label { fill: #8b949e; }
  .chart-point { stroke: #161b22; }
  .banner { background: #0f2a1a; border-color: #238636; }
  .banner.warning { background: #2d2300; border-color: #9e6a03; }
  .health-warnings { background: #2d2300; border-color: #9e6a03; }
  .health-table th { background: #21262d; }
  .health-table th, .health-table td { border-color: #30363d; }
  .topic-row { border-color: #30363d; }
}
@media (max-width: 720px) {
  main { padding: 10px; }
  .topbar, .paper-form { grid-template-columns: 1fr; }
  .queue-controls { justify-content: flex-start; }
  .summary-grid { grid-template-columns: 1fr; }
  .action-rail { grid-template-columns: 64px 1fr; align-items: start; }
  .feedback-actions { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .feedback-state { text-align: left; grid-column: 1 / -1; }
  .health-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .health-graphs { grid-template-columns: 1fr; }
  .health-graph-wide { grid-column: auto; }
  .health-kv { grid-template-columns: 1fr; }
  .topic-columns { grid-template-columns: 1fr; }
  .topic-list { columns: 1; }
  .topic-form-grid, .topic-row { grid-template-columns: 1fr; }
}
"""
