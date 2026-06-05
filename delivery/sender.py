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

TEMPLATE_DIR = os.path.dirname(__file__)


def _parse_json_field(value, fallback=None):
    if fallback is None:
        fallback = []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return fallback
    return fallback


def render_email(digest: dict, callbacks: list, config: dict) -> str:
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("template.html")

    items = []
    for article in digest["articles"]:
        tags = _parse_json_field(article.get("tags"))
        further_reading = _parse_json_field(article.get("further_reading"))

        historical = article.get("historical_analog")
        if isinstance(historical, str):
            try:
                historical = json.loads(historical)
            except Exception:
                historical = None

        items.append({
            "title": article.get("title", "Untitled"),
            "url": article.get("url", "#"),
            "source_name": article.get("source_name", ""),
            "insight": article.get("insight") or article.get("summary", ""),
            "so_what": article.get("so_what", ""),
            "contrarian_angle": article.get("contrarian_angle", ""),
            "tags": tags,
            "think_about_this": article.get("think_about_this", ""),
            "further_reading": further_reading,
            "expert_analyses": article.get("expert_analyses", []),
            "historical_analog": historical,
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

    plain_text = f"Daily Digest — {theme}\n\n"
    for a in digest.get("articles", []):
        insight = a.get("insight") or a.get("summary", "")
        plain_text += f"• {a.get('title', '')}\n  {insight}\n  {a.get('url', '')}\n\n"
    msg.attach(MIMEText(plain_text, "plain"))
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
