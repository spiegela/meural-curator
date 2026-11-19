from __future__ import annotations

import html
import logging
from urllib.parse import urlparse

import requests

from .config import RedditConfig
from .models import RedditPost

LOG = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


class RedditClient:
    def __init__(self, config: RedditConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.user_agent})

    def iter_posts(self) -> list[RedditPost]:
        posts: list[RedditPost] = []
        for subreddit in self.config.subreddits:
            posts.extend(self._fetch_subreddit(subreddit))
        posts.sort(key=lambda post: post.score, reverse=True)
        return posts

    def _fetch_subreddit(self, subreddit: str) -> list[RedditPost]:
        listing = self.config.listing.strip("/")
        url = f"https://www.reddit.com/r/{subreddit}/{listing}.json"
        params = {
            "limit": self.config.limit_per_subreddit,
        }
        if listing == "top":
            params["t"] = self.config.time_filter

        LOG.info("Fetching r/%s %s", subreddit, listing)
        response = self.session.get(url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()

        posts: list[RedditPost] = []
        for child in payload.get("data", {}).get("children", []):
            data = child.get("data", {})
            post = self._parse_post(data, subreddit)
            if post is not None:
                posts.append(post)
        return posts

    def _parse_post(self, data: dict, fallback_subreddit: str) -> RedditPost | None:
        if data.get("stickied") or data.get("is_video"):
            return None
        if data.get("score", 0) < self.config.min_score:
            return None
        if data.get("over_18") and not self.config.include_over_18:
            return None
        if data.get("spoiler") and not self.config.include_spoilers:
            return None

        image_url = best_image_url(data)
        if not image_url:
            return None

        permalink = data.get("permalink", "")
        if permalink.startswith("/"):
            permalink = f"https://www.reddit.com{permalink}"

        return RedditPost(
            id=data["id"],
            subreddit=data.get("subreddit") or fallback_subreddit,
            title=data.get("title") or "Untitled",
            author=data.get("author") or "unknown",
            score=int(data.get("score") or 0),
            permalink=permalink,
            created_utc=float(data.get("created_utc") or 0),
            image_url=image_url,
            over_18=bool(data.get("over_18")),
            spoiler=bool(data.get("spoiler")),
        )


def best_image_url(data: dict) -> str | None:
    url = data.get("url_overridden_by_dest") or data.get("url") or ""
    if _looks_like_image(url):
        return html.unescape(url)

    gallery_url = _gallery_image_url(data)
    if gallery_url:
        return gallery_url

    images = data.get("preview", {}).get("images") or []
    if images:
        source = images[0].get("source", {})
        preview_url = source.get("url")
        if preview_url:
            return html.unescape(preview_url)

    return None


def _looks_like_image(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(IMAGE_EXTENSIONS) or parsed.netloc.endswith("i.redd.it")


def _gallery_image_url(data: dict) -> str | None:
    gallery_items = data.get("gallery_data", {}).get("items") or []
    media_metadata = data.get("media_metadata") or {}
    for item in gallery_items:
        media_id = item.get("media_id")
        metadata = media_metadata.get(media_id, {})
        source = metadata.get("s", {})
        url = source.get("u")
        if url:
            return html.unescape(url)
    return None
