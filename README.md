# artwork-meural

Curate artwork from Reddit and comic-cover data sources into a Meural Canvas cloud gallery, then optionally sync that gallery to a frame so normal Meural rotation keeps working.

This is intended to be standalone first. Home Assistant can still control playback through [HA-meural](https://github.com/GuySie/ha-meural), but permanent uploads and art-card metadata belong on the Meural cloud side.

## What works in this first pass

- Pulls top image posts from configured subreddits.
- Pulls comic book covers from configurable GCD, ComicVine, or Metron sources.
- Filters by score, NSFW/spoiler flags, dimensions, and prior upload state.
- Downloads and normalizes images to JPEG.
- Uploads images to Meural using the current Cognito auth flow used by HA-meural.
- Adds uploaded items to an existing Meural gallery and to a playlist for each configured subreddit.
- Optionally syncs those galleries/playlists to a device.
- Tries to update Meural art-card metadata with title, artist, medium, year, and description.
- Keeps a SQLite database so repeat runs do not upload duplicates.

The metadata endpoint is implemented as best-effort because Netgear does not publish stable docs for editing uploaded item details. Uploading and gallery/device actions are based on public examples and the HA-meural client.

For comic covers, Metron is the best default source because it has direct issue filters and cover fields. ComicVine is also supported with an API key. GCD support remains available for specific historical issue IDs, but its weekly/on-sale feed can include records without cover scans and can rate-limit quickly.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
cp config.example.yaml config.yaml
```

Set credentials:

```bash
export MEURAL_USERNAME="you@example.com"
export MEURAL_PASSWORD="your-password"
```

Start safely:

```bash
reddit-meural sync --config config.yaml --dry-run
```

Then list Meural devices and galleries:

```bash
reddit-meural list-meural --config config.yaml
```

Put either `gallery_id` or `gallery_name` in `config.yaml`. If you want it sent to a specific frame, set `device_name` or `device_id`; names are resolved from Meural’s `/user/devices` endpoint. Then flip `sync.dry_run` to `false` or pass:

```bash
reddit-meural sync --config config.yaml --no-dry-run
```

For a portrait-mounted canvas, keep `images.orientation: vertical` in `config.yaml`. Use `horizontal` for landscape mode, `square` for only square artwork, or `any` to allow every aspect ratio.

## Removing Artwork

To remove an uploaded artwork and prevent future syncs from adding it back, pass either its source artwork ID or its Meural item ID:

```bash
reddit-meural list-artwork --config config.yaml --status uploaded
reddit-meural remove gcd:16556 --config config.yaml --reason "not for the canvas"
reddit-meural remove 123456 --config config.yaml --reason "remove from rotation"
```

The command removes the Meural item from any gallery/playlist where it appears, removes it from the configured device when possible, asks the device to sync, and stores the artwork ID in a local blocklist. You can also block a future item before it has ever uploaded:

```bash
reddit-meural remove gcd:16556 --config config.yaml --dry-run
reddit-meural remove gcd:16556 --config config.yaml --reason "skip this cover"
```

## Docker

```bash
docker build -t reddit-meural .
mkdir -p data
cp config.example.yaml data/config.yaml
docker run --rm \
  -e MEURAL_USERNAME="you@example.com" \
  -e MEURAL_PASSWORD="your-password" \
  -v "$PWD/data:/data" \
  reddit-meural sync --config /data/config.yaml --dry-run
```

## Scheduling

Cron example:

```cron
17 */6 * * * cd /path/to/reddit-meural && . .venv/bin/activate && reddit-meural sync --config config.yaml --no-dry-run >> data/run.log 2>&1
```

Home Assistant can call the same command with `shell_command`, but it is not required.

In the mikado cluster it runs as a Kubernetes CronJob: chart `charts/stable/reddit-meural` in spieg-charts, HelmRelease
`apps/home-assistant/app/reddit-meural-helm-release.yaml` in spieg-clusters (every 6 hours, `/data` on a Pool1
hostPath, credentials from the SOPS `clusterenv.yaml`). The image `spiegela/reddit-meural` is built by
`.github/workflows/docker-release.yml` when a release is published.

## Notes

- Reddit sources can include copyrighted images. This tool preserves source attribution in the metadata description; you should still curate subreddits and usage with care.
- `create_gallery_if_missing` is disabled by default for the main gallery. Source playlists are created by default; set `meural.create_source_playlists` to `false` to keep using only the main gallery.
- ComicVine requires `COMICVINE_API_KEY`. Metron sources use `METRON_USERNAME` and `METRON_PASSWORD` when the endpoint requires authentication.
- GCD sources are disabled in the example config and additionally require `comic_covers.enable_gcd: true`. If you enable them, prefer a short curated `issue_ids` list over weekly discovery.
- If metadata edits fail but upload succeeds, the item remains in the gallery and the run continues.
- Meural API calls retry transient timeouts, `429`, and server errors. Tune `meural.request_timeout_seconds`, `meural.upload_timeout_seconds`, `meural.max_retries`, and `meural.retry_backoff_seconds` if your network or the Meural API is slow.
