"""
Reply parser: monitors the bot's Gmail inbox for replies to digest emails.

Recognized commands (case-insensitive):
- "save item 2" / "bookmark 3" → saves that article for resurfacing
- "more like this" / "👍" → positive feedback signal
- "less of this" / "👎" → negative feedback signal
- "less [topic]" → reduces weight for a topic
- "more [topic]" → increases weight for a topic
- "add https://..." → queues a URL for future digests
"""

import os
import re
import imaplib
import email
from email.header import decode_header
from datetime import datetime
from data.db import save_feedback, add_to_manual_queue


def check_replies(config: dict) -> list:
    """
    Check the bot's Gmail inbox for replies to digest emails.

    Returns a list of parsed actions taken.
    """
    gmail_addr = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    actions = []

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_addr, gmail_pass)
        mail.select("inbox")

        # Search for unread replies to digest emails
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

            # Extract body text
            body = _extract_body(msg)
            if not body:
                continue

            # Parse commands from the body
            parsed = _parse_commands(body, config)
            actions.extend(parsed)

            # Mark as read (already done by fetching with UNSEEN filter)

        mail.logout()

    except Exception as e:
        print(f"  [!] Failed to check replies: {e}")

    return actions


def _extract_body(msg) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
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
    """
    Parse recognized commands from reply body text.

    Returns list of action dicts: {type, details}
    """
    actions = []
    lines = body.strip().split("\n")
    feedback_config = config.get("feedback", {})
    commands = feedback_config.get("commands", {})

    save_keywords = commands.get("save", ["save", "bookmark", "later"])
    positive_keywords = commands.get("positive", ["more", "like this", "great", "👍"])
    negative_keywords = commands.get("negative", ["less", "skip", "boring", "👎"])
    add_keywords = commands.get("add_url", ["add", "queue", "read this"])

    for line in lines:
        line_lower = line.strip().lower()
        if not line_lower:
            continue

        # Skip email reply artifacts (quoted text, signatures)
        if line_lower.startswith(">") or line_lower.startswith("--"):
            break
        if "wrote:" in line_lower:
            break

        # Check for URL additions
        url_match = re.search(r'(https?://\S+)', line)
        if url_match and any(kw in line_lower for kw in add_keywords):
            url = url_match.group(1)
            add_to_manual_queue(url)
            actions.append({"type": "add_url", "url": url})
            print(f"    → Queued URL: {url}")
            continue

        # Even without "add" keyword, a bare URL is likely an addition
        if url_match and len(line.strip().split()) <= 3:
            url = url_match.group(1)
            add_to_manual_queue(url)
            actions.append({"type": "add_url", "url": url})
            print(f"    → Queued URL: {url}")
            continue

        # Check for save commands ("save item 2", "bookmark 3")
        save_match = re.match(r'(?:save|bookmark)\s*(?:item\s*)?(\d+)', line_lower)
        if save_match:
            item_num = int(save_match.group(1))
            save_feedback(article_id=f"item_{item_num}", signal="save",
                         details=f"Saved item {item_num}")
            actions.append({"type": "save", "item": item_num})
            print(f"    → Saved item {item_num}")
            continue

        # Check for topic adjustments ("less crypto", "more GTM")
        topic_match = re.match(r'(more|less)\s+(.+)', line_lower)
        if topic_match:
            direction = topic_match.group(1)
            topic = topic_match.group(2).strip()
            signal = "topic_adjust"
            details = f"{direction} {topic}"
            save_feedback(article_id=None, signal=signal, details=details)
            actions.append({"type": "topic_adjust", "direction": direction,
                          "topic": topic})
            print(f"    → Topic adjustment: {details}")
            continue

        # Check for general positive/negative signals
        if any(kw in line_lower for kw in positive_keywords):
            save_feedback(article_id=None, signal="positive", details=line.strip())
            actions.append({"type": "positive", "text": line.strip()})
            print(f"    → Positive feedback")
            continue

        if any(kw in line_lower for kw in negative_keywords):
            save_feedback(article_id=None, signal="negative", details=line.strip())
            actions.append({"type": "negative", "text": line.strip()})
            print(f"    → Negative feedback")
            continue

    return actions
