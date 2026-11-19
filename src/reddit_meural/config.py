from __future__ import annotations

from dataclasses import dataclass, field
import os
import re
from typing import Any

import yaml


ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")


@dataclass(frozen=True)
class RedditConfig:
    subreddits: list[str] = field(default_factory=list)
    client_id: str | None = None  # Reddit script app (reddit.com/prefs/apps); required since anonymous access closed
    client_secret: str | None = None
    listing: str = "top"
    time_filter: str = "week"
    limit_per_subreddit: int = 40
    min_score: int = 1000
    include_over_18: bool = False
    include_spoilers: bool = False
    user_agent: str = "linux:reddit-meural:0.1"


@dataclass(frozen=True)
class ImageConfig:
    cache_dir: str = "data/images"
    max_dimension: int = 4096
    jpeg_quality: int = 92
    min_width: int = 900
    min_height: int = 900
    max_download_mb: int = 30
    orientation: str = "any"


@dataclass(frozen=True)
class MeuralConfig:
    username: str
    password: str
    token_cache: str = "data/meural-token.json"
    gallery_id: int | None = None
    gallery_name: str | None = None
    create_gallery_if_missing: bool = False
    create_source_playlists: bool = True
    source_playlist_name_template: str = "{source_name}: {collection}"
    create_subreddit_playlists: bool = True
    subreddit_playlist_name_template: str = "Reddit: r/{subreddit}"
    device_id: int | None = None
    device_name: str | None = None
    sync_gallery_to_device: bool = True
    load_uploaded_item: bool = False
    metadata_update: bool = True
    request_timeout_seconds: float = 90.0
    upload_timeout_seconds: float = 180.0
    max_retries: int = 3
    retry_backoff_seconds: float = 3.0


@dataclass(frozen=True)
class ComicCoverSourceConfig:
    name: str
    provider: str = "gcd"
    enabled: bool = True
    issue_ids: list[int | str] = field(default_factory=list)
    max_items: int = 25
    on_sale_weekly: bool = False
    year: int | None = None
    week: int | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComicCoversConfig:
    enabled: bool = False
    user_agent: str = "artwork-meural/0.1"
    enable_gcd: bool = False
    gcd_base_url: str = "https://www.comics.org/api"
    comicvine_base_url: str = "https://comicvine.gamespot.com/api"
    comicvine_api_key: str | None = None
    metron_base_url: str = "https://metron.cloud/api"
    metron_api_token: str | None = None
    metron_username: str | None = None  # deprecated by Metron: Basic auth, used only without a token
    metron_password: str | None = None
    sources: list[ComicCoverSourceConfig] = field(default_factory=list)


@dataclass(frozen=True)
class MuseumSourceConfig:
    name: str  # collection label (playlist "{museum}: {name}")
    provider: str  # aic | met | cleveland | wikidata
    query: str | None = None
    collection: str | None = None  # wikidata: the museum's QID (Nelson-Atkins: Q1976985)
    museum: str | None = None  # display name (source_name); defaults per provider
    enabled: bool = True
    max_items: int = 25
    random_page: bool = True  # a random results page each run, so one search keeps yielding new works
    max_pages: int = 40
    params: dict[str, Any] = field(default_factory=dict)  # extra provider query params


@dataclass(frozen=True)
class MuseumsConfig:
    enabled: bool = False
    user_agent: str = "reddit-meural/0.1 (personal Meural art frame)"
    sources: list[MuseumSourceConfig] = field(default_factory=list)


@dataclass(frozen=True)
class MetadataConfig:
    title_template: str = "{title}"
    artist_template: str = "{author} via {source_name}"
    medium: str | None = "Digital image"
    year: str | int | None = None
    description_template: str = (
        "{title}\n\n"
        "Source: {source_name} ({source_type})\n"
        "Collection: {collection}\n"
        "Credit: {author}\n"
        "Original link: {permalink}"
    )
    # Per-source overrides of the templates/medium above, keyed by source type (reddit, gcd, comicvine, metron,
    # aic, met, cleveland) or kind (comic, museum); the most specific wins.
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncConfig:
    database: str = "data/reddit-meural.sqlite3"
    max_uploads_per_run: int = 10
    sleep_between_uploads_seconds: float = 2.0
    dry_run: bool = True


@dataclass(frozen=True)
class AppConfig:
    reddit: RedditConfig
    images: ImageConfig
    meural: MeuralConfig
    comic_covers: ComicCoversConfig = field(default_factory=ComicCoversConfig)
    museums: MuseumsConfig = field(default_factory=MuseumsConfig)
    metadata: MetadataConfig = field(default_factory=MetadataConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)


def load_config(path: str) -> AppConfig:
    with open(path, "r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    expanded = _expand_env(raw)

    reddit = RedditConfig(**(expanded.get("reddit") or {}))
    images = ImageConfig(**expanded.get("images", {}))
    meural = MeuralConfig(**expanded["meural"])
    comic_covers = _load_comic_covers_config(expanded.get("comic_covers", {}))
    raw_museums = dict(expanded.get("museums") or {})
    museums = MuseumsConfig(
        **{key: value for key, value in raw_museums.items() if key != "sources"},
        sources=[MuseumSourceConfig(**source) for source in raw_museums.get("sources") or []],
    )
    metadata = MetadataConfig(**expanded.get("metadata", {}))
    sync = SyncConfig(**expanded.get("sync", {}))

    if not meural.username or not meural.password:
        raise ValueError("meural.username and meural.password are required")
    if not meural.gallery_id and not meural.gallery_name:
        raise ValueError("Set meural.gallery_id or meural.gallery_name")

    return AppConfig(
        reddit=reddit,
        images=images,
        meural=meural,
        comic_covers=comic_covers,
        museums=museums,
        metadata=metadata,
        sync=sync,
    )


def _load_comic_covers_config(raw: dict[str, Any]) -> ComicCoversConfig:
    sources = [
        ComicCoverSourceConfig(**source)
        for source in raw.get("sources", [])
    ]
    values = {key: value for key, value in raw.items() if key != "sources"}
    return ComicCoversConfig(**values, sources=sources)


def _expand_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, str):
        return ENV_PATTERN.sub(_replace_env, value)
    return value


def _replace_env(match: re.Match[str]) -> str:
    name = match.group(1)
    default = match.group(2)
    if name in os.environ:
        return os.environ[name]
    return default or ""
