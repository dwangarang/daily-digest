"""
API-based content ingestion.

Supports Hacker News and Reddit as built-in sources.
Extensible — add new API sources by adding a function and
registering it in FETCHERS.
"""

import hashlib
import requests


def fetch_api_source(source: dict) -> list:
    """
    Route to the appropriate API fetcher based on source config.
    """
    api_name = source.get("api_name", "")
    fetcher = FETCHERS.get(api_name)
    if not fetcher:
        print(f"  [!] Unknown API source: {api_name}")
        return []
    return fetcher(source)


def _fetch_hackernews(source: dict) -> list:
    """Fetch top stories from Hacker News API (no auth needed)."""
    articles = []
    max_items = source.get("max_items", 10)

    try:
        resp = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        )
        story_ids = resp.json()[:max_items]
    except Exception as e:
        print(f"  [!] Failed to fetch HN top stories: {e}")
        return articles

    for sid in story_ids:
        try:
            item = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{sid}.json", timeout=10
            ).json()

            if not item or item.get("type") != "story":
                continue

            title = item.get("title", "Untitled")
            url = item.get("url", f"https://news.ycombinator.com/item?id={sid}")

            # HN stories don't have full content in the API — just title + URL.
            # The summarizer will fetch the linked page if needed.
            raw_content = item.get("text", "")  # Only for "Ask HN" / text posts

            article_id = hashlib.sha256(
                f"hackernews:{sid}".encode()
            ).hexdigest()[:16]

            articles.append({
                "id": article_id,
                "source_name": source["name"],
                "title": title,
                "url": url,
                "raw_content": raw_content or f"[External link: {url}]",
            })
        except Exception as e:
            print(f"  [!] Failed to fetch HN item {sid}: {e}")
            continue

    return articles


def _fetch_reddit(source: dict) -> list:
    """Fetch top posts from a subreddit (no auth, public JSON endpoint)."""
    articles = []
    subreddit = source.get("subreddit", "technology")
    max_items = source.get("max_items", 10)

    try:
        headers = {"User-Agent": "DailyDigestBot/1.0"}
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/hot.json?limit={max_items}",
            headers=headers, timeout=10
        )
        data = resp.json()
        posts = data.get("data", {}).get("children", [])
    except Exception as e:
        print(f"  [!] Failed to fetch Reddit r/{subreddit}: {e}")
        return articles

    for post in posts:
        p = post.get("data", {})
        if p.get("stickied"):
            continue

        title = p.get("title", "Untitled")
        url = p.get("url", "")
        selftext = p.get("selftext", "")

        article_id = hashlib.sha256(
            f"reddit:{p.get('id', '')}".encode()
        ).hexdigest()[:16]

        articles.append({
            "id": article_id,
            "source_name": source["name"],
            "title": title,
            "url": url,
            "raw_content": selftext[:10000] if selftext else f"[External link: {url}]",
        })

    return articles


# Registry of API fetchers
FETCHERS = {
    "hackernews": _fetch_hackernews,
    "reddit": _fetch_reddit,
}
