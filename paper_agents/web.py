from __future__ import annotations

import html
import json
import mimetypes
import re
import sqlite3
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from paper_agents.db import DEFAULT_DB_PATH, connect_db, health_summary, init_db
from paper_agents.feedback import ProfileProvider, apply_feedback_to_profile, ingest_feedback_blob, parse_feedback_blob
from paper_agents.pdf_links import looks_like_direct_pdf_url
from paper_agents.topic_inventory import scout_topic_inventory
from paper_agents.topic_agent import (
    TopicProposal,
    apply_topic_proposal,
    normalize_topic_conversation,
    suggest_topic_proposal,
    topic_proposal_from_json,
    topic_proposal_to_json,
)
from paper_agents.scout import SCOUT_SOURCES
from paper_agents.runtime_config import default_sources, gemini_enabled, packaged
from paper_agents.topics import (
    CADENCES,
    DEFAULT_TOPIC_CONFIG_PATH,
    DuplicateTopicError,
    PRIORITIES,
    TopicEntry,
    create_topic_from_fast_path,
    ensure_unique_topic,
    load_topic_config_or_seed,
    normalize_sources,
    save_topic_config,
    update_topic_from_form,
)

ASSET_DIR = Path(__file__).with_name("assets")
LOGO_ASSETS = {"logo_light.png", "logo_dark.png"}
LOGO_ASSET_VERSION = "20260829"

FEEDBACK_STATUSES = [
    ("not_interested", "Not interested"),
]

FILTERS = [
    ("all", "All papers"),
    ("has_feedback", "Scored"),
    ("needs_review", "Needs review"),
]
SOURCE_FILTER_ALL = "all"

SORTS = [
    ("score", "Highest score"),
    ("latest", "Newest"),
]

VIEWS = [
    ("full", "Full"),
    ("compact", "Condensed"),
]


def run_review_ui(host: str = "127.0.0.1", port: int = 8000, db_path: Path = DEFAULT_DB_PATH) -> None:
    init_db(db_path)
    if packaged():
        from paper_agents.manual_scout import status as scout_status
        scout_status(db_path)
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
            if parsed.path == "/scout/status":
                if not packaged():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                payload = json.dumps(manual_scout_snapshot(db_path)).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path in {"/ready", "/runtime"}:
                from paper_agents.package_runtime import app_status, model_status
                status = app_status(db_path)
                if parsed.path == "/runtime":
                    status["model"] = model_status()
                payload = json.dumps(status).encode("utf-8")
                self.send_response(HTTPStatus.OK if status["ready"] else HTTPStatus.SERVICE_UNAVAILABLE)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if parsed.path == "/":
                params = urllib.parse.parse_qs(parsed.query)
                self.respond_html(
                    render_review_queue(
                        db_path,
                        saved=params.get("saved", [None])[0] == "1",
                        scout_error=params.get("scout_error", [None])[0],
                        profile_apply_queued=params.get("profile_apply_queued", [None])[0] == "1",
                        profile_apply_failed=params.get("profile_apply_failed", [None])[0] == "1",
                        filter_value=params.get("filter", ["all"])[0],
                        source_value=params.get("source", [SOURCE_FILTER_ALL])[0],
                        sort_value=params.get("sort", ["latest"])[0],
                        view_value=params.get("view", ["full"])[-1],
                        page_value=params.get("page", ["1"])[0],
                    )
                )
                return
            if parsed.path.startswith("/health/scout-diagnostics/"):
                run_value = parsed.path.removeprefix("/health/scout-diagnostics/")
                if not run_value.isascii() or not run_value.isdigit() or len(run_value) > 18:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                from paper_agents.scout_diagnostics import load_report
                report = load_report(db_path, int(run_value))
                if report is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                payload = render_scout_diagnostics_page(report).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
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
                        duplicate=params.get("duplicate", [None])[0] == "1",
                        error=params.get("error", [None])[0],
                        edit_id=params.get("edit", [None])[0],
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
            if parsed.path == "/scout/run":
                if not packaged():
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                from paper_agents.manual_scout import ScoutBusy, start
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    self.send_error(HTTPStatus.FORBIDDEN, "Run Scout from Project Paper")
                    return
                redirect = "/"
                try:
                    start(db_path)
                except ScoutBusy:
                    pass  # Existing status explains that a run is already active.
                except (ValueError, RuntimeError, OSError) as error:
                    redirect = "/?" + urllib.parse.urlencode({"scout_error": str(error)})
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", redirect)
                self.end_headers()
                return
            if parsed.path == "/topics":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                form = urllib.parse.parse_qs(body)
                try:
                    action = form.get("action", [""])[0]
                    if action == "propose":
                        conversation = topic_conversation_from_form(form)
                        request_text = form.get("request_text", [""])[0]
                        proposal = propose_topic_form(form, conversation=conversation)
                        updated_conversation = append_topic_agent_turns(conversation, request_text, proposal)
                        self.respond_html(
                            render_topics_page(
                                proposal=proposal,
                                conversation=updated_conversation,
                            )
                        )
                        return
                    if action == "apply_proposal":
                        apply_topic_proposal_form(form)
                    else:
                        save_topics_form(form)
                    redirect_to = "/topics?saved=1"
                except DuplicateTopicError as error:
                    redirect_to = (
                        "/topics?"
                        + urllib.parse.urlencode({"duplicate": "1", "edit": error.topic.id})
                        + "#topic-editor"
                    )
                except ValueError as error:
                    redirect_to = "/topics?" + urllib.parse.urlencode({"error": str(error)})
                except RuntimeError as error:
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

            status = form.get("status", [""])[-1] or None
            action = form.get("action", ["status"])[-1]
            submitted_notes = form.get("notes", [""])[0]
            notes = submitted_notes.strip()
            feedback_content = submitted_notes if action == "feedback" else ""
            recommendation_id = parse_optional_int(form.get("recommendation_id", [""])[0])
            if action not in {"status", "feedback"}:
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid feedback action")
                return
            if action == "status" and status != "not_interested":
                self.send_error(HTTPStatus.BAD_REQUEST, "Invalid feedback status")
                return
            if action == "feedback" and not feedback_content.strip():
                self.send_error(HTTPStatus.BAD_REQUEST, "Feedback content is required")
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


def manual_scout_snapshot(db_path: Path, config_path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> dict[str, Any]:
    from paper_agents.manual_scout import inference_readiness, selected_topics, status
    state = status(db_path)
    try:
        count = len(selected_topics(config_path))
        setup_message = "" if count else "Add at least one enabled arXiv topic in Topics to run Scout."
    except (ValueError, OSError):
        count = 0
        setup_message = "Topic configuration is unavailable or invalid. Check Topics and the app logs."
    readiness = inference_readiness(db_path) if count else {
        "status": "preparing", "message": "Add an enabled arXiv topic before checking local Qwen inference."}
    return {**state, "can_run": bool(count) and not state["busy"] and readiness["status"] == "ready",
            "topic_count": count, "setup_message": setup_message, "readiness": readiness}


def render_manual_scout_panel(db_path: Path, config_path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> str:
    if not packaged():
        return ""
    state = manual_scout_snapshot(db_path, config_path)
    label = "Retry Scout" if state["status"] in {"failed", "empty", "interrupted"} else "Run Scout"
    return f"""<section class="banner" aria-label="Manual discovery">
      <form method="post" action="/scout/run">
        <button id="run-scout" type="submit" {'' if state['can_run'] else 'disabled'}>{label}</button>
        <span id="scout-state" role="status" aria-live="polite">{escape(state['status'].title())}: {escape(state['message'])}</span>
      </form>
      <p id="scout-setup">{escape(state['setup_message'])}</p>
      <p id="scout-readiness">Local Qwen: {escape(state['readiness']['status'].title())}: {escape(state['readiness']['message'])}</p>
      <a href="/topics">Topics</a> · <a href="/runtime">Runtime</a> · <a href="/health">Health</a> · <a href="/">Refresh papers</a>
      <script>
        (() => {{
          const form = document.getElementById('run-scout').form;
          form.addEventListener('submit', () => {{ document.getElementById('run-scout').disabled = true; }});
          async function pollScout() {{
            try {{
              const response = await fetch('/scout/status', {{cache: 'no-store'}});
              if (!response.ok) throw new Error('Status unavailable');
              const state = await response.json();
              document.getElementById('scout-state').textContent = state.status + ': ' + state.message;
              document.getElementById('scout-setup').textContent = state.setup_message;
              document.getElementById('scout-readiness').textContent = 'Local Qwen: ' + state.readiness.status + ': ' + state.readiness.message;
              const button = document.getElementById('run-scout');
              button.disabled = !state.can_run;
              button.textContent = ['failed','empty','interrupted'].includes(state.status) ? 'Retry Scout' : 'Run Scout';
            }} catch (error) {{
              document.getElementById('scout-state').textContent = 'Status unavailable. Refresh this page to reconnect; saved papers remain local.';
            }}
            setTimeout(pollScout, 5000);
          }}
          setTimeout(pollScout, 5000);
        }})();
      </script>
    </section>"""


def render_review_queue(
    db_path: Path,
    *,
    saved: bool = False,
    scout_error: str | None = None,
    profile_apply_queued: bool = False,
    profile_apply_failed: bool = False,
    filter_value: str = "all",
    source_value: str = SOURCE_FILTER_ALL,
    sort_value: str = "latest",
    view_value: str = "full",
    page_value: str | int = 1,
) -> str:
    filter_value = normalize_filter_value(filter_value)
    source_choices = load_source_filter_choices(db_path)
    source_value = normalize_choice(source_value, source_choices, SOURCE_FILTER_ALL)
    sort_value = normalize_choice(sort_value, SORTS, "latest")
    view_value = normalize_choice(view_value, VIEWS, "full")
    result = load_review_page(db_path, filter_value=filter_value, source_value=source_value,
                              sort_value=sort_value, page=page_value)
    cards = result["cards"]
    banners = []
    if scout_error:
        banners.append(f'<div class="banner warning">{escape(scout_error)}</div>')
    if saved:
        banners.append('<div class="banner">Feedback saved.</div>')
    if profile_apply_queued:
        banners.append('<div class="banner">Profile update queued.</div>')
    if profile_apply_failed:
        banners.append('<div class="banner warning">Profile auto-apply failed. Feedback was saved; run feedback apply manually when ready.</div>')
    saved_banner = "".join(banners)
    request_path = build_queue_href(filter_value, source_value, sort_value, view_value, result["page"])
    pagination = render_queue_pagination(result, filter_value, source_value, sort_value, view_value)
    card_html = "\n".join(render_card(card, view_value=view_value, return_to=request_path) for card in cards)
    if not card_html:
        card_html = '<section class="empty">No selected papers are waiting in the registry yet.</section>'
    controls = f"""
      <form method="get" action="/" class="queue-controls">
        <input type="hidden" name="view" value="{view_value}">
        <input type="hidden" name="page" value="{result['page']}">
        {render_select(FILTERS, "filter", filter_value, "Queue")}
        {render_select(source_choices, "source", source_value, "Source")}
        {render_select(SORTS, "sort", sort_value, "Sort")}
        {render_view_toggle(view_value)}
      </form>
    """

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
    {render_app_header("Review Queue", f"{result['total']} papers | {escape(filter_label(filter_value))} | {escape(selected_label(source_choices, source_value))} | sorted by {escape(selected_label(SORTS, sort_value)).lower()}", controls, "review")}
    {saved_banner}
    {render_manual_scout_panel(db_path)}
    {pagination}
    <div class="cards">{card_html}</div>
    {pagination}
    <script>
      document.querySelectorAll(".pulled-date[data-pulled-at]").forEach((element) => {{
        const pulledAt = new Date(element.dataset.pulledAt);
        if (Number.isNaN(pulledAt.getTime())) {{
          return;
        }}
        const localDay = [
          pulledAt.getFullYear(),
          String(pulledAt.getMonth() + 1).padStart(2, "0"),
          String(pulledAt.getDate()).padStart(2, "0"),
        ].join("-");
        element.textContent = `Pulled ${{localDay}}`;
      }});
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
    signal_tags = "".join(f'<span class="tag">{escape(keyword)}</span>' for keyword in card["matched_keywords"][:6])
    notes = escape(card.get("feedback_notes") or "")
    user_score_html = render_user_score(card.get("user_score"))
    feedback_meta_html = render_feedback_meta(card)
    discussion_prompt = escape(build_discussion_prompt(card))
    summary = card["summary"]
    source_badge = f'<span class="source-badge {source_badge_class(card["source"])}">{escape(card["source_label"])}</span>'
    title_html = render_title_link(card)
    compact_class = " compact" if view_value == "compact" else ""
    summary_html = render_summary(summary, compact=view_value == "compact")
    rationale_html = render_match_rationale(card, signal_tags)
    feedback_summary = "View/edit feedback" if card.get("has_feedback") else "Add feedback"

    return f"""<article class="paper-card{compact_class}">
  <form method="post" action="/feedback" class="paper-form">
    <input type="hidden" name="paper_id" value="{card['id']}">
    <input type="hidden" name="recommendation_id" value="{escape(card['recommendation_id'] or '')}">
    <input type="hidden" name="return_to" value="{escape(return_to)}">
    <div class="paper-main">
      <div class="card-head">
        <div>
          <h2>{title_html}</h2>
          <div class="paper-meta">
            {source_badge}
            <span>{escape(card.get("published") or "date unknown")}</span>
            <span class="source-id">{escape(card["source_id"])}</span>
            {render_pdf_control(card)}
            {render_copy_control(card)}
            {render_pulled_date(card)}
          </div>
        </div>
      </div>
      {rationale_html}
      {summary_html}
      {feedback_meta_html}
      <div class="feedback-tools">
        <button type="button" class="metadata-action discussion-prompt" data-copy-value="{discussion_prompt}" aria-label="Copy Paper Discussion prompt">Copy discussion prompt</button>
      </div>
      <details class="feedback-editor">
        <summary>{feedback_summary}</summary>
        <label>Feedback<textarea name="notes" placeholder="Paste final feedback blob from Paper Discussion here">{notes}</textarea></label>
        <button type="submit" name="action" value="feedback" class="secondary save-feedback">Save feedback</button>
      </details>
      <span class="submit-state" aria-live="polite"></span>
    </div>
    <div class="action-rail">
      <div class="match-score {'score-secondary' if card.get('user_score') is not None else ''}"><span>Match Score</span><strong>{card["score"]:.1f}</strong></div>
      {user_score_html}
    </div>
  </form>
</article>"""


def render_user_score(score: float | None) -> str:
    if score is None:
        return ""
    return f'<div class="user-score"><span>Your score</span><strong>{format_user_score(score)}/5</strong></div>'


def display_rationale(card: dict[str, Any]) -> str:
    reason = (card.get("ranking_reason") or "").strip()
    if not reason:
        return "Matched your current profile signals."
    if reason.lower().startswith("matched ") and card.get("matched_keywords"):
        return "Matched your current profile signals."
    return reason


def render_match_rationale(card: dict[str, Any], signal_tags: str) -> str:
    rationale = display_rationale(card)
    signals_html = f'<div class="tags"><span class="signals-label">Signals</span>{signal_tags}</div>' if signal_tags else ""
    rationale_html = "" if rationale == "Matched your current profile signals." and signal_tags else f"<p>{escape(rationale)}</p>"
    provenance_note = ""
    if (card.get("summary") or {}).get("abstract_only"):
        provenance_note = '<div class="summary-provenance">Abstract-only triage. Full PDF was not downloaded.</div>'
    return f"""<section class="match-rationale">
        {provenance_note}
        <h3>Why this matches you</h3>
        {rationale_html}
        {signals_html}
      </section>"""


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


def build_discussion_prompt(card: dict[str, Any]) -> str:
    summary = card.get("summary") or {}
    summary_lines = discussion_summary_lines(summary)
    summary_block = "\n".join(summary_lines) if summary_lines else "No local triage summary or source abstract is available yet."
    signals = ", ".join(str(keyword) for keyword in card.get("matched_keywords", []) if keyword) or "Not available"
    user_score = card.get("user_score")
    user_score_line = f"User score: {format_user_score(user_score)}/5\n" if user_score is not None else ""
    return (
        "I want to discuss this paper for my Project Paper workflow.\n\n"
        "Paper:\n"
        f"{card.get('title') or 'Untitled paper'}\n"
        f"Source: {card.get('source_label') or 'Unknown source'}\n"
        f"Date: {card.get('published') or 'unknown'}\n"
        f"Link: {card.get('url') or 'not available'}\n"
        f"PDF: {discussion_pdf_link(card)}\n"
        f"Match score: {float(card.get('score') or 0):.1f}\n"
        f"{user_score_line}"
        f"Why this matches me: {display_rationale(card)}\n"
        f"Signals: {signals}\n\n"
        "Available local context:\n"
        f"{summary_block}\n\n"
        "Please help me review it in an audio-friendly way:\n"
        "1. Give me a concise orientation: what problem it addresses, why it matters, and the approach.\n"
        "2. Walk me through the paper section by section in plain language.\n"
        "3. Call out what is practically useful for AI applied to SRE, production operations, observability, incident response, debugging, reliability, or engineering workflows.\n"
        "4. Call out limitations, weak evidence, or reasons it may not be worth my time.\n"
        "5. At the end, produce a final feedback blob for Project Paper in this exact shape:\n\n"
        "Decision: keep|maybe|reject\n"
        "Score: 1-5, where 5 is highest\n"
        "Reason: ...\n"
        "Positive signals: ...\n"
        "Negative signals: ...\n"
        "What I want more of: ...\n"
        "What I want less of: ...\n"
    )


def discussion_pdf_link(card: dict[str, Any]) -> str:
    pdf_artifact = card.get("artifacts", {}).get("pdf")
    if pdf_artifact:
        return f"/artifact/{pdf_artifact['id']}"
    return card.get("pdf_url") or "not available"


def discussion_summary_lines(summary: dict[str, Any]) -> list[str]:
    lines = []
    for label, key in [
        ("Problem", "research_problem"),
        ("Why it matters", "why_it_matters"),
        ("Approach", "approach"),
        ("Source abstract", "source_abstract"),
    ]:
        value = summary_field_text(summary.get(key), fallback="")
        if key == "source_abstract":
            value = truncate_text(value, 900)
        if value:
            lines.append(f"{label}: {value}")
    return lines


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
    controls = f"""
      <form method="get" action="/health" class="queue-controls">
        {render_select([("7", "7 days"), ("21", "21 days"), ("30", "30 days"), ("90", "90 days")], "days", str(days), "Range")}
        {render_select(source_choices, "source", source_value, "Source")}
      </form>
    """

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
    {render_app_header("Health", f"{escape(summary['db']['path'])} | integrity {escape(summary['db']['integrity'])} | {format_bytes(summary['db']['size_bytes'])}", controls, "health")}
    {warning_html}
    <div class="health-cards">{card_html}</div>
    {render_profile_maintenance(summary["profile_maintenance"])}
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


def render_topics_page(
    *,
    saved: bool = False,
    duplicate: bool = False,
    error: str | None = None,
    edit_id: str | None = None,
    proposal: TopicProposal | None = None,
    request_text: str = "",
    conversation: list[dict[str, str]] | None = None,
    config_path: Path = DEFAULT_TOPIC_CONFIG_PATH,
) -> str:
    inventory = scout_topic_inventory(config_path=config_path)
    topics = load_topic_config_or_seed(config_path)
    tabs = "".join(
        f'<a class="topic-tab source-badge {source_badge_class(item["source"])}" href="#{escape(item["source"])}">{escape(item["label"])}</a>'
        for item in inventory
    )
    sections = "".join(render_topic_source_section(item) for item in inventory)
    topic_rows = render_topic_rows(topics, proposal)
    edit_topic = next((topic for topic in topics if topic.id == edit_id), None)
    editor_panel = render_topic_editor_panel(edit_topic) if edit_topic else ""
    all_topics_open = " open" if edit_topic or pending_topic_preview(proposal) else ""
    total_topics = len(topics)
    banner = ""
    if saved:
        banner = '<div class="banner">Saved. Changes apply to future scheduled runs.</div>'
    if duplicate and edit_topic:
        banner = f'<div class="banner">Topic already exists. Editing existing topic: {escape(edit_topic.label)}.</div>'
    if error:
        banner = f'<div class="banner warning">Topic config was not saved: {escape(error)}</div>'
    controls = ""
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
    {render_app_header("Topics", f"{len(inventory)} sources | {total_topics} configured topics | editable file-backed config", controls, "topics")}
    {banner}
    {render_topic_agent_panel(request_text, proposal, conversation or [])}
    {editor_panel}
    <details class="topic-inventory-details" open>
      <summary>Source schedule inventory</summary>
      <nav class="topic-tabs" aria-label="Scout topic sources">{tabs}</nav>
      <div class="topic-sections">{sections}</div>
    </details>
    <details class="topic-source topic-list-details"{all_topics_open}>
      <summary>
        <span>All Topics</span>
        <small>{topic_list_summary(total_topics, proposal)}</small>
      </summary>
      <div class="topic-toolbar">
        <label>Search<input id="topic-search" type="search" placeholder="Filter topics"></label>
        <span>Edit opens a focused panel; toggles affect future runs only.</span>
      </div>
      <div class="topic-table">
        <div class="topic-table-head">
          <span>Label</span><span>Query</span><span>Sources</span><span>Cadence</span><span>Priority</span><span>Enabled</span><span>Actions</span>
        </div>
        {topic_rows}
      </div>
    </details>
    <script>
      const topicSearch = document.getElementById("topic-search");
      if (topicSearch) {{
        topicSearch.addEventListener("input", () => {{
          const needle = topicSearch.value.trim().toLowerCase();
          document.querySelectorAll(".topic-row").forEach((row) => {{
            row.hidden = needle && !row.dataset.topicText.includes(needle);
          }});
        }});
      }}
      document.querySelectorAll(".topic-agent-form").forEach((form) => {{
        form.addEventListener("submit", (event) => {{
          const button = event.submitter || form.querySelector("button[type='submit']");
          const status = form.querySelector(".topic-agent-status");
          if (button) {{
            button.disabled = true;
            button.textContent = "Asking...";
          }}
          if (status) {{
            status.textContent = "Asking local Qwen. If it is slow, Project Paper will fall back to a deterministic proposal.";
          }}
          setTimeout(() => {{
            if (status) {{
              status.textContent = "Still waiting on local Qwen. This request should fall back soon.";
            }}
          }}, 12000);
        }});
      }});
    </script>
  </main>
</body>
</html>"""


def render_topic_source_section(item: dict[str, Any]) -> str:
    next_scheduled_topics = "".join(f"<li>{escape(topic)}</li>" for topic in item["active_topics"])
    if not next_scheduled_topics:
        next_scheduled_topics = "<li>No enabled topics are scheduled for this run.</li>"
    next_window = "AM" if item.get("next_slot", 0) == 0 else "PM"
    next_run = item.get("next_run_date")
    schedule = item["schedule"] if not next_run else f'{item["schedule"]} | next {next_run} {next_window}'
    topics = "".join(
        f"<li>{escape(topic.label)} <span>{escape(topic.cadence)} / {escape(topic.priority)} / {'enabled' if topic.enabled else 'disabled'}</span></li>"
        for topic in item["topics"]
    )
    configured_count = len(item["topics"])
    enabled_count = sum(topic.enabled for topic in item["topics"])
    next_scheduled_count = len(item["active_topics"])
    return f"""<section class="topic-source" id="{escape(item["source"])}">
      <div class="topic-source-head">
        <h2><span class="source-badge {source_badge_class(item["source"])}">{escape(item["label"])}</span></h2>
        <span>{escape(schedule)}</span>
      </div>
      <p>{escape(item["description"])}</p>
      <div class="topic-columns">
        <div>
          <h3>Next scheduled topics ({next_scheduled_count})</h3>
          <ol class="topic-list active-topic-list">{next_scheduled_topics}</ol>
        </div>
        <div>
          <h3>Configured topics ({configured_count} total; {enabled_count} enabled)</h3>
          <ol class="topic-list">{topics}</ol>
        </div>
      </div>
      <p class="topic-note-text">{escape(item["notes"])}</p>
    </section>"""


def render_topic_agent_panel(
    request_text: str = "",
    proposal: TopicProposal | None = None,
    conversation: list[dict[str, str]] | None = None,
) -> str:
    proposal_html = render_topic_proposal_panel(proposal)
    conversation = normalize_topic_conversation(conversation or [])
    conversation_html = render_topic_conversation(conversation)
    conversation_json = topic_conversation_to_json(conversation)
    prompt = "Reply to TopicAgent" if proposal and proposal.action == "ask_clarifying_question" else "What do you want Project Paper to scout?"
    return f"""<section class="topic-source topic-editor">
      <div class="topic-source-head">
        <h2>Topic Agent</h2>
        <span>Describe what to scout; Qwen proposes the config</span>
      </div>
      {conversation_html}
      <form method="post" action="/topics" class="topic-agent-form">
        <input type="hidden" name="action" value="propose">
        <input type="hidden" name="conversation_json" value="{escape(conversation_json)}">
        <label>{escape(prompt)}
          <textarea name="request_text" required placeholder="datalake reliability and operations">{escape(request_text)}</textarea>
        </label>
        {render_source_checkboxes(default_sources())}
        <button type="submit" class="primary">Ask TopicAgent</button>
        <p class="topic-agent-status" aria-live="polite"></p>
      </form>
      {proposal_html}
    </section>"""


def render_topic_conversation(conversation: list[dict[str, str]]) -> str:
    if not conversation:
        return ""
    turns = "".join(
        f'<div class="topic-turn topic-turn-{escape(turn["role"])}"><span>{escape(turn["role"])}</span><p>{escape(turn["content"])}</p></div>'
        for turn in conversation
    )
    return f'<div class="topic-conversation" aria-label="TopicAgent conversation">{turns}</div>'


def render_topic_proposal_panel(proposal: TopicProposal | None) -> str:
    if proposal is None:
        return ""
    if proposal.action == "ask_clarifying_question":
        return f"""<div class="topic-proposal-panel">
          <div class="topic-source-head">
            <h2>TopicAgent Question</h2>
            <span>{escape(proposal.provider)}</span>
          </div>
          <p>{escape(proposal.question)}</p>
          <p class="topic-note-text">{escape(proposal.rationale)}</p>
        </div>"""
    matched = f"<p><strong>Matched existing:</strong> {escape(proposal.matched_topic_id or '')}</p>" if proposal.matched_topic_id else ""
    sources = "".join(
        f'<span class="source-badge {source_badge_class(source)}">{escape(source_display_name(source))}</span>'
        for source in proposal.sources or []
    )
    edit_link = (
        f'<a class="secondary-link" href="/topics?edit={escape(proposal.matched_topic_id)}#topic-editor">Edit manually</a>'
        if proposal.matched_topic_id and proposal.action != "remove_existing"
        else ""
    )
    provider_label = proposal.provider.replace("_", " ")
    proposal_label = f"{proposal.action.replace('_', ' ')} | {provider_label}"
    return f"""<div class="topic-proposal-panel" id="topic-proposal">
      <div class="topic-source-head">
        <h2>TopicAgent Proposal</h2>
        <span>{escape(proposal_label)}</span>
      </div>
      {matched}
      <dl class="topic-proposal-grid">
        <dt>Label</dt><dd>{escape(proposal.label)}</dd>
        <dt>Query</dt><dd>{escape(proposal.query)}</dd>
        <dt>Sources</dt><dd>{sources}</dd>
        <dt>Cadence</dt><dd>{escape(proposal.cadence)}</dd>
        <dt>Priority</dt><dd>{escape(proposal.priority)}</dd>
        <dt>Enabled</dt><dd>{'yes' if proposal.enabled else 'no'}</dd>
        <dt>Rationale</dt><dd>{escape(proposal.rationale or 'No rationale provided.')}</dd>
        <dt>Source rationale</dt><dd>{escape(proposal.source_rationale or 'No source rationale provided.')}</dd>
      </dl>
      <form method="post" action="/topics" class="topic-proposal-actions">
        <input type="hidden" name="action" value="apply_proposal">
        <input type="hidden" name="proposal_json" value="{escape(topic_proposal_to_json(proposal))}">
        <button type="submit" class="primary">{'Remove topic' if proposal.action == 'remove_existing' else 'Apply and save'}</button>
        {edit_link}
        <a class="secondary-link" href="/topics">Cancel</a>
      </form>
    </div>"""


def render_topic_row(topic: TopicEntry) -> str:
    sources = "".join(
        f'<span class="source-badge {source_badge_class(source)}">{escape(source_display_name(source))}</span>'
        for source in topic.sources
    )
    enabled = "enabled" if topic.enabled else "disabled"
    toggle_label = "On" if topic.enabled else "Off"
    next_enabled = "0" if topic.enabled else "1"
    return f"""<div class="topic-row topic-read-row" data-topic-text="{escape((topic.label + ' ' + topic.query).lower())}">
      <div class="topic-cell topic-label"><strong>{escape(topic.label)}</strong></div>
      <div class="topic-cell topic-query" title="{escape(topic.query)}">{escape(topic.query)}</div>
      <div class="topic-cell topic-sources">{sources}</div>
      <div class="topic-cell"><span class="topic-pill cadence-{escape(topic.cadence)}">{escape(topic.cadence)}</span></div>
      <div class="topic-cell"><span class="topic-pill priority-{escape(topic.priority)}">{escape(topic.priority)}</span></div>
      <div class="topic-cell">
        <form method="post" action="/topics" class="topic-toggle-form">
          <input type="hidden" name="action" value="toggle">
          <input type="hidden" name="topic_id" value="{escape(topic.id)}">
          <input type="hidden" name="enabled" value="{next_enabled}">
          <button type="submit" class="topic-status topic-toggle-button status-{enabled}" title="Toggle enabled status">{toggle_label}</button>
        </form>
      </div>
      <div class="topic-actions">
        <a class="topic-edit-link" href="/topics?edit={escape(topic.id)}">Edit</a>
      </div>
    </div>"""


def render_topic_rows(topics: list[TopicEntry], proposal: TopicProposal | None = None) -> str:
    preview = pending_topic_preview(proposal)
    if preview is None:
        return "".join(render_topic_row(topic) for topic in topics)
    rows: list[str] = []
    inserted = False
    for topic in topics:
        if preview.id == topic.id:
            rows.append(render_topic_preview_row(preview, proposal))
            inserted = True
        else:
            rows.append(render_topic_row(topic))
    if not inserted:
        rows.insert(0, render_topic_preview_row(preview, proposal))
    return "".join(rows)


def pending_topic_preview(proposal: TopicProposal | None) -> TopicEntry | None:
    if proposal is None or proposal.action not in {"create_new", "update_existing", "remove_existing"}:
        return None
    if not proposal.label or not proposal.query:
        return None
    return TopicEntry(
        id=proposal.matched_topic_id or f"pending-{proposal.label.casefold().replace(' ', '-')}",
        label=proposal.label,
        query=proposal.query,
        sources=proposal.sources or [],
        cadence=proposal.cadence,
        priority=proposal.priority,
        enabled=proposal.enabled,
    )


def render_topic_preview_row(topic: TopicEntry, proposal: TopicProposal | None) -> str:
    sources = "".join(
        f'<span class="source-badge {source_badge_class(source)}">{escape(source_display_name(source))}</span>'
        for source in topic.sources
    )
    is_remove = proposal is not None and proposal.action == "remove_existing"
    enabled = "disabled" if is_remove else ("enabled" if topic.enabled else "disabled")
    action = "New" if proposal and proposal.action == "create_new" else ("Remove" if is_remove else "Update")
    return f"""<div class="topic-row topic-read-row topic-preview-row" data-topic-text="{escape((topic.label + ' ' + topic.query).lower())}">
      <div class="topic-cell topic-label"><strong>{escape(topic.label)}</strong> <span class="topic-pending-badge">{escape(action)} pending</span></div>
      <div class="topic-cell topic-query" title="{escape(topic.query)}">{escape(topic.query)}</div>
      <div class="topic-cell topic-sources">{sources}</div>
      <div class="topic-cell"><span class="topic-pill cadence-{escape(topic.cadence)}">{escape(topic.cadence)}</span></div>
      <div class="topic-cell"><span class="topic-pill priority-{escape(topic.priority)}">{escape(topic.priority)}</span></div>
      <div class="topic-cell"><span class="topic-status status-{enabled}">{'Will remove' if is_remove else ('On' if topic.enabled else 'Off')}</span></div>
      <div class="topic-actions"><span class="topic-preview-note">{'Apply to remove' if is_remove else 'Apply to save'}</span></div>
    </div>"""


def topic_list_summary(total_topics: int, proposal: TopicProposal | None = None) -> str:
    if pending_topic_preview(proposal) is not None:
        return f"{total_topics} configured | showing pending TopicAgent preview"
    return f"{total_topics} configured | expand to search, toggle, or edit"


def render_topic_editor_panel(topic: TopicEntry) -> str:
    return f"""<section class="topic-source topic-editor-panel" id="topic-editor">
      <div class="topic-source-head">
        <h2>Edit Topic</h2>
        <span>{escape(topic.label)}</span>
      </div>
      <form method="post" action="/topics" class="topic-edit-form" data-topic-text="{escape((topic.label + ' ' + topic.query).lower())}">
        <input type="hidden" name="action" value="update">
        <input type="hidden" name="topic_id" value="{escape(topic.id)}">
        <label>Label<input name="label" value="{escape(topic.label)}" required autofocus></label>
        <label>Query<input name="query" value="{escape(topic.query)}" required></label>
        {render_source_checkboxes(topic.sources)}
        {render_topic_select("cadence", CADENCES, topic.cadence, "Cadence")}
        {render_topic_select("priority", PRIORITIES, topic.priority, "Priority")}
        <label class="inline-check"><input type="checkbox" name="enabled" value="1" {'checked' if topic.enabled else ''}> Enabled</label>
        <div class="topic-form-actions">
          <button type="submit" class="primary">Save</button>
          <a class="secondary-link" href="/topics">Cancel</a>
        </div>
      </form>
    </section>"""


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


def propose_topic_form(
    form: dict[str, list[str]],
    *,
    conversation: list[dict[str, str]] | None = None,
    config_path: Path = DEFAULT_TOPIC_CONFIG_PATH,
) -> TopicProposal:
    topics = load_topic_config_or_seed(config_path)
    requested_sources = form.get("sources", [])
    if not requested_sources:
        raise ValueError("Select at least one source for the TopicAgent proposal")
    selected_sources = normalize_sources(requested_sources)
    proposal = suggest_topic_proposal(
        form.get("request_text", [""])[0],
        topics,
        conversation=conversation,
    )
    if proposal.action in {"create_new", "update_existing"}:
        proposal.sources = selected_sources
        selection_note = "Sources selected in the Topic Agent form."
        proposal.source_rationale = " ".join(
            part for part in (proposal.source_rationale, selection_note) if part
        )
    return proposal


def apply_topic_proposal_form(
    form: dict[str, list[str]],
    *,
    config_path: Path = DEFAULT_TOPIC_CONFIG_PATH,
) -> None:
    topics = load_topic_config_or_seed(config_path)
    proposal = topic_proposal_from_json(form.get("proposal_json", ["{}"])[0], topics)
    save_topic_config(apply_topic_proposal(proposal, topics), config_path)


def topic_conversation_from_form(form: dict[str, list[str]]) -> list[dict[str, str]]:
    raw = form.get("conversation_json", ["[]"])[0]
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(decoded, list):
        return []
    return normalize_topic_conversation([turn for turn in decoded if isinstance(turn, dict)])


def topic_conversation_to_json(conversation: list[dict[str, str]]) -> str:
    return json.dumps(normalize_topic_conversation(conversation), separators=(",", ":"))


def append_topic_agent_turns(
    conversation: list[dict[str, str]],
    user_text: str,
    proposal: TopicProposal,
) -> list[dict[str, str]]:
    turns = normalize_topic_conversation(conversation)
    if user_text.strip():
        turns.append({"role": "user", "content": user_text.strip()})
    turns.append({"role": "agent", "content": topic_agent_message(proposal)})
    return normalize_topic_conversation(turns)


def topic_agent_message(proposal: TopicProposal) -> str:
    if proposal.action == "ask_clarifying_question":
        return proposal.question
    if proposal.action == "remove_existing":
        return f"I found an existing topic to remove: {proposal.label}."
    if proposal.action == "update_existing":
        if not proposal.enabled:
            return f"I found an existing topic to disable: {proposal.label}."
        return f"I found an existing topic to update: {proposal.label}."
    return f"I prepared a topic proposal: {proposal.label}."


def save_topics_form(form: dict[str, list[str]], *, config_path: Path = DEFAULT_TOPIC_CONFIG_PATH) -> None:
    topics = load_topic_config_or_seed(config_path)
    action = form.get("action", [""])[0]
    if action == "add":
        topic = create_topic_from_fast_path(
            form.get("topic_text", [""])[0],
            existing_topics=topics,
            query=form.get("query", [""])[0] or None,
            sources=form.get("sources") or default_sources(),
            cadence=form.get("cadence", ["daily"])[0],
            priority=form.get("priority", ["normal"])[0],
            enabled=form.get("enabled", ["1"])[0] == "1",
        )
        topics.append(topic)
        save_topic_config(topics, config_path)
        return
    if action == "update":
        topic_id = form.get("topic_id", [""])[0]
        for index, topic in enumerate(topics):
            if topic.id != topic_id:
                continue
            updated_topic = update_topic_from_form(
                topic,
                label=form.get("label", [""])[0],
                query=form.get("query", [""])[0],
                sources=form.get("sources", []),
                cadence=form.get("cadence", ["daily"])[0],
                priority=form.get("priority", ["normal"])[0],
                enabled=form.get("enabled", [""])[0] == "1",
            )
            ensure_unique_topic(updated_topic, topics, ignore_id=topic.id)
            topics[index] = updated_topic
            save_topic_config(topics, config_path)
            return
        raise ValueError(f"Unknown topic id: {topic_id}")
    if action == "toggle":
        topic_id = form.get("topic_id", [""])[0]
        enabled = form.get("enabled", [""])[0] == "1"
        for index, topic in enumerate(topics):
            if topic.id != topic_id:
                continue
            topics[index] = TopicEntry(
                id=topic.id,
                label=topic.label,
                query=topic.query,
                sources=topic.sources,
                cadence=topic.cadence,
                priority=topic.priority,
                enabled=enabled,
                note=topic.note,
            )
            save_topic_config(topics, config_path)
            return
        raise ValueError(f"Unknown topic id: {topic_id}")
    raise ValueError("Unknown topic action")


def render_warnings(warnings: list[dict[str, Any]]) -> str:
    if not warnings:
        return ""
    items = "".join(
        f'<li class="{escape(warning["level"])}"><strong>{escape(warning["level"])}</strong> {escape(warning["message"])}{render_diagnostic_link(warning)}</li>'
        for warning in warnings
    )
    return f'<section class="health-warnings"><h2>Warnings</h2><p>Scout warnings: latest run per scheduled source in the last 7 days, independent of the selected chart range. Run times are shown below.</p><ul>{items}</ul></section>'


def render_diagnostic_link(warning: dict) -> str:
    run_id = warning.get("scout_run_id")
    if not isinstance(run_id, int) or run_id <= 0:
        return ""
    return f' <a href="/health/scout-diagnostics/{run_id}" target="_blank" rel="noopener">View diagnostics</a>'


def render_scout_diagnostics_page(report: dict[str, Any]) -> str:
    run = report["run"]
    title = f"Scout diagnostics — {run['source']} run #{run['id']}"
    report_text = escape(json.dumps(report, indent=2, ensure_ascii=False))
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>{page_css()}
    #diagnostic-report {{ display: block; box-sizing: border-box; width: 100%; height: 65vh;
      margin-top: 12px; font-family: monospace; white-space: pre; overflow: auto; }}
  </style>
</head>
<body><main>
  <a href="/health">Back to Health</a>
  <h1>{escape(title)}</h1>
  <p>Run started: {escape(str(run['started_at']))} · Report captured: {escape(report['captured_at'])}</p>
  <p>Copy this report to share the recorded troubleshooting details.</p>
  <button type="button" id="copy-report">Copy report</button>
  <span id="copy-status" role="status" aria-live="polite"></span>
  <label for="diagnostic-report">Diagnostic report</label>
  <textarea id="diagnostic-report" readonly spellcheck="false">{report_text}</textarea>
</main>
<script>
  document.getElementById('copy-report').addEventListener('click', async () => {{
    const report = document.getElementById('diagnostic-report');
    const status = document.getElementById('copy-status');
    try {{
      if (!navigator.clipboard || !window.isSecureContext) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(report.value);
      status.textContent = 'Copied.';
    }} catch (_) {{
      report.focus();
      report.select();
      let copied = false;
      try {{ copied = document.execCommand('copy'); }} catch (_) {{}}
      status.textContent = copied ? 'Copied.' : 'Report selected. Press Ctrl+C or Command+C to copy.';
    }}
  }});
</script>
</body></html>"""


def render_profile_maintenance(notices: list[dict[str, str]]) -> str:
    if not notices:
        return ""
    items = "".join(f'<li>{escape(notice["message"])}</li>' for notice in notices)
    return f'<section class="health-section"><h2>Profile maintenance / Needs attention</h2><ul>{items}</ul></section>'


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


def render_title_link(card: dict[str, Any]) -> str:
    title = escape(card["title"])
    url = card.get("url")
    if not url:
        return title
    return f'<a class="title-link" href="{escape(url)}" target="_blank" rel="noreferrer">{title}</a>'


def render_pdf_control(card: dict[str, Any]) -> str:
    pdf_artifact = card.get("artifacts", {}).get("pdf")
    if pdf_artifact:
        return f'<a class="metadata-action pdf-action" href="/artifact/{pdf_artifact["id"]}" target="_blank" rel="noreferrer">Open PDF</a>'
    pdf_url = card.get("pdf_url")
    if looks_like_direct_pdf_url(card.get("source"), pdf_url):
        return f'<a class="metadata-action pdf-action" href="{escape(pdf_url)}" target="_blank" rel="noreferrer">Open PDF</a>'
    return ""



def render_copy_control(card: dict[str, Any]) -> str:
    url = card.get("url")
    if not url:
        return ""
    escaped_url = escape(url)
    return (
        f'<button type="button" class="metadata-action copy-url secondary-action" '
        f'data-copy-value="{escaped_url}" aria-label="Copy paper URL">Copy</button>'
    )


def render_pulled_date(card: dict[str, Any]) -> str:
    pulled_at = str(card.get("pulled_at") or "").strip()
    if not pulled_at:
        return ""
    timestamp = pulled_at.replace(" ", "T")
    if not re.search(r"(?:Z|[+-]\\d{2}:?\\d{2})$", timestamp, flags=re.IGNORECASE):
        timestamp += "Z"
    return (
        f'<time class="pulled-date" data-pulled-at="{escape(timestamp)}" '
        f'datetime="{escape(timestamp)}">Pulled {escape(pulled_at[:10])}</time>'
    )


def render_summary(summary: dict[str, Any], *, compact: bool) -> str:
    has_extracted_summary = any(summary.get(key) for key in ["research_problem", "why_it_matters", "approach"])
    source_abstract = summary.get("source_abstract")
    if not has_extracted_summary and source_abstract:
        abstract = escape(truncate_text(summary_field_text(source_abstract), 900))
        if compact:
            return f'<div class="compact-summary"><strong>Source Abstract:</strong> {abstract}</div>'
        return f"""<div class="source-summary">
    <section><h3>Source Abstract</h3><p>{abstract}</p></section>
  </div>"""

    problem = escape(summary_display_text(summary.get("research_problem")))
    if compact:
        return f'<div class="compact-summary"><strong>Problem:</strong> {problem}</div>'
    return f"""<div class="summary-grid">
    <section><h3>Problem</h3><p>{problem}</p></section>
    <section><h3>Why it matters</h3><p>{escape(summary_display_text(summary.get("why_it_matters")))}</p></section>
    <section><h3>Approach</h3><p>{escape(summary_display_text(summary.get("approach")))}</p></section>
  </div>"""


def summary_display_text(value: Any, *, max_chars: int = 520) -> str:
    return truncate_text(summary_field_text(value), max_chars)


def summary_field_text(value: Any, *, fallback: str = "Not extracted yet.") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        text = value.strip()
        decoded = decode_summary_json_string(text)
        if decoded is not None:
            return summary_field_text(decoded, fallback=fallback)
        if looks_like_non_prose_summary(text):
            return fallback
        return text or fallback
    if isinstance(value, list):
        parts = [summary_field_text(item, fallback="") for item in value]
        text = "; ".join(part for part in parts if part)
        return text or fallback
    if isinstance(value, dict):
        name = summary_field_text(value.get("name"), fallback="")
        description = summary_field_text(value.get("description"), fallback="")
        if name and description:
            return f"{name}: {description}"
        text_parts = []
        for key, item in value.items():
            part = summary_field_text(item, fallback="")
            if not part:
                continue
            text_parts.append(f"{summary_label(key)}: {part}")
        text = "; ".join(part for part in text_parts if part)
        return text or fallback
    return str(value).strip() or fallback


def summary_label(value: Any) -> str:
    text = str(value).strip().replace("_", " ").replace("-", " ")
    return " ".join(word.capitalize() for word in text.split())


def decode_summary_json_string(text: str) -> Any | None:
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def truncate_text(value: str, max_chars: int) -> str:
    text = " ".join(value.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def looks_like_non_prose_summary(text: str) -> bool:
    if not text:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z-]{2,}", text)
    alpha_count = sum(1 for character in text if character.isalpha())
    symbol_count = sum(
        1
        for character in text
        if not character.isalnum()
        and not character.isspace()
        and character not in ".,;:!?'-/()"
    )
    has_math_markers = any(marker in text for marker in ["=", "{", "}", "|", "\\", "_"])
    if has_math_markers and len(words) < 5:
        return True
    if has_math_markers and symbol_count > max(8, alpha_count // 3):
        return True
    return False


def button_class(card: dict[str, Any], status: str) -> str:
    return "primary" if card.get("feedback_status") == status else "secondary"


REVIEW_PAGE_SIZE = 50


def load_review_cards(db_path: Path, *, filter_value: str, source_value: str, sort_value: str,
                      page: int = 1) -> list[dict[str, Any]]:
    return load_review_page(db_path, filter_value=filter_value, source_value=source_value,
                            sort_value=sort_value, page=page)["cards"]


def load_review_page(db_path: Path, *, filter_value: str, source_value: str, sort_value: str,
                     page: str | int = 1) -> dict[str, Any]:
    init_db(db_path)
    where_clauses = []
    params: list[Any] = []
    if filter_value == "needs_review":
        where_clauses.append("latest_recommendation.paper_id IS NOT NULL")
        where_clauses.append("latest_feedback.status IS NULL")
        where_clauses.append("latest_structured_feedback.paper_id IS NULL")
        where_clauses.append("latest_raw_feedback.paper_id IS NULL")
    elif filter_value == "has_feedback":
        where_clauses.append(
            "("
            "latest_structured_feedback.paper_id IS NOT NULL "
            "OR latest_raw_feedback.paper_id IS NOT NULL "
            "OR LOWER(COALESCE(latest_feedback.notes, '')) LIKE '%score:%'"
            ")"
        )
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
        order_clause = (
            "ORDER BY papers.first_discovered_at DESC NULLS LAST, "
            "latest_recommendation.curator_run_id DESC, latest_recommendation.recommendation_order ASC, papers.id DESC"
        )

    connection = connect_db(db_path)
    try:
        # Count and page rows share a read snapshot, including during feedback writes.
        connection.execute("BEGIN")
        query = f"""
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
                    content,
                    received_at,
                    ROW_NUMBER() OVER (PARTITION BY paper_id ORDER BY id DESC) AS row_number
                FROM raw_feedback
            ), primary_source AS (
                SELECT
                    paper_id,
                    source,
                    source_id,
                    url,
                    pdf_url,
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
            ), artifact_status AS (
                SELECT
                    paper_id,
                    SUM(CASE WHEN artifact_type = 'pdf' THEN 1 ELSE 0 END) AS pdf_count,
                    SUM(CASE WHEN artifact_type = 'triage_summary' THEN 1 ELSE 0 END) AS triage_summary_count
                FROM artifacts
                GROUP BY paper_id
            )
            SELECT
                papers.id,
                latest_recommendation.recommendation_id,
                primary_source.source,
                primary_source.source_id,
                papers.title,
                papers.published,
                papers.first_discovered_at,
                papers.abstract,
                primary_source.url,
                primary_source.pdf_url,
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
                latest_raw_feedback.content,
                COALESCE(latest_structured_feedback.created_at, latest_raw_feedback.received_at) AS latest_feedback_received_at
            FROM papers
            LEFT JOIN latest_recommendation
              ON latest_recommendation.paper_id = papers.id
             AND latest_recommendation.row_number = 1
            LEFT JOIN primary_source ON primary_source.paper_id = papers.id AND primary_source.row_number = 1
            LEFT JOIN source_rollup ON source_rollup.paper_id = papers.id
            LEFT JOIN artifact_status ON artifact_status.paper_id = papers.id
            LEFT JOIN latest_feedback ON latest_feedback.paper_id = papers.id AND latest_feedback.row_number = 1
            LEFT JOIN latest_structured_feedback ON latest_structured_feedback.paper_id = papers.id AND latest_structured_feedback.row_number = 1
            LEFT JOIN latest_raw_feedback ON latest_raw_feedback.paper_id = papers.id AND latest_raw_feedback.row_number = 1
            {where_clause}
            """
        total = connection.execute(f"SELECT COUNT(*) FROM ({query})", tuple(params)).fetchone()[0]
        pages = max(1, (total + REVIEW_PAGE_SIZE - 1) // REVIEW_PAGE_SIZE)
        try:
            page = max(1, min(int(page), pages))
        except (ValueError, TypeError, OverflowError):
            page = 1
        rows = connection.execute(
            query + f" {order_clause} LIMIT ? OFFSET ?",
            (*params, REVIEW_PAGE_SIZE, (page - 1) * REVIEW_PAGE_SIZE),
        ).fetchall()

        cards = []
        for row in rows:
            paper_id = row[0]
            artifacts = load_artifacts_for_paper(connection, paper_id)
            lightweight_score = parse_lightweight_feedback_score(row[14])
            user_score = row[16] if row[16] is not None else lightweight_score
            has_feedback = bool(
                row[17] is not None
                or row[18] is not None
                or row[19] is not None
                or lightweight_score is not None
            )
            cards.append(
                {
                    "id": paper_id,
                    "recommendation_id": row[1],
                    "source": row[2] or "unknown",
                    "source_id": row[3] or "unknown",
                    "source_label": source_label(row[2] or "unknown", parse_sources(row[15])),
                    "user_score": user_score,
                    "feedback_decision": row[17],
                    "feedback_received_at": row[19] or row[18],
                    "has_feedback": has_feedback,
                    "title": row[4],
                    "published": row[5],
                    "pulled_at": row[6],
                    "url": row[8],
                    "pdf_url": row[9],
                    "score": float(row[10] or 0),
                    "matched_keywords": decode_json(row[11], []),
                    "ranking_reason": row[12],
                    "feedback_status": row[13],
                    "feedback_notes": row[20] or row[14],
                    "artifacts": artifacts,
                    "summary": with_source_abstract(load_summary(artifacts.get("triage_summary")), row[7]),
                }
            )
    finally:
        connection.close()
    return {"cards": cards, "total": total, "page": page, "pages": pages,
            "start": (page - 1) * REVIEW_PAGE_SIZE + 1 if total else 0,
            "end": min(page * REVIEW_PAGE_SIZE, total)}


def parse_lightweight_feedback_score(notes: str | None) -> float | None:
    if not notes:
        return None
    parsed = parse_feedback_blob(notes)
    return parsed["score"]


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
        f'<label class="control-label"><span class="visually-hidden">{escape(label)}</span>'
        f'<select name="{escape(name)}" onchange="if(this.form.elements.page){{this.form.elements.page.value=1;}}this.form.submit()">{"".join(options)}</select>'
        "</label>"
    )


def render_view_toggle(view_value: str) -> str:
    options = []
    for value, label, icon in [("full", "Full", "▦"), ("compact", "Condensed", "☰")]:
        current = value == view_value
        options.append(
            f'<button type="submit" name="view" value="{value}" '
            f'class="view-option{" current" if current else ""}" '
            f'aria-label="{label} view" title="{label} view" aria-pressed="{str(current).lower()}">'
            f'<span aria-hidden="true">{icon}</span></button>'
        )
    return f'<div class="view-toggle" role="group" aria-label="View density">{"".join(options)}</div>'


def render_app_header(page_title: str, subtitle: str, controls_html: str, current_page: str) -> str:
    controls_block = f'<div class="header-controls">{controls_html}</div>' if controls_html.strip() else ""
    return f"""<header class="topbar">
      <div class="header-main">
        <div>
          <h1 class="brand-title">
            <a class="brand-home" href="/">
              <picture class="logo-frame"><source srcset="/assets/logo_dark.png?v={LOGO_ASSET_VERSION}" media="(prefers-color-scheme: dark)"><img src="/assets/logo_light.png?v={LOGO_ASSET_VERSION}" alt="" class="brand-logo"></picture>
              <span class="brand-name">Project Paper</span>
              <span class="page-title">{escape(page_title)}</span>
            </a>
          </h1>
          <p>{subtitle}</p>
        </div>
        <nav class="primary-nav" aria-label="Primary">
          {render_primary_nav(current_page)}
        </nav>
      </div>
      {controls_block}
    </header>"""


def render_primary_nav(current_page: str) -> str:
    links = [
        ("review", "/", "Review Queue"),
        ("topics", "/topics", "Topics"),
        ("health", "/health", "Health"),
    ]
    return "".join(
        f'<a class="secondary-link{" current" if key == current_page else ""}" href="{href}">{label}</a>'
        for key, href, label in links
    )


def build_queue_href(filter_value: str, source_value: str, sort_value: str, view_value: str, page: int = 1) -> str:
    return "/?" + urllib.parse.urlencode({"filter": filter_value, "source": source_value, "sort": sort_value, "view": view_value, "page": page})


def render_queue_pagination(result: dict[str, Any], filter_value: str, source_value: str,
                            sort_value: str, view_value: str) -> str:
    if result["pages"] <= 1:
        return ""
    links = []
    for target, label, symbol in [(result["page"] - 1, "Previous page", "&#8592;"),
                                  (result["page"] + 1, "Next page", "&#8594;")]:
        if 1 <= target <= result["pages"]:
            href = build_queue_href(filter_value, source_value, sort_value, view_value, target)
            links.append(f'<a class="secondary-link" href="{escape(href)}" aria-label="{label}" title="{label}">{symbol}</a>')
        else:
            links.append(f'<span class="page-disabled" aria-disabled="true" aria-label="{label}">{symbol}</span>')
    return (f'<nav class="queue-pagination" aria-label="Review Queue pages">'
            f'<span>{result["start"]}–{result["end"]} of {result["total"]} papers | '
            f'Page {result["page"]} of {result["pages"]}</span>{"".join(links)}</nav>')


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


def normalize_filter_value(value: str) -> str:
    allowed = {choice for choice, _ in FILTERS}
    return value if value in allowed else "all"


def filter_label(value: str) -> str:
    return dict(FILTERS).get(value) or value


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
    metadata = artifact.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    summary = {}
    if (metadata.get("abstract_only") or metadata.get("full_text_available") is False
            or metadata.get("source_type") == "source_abstract"):
        summary.update(abstract_only=True, source_type="source_abstract")
    # Keep database provenance even when the JSON artifact is absent or malformed.
    try:
        data = json.loads(Path(artifact["path"]).read_text(encoding="utf-8"))
    except (KeyError, TypeError, ValueError, OSError, UnicodeError):
        return summary
    if not isinstance(data, dict):
        return summary
    merged = data.get("merged")
    if isinstance(merged, dict):
        summary = {**merged, **summary}
    if (data.get("abstract_only") or data.get("full_text_available") is False
            or data.get("source_type") == "source_abstract"):
        summary["abstract_only"] = True
        summary["source_type"] = "source_abstract"
    return summary


def save_feedback(
    db_path: Path,
    *,
    paper_id: int,
    status: str | None,
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
    has_feedback_content = bool(feedback_content and feedback_content.strip())
    with connect_db(db_path) as connection:
        if not has_feedback_content:
            if status not in {value for value, _ in FEEDBACK_STATUSES}:
                raise ValueError("Only not_interested is supported as a lightweight status")
            connection.execute(
                "INSERT INTO feedback (paper_id, status, notes) VALUES (?, ?, ?)",
                (paper_id, status, notes),
            )
        if has_feedback_content:
            connection.execute(
                "INSERT INTO feedback (paper_id, status, notes) VALUES (?, ?, ?)",
                (paper_id, status or "reviewed", notes),
            )
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
    if ingest_output is None or not gemini_enabled():
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
:root {
  color-scheme: dark;
  --bg: #080d14;
  --bg-soft: #0c1420;
  --surface: #111a26;
  --surface-raised: #151f2d;
  --surface-glow: rgba(56, 189, 248, 0.10);
  --border: #243244;
  --border-strong: #33465f;
  --text: #edf4ff;
  --muted: #94a3b8;
  --muted-strong: #b8c4d6;
  --accent: #38bdf8;
  --accent-strong: #60a5fa;
  --accent-ink: #06121f;
  --success: #34d399;
  --warning: #fbbf24;
  --danger: #fb7185;
  --radius: 10px;
  --radius-sm: 7px;
}
body { margin: 0; font: 13px/1.42 Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: radial-gradient(circle at top left, rgba(56, 189, 248, 0.10), transparent 28rem), linear-gradient(180deg, #0a1019 0%, var(--bg) 34rem); color: var(--text); }
main { max-width: 1180px; margin: 0 auto; padding: 14px; }
.topbar { display: grid; gap: 6px; border-bottom: 1px solid var(--border); padding-bottom: 7px; margin-bottom: 10px; }
.header-main { display: grid; grid-template-columns: minmax(260px, 1fr) auto; gap: 18px; align-items: center; }
.header-controls { display: flex; justify-content: flex-end; }
h1 { margin: 0 0 2px; font-size: 18px; font-weight: 760; letter-spacing: 0; }
.brand-title, .brand-home { display: flex; gap: 8px; align-items: center; }
.brand-home { color: inherit; text-decoration: none; }
.logo-frame { display: block; width: 42px; height: 42px; overflow: hidden; border-radius: 9px; box-shadow: 0 0 0 1px var(--border), 0 12px 24px rgba(0, 0, 0, 0.28); }
.brand-logo { display: block; width: 100%; height: 100%; object-fit: cover; object-position: center; transform: scale(1.22); }
.brand-name { font-weight: 750; }
.page-title { color: var(--muted); font-weight: 650; }
.title-link { color: var(--text); text-decoration: none; }
.title-link:hover { color: #cbeafe; text-decoration: underline; text-decoration-color: rgba(56, 189, 248, 0.55); text-underline-offset: 3px; }
h2 { margin: 0 0 3px; font-size: 15px; font-weight: 720; line-height: 1.25; letter-spacing: 0; }
h3 { margin: 0 0 3px; font-size: 11px; font-weight: 760; color: var(--muted); letter-spacing: 0.02em; text-transform: uppercase; }
p { margin: 0; }
.topbar p, .card-head p { color: var(--muted); font-size: 12px; }
.queue-controls { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; align-items: end; }
.queue-pagination { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 8px; margin: 12px 0; color: var(--muted-strong); }
.queue-pagination a, .page-disabled { display: inline-flex; align-items: center; justify-content: center; width: 36px; height: 36px; }
.page-disabled { opacity: 0.4; }
.primary-nav { display: flex; gap: 16px; flex-wrap: wrap; justify-content: flex-end; align-items: center; }
.control-label { display: grid; gap: 2px; color: var(--muted); font-size: 11px; }
select, button, input { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 4px 8px; background: var(--surface); color: var(--text); font: inherit; min-height: 28px; box-sizing: border-box; }
select:focus-visible, button:focus-visible, input:focus-visible, textarea:focus-visible, .secondary-link:focus-visible, .source-link:focus-visible { outline: 2px solid rgba(56, 189, 248, 0.55); outline-offset: 2px; }
button { cursor: pointer; }
code { font: 12px/1.3 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
.visually-hidden { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
.secondary-link { display: inline-flex; align-items: center; min-height: 28px; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0 8px; color: var(--muted-strong); background: rgba(17, 26, 38, 0.78); text-decoration: none; transition: border-color 120ms ease, color 120ms ease, background 120ms ease; }
.secondary-link.current { font-weight: 700; border-color: rgba(56, 189, 248, 0.55); color: var(--text); background: rgba(56, 189, 248, 0.10); }
.primary-nav .secondary-link { min-height: 24px; border: 0; border-bottom: 2px solid transparent; border-radius: 0; padding: 1px 0 3px; color: var(--muted); background: transparent; font-weight: 680; }
.primary-nav .secondary-link.current { color: var(--text); border-bottom-color: var(--accent); background: transparent; }
.primary-nav .secondary-link:hover { color: var(--text); background: transparent; border-bottom-color: rgba(56, 189, 248, 0.48); }
.source-link { color: var(--accent); text-decoration: none; }
.source-link:hover, .secondary-link:hover, .topic-edit-link:hover { text-decoration: none; border-color: var(--border-strong); color: var(--text); }
.primary-action { display: inline-flex; align-items: center; min-height: 28px; border: 1px solid rgba(96, 165, 250, 0.58); border-radius: var(--radius-sm); padding: 0 10px; color: #f7fbff; background: linear-gradient(180deg, rgba(37, 99, 235, 0.96), rgba(29, 78, 216, 0.96)); font-weight: 740; box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.10); }
.primary-action:hover { border-color: rgba(125, 211, 252, 0.68); background: linear-gradient(180deg, rgba(37, 99, 235, 1), rgba(30, 64, 175, 1)); }
.source-link.primary-action, .source-link.primary-action:visited, .source-link.primary-action:hover { color: #f7fbff; }
.copy-url { display: inline-flex; align-items: center; min-height: 22px; margin-left: 0; padding: 1px 6px; font-size: 11px; }
.metadata-action { display: inline-flex; align-items: center; min-height: 22px; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 1px 6px; color: var(--muted-strong); background: rgba(17, 26, 38, 0.72); font-size: 11px; font-weight: 650; text-decoration: none; }
.pdf-action { border-color: rgba(96, 165, 250, 0.30); color: #bdd7ff; background: rgba(37, 99, 235, 0.10); }
.metadata-action:hover { border-color: var(--border-strong); color: var(--text); background: var(--surface-raised); }
.view-toggle { display: inline-flex; border: 1px solid var(--border); border-radius: var(--radius-sm); overflow: hidden; background: rgba(17, 26, 38, 0.72); }
.view-option { display: inline-flex; justify-content: center; align-items: center; min-width: 30px; min-height: 28px; border: 0; border-right: 1px solid var(--border); border-radius: 0; padding: 0 8px; color: var(--muted-strong); background: transparent; }
.view-option:last-child { border-right: 0; }
.view-option.current { color: var(--text); background: rgba(56, 189, 248, 0.14); }
.view-option:hover { color: var(--text); background: var(--surface-raised); }
.secondary-action { color: var(--muted-strong); background: rgba(17, 26, 38, 0.86); border-color: var(--border); }
.secondary-action:hover, button.secondary:hover, .links a:hover { border-color: var(--border-strong); color: var(--text); background: var(--surface-raised); }
.links a { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 3px 7px; background: rgba(17, 26, 38, 0.78); color: var(--muted-strong); text-decoration: none; }
button.primary { background: linear-gradient(180deg, #7dd3fc, var(--accent)); color: var(--accent-ink); border-color: rgba(56, 189, 248, 0.72); font-weight: 760; }
button.secondary { background: rgba(17, 26, 38, 0.86); color: var(--muted-strong); }
.banner { padding: 7px 9px; border: 1px solid rgba(52, 211, 153, 0.42); background: rgba(52, 211, 153, 0.12); border-radius: var(--radius-sm); margin-bottom: 8px; }
.banner.warning { border-color: rgba(251, 191, 36, 0.48); background: rgba(251, 191, 36, 0.12); }
.cards { display: grid; gap: 8px; }
.paper-card, .empty { background: linear-gradient(180deg, rgba(21, 31, 45, 0.97), rgba(15, 23, 34, 0.98)); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; box-shadow: 0 10px 30px rgba(0, 0, 0, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.035); }
.paper-card:hover { border-color: var(--border-strong); box-shadow: 0 12px 34px rgba(0, 0, 0, 0.22), 0 0 0 1px rgba(56, 189, 248, 0.04), inset 0 1px 0 rgba(255, 255, 255, 0.045); }
.paper-form { display: grid; grid-template-columns: minmax(0, 1fr) 88px; gap: 12px; align-items: start; }
.paper-main { min-width: 0; }
.card-head { display: grid; grid-template-columns: minmax(0, 1fr); gap: 10px; align-items: start; }
.paper-meta { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; margin-top: 5px; color: var(--muted); font-size: 11px; line-height: 1.25; }
.paper-meta .source-id { color: #75859a; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow-wrap: anywhere; }
.action-rail { display: grid; gap: 6px; }
.pulled-date { color: var(--muted); font-size: 11px; line-height: 1.25; margin-left: auto; text-align: right; }
.match-score { text-align: center; border: 1px solid rgba(56, 189, 248, 0.45); border-radius: var(--radius-sm); padding: 6px; background: linear-gradient(180deg, rgba(56, 189, 248, 0.15), rgba(56, 189, 248, 0.055)); box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05); }
.match-score strong { display: block; font-size: 18px; line-height: 1; color: #e0f7ff; }
.match-score span, .user-score span { display: block; color: var(--muted); font-size: 9px; line-height: 1.05; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.04em; }
.score-secondary { background: transparent; }
.score-secondary strong { font-size: 18px; color: #e0f7ff; }
.user-score { text-align: center; border: 1px solid rgba(52, 211, 153, 0.48); border-radius: var(--radius-sm); padding: 6px; background: rgba(52, 211, 153, 0.12); color: #9ff3cf; }
.user-score strong { display: block; font-size: 18px; line-height: 1; }
.feedback-state { color: var(--muted); font-size: 11px; text-align: center; }
.feedback-meta { display: inline-block; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 5px 7px; color: var(--muted); background: rgba(17, 26, 38, 0.74); font-size: 11px; line-height: 1.3; overflow-wrap: anywhere; margin-top: 6px; }
.feedback-tools { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 7px; }
.discussion-prompt { color: #d3e6ff; border-color: rgba(96, 165, 250, 0.34); background: rgba(37, 99, 235, 0.12); }
.tags, .links { display: flex; gap: 5px; flex-wrap: wrap; align-items: center; }
.tag { border: 1px solid var(--border); color: var(--muted); border-radius: 999px; padding: 1px 6px; font-size: 11px; background: rgba(148, 163, 184, 0.07); }
.signals-label { color: #7ea0bd; font-size: 10px; font-weight: 760; letter-spacing: 0.04em; text-transform: uppercase; }
.source-badge { border-radius: 999px; padding: 1px 7px; font-size: 11px; font-weight: 650; border: 1px solid transparent; white-space: nowrap; }
.source-badge::before { margin-right: 4px; }
.source-badge-arxiv { color: #ffd166; background: rgba(251, 191, 36, 0.13); border-color: rgba(251, 191, 36, 0.58); }
.source-badge-arxiv::before { content: "A"; }
.source-badge-openalex { color: #7dd3fc; background: rgba(14, 165, 233, 0.14); border-color: rgba(56, 189, 248, 0.58); }
.source-badge-openalex::before { content: "O"; }
.source-badge-semantic-scholar { color: #d8b4fe; background: rgba(126, 34, 206, 0.16); border-color: rgba(168, 85, 247, 0.58); }
.source-badge-semantic-scholar::before { content: "S"; }
.source-badge-unknown { color: var(--muted); background: rgba(148, 163, 184, 0.07); border-color: var(--border); }
.source-badge-unknown::before { content: "?"; }
.summary-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 8px; margin: 8px 0; }
.summary-grid section { min-width: 0; }
.summary-grid p, .source-summary p, .compact-summary { color: var(--muted-strong); font-size: 12px; }
.source-summary { margin: 8px 0; }
.source-summary p {
  display: -webkit-box;
  -webkit-line-clamp: 4;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.summary-provenance { position: absolute; top: 9px; right: 10px; max-width: 48%; color: #f6e3a4; font-size: 11px; font-weight: 650; line-height: 1.25; text-align: right; }
.compact-summary { margin: 6px 0; line-height: 1.35; }
.links { margin-bottom: 7px; }
.match-rationale { position: relative; margin-top: 7px; border: 1px solid rgba(56, 189, 248, 0.24); border-left-color: rgba(56, 189, 248, 0.72); border-radius: var(--radius-sm); padding: 7px 9px; background: linear-gradient(90deg, rgba(56, 189, 248, 0.105), rgba(56, 189, 248, 0.025)); }
.match-rationale h3 { color: #b9eaff; }
.match-rationale p { color: #d7e8f5; font-size: 12px; margin-bottom: 5px; }
.paper-card.compact { padding: 8px 10px; }
.paper-card.compact .tags, .paper-card.compact .links { margin-top: 5px; }
.submit-state { color: var(--muted); font-size: 11px; line-height: 1.25; }
label { display: grid; gap: 3px; color: var(--muted); font-size: 12px; }
textarea { box-sizing: border-box; width: 100%; min-height: 42px; resize: vertical; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 6px; font: inherit; color: var(--text); background: #0d1521; }
.save-feedback { margin-top: 5px; }
.feedback-editor { margin-top: 7px; }
.feedback-editor > summary { display: inline-flex; align-items: center; min-height: 26px; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 0 8px; color: var(--muted-strong); background: rgba(17, 26, 38, 0.86); cursor: pointer; font-weight: 700; list-style: none; }
.feedback-editor > summary:hover { border-color: var(--border-strong); color: var(--text); }
.feedback-editor > summary::-webkit-details-marker { display: none; }
.feedback-editor[open] > summary { margin-bottom: 6px; }
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
.topic-tabs { display: flex; gap: 7px; flex-wrap: wrap; margin: 8px 0; }
.topic-tab { text-decoration: none; }
.topic-sections { display: grid; gap: 10px; }
.topic-source { border: 1px solid #d8dee4; background: #ffffff; border-radius: 6px; padding: 10px; scroll-margin-top: 10px; }
.topic-list-details { padding: 0; }
.topic-list-details > summary, .topic-inventory-details > summary { display: flex; justify-content: space-between; align-items: center; gap: 10px; cursor: pointer; padding: 8px 10px; font-weight: 650; list-style: none; }
.topic-list-details > summary::-webkit-details-marker, .topic-inventory-details > summary::-webkit-details-marker { display: none; }
.topic-list-details > summary::before, .topic-inventory-details > summary::before { content: "▸"; color: #57606a; font-size: 11px; }
.topic-list-details[open] > summary::before, .topic-inventory-details[open] > summary::before { content: "▾"; }
.topic-list-details > summary span, .topic-inventory-details > summary span { margin-right: auto; }
.topic-list-details > summary small, .topic-inventory-details > summary small { color: #57606a; font-size: 12px; font-weight: 500; }
.topic-source-head { display: flex; justify-content: space-between; gap: 8px; align-items: center; margin-bottom: 6px; }
.topic-source-head h2 { margin: 0; }
.topic-source-head > span, .topic-source > p, .topic-note-text { color: #57606a; font-size: 12px; }
.topic-columns { display: grid; grid-template-columns: minmax(180px, 0.38fr) minmax(0, 1fr); gap: 12px; margin-top: 8px; }
.topic-list { margin: 0; padding-left: 22px; columns: 2; column-gap: 28px; }
.active-topic-list { columns: 1; }
.topic-list li { break-inside: avoid; margin: 0 0 3px; padding-left: 2px; font-size: 12px; }
.topic-list li span { color: #57606a; font-size: 11px; }
.topic-note-text { margin-top: 8px; }
.topic-agent-form { display: grid; grid-template-columns: minmax(260px, 1fr) auto; gap: 7px; align-items: end; }
.topic-agent-form textarea { min-height: 38px; }
.topic-agent-status { grid-column: 1 / -1; margin: 0; color: #57606a; font-size: 12px; }
.topic-conversation { display: grid; gap: 5px; margin-bottom: 8px; }
.topic-turn { display: grid; grid-template-columns: 54px minmax(0, 1fr); gap: 8px; align-items: start; padding: 5px 7px; border: 1px solid #d8dee4; border-radius: 6px; background: #f6f8fa; }
.topic-turn span { color: #57606a; font-size: 11px; font-weight: 700; text-transform: uppercase; }
.topic-turn p { margin: 0; font-size: 12px; overflow-wrap: anywhere; }
.topic-turn-agent { background: #eef6ff; border-color: #b6e3ff; }
.topic-proposal-panel { margin-top: 10px; padding-top: 10px; border-top: 1px solid #d8dee4; }
.topic-proposal-grid { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 5px 10px; margin: 0 0 8px; }
.topic-proposal-grid dt { color: #57606a; font-weight: 650; }
.topic-proposal-grid dd { margin: 0; min-width: 0; }
.topic-proposal-actions { display: flex; gap: 7px; flex-wrap: wrap; align-items: center; }
.topic-edit-form { display: grid; grid-template-columns: minmax(150px, 0.8fr) minmax(260px, 1.4fr) minmax(180px, 0.8fr) 100px 100px 92px auto; gap: 8px; align-items: end; }
.topic-form-actions { display: flex; gap: 6px; align-items: center; justify-content: flex-end; }
.topic-toolbar { display: flex; justify-content: space-between; align-items: end; gap: 10px; margin: 0; padding: 0 10px 8px; color: #57606a; font-size: 12px; }
.topic-toolbar label { max-width: 240px; width: 100%; }
.topic-toolbar input { min-height: 28px; }
.topic-table { display: grid; border-top: 1px solid #d8dee4; background: #ffffff; }
.topic-table-head, .topic-row { display: grid; grid-template-columns: minmax(150px, 0.9fr) minmax(260px, 1.6fr) minmax(140px, 0.7fr) 70px 70px 62px 42px; gap: 8px; align-items: center; }
.topic-table-head { padding: 5px 10px; background: #f6f8fa; color: #57606a; font-size: 10px; font-weight: 650; text-transform: uppercase; }
.topic-row { min-height: 30px; padding: 3px 10px; border-top: 1px solid #d8dee4; }
.topic-read-row:nth-child(odd) { background: #fbfcfd; }
.topic-preview-row { outline: 1px solid #bf8700; outline-offset: -1px; background: #fff8c5; }
.topic-pending-badge { display: inline-flex; margin-left: 5px; padding: 0 5px; border: 1px solid #bf8700; border-radius: 999px; color: #9a6700; font-size: 10px; font-weight: 700; text-transform: uppercase; white-space: nowrap; }
.topic-preview-note { color: #9a6700; font-size: 11px; font-weight: 650; white-space: nowrap; }
.topic-cell { min-width: 0; font-size: 12px; }
.topic-label strong, .topic-query { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.topic-label strong { font-size: 12px; }
.topic-query { color: #3f4650; }
.topic-sources { display: flex; gap: 4px; flex-wrap: nowrap; overflow: hidden; }
.topic-actions { display: flex; gap: 5px; align-items: center; justify-content: flex-end; }
.topic-edit-link { color: #0969da; font-size: 12px; text-decoration: none; }
.topic-edit-link:hover { text-decoration: underline; }
.topic-toggle-form { margin: 0; }
.topic-pill, .topic-status { display: inline-flex; align-items: center; min-height: 20px; border-radius: 999px; padding: 0 7px; border: 1px solid #d8dee4; font-size: 11px; font-weight: 650; text-transform: lowercase; white-space: nowrap; }
.topic-toggle-button { cursor: pointer; min-height: 20px; padding: 0 8px; }
.topic-inventory-details { margin-top: 10px; border: 1px solid #d8dee4; background: #ffffff; border-radius: 6px; padding: 0; }
.topic-inventory-details .topic-sections { padding: 0 10px 10px; }
.cadence-daily { color: #0969da; background: #ddf4ff; border-color: #54aeef; }
.cadence-weekly { color: #8250df; background: #fbefff; border-color: #d8b9ff; }
.cadence-manual { color: #57606a; background: #f6f8fa; border-color: #d8dee4; }
.priority-high { color: #a40e26; background: #ffebe9; border-color: #ff8182; }
.priority-normal { color: #1a7f37; background: #dafbe1; border-color: #4ac26b; }
.priority-low { color: #57606a; background: #f6f8fa; border-color: #d8dee4; }
.status-enabled { color: #116329; background: #dafbe1; border-color: #4ac26b; }
.status-disabled { color: #6e7781; background: #f6f8fa; border-color: #d8dee4; }
.source-checks { display: flex; gap: 6px; flex-wrap: wrap; margin: 0; padding: 0; border: 0; min-width: 0; }
.source-checks legend { color: #57606a; font-size: 12px; padding: 0; margin-bottom: 3px; }
.inline-check { display: inline-flex; grid-auto-flow: column; gap: 4px; align-items: center; color: #57606a; font-size: 12px; }
.inline-check input { min-height: auto; }
@media (prefers-color-scheme: dark) {
  body { background: radial-gradient(circle at top left, rgba(56, 189, 248, 0.10), transparent 28rem), linear-gradient(180deg, #0a1019 0%, var(--bg) 34rem); color: var(--text); }
  .topbar { border-color: var(--border); }
  .topbar p, .page-title, .card-head p, h3, .feedback-state, .feedback-meta, .submit-state, .tag, label, .compact-summary, .match-score span, .user-score span, .health-card h2, .health-card span, .graph-head span, .chart-legend, .health-table th, .health-kv dt, .topic-source-head > span, .topic-source > p, .topic-note-text { color: var(--muted); }
  .summary-grid p, .source-summary p, .match-rationale p { color: var(--muted-strong); }
  .source-link { color: var(--accent); }
  .links a, button, select, input, .secondary-link, textarea, .feedback-meta, .feedback-editor > summary, .health-card, .health-graph, .health-table table, .health-kv, .topic-source, .topic-inventory-details { background: var(--surface); color: var(--text); border-color: var(--border); }
  .metadata-action { background: rgba(17, 26, 38, 0.72); color: var(--muted-strong); border-color: var(--border); }
  .pdf-action { border-color: rgba(96, 165, 250, 0.30); color: #bdd7ff; background: rgba(37, 99, 235, 0.10); }
  .discussion-prompt { color: #d3e6ff; border-color: rgba(96, 165, 250, 0.34); background: rgba(37, 99, 235, 0.12); }
  .paper-card, .empty { background: linear-gradient(180deg, rgba(21, 31, 45, 0.97), rgba(15, 23, 34, 0.98)); border-color: var(--border); }
  button.secondary { background: rgba(17, 26, 38, 0.86); }
  .score-secondary { background: transparent; }
  .user-score { background: rgba(52, 211, 153, 0.12); color: #9ff3cf; border-color: rgba(52, 211, 153, 0.48); }
  .source-badge-arxiv { color: #ffd166; background: rgba(251, 191, 36, 0.13); border-color: rgba(251, 191, 36, 0.58); }
  .source-badge-openalex { color: #7dd3fc; background: rgba(14, 165, 233, 0.14); border-color: rgba(56, 189, 248, 0.58); }
  .source-badge-semantic-scholar { color: #d8b4fe; background: rgba(126, 34, 206, 0.16); border-color: rgba(168, 85, 247, 0.58); }
  .source-badge-unknown { color: var(--muted); background: rgba(148, 163, 184, 0.07); border-color: var(--border); }
  .chart-axis { stroke: #8b949e; }
  .chart-grid { stroke: #30363d; opacity: 1; }
  .chart-label { fill: #8b949e; }
  .chart-point { stroke: #161b22; }
  .banner { background: #0f2a1a; border-color: #238636; }
  .banner.warning { background: #2d2300; border-color: #9e6a03; }
  .health-warnings { background: #2d2300; border-color: #9e6a03; }
  .health-table th { background: #21262d; }
  .health-table th, .health-table td { border-color: #30363d; }
  .topic-table, .topic-table-head, .topic-row { border-color: #30363d; }
  .topic-proposal-panel { border-color: #30363d; }
  .topic-turn { background: #21262d; border-color: #30363d; }
  .topic-turn-agent { background: #0d263f; border-color: #1f6feb; }
  .topic-table, .topic-read-row:nth-child(odd) { background: #161b22; }
  .topic-preview-row { background: #2d2300; outline-color: #9e6a03; }
  .topic-pending-badge { color: #f2cc60; border-color: #9e6a03; }
  .topic-preview-note { color: #f2cc60; }
  .topic-table-head { background: #21262d; }
  .topic-query { color: #c9d1d9; }
  .topic-edit-link { color: #58a6ff; }
  .cadence-daily { color: #79c0ff; background: #0d263f; border-color: #1f6feb; }
  .cadence-weekly { color: #d8b9ff; background: #2a163f; border-color: #8250df; }
  .cadence-manual, .priority-low, .status-disabled { color: #8b949e; background: #21262d; border-color: #30363d; }
  .priority-high { color: #ffb3ba; background: #3b1016; border-color: #f85149; }
  .priority-normal, .status-enabled { color: #7ee787; background: #0f2a1a; border-color: #238636; }
}
@media (max-width: 720px) {
  main { padding: 10px; }
  .header-main, .paper-form { grid-template-columns: 1fr; }
  .header-controls { justify-content: flex-start; }
  .queue-controls, .primary-nav { justify-content: flex-start; }
  .summary-grid { grid-template-columns: 1fr; }
  .pulled-date { margin-left: 0; text-align: left; }
  .feedback-state { text-align: left; grid-column: 1 / -1; }
  .health-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .health-graphs { grid-template-columns: 1fr; }
  .health-graph-wide { grid-column: auto; }
  .health-kv { grid-template-columns: 1fr; }
  .topic-columns { grid-template-columns: 1fr; }
  .topic-list { columns: 1; }
  .topic-agent-form, .topic-edit-form, .topic-table-head, .topic-row, .topic-proposal-grid { grid-template-columns: 1fr; }
  .topic-table-head { display: none; }
  .topic-toolbar, .topic-list-details > summary, .topic-inventory-details > summary { align-items: flex-start; flex-direction: column; }
  .topic-actions, .topic-form-actions { justify-content: flex-start; }
  .topic-query, .topic-label strong { white-space: normal; }
}
"""
