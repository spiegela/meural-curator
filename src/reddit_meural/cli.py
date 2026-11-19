from __future__ import annotations

import argparse
import logging
import os
import sys

from .config import load_config
from .meural import MeuralClient
from .remove import remove_artwork
from .state import StateStore
from .sync import run_sync


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reddit-meural")
    parser.add_argument("--log-level", default="INFO")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sync_parser = subparsers.add_parser("sync", help="Curate Reddit posts and upload to Meural")
    sync_parser.add_argument("--log-level", dest="command_log_level")
    sync_parser.add_argument("--config", default=os.getenv("REDDIT_MEURAL_CONFIG", "config.yaml"))
    dry_group = sync_parser.add_mutually_exclusive_group()
    dry_group.add_argument("--dry-run", action="store_true", default=None)
    dry_group.add_argument("--no-dry-run", action="store_false", dest="dry_run")

    list_parser = subparsers.add_parser("list-meural", help="List Meural devices and galleries")
    list_parser.add_argument("--log-level", dest="command_log_level")
    list_parser.add_argument("--config", default=os.getenv("REDDIT_MEURAL_CONFIG", "config.yaml"))

    remove_parser = subparsers.add_parser("remove", help="Remove an artwork and block it from future syncs")
    remove_parser.add_argument("identifier", help="Artwork id, e.g. gcd:16556, Reddit id, or Meural item id")
    remove_parser.add_argument("--log-level", dest="command_log_level")
    remove_parser.add_argument("--config", default=os.getenv("REDDIT_MEURAL_CONFIG", "config.yaml"))
    remove_parser.add_argument("--reason", default=None)
    remove_parser.add_argument("--dry-run", action="store_true")

    artwork_parser = subparsers.add_parser("list-artwork", help="List local artwork sync history")
    artwork_parser.add_argument("--log-level", dest="command_log_level")
    artwork_parser.add_argument("--config", default=os.getenv("REDDIT_MEURAL_CONFIG", "config.yaml"))
    artwork_parser.add_argument("--status", default=None)
    artwork_parser.add_argument("--limit", type=int, default=50)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, (args.command_log_level or args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
        if args.command == "sync":
            run_sync(config, dry_run=args.dry_run)
        elif args.command == "list-meural":
            _list_meural(config.meural)
        elif args.command == "remove":
            result = remove_artwork(
                config,
                args.identifier,
                reason=args.reason,
                dry_run=args.dry_run,
            )
            _print_remove_result(result)
        elif args.command == "list-artwork":
            _list_artwork(config.sync.database, status=args.status, limit=args.limit)
        else:
            parser.error(f"Unknown command: {args.command}")
    except Exception as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    return 0


def _list_meural(config) -> None:
    client = MeuralClient(config)
    print("Devices:")
    for device in client.list_devices():
        print(
            f"  {device.get('id')}: {device.get('alias') or device.get('name')} "
            f"({device.get('localIp', 'no local IP')})"
        )
    print("\nGalleries:")
    for gallery in client.list_galleries():
        print(f"  {gallery.get('id')}: {gallery.get('name')} ({len(gallery.get('itemIds') or [])} items)")


def _print_remove_result(result: dict[str, object]) -> None:
    action = "Would block" if result["dry_run"] else "Blocked"
    print(f"{action} artwork {result['artwork_id']}")
    if result["item_id"]:
        print(f"Meural item: {result['item_id']}")
    if result["removed_gallery_ids"]:
        galleries = ", ".join(str(value) for value in result["removed_gallery_ids"])
        print(f"Removed from galleries: {galleries}")
    if result["removed_from_device"]:
        print("Removed from configured device and requested device sync")


def _list_artwork(database: str, *, status: str | None, limit: int) -> None:
    state = StateStore(database)
    rows = state.list_posts(status=status, limit=limit)
    for row in rows:
        print(
            f"{row['id']}\titem={row['item_id'] or '-'}\t"
            f"status={row['status']}\t{row['title']}"
        )


if __name__ == "__main__":
    sys.exit(main())
