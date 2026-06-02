"""
RSS/Atom feed ingestion.

Fetches entries from RSS feeds, extracts title/content/URL,
and returns them as standardized article dicts.
"""

import hashlib
import feedparser
import requests
from bs4 import BeautifulSoup


def fetch_rss_source(source: dict) -> list:
    """
    Fetch articles from an RSS feed source.

    Args:
        source: dict with keys 'name', 'url', 'topics', and optional 'max_items'

    Returns:
        List of article dicts with keys: id, source_name, title, url, raw_content
    """
    articles = []
    max_items = source.get("max_items", 10)

    try:
        feed = feedparser.parse(source["url"])
    except Exception as e:
        print(f"  [!] Failed to fetch RSS from {source['name']}: {e}")
        return articles

    for entry in feed.entries[:max_items]:
        # Extract content - RSS feeds store it in different fields
        content = ""
        if hasattr(entry, "content") and entry.content:
            content = entry.content[0].get("value", "")
        elif hasattr(entry, "summary"):
            content = entry.summary or ""
        elif hasattr(entry, "description"):
            content = entry.description or ""

        # Strip HTML tags from content for cleaner LLM processing
        if content:
            soup = BeautifulSoup(content, "html.parser")
            content = soup.get_text(separator="\n", strip=True)

        title = entry.get("title", "Untitled")
        url = entry.get("link", "")

        # Create a stable ID from source + URL so we don't re-ingest
        article_id = hashlib.sha256(f"{source['name']}:{url}".encode()).hexdigest()[:16]

        articles.append({
            "id": article_id,
            "source_name": source["name"],
            "title": title,
            "url": url,
            "raw_content": content[:10000],  # Cap at ~10k chars
        })

    return articles
