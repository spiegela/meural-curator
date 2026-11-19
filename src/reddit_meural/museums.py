"""Open-access museum artwork (public domain / CC0, high resolution, no API keys).

Providers:
  aic        Art Institute of Chicago  api.artic.edu/api/v1/artworks/search  (+ IIIF images at 1686 px wide)
  met        The Metropolitan Museum   collectionapi.metmuseum.org v1.1/search, then v1/objects/{id}
  cleveland  Cleveland Museum of Art   openaccess-api.clevelandart.org/api/artworks (cc0=1)

Each source is one search. `random_page` (default on) picks a random page of the results each run, so the
same search keeps surfacing new works instead of the same first page (already uploaded items are skipped by
the sync state anyway). Where the API reports image size (AIC, Cleveland), works that don't match
images.orientation are dropped before download.
"""
from __future__ import annotations

import logging
import random
import re
from typing import Any

import requests

from .config import MuseumsConfig, MuseumSourceConfig
from .models import RedditPost

LOG = logging.getLogger(__name__)

AIC_API = "https://api.artic.edu/api/v1/artworks/search"
AIC_IIIF = "https://www.artic.edu/iiif/2/{image_id}/full/1686,/0/default.jpg"
MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1.1/search"
MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{id}"
CLEVELAND_API = "https://openaccess-api.clevelandart.org/api/artworks/"

MUSEUM_NAMES = {"aic": "Art Institute of Chicago", "met": "The Met", "cleveland": "Cleveland Museum of Art"}


class MuseumClient:
    def __init__(
        self,
        config: MuseumsConfig,
        *,
        orientation: str = "any",
        session: requests.Session | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.orientation = orientation
        self.session = session or requests.Session()
        self.rng = rng or random.Random()

    def iter_posts(self) -> list[RedditPost]:
        if not self.config.enabled:
            return []
        posts: list[RedditPost] = []
        for source in self.config.sources:
            if not source.enabled:
                continue
            fetch = {"aic": self._aic, "met": self._met, "cleveland": self._cleveland}.get(source.provider.lower())
            if fetch is None:
                raise ValueError(f"Unsupported museum provider: {source.provider}")
            LOG.info("Fetching museum source %s via %s", source.name, source.provider)
            try:
                posts.extend(fetch(source)[: source.max_items])
            except Exception as exc:  # one museum down must not stop the run
                LOG.warning("Museum source %s failed: %s", source.name, exc)
        return posts

    # --- providers ---

    def _aic(self, source: MuseumSourceConfig) -> list[RedditPost]:
        def page(number: int) -> dict[str, Any]:
            return self._get(AIC_API, {
                "q": source.query or None,
                "query[term][is_public_domain]": "true",
                "fields": "id,title,artist_display,date_display,medium_display,image_id,thumbnail",
                "limit": source.max_items,
                "page": number,
                **source.params,
            }, headers={"AIC-User-Agent": self.config.user_agent})

        payload = page(1)
        pages = min(int(payload.get("pagination", {}).get("total_pages") or 1), source.max_pages)
        number = self._pick(pages, source)
        if number != 1:
            payload = page(number)
        posts = []
        for art in payload.get("data", []):
            thumb = art.get("thumbnail") or {}
            if not art.get("image_id") or not self._fits(thumb.get("width"), thumb.get("height")):
                continue
            posts.append(self._post(
                source, "aic", art["id"], art.get("title"), _first_line(art.get("artist_display")),
                f"https://www.artic.edu/artworks/{art['id']}", AIC_IIIF.format(image_id=art["image_id"]),
                art.get("date_display"), art.get("medium_display"),
            ))
        return posts

    def _met(self, source: MuseumSourceConfig) -> list[RedditPost]:
        # v1.1 search returns object IDs only (paginated); public-domain status and the image come per object.
        def search(offset: int) -> dict[str, Any]:
            return self._get(MET_SEARCH, {
                "q": source.query or "*",
                "hasImages": "true",
                "limit": source.max_items,
                "offset": offset,
                **source.params,
            })

        payload = search(0)
        pages = min(-(-int(payload.get("total") or 0) // source.max_items) or 1, source.max_pages)
        number = self._pick(pages, source)
        if number != 1:
            payload = search((number - 1) * source.max_items)
        posts = []
        for object_id in payload.get("objectIDs") or []:
            try:
                art = self._get(MET_OBJECT.format(id=object_id))
            except requests.RequestException as exc:
                LOG.warning("Met object %s failed: %s", object_id, exc)
                continue
            if not art.get("isPublicDomain") or not art.get("primaryImage"):
                continue
            posts.append(self._post(
                source, "met", object_id, art.get("title"), art.get("artistDisplayName") or art.get("culture"),
                art.get("objectURL") or "", art["primaryImage"], art.get("objectDate"), art.get("medium"),
            ))
        return posts

    def _cleveland(self, source: MuseumSourceConfig) -> list[RedditPost]:
        def search(skip: int) -> dict[str, Any]:
            return self._get(CLEVELAND_API, {
                "q": source.query or None,
                "cc0": 1,
                "has_image": 1,
                "limit": source.max_items,
                "skip": skip,
                **source.params,
            })

        payload = search(0)
        pages = min(-(-int(payload.get("info", {}).get("total") or 0) // source.max_items) or 1, source.max_pages)
        number = self._pick(pages, source)
        if number != 1:
            payload = search((number - 1) * source.max_items)
        posts = []
        for art in payload.get("data", []):
            images = art.get("images") or {}
            image = images.get("print") or images.get("web") or {}
            if not image.get("url") or not self._fits(image.get("width"), image.get("height")):
                continue
            creators = art.get("creators") or []
            posts.append(self._post(
                source, "cleveland", art["id"], art.get("title"),
                creators[0].get("description") if creators else (art.get("culture") or [None])[0],
                art.get("url") or "", image["url"], art.get("creation_date"), art.get("technique"),
            ))
        return posts

    # --- helpers ---

    def _get(self, url: str, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        params = {key: value for key, value in (params or {}).items() if value is not None}
        response = self.session.get(
            url, params=params, headers={"User-Agent": self.config.user_agent, **(headers or {})}, timeout=30
        )
        response.raise_for_status()
        return response.json()

    def _pick(self, pages: int, source: MuseumSourceConfig) -> int:
        return self.rng.randint(1, max(1, pages)) if source.random_page else 1

    def _fits(self, width: Any, height: Any) -> bool:
        try:
            width, height = int(width), int(height)
        except (TypeError, ValueError):
            return True  # unknown size: let the downloader decide
        if self.orientation == "vertical":
            return height >= width
        if self.orientation == "horizontal":
            return width >= height
        return True

    def _post(self, source, provider, item_id, title, artist, permalink, image_url, date, medium) -> RedditPost:
        return RedditPost(
            id=f"{provider}:{item_id}",
            subreddit=source.name,
            title=(title or "Untitled").strip(),
            author=(artist or "Unknown artist").strip(),
            score=0,
            permalink=permalink,
            created_utc=0,
            image_url=image_url,
            over_18=False,
            spoiler=False,
            source_type=provider,
            source_name=MUSEUM_NAMES[provider],
            date=date or None,
            medium=medium or None,
        )


def _first_line(value: str | None) -> str | None:
    return value.splitlines()[0] if value else value


YEAR = re.compile(r"\b(1[0-9]{3}|20[0-9]{2})\b")


def year_of(date: str | None) -> int | None:
    """First 4-digit year in a museum date string ('c. 1665', '1635 or earlier'), for Meural's year field."""
    match = YEAR.search(date or "")
    return int(match.group(1)) if match else None
