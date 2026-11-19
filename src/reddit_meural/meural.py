from __future__ import annotations

import json
import logging
from pathlib import Path
import time
from typing import Any

import boto3
import requests

from .config import MeuralConfig
from .models import ArtworkMetadata, ImageAsset

LOG = logging.getLogger(__name__)

BASE_URL = "https://api.meural.com/v0"
AUTH_CLIENT_REGION = "eu-west-1"
AUTH_CLIENT_ID = "487bd4kvb1fnop6mbgk8gu5ibf"


class MeuralClient:
    def __init__(self, config: MeuralConfig, session: requests.Session | None = None):
        self.config = config
        self.session = session or requests.Session()
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self._load_token_cache()

    def list_devices(self) -> list[dict[str, Any]]:
        return self.request("get", "user/devices", params={"count": 1000})

    def list_galleries(self) -> list[dict[str, Any]]:
        return self.request("get", "user/galleries", params={"count": 1000})

    def resolve_device_id(self) -> int | None:
        if self.config.device_id is not None:
            return int(self.config.device_id)
        if not self.config.device_name:
            return None

        devices = self.list_devices()
        matched_device = _find_named_resource(devices, self.config.device_name)
        if matched_device is None:
            raise ValueError(
                f"Could not find Meural device named {self.config.device_name!r}. "
                "Run list-meural to see available devices."
            )
        return int(matched_device["id"])

    def resolve_gallery_id(self) -> int:
        if self.config.gallery_id is not None:
            return int(self.config.gallery_id)
        galleries = self.list_galleries()
        for gallery in galleries:
            if gallery.get("name") == self.config.gallery_name:
                return int(gallery["id"])
        if self.config.create_gallery_if_missing:
            gallery = self.create_gallery(self.config.gallery_name or "Reddit Curated")
            return int(gallery["id"])
        raise ValueError(
            f"Could not find Meural gallery named {self.config.gallery_name!r}. "
            "Set meural.gallery_id or create it in Meural first."
        )

    def resolve_subreddit_playlist_ids(self, subreddits: list[str]) -> dict[str, int]:
        return self.resolve_source_playlist_ids(
            [
                {
                    "collection": subreddit,
                    "source_name": "Reddit",
                    "subreddit": subreddit,
                }
                for subreddit in subreddits
            ]
        )

    def resolve_source_playlist_ids(self, sources: list[dict[str, str]]) -> dict[str, int]:
        galleries_by_name = {
            gallery.get("name"): gallery
            for gallery in self.list_galleries()
            if gallery.get("name") is not None
        }
        playlist_ids: dict[str, int] = {}

        seen_collections: set[str] = set()
        for source in sources:
            collection = source["collection"]
            normalized_collection = _normalize_collection(collection)
            if normalized_collection in seen_collections:
                continue
            seen_collections.add(normalized_collection)
            name = self.source_playlist_name(source)
            gallery = galleries_by_name.get(name)
            if gallery is None:
                gallery = self.create_gallery(name)
                galleries_by_name[name] = gallery
                LOG.info("Created source playlist %s", name)
            playlist_ids[normalized_collection] = int(gallery["id"])

        return playlist_ids

    def source_playlist_name(self, source: dict[str, str]) -> str:
        if source.get("subreddit") and not getattr(self.config, "create_source_playlists", True):
            return self.subreddit_playlist_name(source["subreddit"])
        return self.config.source_playlist_name_template.format(**source)

    def subreddit_playlist_name(self, subreddit: str) -> str:
        return self.config.subreddit_playlist_name_template.format(subreddit=subreddit)

    def upload_item(self, asset: ImageAsset) -> dict[str, Any]:
        self._ensure_token()
        url = f"{BASE_URL}/items"
        return _unwrap_data(self._retry_upload_item(asset, url))

    def _upload_item_once(self, asset: ImageAsset, url: str) -> requests.Response:
        with open(asset.path, "rb") as image:
            return self.session.post(
                url,
                headers=self._headers(),
                files={"image": (Path(asset.path).name, image, asset.content_type)},
                timeout=self.config.upload_timeout_seconds,
            )

    def _retry_upload_item(self, asset: ImageAsset, url: str) -> requests.Response:
        for attempt in range(self.config.max_retries + 1):
            try:
                response = self._upload_item_once(asset, url)
                if response.status_code == 401:
                    self.access_token = None
                    self._ensure_token()
                    response = self._upload_item_once(asset, url)
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                if not _should_retry_exception(exc) or attempt >= self.config.max_retries:
                    raise
                self._sleep_before_retry("upload item", attempt, exc)
        raise RuntimeError("Unreachable upload retry state")

    def update_item_metadata(self, item_id: int | str, metadata: ArtworkMetadata) -> bool:
        payload = {
            "name": metadata.name,
            "title": metadata.name,
            "artistName": metadata.artist_name,
            "author": metadata.artist_name,
            "description": metadata.description,
        }
        if metadata.medium:
            payload["medium"] = metadata.medium
        if metadata.year:
            payload["year"] = metadata.year

        for method in ("put", "patch"):
            try:
                self.request(method, f"items/{item_id}", json_body=payload)
                return True
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else "unknown"
                LOG.warning("Metadata update via %s failed for item %s: %s", method.upper(), item_id, status)
        return False

    def add_item_to_gallery(self, gallery_id: int | str, item_id: int | str) -> dict[str, Any]:
        try:
            return self.request("post", f"galleries/{gallery_id}/items/{item_id}")
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status == 409:
                LOG.info("Item %s is already in gallery %s", item_id, gallery_id)
                return {}
            raise

    def remove_item_from_gallery(self, gallery_id: int | str, item_id: int | str) -> dict[str, Any]:
        return self.request("delete", f"galleries/{gallery_id}/items/{item_id}")

    def remove_item_from_all_galleries(self, item_id: int | str) -> list[int]:
        removed_gallery_ids: list[int] = []
        item_id_text = str(item_id)
        for gallery in self.list_galleries():
            gallery_id = gallery.get("id")
            gallery_item_ids = {str(value) for value in gallery.get("itemIds") or []}
            if not gallery_item_ids or item_id_text in gallery_item_ids:
                try:
                    self.remove_item_from_gallery(gallery_id, item_id)
                    removed_gallery_ids.append(int(gallery_id))
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    if status not in {404, 409}:
                        raise
        return removed_gallery_ids

    def load_gallery_to_device(self, device_id: int | str, gallery_id: int | str) -> dict[str, Any]:
        return self.request("post", f"devices/{device_id}/galleries/{gallery_id}")

    def load_item_to_device(self, device_id: int | str, item_id: int | str) -> dict[str, Any]:
        return self.request("post", f"devices/{device_id}/items/{item_id}")

    def remove_item_from_device(self, device_id: int | str, item_id: int | str) -> dict[str, Any]:
        return self.request("delete", f"devices/{device_id}/items/{item_id}")

    def sync_device(self, device_id: int | str) -> dict[str, Any]:
        return self.request("post", f"devices/{device_id}/sync")

    def create_gallery(self, name: str) -> dict[str, Any]:
        return self.request("post", "galleries", json_body={"name": name})

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        self._ensure_token()
        method_name = method.upper()
        url = f"{BASE_URL}/{path.lstrip('/')}"
        response = None

        for attempt in range(self.config.max_retries + 1):
            try:
                response = self.session.request(
                    method_name,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=self.config.request_timeout_seconds,
                )
                if response.status_code == 401:
                    self.access_token = None
                    self._ensure_token()
                    response = self.session.request(
                        method_name,
                        url,
                        headers=self._headers(),
                        params=params,
                        json=json_body,
                        timeout=self.config.request_timeout_seconds,
                    )
                if _should_retry_response(response) and attempt < self.config.max_retries:
                    self._sleep_before_retry(path, attempt, response)
                    continue
                response.raise_for_status()
                return _unwrap_data(response)
            except requests.RequestException as exc:
                if not _should_retry_exception(exc) or attempt >= self.config.max_retries:
                    raise
                self._sleep_before_retry(path, attempt, exc)

        if response is not None:
            response.raise_for_status()
        raise RuntimeError("Unreachable request retry state")

    def _sleep_before_retry(
        self,
        operation: str,
        attempt: int,
        cause: requests.Response | requests.RequestException,
    ) -> None:
        delay = _retry_delay(
            cause,
            self.config.retry_backoff_seconds * (2 ** attempt),
        )
        LOG.warning(
            "Retrying Meural %s after %s in %.1fs (attempt %d/%d)",
            operation,
            _retry_cause(cause),
            delay,
            attempt + 1,
            self.config.max_retries,
        )
        time.sleep(delay)

    def _ensure_token(self) -> None:
        if self.access_token:
            return
        if self.refresh_token:
            try:
                self.access_token = self._refresh_access_token()
                self._save_token_cache()
                return
            except Exception as exc:
                LOG.info("Refreshing Meural token failed, using password auth: %s", exc)
        self.access_token, self.refresh_token = self._authenticate()
        self._save_token_cache()

    def _authenticate(self) -> tuple[str, str | None]:
        LOG.info("Authenticating with Meural")
        client = boto3.client("cognito-idp", region_name=AUTH_CLIENT_REGION)
        response = client.initiate_auth(
            ClientId=AUTH_CLIENT_ID,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": self.config.username,
                "PASSWORD": self.config.password,
            },
        )
        auth = response["AuthenticationResult"]
        return auth["AccessToken"], auth.get("RefreshToken")

    def _refresh_access_token(self) -> str:
        client = boto3.client("cognito-idp", region_name=AUTH_CLIENT_REGION)
        response = client.initiate_auth(
            ClientId=AUTH_CLIENT_ID,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": self.refresh_token or ""},
        )
        return response["AuthenticationResult"]["AccessToken"]

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self.access_token}",
            "x-meural-api-version": "3",
        }

    def _load_token_cache(self) -> None:
        path = Path(self.config.token_cache)
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        self.access_token = payload.get("access_token")
        self.refresh_token = payload.get("refresh_token")

    def _save_token_cache(self) -> None:
        path = Path(self.config.token_cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "access_token": self.access_token,
                    "refresh_token": self.refresh_token,
                    "updated_at": int(time.time()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )


def _unwrap_data(response: requests.Response) -> Any:
    if not response.content:
        return {}
    payload = response.json()
    return payload.get("data", payload)


def _find_named_resource(resources: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for resource in resources:
        if resource.get("name") == name or resource.get("alias") == name:
            return resource

    normalized_name = name.casefold()
    for resource in resources:
        candidates = [resource.get("name"), resource.get("alias")]
        if any(
            isinstance(candidate, str) and candidate.casefold() == normalized_name
            for candidate in candidates
        ):
            return resource
    return None


def _normalize_collection(collection: str) -> str:
    return collection.casefold()


def _should_retry_response(response: requests.Response) -> bool:
    return response.status_code == 429 or 500 <= response.status_code < 600


def _should_retry_exception(exc: requests.RequestException) -> bool:
    if isinstance(
        exc,
        (
            requests.Timeout,
            requests.ConnectionError,
        ),
    ):
        return True
    response = getattr(exc, "response", None)
    return response is not None and _should_retry_response(response)


def _retry_delay(
    cause: requests.Response | requests.RequestException,
    fallback: float,
) -> float:
    response = cause if isinstance(cause, requests.Response) else getattr(cause, "response", None)
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
    return min(fallback, 60.0)


def _retry_cause(cause: requests.Response | requests.RequestException) -> str:
    if isinstance(cause, requests.Response):
        return f"HTTP {cause.status_code}"
    response = getattr(cause, "response", None)
    if response is not None:
        return f"HTTP {response.status_code}"
    return cause.__class__.__name__
