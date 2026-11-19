from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RedditPost:
    id: str
    subreddit: str
    title: str
    author: str
    score: int
    permalink: str
    created_utc: float
    image_url: str
    over_18: bool
    spoiler: bool
    source_type: str = "reddit"
    source_name: str | None = None

    @property
    def collection(self) -> str:
        return self.subreddit


@dataclass(frozen=True)
class ImageAsset:
    path: str
    sha256: str
    width: int
    height: int
    content_type: str = "image/jpeg"


@dataclass(frozen=True)
class ArtworkMetadata:
    name: str
    artist_name: str
    description: str
    medium: str | None = None
    year: str | int | None = None
