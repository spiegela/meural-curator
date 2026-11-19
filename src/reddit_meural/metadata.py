from __future__ import annotations

from .config import MetadataConfig
from .models import ArtworkMetadata, RedditPost


def build_metadata(post: RedditPost, config: MetadataConfig) -> ArtworkMetadata:
    values = {
        "id": post.id,
        "subreddit": post.subreddit,
        "collection": post.collection,
        "source_name": post.source_name or f"r/{post.subreddit}",
        "source_type": post.source_type,
        "title": post.title,
        "author": post.author,
        "score": post.score,
        "permalink": post.permalink,
        "image_url": post.image_url,
    }
    return ArtworkMetadata(
        name=_format(config.title_template, values),
        artist_name=_format(config.artist_template, values),
        description=_format(config.description_template, values),
        medium=config.medium,
        year=config.year,
    )


def _format(template: str, values: dict) -> str:
    return template.format_map(_SafeDict(values)).strip()


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
