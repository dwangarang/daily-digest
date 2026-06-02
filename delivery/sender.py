"""
Email delivery via Gmail SMTP.

Renders the Jinja2 template and sends the digest email.
"""

import os
import smtplib
import json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import date
from jinja2 import Environment, FileSystemLoader


# Template directory
TEMPLATE_DIR = os.path.dirname(__file__)


def render_email(digest: dict, callbacks: list, config: dict) -> str:
    """
    Render the digest email from template + data.

    Args:
        digest: dict with theme, theme_description, articles
        callbacks: list of callback question dicts
        config: full config dict

    Returns:
        Rendered HTML string
    """
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("template.html")

    # Prepare items with parsed JSON fields
    items = []
    for article in digest["articles"]:
        tags = article.get("tags", "[]")
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except json.JSONDecodeError:
                tags = []

        related = article.get("related_search_terms", [])
        if isinstance(related, str):
            try:
                related = json.loads(related)
            except json.JSONDecodeError:
                related = []

        items.append({
            "title": article.get("title", "Untitled"),
            "url": article.get("url", "#"),
            "source_name": article.get("source_name", ""),
            "summary": article.get("summary", ""),
            "tags": tags,
            "think_about_this": article.get("think_about_this", ""),
            "related_search_terms": related,
        })

    html = template.render(
        date=date.today().strftime("%B %d, %Y"),
        theme=digest.get("theme", "Today's Reads"),
        theme_description=digest.get("theme_description", ""),
        item_count=len(items),
        items=items,
        callbacks=callbacks,
        bot_email=os.environ.get("GMAIL_ADDRESS", ""),
    )

    return html


def send_email(html: str, digest: dict):
    """Send the rendered digest email via Gmail SMTP."""
    gmail_addr = os.environ["GMAIL_ADDRESS"]
    gmail_pass = os.environ["GMAIL_APP_PASSWORD"]
    recipient = os.environ["RECIPIENT_EMAIL"]

    theme = digest.get("theme", "Today's Reads")
    item_count = len(digest.get("articles", []))
    subject = f"🧠 {date.today().strftime('%b %d')} — {theme} ({item_count} items)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Daily Digest <{gmail_addr}>"
    msg["To"] = recipient

    # Plain text fallback
    plain_text = f"Daily Digest — {theme}\n\n"
    for a in digest.get("articles", []):
        plain_text += f"• {a.get('title', '')}\n  {a.get('summary', '')}\n  {a.get('url', '')}\n\n"
    msg.attach(MIMEText(plain_text, "plain"))

    # HTML version
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(gmail_addr, gmail_pass)
            server.send_message(msg)
        print(f"  [✓] Digest sent to {recipient}")
        return True
    except Exception as e:
        print(f"  [!] Failed to send email: {e}")
        return False
