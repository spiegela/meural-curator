from __future__ import annotations

from itertools import chain, zip_longest
import logging
import time

import requests

from .config import AppConfig
from .comics import ComicCoverClient
from .images import download_and_prepare
from .metadata import build_metadata
from .meural import MeuralClient
from .models import RedditPost
from .reddit import RedditClient
from .state import StateStore

LOG = logging.getLogger(__name__)


def run_sync(config: AppConfig, *, dry_run: bool | None = None) -> int:
    effective_dry_run = config.sync.dry_run if dry_run is None else dry_run
    state = StateStore(config.sync.database)
    session = requests.Session()
    reddit = RedditClient(config.reddit, session=session)
    comics = ComicCoverClient(config.comic_covers, session=session)
    meural = None if effective_dry_run else MeuralClient(config.meural)

    uploaded = 0
    # Alternate Reddit and comic candidates so comics get upload slots before max_uploads_per_run is spent on Reddit.
    posts = _interleave(reddit.iter_posts(), comics.iter_posts())
    LOG.info("Found %d candidate posts", len(posts))

    gallery_id = None
    device_id = None
    subreddit_playlist_ids: dict[str, int] = {}
    if not effective_dry_run:
        gallery_id = meural.resolve_gallery_id()
        device_id = meural.resolve_device_id()
        if config.meural.create_source_playlists:
            subreddit_playlist_ids = meural.resolve_source_playlist_ids(
                _unique_sources(config.reddit.subreddits, posts)
            )
        elif config.meural.create_subreddit_playlists:
            subreddit_playlist_ids = meural.resolve_subreddit_playlist_ids(
                _unique_subreddits(config.reddit.subreddits, posts)
            )

    for post in posts:
        if uploaded >= config.sync.max_uploads_per_run:
            break
        if state.already_done(post.id):
            continue

        try:
            asset = download_and_prepare(post, config.images, session=session)
            metadata = build_metadata(post, config.metadata)
        except Exception as exc:
            LOG.warning("Skipping %s: %s", post.id, exc)
            state.mark_skipped(post, str(exc))
            continue

        LOG.info(
            "Candidate: %s/%s score=%s %sx%s %s",
            post.source_type,
            post.collection,
            post.score,
            asset.width,
            asset.height,
            post.title,
        )

        if effective_dry_run:
            uploaded += 1
            continue

        try:
            item = meural.upload_item(asset)
            item_id = item["id"]
            if config.meural.metadata_update:
                updated = meural.update_item_metadata(item_id, metadata)
                if not updated:
                    LOG.warning("Uploaded item %s, but metadata update did not succeed", item_id)
            meural.add_item_to_gallery(gallery_id, item_id)
            subreddit_playlist_id = subreddit_playlist_ids.get(_normalize_collection(post.collection))
            if subreddit_playlist_id is not None:
                meural.add_item_to_gallery(subreddit_playlist_id, item_id)
            if device_id and config.meural.load_uploaded_item:
                meural.load_item_to_device(device_id, item_id)
            state.mark_uploaded(post, item_id, gallery_id, asset.sha256, metadata)
            LOG.info("Uploaded item %s to gallery %s", item_id, gallery_id)
            uploaded += 1
            if config.sync.sleep_between_uploads_seconds:
                time.sleep(config.sync.sleep_between_uploads_seconds)
        except Exception as exc:
            LOG.exception("Failed to upload %s", post.id)
            state.mark_failed(post, str(exc))

    if not effective_dry_run and uploaded and device_id and config.meural.sync_gallery_to_device:
        gallery_ids_to_sync = list(
            dict.fromkeys([gallery_id, *subreddit_playlist_ids.values()])
        )
        loaded_gallery_count = 0
        for gallery_id_to_sync in gallery_ids_to_sync:
            if _try_load_gallery_to_device(meural, device_id, gallery_id_to_sync):
                loaded_gallery_count += 1
        _try_sync_device(meural, device_id)
        LOG.info(
            "Loaded %d/%d gallery playlist(s) to device %s",
            loaded_gallery_count,
            len(gallery_ids_to_sync),
            device_id,
        )

    LOG.info("%s %d item(s)", "Would upload" if effective_dry_run else "Uploaded", uploaded)
    return uploaded


def _unique_subreddits(
    configured_subreddits: list[str],
    posts: list[RedditPost],
) -> list[str]:
    subreddits_by_key = {
        _normalize_subreddit(subreddit): subreddit
        for subreddit in configured_subreddits
    }
    for post in posts:
        if post.source_type != "reddit":
            continue
        subreddits_by_key[_normalize_subreddit(post.subreddit)] = post.subreddit
    return list(subreddits_by_key.values())


def _unique_sources(
    configured_subreddits: list[str],
    posts: list[RedditPost],
) -> list[dict[str, str]]:
    sources = [
        {
            "collection": subreddit,
            "source_name": "Reddit",
            "source_type": "reddit",
            "subreddit": subreddit,
        }
        for subreddit in configured_subreddits
    ]
    for post in posts:
        sources.append(
            {
                "collection": post.collection,
                "source_name": post.source_name or f"r/{post.collection}",
                "source_type": post.source_type,
                "subreddit": post.collection if post.source_type == "reddit" else "",
            }
        )

    unique_sources: dict[str, dict[str, str]] = {}
    for source in sources:
        unique_sources[_normalize_collection(source["collection"])] = source
    return list(unique_sources.values())


def _normalize_subreddit(subreddit: str) -> str:
    return _normalize_collection(subreddit)


def _normalize_collection(collection: str) -> str:
    return collection.casefold()


def _try_load_gallery_to_device(
    meural: MeuralClient,
    device_id: int | str,
    gallery_id: int | str,
) -> bool:
    try:
        meural.load_gallery_to_device(device_id, gallery_id)
        return True
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        LOG.warning(
            "Could not load gallery %s to device %s: HTTP %s",
            gallery_id,
            device_id,
            status,
        )
        return False


def _try_sync_device(meural: MeuralClient, device_id: int | str) -> bool:
    try:
        meural.sync_device(device_id)
        return True
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        LOG.warning("Could not request sync for device %s: HTTP %s", device_id, status)
        return False


def _interleave(*streams: list[RedditPost]) -> list[RedditPost]:
    """Round-robin the candidate lists (a, b, a, b, ...), keeping each list's own order."""
    return [post for post in chain.from_iterable(zip_longest(*streams)) if post is not None]
