from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest

import requests

from meural_curator.comics import BearerAuth, ComicCoverClient, _resolve_dates
from meural_curator.config import (
    ComicCoversConfig,
    ComicCoverSourceConfig,
    MeuralConfig,
    MetadataConfig,
    MuseumsConfig,
    MuseumSourceConfig,
    RedditConfig,
)
from meural_curator.images import _matches_orientation
from meural_curator.meural import MeuralClient
from meural_curator.metadata import build_metadata
from meural_curator.models import RedditPost, interleave
from meural_curator.museums import MuseumClient, year_of
from meural_curator.reddit import RedditClient, best_image_url
from meural_curator.state import StateStore
from meural_curator.sync import _try_load_gallery_to_device


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
        if self.status_code >= 400 and self.status_code != 429:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self.payload


class FakeRateLimitedSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, url: str, params: dict | None = None, auth=None, timeout=None) -> FakeResponse:
        return FakeResponse({}, status_code=429)


class FakeRedditSession:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.fail = fail or set()
        self.token_requests: list[tuple] = []
        self.gets: list[tuple[str, dict, dict]] = []

    def post(self, url, auth=None, data=None, headers=None, timeout=None) -> FakeResponse:
        self.token_requests.append((url, auth, data, headers))
        return FakeResponse({"access_token": "tok", "expires_in": 86400})

    def get(self, url, params=None, headers=None, timeout=None) -> FakeResponse:
        self.gets.append((url, params, headers))
        subreddit = url.split("/r/")[1].split("/")[0]
        if subreddit in self.fail:
            raise requests.HTTPError("403 Blocked")
        post = {"id": f"{subreddit}1", "title": "Art", "author": "a", "score": 5000, "permalink": "/r/x/1",
                "url": f"https://i.redd.it/{subreddit}.jpg", "subreddit": subreddit}
        return FakeResponse({"data": {"children": [{"data": post}]}})


class FakeMuseumSession:
    """Canned AIC and Met responses; anything else (Cleveland here) raises."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None) -> FakeResponse:
        self.calls.append((url, params or {}, headers or {}))
        if "artic.edu" in url:
            return FakeResponse({"pagination": {"total_pages": 1}, "data": [
                {"id": 1, "title": "Tall", "artist_display": "Painter A\nFrench, 1850", "date_display": "c. 1885",
                 "medium_display": "Oil on canvas", "image_id": "abc", "thumbnail": {"width": 800, "height": 1200}},
                {"id": 2, "title": "Wide", "image_id": "def", "thumbnail": {"width": 1200, "height": 800}},
                {"id": 3, "title": "No image", "image_id": None},
            ]})
        if "query.wikidata.org" in url:
            row = lambda qid, w, h: {"w": {"value": f"http://www.wikidata.org/entity/{qid}"}, "title": {"value": qid},  # noqa: E731
                                     "image": {"value": f"http://commons.wikimedia.org/wiki/Special:FilePath/{qid}.jpg"},
                                     "date": {"value": "1889-01-01T00:00:00Z"}, "width": {"value": w}, "height": {"value": h}}
            return FakeResponse({"results": {"bindings": [row("Q1", "73", "92"), row("Q2", "120", "60")]}})
        if url.endswith("/v1.1/search"):
            return FakeResponse({"total": 2, "objectIDs": [10, 11]})
        if "/objects/10" in url:
            return FakeResponse({"isPublicDomain": True, "primaryImage": "https://images.met/10.jpg", "title": "Met PD",
                                 "artistDisplayName": "Painter B", "objectURL": "https://met/10", "objectDate": "1518"})
        if "/objects/11" in url:
            return FakeResponse({"isPublicDomain": False, "primaryImage": "", "title": "In copyright"})
        raise requests.ConnectionError("cleveland down")


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

    def test_failing_comic_source_does_not_stop_others(self) -> None:
        client = ComicCoverClient(
            ComicCoversConfig(
                enabled=True,
                enable_gcd=True,
                sources=[
                    ComicCoverSourceConfig(name="No key", provider="comicvine", params={"sort": "cover_date:desc"}),
                    ComicCoverSourceConfig(name="Classic", provider="gcd", issue_ids=[16556]),
                ],
            ),
            session=FakeComicSession(),
        )

        self.assertEqual([post.id for post in client.iter_posts()], ["gcd:16556"])

    def test_comic_params_expand_relative_dates(self) -> None:
        self.assertEqual(
            _resolve_dates(
                {"filter": "cover_date:{today-30}|{today+60}", "after": "{today}", "limit": 5},
                today=date(2026, 11, 18),
            ),
            {"filter": "cover_date:2026-10-19|2027-01-17", "after": "2026-11-18", "limit": 5},
        )

    def test_metron_prefers_api_token_over_basic_auth(self) -> None:
        both = ComicCoverClient(ComicCoversConfig(metron_api_token="tok", metron_username="u", metron_password="p"))
        auth = both._metron_auth()
        self.assertIsInstance(auth, BearerAuth)
        request = auth(requests.Request("GET", "https://metron.cloud/api/issue/").prepare())
        self.assertEqual(request.headers["Authorization"], "Bearer tok")
        self.assertEqual(ComicCoverClient(ComicCoversConfig(metron_username="u", metron_password="p"))._metron_auth(), ("u", "p"))
        self.assertIsNone(ComicCoverClient(ComicCoversConfig())._metron_auth())

    def test_reddit_reads_through_oauth_app_token(self) -> None:
        session = FakeRedditSession(fail={"Broken"})
        client = RedditClient(
            RedditConfig(subreddits=["ArtPorn", "Broken", "Museum"], client_id="id", client_secret="sec", user_agent="ua"),
            session=session,
        )

        posts = client.iter_posts()

        self.assertEqual(sorted(post.id for post in posts), ["ArtPorn1", "Museum1"])
        self.assertEqual(len(session.token_requests), 1)
        url, auth, data, headers = session.token_requests[0]
        self.assertEqual((url, auth, data), ("https://www.reddit.com/api/v1/access_token", ("id", "sec"), {"grant_type": "client_credentials"}))
        self.assertEqual(session.gets[0][0], "https://oauth.reddit.com/r/ArtPorn/top")
        self.assertEqual(session.gets[0][2], {"User-Agent": "ua", "Authorization": "bearer tok"})
        self.assertNotIn("Authorization", session.headers)

    def test_reddit_without_app_credentials_is_skipped(self) -> None:
        session = FakeRedditSession()
        self.assertEqual(RedditClient(RedditConfig(subreddits=["ArtPorn"]), session=session).iter_posts(), [])
        self.assertEqual(session.gets, [])

    def test_museum_sources_map_filter_and_isolate_failures(self) -> None:
        session = FakeMuseumSession()
        client = MuseumClient(
            MuseumsConfig(enabled=True, sources=[
                MuseumSourceConfig(name="Impressionism", provider="aic", query="impressionism"),
                MuseumSourceConfig(name="Paintings", provider="met", random_page=False),
                MuseumSourceConfig(name="Portraits", provider="cleveland"),
            ]),
            orientation="vertical",
            session=session,
        )

        posts = client.iter_posts()

        self.assertEqual([post.id for post in posts], ["aic:1", "met:10"])  # wide, imageless, non-PD, failing: dropped
        aic, met = posts
        self.assertEqual(aic.image_url, "https://www.artic.edu/iiif/2/abc/full/1686,/0/default.jpg")
        self.assertEqual((aic.author, aic.source_name, aic.subreddit, aic.medium), ("Painter A", "Art Institute of Chicago", "Impressionism", "Oil on canvas"))
        self.assertEqual(session.calls[0][1]["query[term][is_public_domain]"], "true")
        self.assertIn("AIC-User-Agent", session.calls[0][2])
        self.assertEqual((met.title, met.permalink, met.source_name), ("Met PD", "https://met/10", "The Met"))
        self.assertEqual(session.calls[1][1]["q"], "*")

    def test_museum_request_retries_once_on_gateway_error(self) -> None:
        class Flaky:
            def __init__(self) -> None:
                self.headers: dict[str, str] = {}
                self.codes = [504, 200]

            def get(self, url, params=None, headers=None, timeout=None) -> FakeResponse:
                return FakeResponse({"ok": True}, status_code=self.codes.pop(0))

        self.assertEqual(MuseumClient(MuseumsConfig(), session=Flaky())._get("https://museum.test/api"), {"ok": True})

    def test_wikidata_collection_source(self) -> None:
        session = FakeMuseumSession()
        source = MuseumSourceConfig(name="Collection", provider="wikidata", collection="Q1976985", museum="Nelson-Atkins Museum of Art")
        posts = MuseumClient(MuseumsConfig(enabled=True, sources=[source]), orientation="vertical", session=session).iter_posts()

        self.assertEqual([post.id for post in posts], ["wikidata:Q1"])  # Q2 is wider than tall
        post = posts[0]
        self.assertEqual(post.image_url, "https://commons.wikimedia.org/wiki/Special:FilePath/Q1.jpg?width=1920")
        self.assertEqual((post.source_name, post.date, post.permalink), ("Nelson-Atkins Museum of Art", "1889", "https://www.wikidata.org/wiki/Q1"))
        self.assertIn("wdt:P195 wd:Q1976985", session.calls[0][1]["query"])
        self.assertIn("wdt:P31 wd:Q3305213", session.calls[0][1]["query"])

    def test_metadata_source_overrides_and_museum_year(self) -> None:
        post = RedditPost(id="aic:1", subreddit="Impressionism", title="Tall", author="Painter A", score=0,
                          permalink="https://aic/1", created_utc=0, image_url="x", over_18=False, spoiler=False,
                          source_type="aic", source_name="Art Institute of Chicago", date="c. 1885", medium="Oil on canvas")
        config = MetadataConfig(artist_template="u/{author} via r/{subreddit}", medium="Digital image",
                                sources={"museum": {"artist_template": "{author}", "description_template": "{title}, {date}"},
                                         "aic": {"description_template": "{title} ({source_name})"}})
        metadata = build_metadata(post, config)
        self.assertEqual((metadata.artist_name, metadata.description), ("Painter A", "Tall (Art Institute of Chicago)"))
        self.assertEqual((metadata.medium, metadata.year), ("Oil on canvas", 1885))
        self.assertEqual(year_of("1635 or earlier"), 1635)
        self.assertIsNone(year_of("Ming dynasty"))

    def test_state_adopts_database_from_before_the_rename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            StateStore(str(Path(directory) / "reddit-meural.sqlite3")).mark_blocked("gcd:1", item_id=1, reason="x")

            state = StateStore(str(Path(directory) / "meural-curator.sqlite3"))

            self.assertTrue(state.already_done("gcd:1"))
            self.assertFalse((Path(directory) / "reddit-meural.sqlite3").exists())

    def test_interleave_alternates_candidate_lists(self) -> None:
        self.assertEqual(interleave([1, 2, 3, 4], ["a", "b"], []), [1, "a", 2, "b", 3, 4])

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
