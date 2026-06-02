"""
Reply parser: monitors the bot's Gmail inbox for replies to digest emails.

Recognized commands (case-insensitive):
- "save item 2" / "bookmark 3"         → saves that article for resurfacing
- "more item 2" / "less item 2"         → positive/negative feedback tied to actual article_id
- "more like this" / "👍"               → general positive signal
- "less of this" / "👎"                 → general negative signal
- "less [topic]" / "more [topic]"       → adjusts topic weight preference
- "add https://..."                     → queues a URL for future digests
- "concept: [name] | [explanation] | [topic]"  → queues a concept for processing
"""

import os
import re
import imaplib
import email
from data.db import (
    save_feedback, add_to_manual_queue, add_to_concept_queue,
    get_most_recent_digest_articles
)


def _get_recent_article_map() -> dict:
    """Returns {position: article_id} for the most recent digest."""
    return {pos: aid for pos, aid in get_most_recent_digest_articles()}


def check_replies(config: dict) -> list:
    gmail_addr = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    actions = []

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_addr, gmail_pass)
        mail.select("inbox")

        status, messages = mail.search(None, '(UNSEEN SUBJECT "Re: Daily Digest")')
        if status != "OK" or not messages[0]:
            mail.logout()
            return actions

        for msg_id in messages[0].split():
            status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)
            body = _extract_body(msg)
            if not body:
                continue

            parsed = _parse_commands(body, config)
            actions.extend(parsed)

        mail.logout()

    except Exception as e:
        print(f"  [!] Failed to check replies: {e}")

    return actions


def _extract_body(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    continue
    else:
        try:
            return msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            return ""
    return ""


def _parse_commands(body: str, config: dict) -> list:
    actions = []
    lines = body.strip().split("\n")
    feedback_config = config.get("feedback", {})
    commands = feedback_config.get("commands", {})

    save_keywords = commands.get("save", ["save", "bookmark", "later"])
    positive_keywords = commands.get("positive", ["more like this", "great", "good", "👍"])
    negative_keywords = commands.get("negative", ["less of this", "skip", "boring", "👎"])
    add_keywords = commands.get("add_url", ["add", "queue", "read this"])

    article_map = None  # lazy-loaded only if needed

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

        # URL queuing
        url_match = re.search(r'(https?://\S+)', line)
        if url_match and any(kw in line_lower for kw in add_keywords):
            url = url_match.group(1)
            add_to_manual_queue(url)
            actions.append({"type": "add_url", "url": url})
            print(f"    → Queued URL: {url}")
            continue

        if url_match and len(line.strip().split()) <= 3:
            url = url_match.group(1)
            add_to_manual_queue(url)
            actions.append({"type": "add_url", "url": url})
            print(f"    → Queued URL: {url}")
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
