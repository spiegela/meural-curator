from __future__ import annotations

from datetime import date
import logging
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from .config import ComicCoversConfig, ComicCoverSourceConfig
from .models import RedditPost

LOG = logging.getLogger(__name__)


class ComicCoverClient:
    def __init__(
        self,
        config: ComicCoversConfig,
        session: requests.Session | None = None,
    ):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.user_agent})

    def iter_posts(self) -> list[RedditPost]:
        if not self.config.enabled:
            return []

        posts: list[RedditPost] = []
        for source in self.config.sources:
            if not source.enabled:
                continue
            provider = source.provider.lower()
            if provider == "gcd" and not self.config.enable_gcd:
                LOG.info("Skipping GCD source %s because comic_covers.enable_gcd is false", source.name)
                continue
            fetch = {
                "gcd": self._fetch_gcd_source,
                "comicvine": self._fetch_comicvine_source,
                "metron": self._fetch_metron_source,
            }.get(provider)
            if fetch is None:
                raise ValueError(f"Unsupported comic cover provider: {source.provider}")
            LOG.info("Fetching comic cover source %s via %s", source.name, provider)
            try:
                posts.extend(fetch(source))
            except Exception as exc:  # one source down (rate limit, bad key) must not stop the run
                LOG.warning("Comic cover source %s failed: %s", source.name, exc)

        return posts

    def _fetch_gcd_source(self, source: ComicCoverSourceConfig) -> list[RedditPost]:
        posts = []
        for issue_id in source.issue_ids:
            post = self._try_gcd_issue_to_post(source, self._get_gcd_issue(issue_id))
            if post is not None:
                posts.append(post)

        if source.on_sale_weekly:
            today = date.today()
            year, week, _ = today.isocalendar()
            url = _api_url(
                self.config.gcd_base_url,
                f"issue/on_sale_weekly/{source.year or year}/week/{source.week or week}",
            )
            for item in self._get_paginated(
                url,
                params={"format": "json"},
                limit=source.max_items,
            ):
                post = self._try_gcd_issue_to_post(source, self._get_url(item["api_url"]))
                if post is not None:
                    posts.append(post)

        return _limit_posts(posts, source.max_items)

    def _get_gcd_issue(self, issue_id: int | str) -> dict[str, Any]:
        return self._get_url(
            _api_url(self.config.gcd_base_url, f"issue/{issue_id}"),
            params={"format": "json"},
        )

    def _gcd_issue_to_post(
        self,
        source: ComicCoverSourceConfig,
        issue: dict[str, Any],
    ) -> RedditPost:
        cover_url = issue.get("cover")
        if not cover_url:
            raise ValueError(f"GCD issue {issue.get('api_url')} does not include a cover")
        issue_id = _id_from_api_url(issue.get("api_url", ""))
        return _comic_post(
            source=source,
            provider="gcd",
            item_id=issue_id,
            title=_issue_title(issue.get("series_name"), issue.get("descriptor")),
            author=_cover_credit(issue),
            permalink=(issue.get("api_url") or "").replace("/api/", "/").replace("?format=json", ""),
            image_url=cover_url,
            created_utc=_year_timestamp(issue.get("key_date")),
        )

    def _try_gcd_issue_to_post(
        self,
        source: ComicCoverSourceConfig,
        issue: dict[str, Any],
    ) -> RedditPost | None:
        if not issue:
            return None
        try:
            return self._gcd_issue_to_post(source, issue)
        except ValueError as exc:
            LOG.info("Skipping GCD issue without usable cover: %s", exc)
            return None

    def _fetch_comicvine_source(self, source: ComicCoverSourceConfig) -> list[RedditPost]:
        if not self.config.comicvine_api_key:
            raise ValueError("comic_covers.comicvine_api_key is required for ComicVine sources")

        posts = []
        for issue_id in source.issue_ids:
            post = self._try_comicvine_issue_to_post(
                source,
                self._get_comicvine_issue(issue_id),
            )
            if post is not None:
                posts.append(post)
        if source.params:
            params = {
                "api_key": self.config.comicvine_api_key,
                "format": "json",
                "limit": source.max_items,
                "field_list": "id,name,issue_number,cover_date,site_detail_url,image,volume,person_credits",
                **source.params,
            }
            payload = self._get_url(_api_url(self.config.comicvine_base_url, "issues"), params=params)
            for item in payload.get("results", []):
                post = self._try_comicvine_issue_to_post(source, item)
                if post is not None:
                    posts.append(post)
        return _limit_posts(posts, source.max_items)

    def _get_comicvine_issue(self, issue_id: int | str) -> dict[str, Any]:
        payload = self._get_url(
            _api_url(self.config.comicvine_base_url, f"issue/4000-{issue_id}"),
            params={
                "api_key": self.config.comicvine_api_key,
                "format": "json",
                "field_list": "id,name,issue_number,cover_date,site_detail_url,image,volume,person_credits",
            },
        )
        return payload["results"]

    def _comicvine_issue_to_post(
        self,
        source: ComicCoverSourceConfig,
        issue: dict[str, Any],
    ) -> RedditPost:
        image = issue.get("image") or {}
        cover_url = image.get("original_url") or image.get("super_url") or image.get("medium_url")
        if not cover_url:
            raise ValueError(f"ComicVine issue {issue.get('id')} does not include a cover")
        volume = issue.get("volume") or {}
        return _comic_post(
            source=source,
            provider="comicvine",
            item_id=str(issue["id"]),
            title=_issue_title(volume.get("name"), issue.get("issue_number") or issue.get("name")),
            author=_people_credit(issue.get("person_credits") or []),
            permalink=issue.get("site_detail_url") or "",
            image_url=cover_url,
            created_utc=_year_timestamp(issue.get("cover_date")),
        )

    def _try_comicvine_issue_to_post(
        self,
        source: ComicCoverSourceConfig,
        issue: dict[str, Any],
    ) -> RedditPost | None:
        if not issue:
            return None
        try:
            return self._comicvine_issue_to_post(source, issue)
        except ValueError as exc:
            LOG.info("Skipping ComicVine issue without usable cover: %s", exc)
            return None

    def _fetch_metron_source(self, source: ComicCoverSourceConfig) -> list[RedditPost]:
        posts = []
        for issue_id in source.issue_ids:
            post = self._try_metron_issue_to_post(source, self._get_metron_issue(issue_id))
            if post is not None:
                posts.append(post)
        if source.params:
            for item in self._get_paginated(
                _api_url(self.config.metron_base_url, "issue"),
                params=source.params,
                limit=source.max_items,
                auth=self._metron_auth(),
            ):
                post = self._try_metron_issue_to_post(source, item)
                if post is not None:
                    posts.append(post)
        return _limit_posts(posts, source.max_items)

    def _get_metron_issue(self, issue_id: int | str) -> dict[str, Any]:
        return self._get_url(
            _api_url(self.config.metron_base_url, f"issue/{issue_id}"),
            auth=self._metron_auth(),
        )

    def _metron_issue_to_post(
        self,
        source: ComicCoverSourceConfig,
        issue: dict[str, Any],
    ) -> RedditPost:
        cover_url = issue.get("image") or issue.get("cover")
        if isinstance(cover_url, dict):
            cover_url = cover_url.get("original") or cover_url.get("url")
        if not cover_url:
            raise ValueError(f"Metron issue {issue.get('id')} does not include a cover")
        series = issue.get("series") or {}
        return _comic_post(
            source=source,
            provider="metron",
            item_id=str(issue["id"]),
            title=_issue_title(series.get("name") or issue.get("series_name"), issue.get("number")),
            author=_metron_credit(issue),
            permalink=issue.get("resource_url") or issue.get("url") or "",
            image_url=cover_url,
            created_utc=_year_timestamp(issue.get("cover_date") or issue.get("store_date")),
        )

    def _try_metron_issue_to_post(
        self,
        source: ComicCoverSourceConfig,
        issue: dict[str, Any],
    ) -> RedditPost | None:
        if not issue:
            return None
        try:
            return self._metron_issue_to_post(source, issue)
        except ValueError as exc:
            LOG.info("Skipping Metron issue without usable cover: %s", exc)
            return None

    def _metron_auth(self) -> tuple[str, str] | None:
        if self.config.metron_username and self.config.metron_password:
            return (self.config.metron_username, self.config.metron_password)
        return None

    def _get_paginated(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int,
        auth: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while url and len(items) < limit:
            payload = self._get_url(url, params=params, auth=auth)
            page_items = payload.get("results", [])
            items.extend(page_items[: max(0, limit - len(items))])
            url = payload.get("next")
            params = None
        return items

    def _get_url(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        auth: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        response = self.session.get(url, params=params, auth=auth, timeout=30)
        if response.status_code == 429:
            retry_after = _retry_after_seconds(response)
            if retry_after:
                LOG.warning("Rate limited by %s; sleeping %.1fs", url, retry_after)
                time.sleep(retry_after)
                response = self.session.get(url, params=params, auth=auth, timeout=30)
            if response.status_code == 429:
                LOG.warning("Skipping rate-limited comic API request: %s", url)
                return {}
        response.raise_for_status()
        return response.json()


def _comic_post(
    *,
    source: ComicCoverSourceConfig,
    provider: str,
    item_id: str,
    title: str,
    author: str,
    permalink: str,
    image_url: str,
    created_utc: float,
) -> RedditPost:
    return RedditPost(
        id=f"{provider}:{item_id}",
        subreddit=source.name,
        title=title,
        author=author or provider,
        score=0,
        permalink=permalink,
        created_utc=created_utc,
        image_url=image_url,
        over_18=False,
        spoiler=False,
        source_type=provider,
        source_name=source.name,
    )


def _api_url(base_url: str, path: str) -> str:
    return urljoin(f"{base_url.rstrip('/')}/", path.strip("/"))


def _id_from_api_url(url: str) -> str:
    path = urlparse(url).path
    return path.rstrip("/").split("/")[-1]


def _issue_title(series_name: str | None, issue_number: str | int | None) -> str:
    if series_name and issue_number:
        return f"{series_name} #{issue_number}"
    return series_name or str(issue_number or "Untitled comic cover")


def _cover_credit(issue: dict[str, Any]) -> str:
    for story in issue.get("story_set", []):
        if story.get("type") == "cover":
            return _join_credit_parts(
                story.get("script"),
                story.get("pencils"),
                story.get("inks"),
                story.get("colors"),
                story.get("letters"),
            )
    return "Grand Comics Database"


def _people_credit(people: list[dict[str, Any]]) -> str:
    names = [person.get("name") for person in people if person.get("name")]
    return ", ".join(names[:4])


def _metron_credit(issue: dict[str, Any]) -> str:
    credits = issue.get("credits") or []
    if isinstance(credits, list):
        return _people_credit(credits)
    return ""


def _join_credit_parts(*parts: str | None) -> str:
    values = [part.strip() for part in parts if part and part.strip()]
    return "; ".join(values)


def _year_timestamp(value: str | None) -> float:
    if not value:
        return 0
    try:
        return float(int(value[:4]))
    except ValueError:
        return 0


def _limit_posts(posts: list[RedditPost], limit: int) -> list[RedditPost]:
    return posts[:limit] if limit else posts


def _retry_after_seconds(response: requests.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return min(float(value), 60.0)
    except ValueError:
        return None
