from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import requests

from reddit_meural.comics import ComicCoverClient
from reddit_meural.config import (
    ComicCoversConfig,
    ComicCoverSourceConfig,
    MeuralConfig,
    MetadataConfig,
)
from reddit_meural.images import _matches_orientation
from reddit_meural.meural import MeuralClient
from reddit_meural.metadata import build_metadata
from reddit_meural.models import RedditPost
from reddit_meural.reddit import best_image_url
from reddit_meural.state import StateStore
from reddit_meural.sync import _try_load_gallery_to_device


class FakeMeuralClient(MeuralClient):
    def __init__(self) -> None:
        self.config = MeuralConfig(
            username="user",
            password="password",
            create_source_playlists=False,
            subreddit_playlist_name_template="From r/{subreddit}",
        )
        self.galleries = [{"id": 10, "name": "From r/ArtPorn"}]
        self.created_names: list[str] = []

    def list_galleries(self) -> list[dict]:
        return self.galleries

    def create_gallery(self, name: str) -> dict:
        self.created_names.append(name)
        gallery = {"id": 10 + len(self.galleries), "name": name}
        self.galleries.append(gallery)
        return gallery


class FakeDeviceMeuralClient(MeuralClient):
    def __init__(
        self,
        *,
        device_id: int | None = None,
        device_name: str | None = None,
    ) -> None:
        self.config = MeuralConfig(
            username="user",
            password="password",
            device_id=device_id,
            device_name=device_name,
        )
        self.list_devices_called = False

    def list_devices(self) -> list[dict]:
        self.list_devices_called = True
        return [
            {"id": 20, "name": "Studio Canvas"},
            {"id": 21, "alias": "Kitchen Frame", "name": "Canvas II"},
        ]


class FakeResponse:
    def __init__(
        self,
        payload: dict,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.content = b"{}" if payload else b""

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.payload


class FakeRateLimitedSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict | None = None, auth=None, timeout=None) -> FakeResponse:
        return FakeResponse({}, status_code=429)


class FakeComicSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.requests: list[tuple[str, dict | None]] = []

    def get(self, url: str, params: dict | None = None, auth=None, timeout=None) -> FakeResponse:
        self.requests.append((url, params))
        return FakeResponse(
            {
                "api_url": "https://www.comics.org/api/issue/16556/?format=json",
                "series_name": "Fantastic Four (1961 series)",
                "descriptor": "1",
                "key_date": "1961-11-00",
                "cover": "https://files1.comics.org/img/gcd/covers_by_id/21/w400/21867.jpg",
                "story_set": [
                    {
                        "type": "cover",
                        "pencils": "Jack Kirby",
                        "inks": "George Klein",
                    }
                ],
            }
        )


class FakeTimeoutMeuralSession:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, method, url, headers=None, params=None, json=None, timeout=None) -> FakeResponse:
        self.calls += 1
        if self.calls == 1:
            raise requests.ReadTimeout("slow meural")
        return FakeResponse({"data": {"ok": True}})


class FakeBadDeviceLoadMeuralClient:
    def load_gallery_to_device(self, device_id, gallery_id):
        response = FakeResponse({}, status_code=400)
        error = requests.HTTPError("bad request")
        error.response = response
        raise error


class RedditMeuralTests(unittest.TestCase):
    def test_best_image_url_prefers_direct_image(self) -> None:
        self.assertEqual(
            best_image_url({"url": "https://i.redd.it/example.jpg"}),
            "https://i.redd.it/example.jpg",
        )

    def test_best_image_url_uses_gallery_metadata(self) -> None:
        post = {
            "gallery_data": {"items": [{"media_id": "abc"}]},
            "media_metadata": {
                "abc": {
                    "s": {
                        "u": "https://preview.redd.it/a.jpg?width=1000&amp;format=pjpg"
                    }
                }
            },
        }
        self.assertEqual(
            best_image_url(post),
            "https://preview.redd.it/a.jpg?width=1000&format=pjpg",
        )

    def test_orientation_filter_matches_canvas_modes(self) -> None:
        self.assertTrue(_matches_orientation(900, 1400, "vertical"))
        self.assertTrue(_matches_orientation(1400, 900, "horizontal"))
        self.assertTrue(_matches_orientation(1000, 1000, "square"))
        self.assertTrue(_matches_orientation(900, 1400, "portrait"))
        self.assertFalse(_matches_orientation(1400, 900, "vertical"))
        self.assertTrue(_matches_orientation(1400, 900, "any"))

    def test_orientation_filter_rejects_unknown_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "images.orientation"):
            _matches_orientation(900, 1400, "diagonal")

    def test_build_metadata_templates(self) -> None:
        post = RedditPost(
            id="abc123",
            subreddit="ArtPorn",
            title="A painting",
            author="artistfan",
            score=1234,
            permalink="https://reddit.test/r/ArtPorn/comments/abc123",
            created_utc=0,
            image_url="https://example.test/image.jpg",
            over_18=False,
            spoiler=False,
        )
        metadata = build_metadata(
            post,
            MetadataConfig(
                title_template="{title}",
                artist_template="u/{author}",
                description_template="{score} from r/{subreddit}",
            ),
        )
        self.assertEqual(metadata.name, "A painting")
        self.assertEqual(metadata.artist_name, "u/artistfan")
        self.assertEqual(metadata.description, "1234 from r/ArtPorn")

    def test_build_metadata_exposes_generic_source_values(self) -> None:
        post = RedditPost(
            id="gcd:16556",
            subreddit="Classic Comic Covers",
            title="Fantastic Four (1961 series) #1",
            author="Jack Kirby",
            score=0,
            permalink="https://www.comics.org/issue/16556/",
            created_utc=1961,
            image_url="https://example.test/cover.jpg",
            over_18=False,
            spoiler=False,
            source_type="gcd",
            source_name="Classic Comic Covers",
        )

        metadata = build_metadata(
            post,
            MetadataConfig(
                artist_template="{author} via {source_name}",
                description_template="{collection} from {source_type}",
            ),
        )

        self.assertEqual(metadata.artist_name, "Jack Kirby via Classic Comic Covers")
        self.assertEqual(metadata.description, "Classic Comic Covers from gcd")

    def test_resolve_subreddit_playlist_ids_reuses_existing_and_creates_missing(self) -> None:
        client = FakeMeuralClient()

        playlist_ids = client.resolve_subreddit_playlist_ids(
            ["ArtPorn", "artporn", "EarthPorn"]
        )

        self.assertEqual(playlist_ids, {"artporn": 10, "earthporn": 11})
        self.assertEqual(client.created_names, ["From r/EarthPorn"])

    def test_resolve_device_id_prefers_configured_id(self) -> None:
        client = FakeDeviceMeuralClient(device_id=99, device_name="Studio Canvas")

        self.assertEqual(client.resolve_device_id(), 99)
        self.assertFalse(client.list_devices_called)

    def test_resolve_device_id_matches_name_or_alias(self) -> None:
        named_client = FakeDeviceMeuralClient(device_name="studio canvas")
        alias_client = FakeDeviceMeuralClient(device_name="Kitchen Frame")

        self.assertEqual(named_client.resolve_device_id(), 20)
        self.assertEqual(alias_client.resolve_device_id(), 21)

    def test_resolve_device_id_raises_for_unknown_name(self) -> None:
        client = FakeDeviceMeuralClient(device_name="Bedroom")

        with self.assertRaisesRegex(ValueError, "Could not find Meural device"):
            client.resolve_device_id()

    def test_meural_request_retries_read_timeout(self) -> None:
        session = FakeTimeoutMeuralSession()
        client = MeuralClient(
            MeuralConfig(
                username="user",
                password="password",
                token_cache=str(Path(tempfile.gettempdir()) / "missing-meural-token.json"),
                max_retries=1,
                retry_backoff_seconds=0,
            ),
            session=session,
        )
        client.access_token = "token"

        self.assertEqual(client.add_item_to_gallery(1, 2), {"ok": True})
        self.assertEqual(session.calls, 2)

    def test_device_gallery_load_bad_request_is_nonfatal(self) -> None:
        self.assertFalse(_try_load_gallery_to_device(FakeBadDeviceLoadMeuralClient(), 7382, 622029))

    def test_gcd_issue_source_builds_comic_cover_post(self) -> None:
        session = FakeComicSession()
        client = ComicCoverClient(
            ComicCoversConfig(
                enabled=True,
                enable_gcd=True,
                sources=[
                    ComicCoverSourceConfig(
                        name="Classic Comic Covers",
                        provider="gcd",
                        issue_ids=[16556],
                    )
                ],
            ),
            session=session,
        )

        posts = client.iter_posts()

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].id, "gcd:16556")
        self.assertEqual(posts[0].source_name, "Classic Comic Covers")
        self.assertEqual(posts[0].title, "Fantastic Four (1961 series) #1")
        self.assertEqual(posts[0].author, "Jack Kirby; George Klein")

    def test_gcd_issue_without_cover_is_skipped(self) -> None:
        client = ComicCoverClient(ComicCoversConfig())

        post = client._try_gcd_issue_to_post(
            ComicCoverSourceConfig(name="This Week's Comic Covers"),
            {
                "api_url": "https://www.comics.org/api/issue/2831727/?format=json",
                "series_name": "2000 AD (2001 series)",
                "descriptor": "2480",
            },
        )

        self.assertIsNone(post)

    def test_rate_limited_comic_api_request_is_skipped(self) -> None:
        client = ComicCoverClient(ComicCoversConfig(), session=FakeRateLimitedSession())

        self.assertEqual(client._get_url("https://example.test/api/issue/1"), {})

    def test_blocked_item_is_treated_as_already_done(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = StateStore(str(Path(directory) / "state.sqlite3"))

            state.mark_blocked("gcd:16556", item_id=123, reason="not for canvas")

            self.assertTrue(state.already_done("gcd:16556"))
            row = state.find_uploaded("gcd:16556")
            self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
