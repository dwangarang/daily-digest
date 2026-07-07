"""
Reply parser: monitors the bot's Gmail inbox for replies from the reader.

Matching is by sender (RECIPIENT_EMAIL), not subject, so forwards and
attachment-only emails work too. Messages are fetched with BODY.PEEK and only
marked read after successful processing — a failed parse stays unread and is
retried on the next run instead of being silently consumed.

Recognized commands (case-insensitive):
- "save item 2" / "bookmark 3"          → saves that article for resurfacing
- "more item 2"                          → 👍: feedback + enrolls the item in the recall queue
- "less item 2"                          → 👎: negative feedback tied to article_id
- "explore item 2"                       → emails back an Opus deep-dive prompt
- "recall 1 got it" / "recall 1 missed"  → grades a resurfaced recall card
- "stop recall 1"                        → retires a recall card permanently
- "more like this" / "👍"                → general positive signal
- "less of this" / "👎"                  → general negative signal
- "less [topic]" / "more [topic]"        → adjusts topic weight preference
- "add https://..." or a bare URL        → fetches + processes it now, prioritized
- "concept: [name] | [explanation] | [topic]"  → queues a concept for processing

Attachments (multi-modal input, all prioritized for the next digest):
- PDF        → text extracted via pdfplumber
- image      → content extracted via Claude vision
- .txt/.md   → decoded directly
"""

import os
import io
import re
import base64
import hashlib
import imaplib
import email
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from anthropic import Anthropic
from data.db import (
    save_feedback, add_to_manual_queue, add_to_concept_queue,
    get_most_recent_digest_articles, get_latest_digest_callbacks,
    get_article, article_exists, save_article, set_article_priority,
    save_processed_result,
)
from processing.repetition import enroll_article, grade_card, drop_card

IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

client = None


def _get_client():
    global client
    if client is None:
        client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    return client


def _get_recent_article_map() -> dict:
    """Returns {position: article_id} for the most recent digest."""
    return {pos: aid for pos, aid in get_most_recent_digest_articles()}


def check_replies(config: dict) -> list:
    gmail_addr = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]
    actions = []

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_addr, gmail_pass)
        mail.select("inbox")

        # Match on sender, not subject: catches replies, forwards, and
        # attachment-only emails alike. Only the reader can drive the bot.
        status, messages = mail.search(None, f'(UNSEEN FROM "{recipient}")')
        if status != "OK" or not messages[0]:
            mail.logout()
            return actions

        for msg_id in messages[0].split():
            # BODY.PEEK does not set \Seen — we mark it explicitly only after
            # the message has been fully processed.
            status, msg_data = mail.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK":
                continue

            try:
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                body = _extract_body(msg)
                if body:
                    actions.extend(_parse_commands(body, config))

                actions.extend(_process_attachments(msg, config))

                mail.store(msg_id, "+FLAGS", "\\Seen")
            except Exception as e:
                # Leave unread so the next run retries instead of losing it.
                print(f"  [!] Failed to process reply {msg_id}: {e} — left unread for retry")

        mail.logout()

    except Exception as e:
        print(f"  [!] Failed to check replies: {e}")

    return actions


def _extract_body(msg) -> str:
    """Prefer text/plain; fall back to de-tagged text/html for HTML-only clients."""
    html_fallback = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_filename():
                continue  # attachments are handled separately
            ctype = part.get_content_type()
            if ctype == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    continue
            if ctype == "text/html" and not html_fallback:
                try:
                    html_fallback = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
            if msg.get_content_type() == "text/html":
                html_fallback = payload
            else:
                return payload
        except Exception:
            return ""

    if html_fallback:
        text = re.sub(r"<(br|/p|/div)[^>]*>", "\n", html_fallback, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return text
    return ""


# ---- Multi-modal attachment intake ----

def _process_attachments(msg, config: dict) -> list:
    actions = []
    if not msg.is_multipart():
        return actions

    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue

        try:
            payload = part.get_payload(decode=True)
            if not payload:
                continue

            ctype = part.get_content_type()
            suffix = os.path.splitext(filename)[1].lower()
            content = None
            kind = None

            if ctype == "application/pdf" or suffix == ".pdf":
                content = _extract_pdf_text(payload)
                kind = "pdf"
            elif ctype in IMAGE_MEDIA_TYPES:
                content = _extract_image_content(payload, ctype)
                kind = "image"
            elif ctype.startswith("text/") or suffix in (".txt", ".md"):
                content = payload.decode("utf-8", errors="ignore")
                kind = "text"
            else:
                print(f"    [!] Unsupported attachment type {ctype}: {filename}")
                continue

            if not content or not content.strip():
                print(f"    [!] No content extracted from attachment: {filename}")
                continue

            article_id = ingest_priority_content(
                title=os.path.splitext(filename)[0].replace("_", " ").replace("-", " "),
                content=content,
                url="",
                source_label=f"[Manual] Attachment ({kind})",
                config=config,
                dedupe_key=f"attachment:{filename}:{len(payload)}",
            )
            if article_id:
                actions.append({"type": "attachment", "kind": kind,
                                "filename": filename, "article_id": article_id})
        except Exception as e:
            print(f"    [!] Failed to ingest attachment {filename}: {e}")

    return actions


def _extract_pdf_text(payload: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _extract_image_content(payload: bytes, media_type: str) -> str:
    """Extract substantive content from an image via Claude vision."""
    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
    response = _get_client().messages.create(
        model=model,
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(payload).decode("ascii"),
                }},
                {"type": "text", "text": (
                    "The reader emailed this image to their learning digest to be "
                    "analyzed later. Extract its substantive content as text: "
                    "transcribe any text/article/slide verbatim; for charts or "
                    "diagrams, describe the data, axes, and the relationship shown, "
                    "including specific numbers. Return only the extracted content."
                )},
            ],
        }],
    )
    return response.content[0].text.strip()


def ingest_priority_content(title: str, content: str, url: str, source_label: str,
                            config: dict, dedupe_key: str) -> str | None:
    """
    Save reader-submitted content as a prioritized article and process it
    immediately (synchronously), so it is eligible for today's digest.
    """
    from processing.summarizer import process_article

    article_id = hashlib.sha256(dedupe_key.encode()).hexdigest()[:16]
    if article_exists(article_id):
        print(f"    → Already ingested: {title[:50]}")
        return None

    article = {
        "id": article_id,
        "source_name": source_label,
        "title": title[:200],
        "url": url,
        "raw_content": content[:15000],
    }
    save_article(article)
    set_article_priority(article_id, 1)

    print(f"    → Processing prioritized submission: {title[:50]}")
    result = process_article(article, config)
    if result:
        save_processed_result(article_id, result)
    else:
        # Left unprocessed — the regular process stage picks it up next run,
        # and the priority flag survives.
        print(f"    [!] Immediate processing failed for {title[:50]}; queued for next run")
    return article_id


# ---- Command parsing ----

def _parse_commands(body: str, config: dict) -> list:
    actions = []
    lines = body.strip().split("\n")
    feedback_config = config.get("feedback", {})
    commands = feedback_config.get("commands", {})

    positive_keywords = commands.get("positive", ["more like this", "great", "good", "👍"])
    negative_keywords = commands.get("negative", ["less of this", "skip", "boring", "👎"])
    add_keywords = commands.get("add_url", ["add", "queue", "read this"])

    article_map = None   # lazy-loaded only if needed
    callback_map = None  # {position: repetition_id} for the last digest's recall cards

    for line in lines:
        line_lower = line.strip().lower()
        if not line_lower:
            continue
        if line_lower.startswith(">") or line_lower.startswith("--"):
            break
        if "wrote:" in line_lower:
            break

        # concept: [name] | [explanation] | [topic]
        if line_lower.startswith("concept:"):
            parts = line[8:].split("|")
            if len(parts) >= 2:
                name = parts[0].strip()
                explanation = parts[1].strip()
                topic = parts[2].strip() if len(parts) >= 3 else "General Interest"
                add_to_concept_queue(name, explanation, topic)
                actions.append({"type": "concept", "name": name})
                print(f"    → Queued concept: {name}")
            continue

        # Recall card grading — must be parsed before the more/less handlers,
        # since "stop recall 1" would otherwise match the topic-adjust regex.
        retire_match = re.match(r'(?:stop|drop|less)\s+recall\s*(\d+)', line_lower) \
            or re.match(r'recall\s*(\d+)\s*(?:stop|drop|👎)', line_lower)
        if retire_match:
            pos = int(retire_match.group(1))
            if callback_map is None:
                callback_map = get_latest_digest_callbacks()
            rep_id = callback_map.get(pos)
            if rep_id:
                drop_card(rep_id)
                actions.append({"type": "recall_retired", "position": pos})
                print(f"    → Retired recall card R{pos}")
            continue

        grade_match = re.match(r'recall\s*(\d+)\s*(got it|✓|right|missed|✗|wrong|forgot)', line_lower)
        if grade_match:
            pos = int(grade_match.group(1))
            passed = grade_match.group(2) in ("got it", "✓", "right")
            if callback_map is None:
                callback_map = get_latest_digest_callbacks()
            rep_id = callback_map.get(pos)
            if rep_id:
                grade_card(rep_id, passed, config)
                actions.append({"type": "recall_graded", "position": pos, "passed": passed})
                print(f"    → Recall card R{pos}: {'passed' if passed else 'missed — interval reset'}")
            continue

        # URL queuing — fetched and processed immediately, prioritized
        url_match = re.search(r'(https?://\S+)', line)
        if url_match and (any(kw in line_lower for kw in add_keywords)
                          or len(line.strip().split()) <= 3):
            url = url_match.group(1).rstrip(">).,")
            _ingest_url(url, config)
            actions.append({"type": "add_url", "url": url})
            continue

        # explore item N → email back an Opus deep-dive prompt
        explore_match = re.match(r'explore\s+item\s*(\d+)', line_lower)
        if explore_match:
            item_num = int(explore_match.group(1))
            if article_map is None:
                article_map = _get_recent_article_map()
            article_id = article_map.get(item_num)
            if article_id:
                _send_explore_prompt(article_id, item_num, config)
                actions.append({"type": "explore", "item": item_num})
            continue

        # more item N / less item N — tied to actual article_id
        item_feedback_match = re.match(r'(more|less)\s+item\s*(\d+)', line_lower)
        if item_feedback_match:
            direction = item_feedback_match.group(1)
            item_num = int(item_feedback_match.group(2))
            if article_map is None:
                article_map = _get_recent_article_map()
            article_id = article_map.get(item_num)
            signal = "positive" if direction == "more" else "negative"
            save_feedback(article_id=article_id, signal=signal,
                          details=f"{direction} item {item_num}")
            actions.append({"type": signal, "item": item_num, "article_id": article_id})
            print(f"    → {signal.capitalize()} feedback on item {item_num} ({article_id})")

            # 👍 is the opt-in signal for spaced repetition: enroll the concept.
            if direction == "more" and article_id:
                enroll_article(article_id, config)
            continue

        # save item N / bookmark N
        save_match = re.match(r'(?:save|bookmark)\s*(?:item\s*)?(\d+)', line_lower)
        if save_match:
            item_num = int(save_match.group(1))
            if article_map is None:
                article_map = _get_recent_article_map()
            article_id = article_map.get(item_num, f"item_{item_num}")
            save_feedback(article_id=article_id, signal="save",
                          details=f"Saved item {item_num}")
            actions.append({"type": "save", "item": item_num, "article_id": article_id})
            print(f"    → Saved item {item_num} ({article_id})")
            continue

        # more [topic] / less [topic] — topic weight adjustment
        topic_match = re.match(r'(more|less)\s+(.+)', line_lower)
        if topic_match:
            direction = topic_match.group(1)
            topic = topic_match.group(2).strip()
            save_feedback(article_id=None, signal="topic_adjust",
                          details=f"{direction} {topic}")
            actions.append({"type": "topic_adjust", "direction": direction, "topic": topic})
            print(f"    → Topic adjustment: {direction} {topic}")
            continue

        # General positive/negative
        if any(kw in line_lower for kw in positive_keywords):
            save_feedback(article_id=None, signal="positive", details=line.strip())
            actions.append({"type": "positive", "text": line.strip()})
            print("    → Positive feedback")
            continue

        if any(kw in line_lower for kw in negative_keywords):
            save_feedback(article_id=None, signal="negative", details=line.strip())
            actions.append({"type": "negative", "text": line.strip()})
            print("    → Negative feedback")
            continue

    return actions


def _ingest_url(url: str, config: dict):
    """Fetch a reader-submitted URL and process it now, prioritized.
    Falls back to the manual queue if the fetch fails."""
    from sources.scraper import fetch_article_with_title

    title, content = fetch_article_with_title(url)
    if content:
        ingest_priority_content(
            title=title or url, content=content, url=url,
            source_label="[Manual] Reader link",
            config=config,
            dedupe_key=f"manual:{url}",
        )
    else:
        add_to_manual_queue(url)
        print(f"    → Fetch failed; queued URL for retry: {url}")


def _send_explore_prompt(article_id: str, item_num: int, config: dict):
    """Generate an Opus deep-dive prompt and email it back to the reader."""
    from processing.analyst import generate_expert_analyses, build_opus_prompt

    article = get_article(article_id)
    if not article:
        print(f"  [!] Could not find article {article_id} for explore prompt")
        return

    expert_analyses = generate_expert_analyses(article, config)
    opus_prompt = build_opus_prompt(article, expert_analyses, config)

    gmail_addr = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    title = article.get("title", f"Item {item_num}")[:60]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Opus prompt — {title}"
    msg["From"] = f"Daily Digest <{gmail_addr}>"
    msg["To"] = recipient

    body = f"""Opus deep-dive prompt for item {item_num}: {title}

Copy everything between the lines and paste into claude.ai → set model to Claude Opus 4 → send.
Your Pro subscription covers it — zero API cost.

{'━' * 60}

{opus_prompt}

{'━' * 60}

After Opus responds, try asking it to "steelman the contrarian angle" or "apply [framework] more rigorously" to keep going.
"""
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_addr, gmail_pass)
            server.send_message(msg)
        print(f"  → Opus prompt emailed for item {item_num}")
    except Exception as e:
        print(f"  [!] Failed to send explore prompt: {e}")
