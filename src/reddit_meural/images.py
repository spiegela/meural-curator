from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

from PIL import Image, ImageOps
import requests

from .config import ImageConfig
from .models import ImageAsset, RedditPost

LOG = logging.getLogger(__name__)


def download_and_prepare(
    post: RedditPost,
    config: ImageConfig,
    session: requests.Session | None = None,
) -> ImageAsset:
    session = session or requests.Session()
    Path(config.cache_dir).mkdir(parents=True, exist_ok=True)

    tmp_path = Path(config.cache_dir) / f"{post.id}.download"
    final_path = Path(config.cache_dir) / f"{post.id}-{slugify(post.title)}.jpg"

    if final_path.exists():
        return _asset_from_existing(final_path)

    LOG.info("Downloading %s", post.image_url)
    with session.get(post.image_url, stream=True, timeout=45) as response:
        response.raise_for_status()
        max_bytes = config.max_download_mb * 1024 * 1024
        downloaded = 0
        with open(tmp_path, "wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    raise ValueError(f"Image exceeded {config.max_download_mb} MB")
                handle.write(chunk)

    try:
        with Image.open(tmp_path) as image:
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            if width < config.min_width or height < config.min_height:
                raise ValueError(f"Image too small: {width}x{height}")
            if not _matches_orientation(width, height, config.orientation):
                raise ValueError(
                    f"Image orientation is {_orientation(width, height)}, "
                    f"wanted {config.orientation}"
                )
            image.thumbnail(
                (config.max_dimension, config.max_dimension),
                Image.Resampling.LANCZOS,
            )
            if image.mode not in ("RGB", "L"):
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode == "RGBA":
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image)
                image = background
            elif image.mode == "L":
                image = image.convert("RGB")
            image.save(final_path, format="JPEG", quality=config.jpeg_quality, optimize=True)
    finally:
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass

    return _asset_from_existing(final_path)


def _asset_from_existing(path: Path) -> ImageAsset:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    with Image.open(path) as image:
        width, height = image.size
    return ImageAsset(
        path=str(path),
        sha256=digest.hexdigest(),
        width=width,
        height=height,
    )


def _matches_orientation(width: int, height: int, wanted: str) -> bool:
    wanted = wanted.lower()
    if wanted in {"any", "all", ""}:
        return True
    if wanted not in {"vertical", "portrait", "horizontal", "landscape", "square"}:
        raise ValueError(
            "images.orientation must be one of: any, vertical, horizontal, square"
        )
    actual = _orientation(width, height)
    if wanted == "portrait":
        wanted = "vertical"
    if wanted == "landscape":
        wanted = "horizontal"
    return actual == wanted


def _orientation(width: int, height: int) -> str:
    if height > width:
        return "vertical"
    if width > height:
        return "horizontal"
    return "square"


def slugify(value: str, max_length: int = 72) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return (value or "image")[:max_length].strip("-")
