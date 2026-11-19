from __future__ import annotations

import logging

from .config import AppConfig
from .meural import MeuralClient
from .state import StateStore

LOG = logging.getLogger(__name__)


def remove_artwork(
    config: AppConfig,
    identifier: str,
    *,
    reason: str | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    state = StateStore(config.sync.database)
    row = state.find_uploaded(identifier)

    artwork_id = row["id"] if row is not None else identifier
    item_id = row["item_id"] if row is not None else None
    if item_id is None and identifier.isdigit():
        item_id = identifier

    removed_gallery_ids: list[int] = []
    removed_from_device = False

    if not dry_run and item_id is not None:
        meural = MeuralClient(config.meural)
        removed_gallery_ids = meural.remove_item_from_all_galleries(item_id)
        device_id = meural.resolve_device_id()
        if device_id is not None:
            try:
                meural.remove_item_from_device(device_id, item_id)
                removed_from_device = True
            except Exception as exc:
                LOG.warning("Could not remove item %s from device %s: %s", item_id, device_id, exc)
            meural.sync_device(device_id)

    if not dry_run:
        state.mark_blocked(artwork_id, item_id=item_id, reason=reason)

    return {
        "artwork_id": artwork_id,
        "item_id": item_id,
        "removed_gallery_ids": removed_gallery_ids,
        "removed_from_device": removed_from_device,
        "dry_run": dry_run,
    }
