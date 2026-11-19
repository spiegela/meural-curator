from __future__ import annotations

from contextlib import contextmanager
import json
import logging
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import ArtworkMetadata, RedditPost

LOG = logging.getLogger(__name__)
LEGACY_DATABASE = "reddit-meural.sqlite3"  # the default name before the rename to meural-curator


class StateStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        legacy = Path(path).with_name(LEGACY_DATABASE)
        if not Path(path).exists() and legacy.exists():  # keep the upload history across the rename
            LOG.info("Adopting %s as %s", legacy, path)
            legacy.rename(path)
        self._init_db()

    def already_done(self, post_id: str) -> bool:
        with self._connect() as conn:
            blocked = conn.execute(
                "select 1 from blocked_items where id = ?",
                (post_id,),
            ).fetchone()
            if blocked:
                return True
            row = conn.execute(
                "select status from posts where id = ?",
                (post_id,),
            ).fetchone()
        return bool(row and row["status"] in {"uploaded", "skipped", "blocked"})

    def find_uploaded(self, identifier: str) -> sqlite3.Row | None:
        with self._connect() as conn:
            return conn.execute(
                """
                select * from posts
                where id = ? or item_id = ?
                order by uploaded_at desc
                limit 1
                """,
                (identifier, identifier),
            ).fetchone()

    def list_posts(self, *, limit: int = 50, status: str | None = None) -> list[sqlite3.Row]:
        with self._connect() as conn:
            if status is None:
                return list(
                    conn.execute(
                        """
                        select * from posts
                        order by uploaded_at desc
                        limit ?
                        """,
                        (limit,),
                    )
                )
            return list(
                conn.execute(
                    """
                    select * from posts
                    where status = ?
                    order by uploaded_at desc
                    limit ?
                    """,
                    (status, limit),
                )
            )

    def mark_blocked(
        self,
        artwork_id: str,
        *,
        item_id: int | str | None = None,
        reason: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                insert into blocked_items (id, item_id, reason, blocked_at)
                values (?, ?, ?, datetime('now'))
                on conflict(id) do update set
                  item_id = coalesce(excluded.item_id, blocked_items.item_id),
                  reason = excluded.reason,
                  blocked_at = excluded.blocked_at
                """,
                (artwork_id, str(item_id) if item_id is not None else None, reason),
            )
            conn.execute(
                """
                update posts
                set status = 'blocked',
                    error = ?,
                    uploaded_at = datetime('now')
                where id = ? or item_id = ?
                """,
                (reason, artwork_id, str(item_id) if item_id is not None else artwork_id),
            )

    def mark_uploaded(
        self,
        post: RedditPost,
        item_id: int | str,
        gallery_id: int | str | None,
        image_hash: str,
        metadata: ArtworkMetadata,
    ) -> None:
        self._upsert(
            post=post,
            status="uploaded",
            item_id=str(item_id),
            gallery_id=str(gallery_id) if gallery_id is not None else None,
            image_hash=image_hash,
            error=None,
            metadata=metadata,
        )

    def mark_skipped(self, post: RedditPost, reason: str) -> None:
        self._upsert(
            post=post,
            status="skipped",
            item_id=None,
            gallery_id=None,
            image_hash=None,
            error=reason,
            metadata=None,
        )

    def mark_failed(self, post: RedditPost, reason: str) -> None:
        self._upsert(
            post=post,
            status="failed",
            item_id=None,
            gallery_id=None,
            image_hash=None,
            error=reason,
            metadata=None,
        )

    def _upsert(
        self,
        post: RedditPost,
        status: str,
        item_id: str | None,
        gallery_id: str | None,
        image_hash: str | None,
        error: str | None,
        metadata: ArtworkMetadata | None,
    ) -> None:
        metadata_json = json.dumps(metadata.__dict__) if metadata else None
        with self._connect() as conn:
            conn.execute(
                """
                insert into posts (
                  id, subreddit, title, url, permalink, score, created_utc,
                  status, item_id, gallery_id, image_hash, error, metadata_json,
                  uploaded_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                on conflict(id) do update set
                  subreddit = excluded.subreddit,
                  title = excluded.title,
                  url = excluded.url,
                  permalink = excluded.permalink,
                  score = excluded.score,
                  created_utc = excluded.created_utc,
                  status = excluded.status,
                  item_id = excluded.item_id,
                  gallery_id = excluded.gallery_id,
                  image_hash = excluded.image_hash,
                  error = excluded.error,
                  metadata_json = excluded.metadata_json,
                  uploaded_at = excluded.uploaded_at
                """,
                (
                    post.id,
                    post.subreddit,
                    post.title,
                    post.image_url,
                    post.permalink,
                    post.score,
                    post.created_utc,
                    status,
                    item_id,
                    gallery_id,
                    image_hash,
                    error,
                    metadata_json,
                ),
            )

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists posts (
                  id text primary key,
                  subreddit text not null,
                  title text not null,
                  url text not null,
                  permalink text not null,
                  score integer not null,
                  created_utc real not null,
                  status text not null,
                  item_id text,
                  gallery_id text,
                  image_hash text,
                  error text,
                  metadata_json text,
                  uploaded_at text
                )
                """
            )
            conn.execute(
                """
                create table if not exists blocked_items (
                  id text primary key,
                  item_id text,
                  reason text,
                  blocked_at text
                )
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()
