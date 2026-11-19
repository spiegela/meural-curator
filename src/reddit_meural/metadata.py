from __future__ import annotations

from .config import MetadataConfig
from .models import ArtworkMetadata, RedditPost
from .museums import year_of


KINDS = {"gcd": "comic", "comicvine": "comic", "metron": "comic", "aic": "museum", "met": "museum", "cleveland": "museum", "wikidata": "museum"}


def build_metadata(post: RedditPost, config: MetadataConfig) -> ArtworkMetadata:
    kind = KINDS.get(post.source_type, post.source_type)
    override = {**config.sources.get(kind, {}), **config.sources.get(post.source_type, {})}

    def template(key: str):
        return override.get(key, getattr(config, key))

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
        "date": post.date or "",
        "medium": post.medium or "",
        "kind": kind,
    }
    return ArtworkMetadata(
        name=_format(template("title_template"), values),
        artist_name=_format(template("artist_template"), values),
        description=_format(template("description_template"), values),
        medium=post.medium or template("medium"),
        year=year_of(post.date) or template("year"),
    )


def _format(template: str, values: dict) -> str:
    return template.format_map(_SafeDict(values)).strip()


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"
