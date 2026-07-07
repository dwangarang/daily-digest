"""
Web scraping for sources without RSS feeds.

Handles two modes:
- "scrape": Fetch a single page and extract article text
- "evergreen": Fetch an index page listing many articles, extract all links,
  then serve individual articles over time
"""

import hashlib
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin


def fetch_scrape_source(source: dict) -> list:
    """Fetch and extract content from a single web page."""
    try:
        resp = requests.get(source["url"], timeout=15,
                          headers={"User-Agent": "DailyDigestBot/1.0"})
        resp.raise_for_status()
    except Exception as e:
        print(f"  [!] Failed to scrape {source['name']}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove nav, footer, script, style elements
    for tag in soup(["nav", "footer", "script", "style", "header", "aside"]):
        tag.decompose()

    # Try to find the main article content
    article = soup.find("article") or soup.find("main") or soup.find("body")
    text = article.get_text(separator="\n", strip=True) if article else ""

    title = ""
    if soup.title:
        title = soup.title.string or ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(strip=True)

    article_id = hashlib.sha256(
        f"{source['name']}:{source['url']}".encode()
    ).hexdigest()[:16]

    return [{
        "id": article_id,
        "source_name": source["name"],
        "title": title or "Untitled",
        "url": source["url"],
        "raw_content": text[:10000],
    }]


def fetch_evergreen_source(source: dict) -> list:
    """
    Fetch an index page and extract individual article links.

    Returns articles with URLs but minimal content — the summarizer
    will fetch full content for each article when it processes them.
    """
    try:
        # paulgraham.com has an SSL cert issue with Python's verifier on macOS
        verify = "paulgraham.com" not in source["url"]
        resp = requests.get(source["url"], timeout=15,
                            headers={"User-Agent": "DailyDigestBot/1.0"},
                            verify=verify)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [!] Failed to fetch evergreen index {source['name']}: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    articles = []

    # Find all links that look like article links
    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)

        # Skip navigation links, anchors, and very short text
        if not text or len(text) < 5:
            continue
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("javascript:"):
            continue

        full_url = urljoin(source["url"], href)
        article_id = hashlib.sha256(
            f"{source['name']}:{full_url}".encode()
        ).hexdigest()[:16]

        articles.append({
            "id": article_id,
            "source_name": source["name"],
            "title": text,
            "url": full_url,
            "raw_content": "",  # Will be fetched during processing
        })

    return articles


def fetch_full_article(url: str) -> str:
    """Fetch full text content from an article URL. Used by the summarizer."""
    return fetch_article_with_title(url)[1]


def fetch_article_with_title(url: str) -> tuple[str, str]:
    """Fetch (page title, full text) from an article URL."""
    try:
        verify = "paulgraham.com" not in url
        resp = requests.get(url, timeout=15,
                            headers={"User-Agent": "DailyDigestBot/1.0"},
                            verify=verify)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [!] Failed to fetch article at {url}: {e}")
        return "", ""

    soup = BeautifulSoup(resp.text, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""

    for tag in soup(["nav", "footer", "script", "style", "header", "aside"]):
        tag.decompose()

    article = soup.find("article") or soup.find("main") or soup.find("body")
    text = article.get_text(separator="\n", strip=True) if article else ""
    return title, text[:15000]
