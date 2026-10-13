"""Tests for the BrightBean Platform relay behind YouTubeProvider (local patch)."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from providers import platform_relay
from providers.types import PostType, PublishContent, PublishState
from providers.youtube import YouTubeProvider

API = "https://api-platform.brightbean.xyz/api/v1"
ACCOUNT = "acc-youtube-1"


@pytest.fixture
def relay_env(monkeypatch):
    monkeypatch.setenv("PLATFORM_RELAY_KEY", "bb_live_test")
    monkeypatch.setenv("PLATFORM_YOUTUBE_RELAY_ACCOUNT_ID", ACCOUNT)
    monkeypatch.delenv("PLATFORM_TIKTOK_RELAY_KEY", raising=False)
    monkeypatch.delenv("PLATFORM_RELAY_URL", raising=False)


@pytest.fixture
def no_relay(monkeypatch):
    for name in ("PLATFORM_RELAY_KEY", "PLATFORM_TIKTOK_RELAY_KEY", "PLATFORM_YOUTUBE_RELAY_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)


def _resp(payload=None, headers=None):
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload or {})
    resp.content = b"x" if payload is not None else b""
    resp.headers = headers or {}
    return resp


def _post(status, *, url="", error="", caption="Descripción", title="Título #Shorts"):
    return {
        "id": "post-1",
        "caption": caption,
        "title": title,
        "status": status,
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "channels": [{"accountId": ACCOUNT, "platform": "youtube", "status": status, "url": url, "error": error}],
    }


class FakePlatform:
    def __init__(self, listing=None):
        self.calls = []
        self.listing = listing or []
        self.uploads = 0

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.startswith("https://storage.example/"):
            return _resp(headers={"ETag": f'"etag-{url[-1]}"'})
        path = url[len(API) :]
        if path.startswith("/posts/?"):
            return _resp({"data": self.listing})
        if path == "/media/uploads":
            self.uploads += 1
            n = self.uploads
            return _resp(
                {
                    "mediaAssetId": f"asset-{n}",
                    "partSize": 64 * 1024 * 1024,
                    "partCount": 1,
                    "urls": [{"partNumber": 1, "url": f"https://storage.example/part-{n}"}],
                }
            )
        if path.endswith("/complete"):
            return _resp({"id": path.split("/")[3]})
        if path == "/posts/":
            return _resp({"post": _post("draft"), "routed": "draft"})
        if path == "/posts/post-1/publish":
            return _resp({"post": _post("publishing"), "routed": "enacted"})
        raise AssertionError(f"unexpected call {method} {url}")

    def json_of(self, path):
        return next(k["json"] for m, u, k in self.calls if u == f"{API}{path}")


def _content(tmp_path, post_type=PostType.SHORT, **extra):
    video = tmp_path / "short.mp4"
    video.write_bytes(b"\x00" * 2048)
    return PublishContent(
        text="Descripción",
        title="Título",
        description="Descripción",
        post_type=post_type,
        media_files=[str(video)],
        video_duration_sec=23.9,
        extra=extra,
    )


def _provider(fake):
    provider = YouTubeProvider({"client_id": "id", "client_secret": "secret"})
    provider._request = MagicMock(side_effect=fake)
    return provider


class TestSwitch:
    def test_without_settings_youtube_stays_direct_and_synchronous(self, no_relay):
        assert YouTubeProvider({"client_id": "i", "client_secret": "s"}).publish_is_async is False

    def test_with_settings_the_publish_is_settled_later(self, relay_env):
        assert YouTubeProvider({"client_id": "i", "client_secret": "s"}).publish_is_async is True

    def test_the_first_deployments_tiktok_key_still_works(self, monkeypatch):
        monkeypatch.delenv("PLATFORM_RELAY_KEY", raising=False)
        monkeypatch.setenv("PLATFORM_TIKTOK_RELAY_KEY", "bb_live_old")
        monkeypatch.setenv("PLATFORM_YOUTUBE_RELAY_ACCOUNT_ID", ACCOUNT)
        assert platform_relay.settings("youtube")[1] == "bb_live_old"


class TestPublish:
    def test_a_short_goes_with_its_title_description_and_options(self, relay_env, tmp_path):
        fake = FakePlatform()
        provider = _provider(fake)
        content = _content(
            tmp_path,
            privacy_status="unlisted",
            self_declared_made_for_kids=False,
            tags=["propiedad horizontal", " "],
            category_id="22",
        )

        result = provider.publish_post("google-token", content)

        assert result.platform_post_id == "bbp:post-1"
        body = fake.json_of("/posts/")
        assert body["title"] == "Título #Shorts"
        assert body["caption"] == "Descripción"
        assert body["mediaAssetIds"] == ["asset-1"]
        assert body["perAccount"][ACCOUNT]["options"]["youtube"] == {
            "privacyStatus": "unlisted",
            "madeForKids": False,
            "tags": ["propiedad horizontal"],
            "categoryId": "22",
        }
        publish = next(k for m, u, k in fake.calls if u.endswith("/publish"))
        assert publish["headers"]["Idempotency-Key"] == "studio-publish-post-1"

    def test_defaults_match_the_direct_path(self, relay_env, tmp_path):
        fake = FakePlatform()
        provider = _provider(fake)

        provider.publish_post("t", _content(tmp_path, post_type=PostType.VIDEO))

        body = fake.json_of("/posts/")
        assert body["title"] == "Título"  # no #Shorts on a regular video
        assert body["perAccount"][ACCOUNT]["options"]["youtube"] == {"privacyStatus": "public", "madeForKids": False}

    def test_a_custom_thumbnail_is_uploaded_and_attached(self, relay_env, tmp_path):
        thumb = tmp_path / "thumb.jpg"
        thumb.write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
        fake = FakePlatform()
        provider = _provider(fake)

        provider.publish_post("t", _content(tmp_path, thumbnail_file=str(thumb)))

        uploads = [k["json"] for m, u, k in fake.calls if u == f"{API}/media/uploads"]
        assert [u["mediaType"] for u in uploads] == ["video", "image"]
        options = fake.json_of("/posts/")["perAccount"][ACCOUNT]["options"]["youtube"]
        assert options["thumbnail"] == {"assetId": "asset-2"}

    def test_a_retry_reuses_the_post_an_earlier_attempt_left(self, relay_env, tmp_path):
        fake = FakePlatform(listing=[_post("publishing")])
        provider = _provider(fake)

        assert provider.publish_post("t", _content(tmp_path)).platform_post_id == "bbp:post-1"
        assert not any(u.endswith(("/media/uploads", "/posts/", "/publish")) for _, u, _ in fake.calls)

    def test_a_post_with_another_title_is_not_mistaken_for_this_one(self, relay_env, tmp_path):
        fake = FakePlatform(listing=[_post("publishing", title="Otro video #Shorts")])
        provider = _provider(fake)

        provider.publish_post("t", _content(tmp_path))

        assert any(u == f"{API}/posts/" for _, u, _ in fake.calls)

    def test_the_post_type_check_still_comes_first(self, relay_env, tmp_path):
        from providers.exceptions import PublishError

        provider = _provider(FakePlatform())
        with pytest.raises(PublishError):
            provider.publish_post("t", _content(tmp_path, post_type=PostType.IMAGE))
        provider._request.assert_not_called()


class TestStatus:
    @pytest.mark.parametrize(
        ("url", "video_id"),
        [
            ("https://youtu.be/HD95F0AGxlI", "HD95F0AGxlI"),
            ("https://www.youtube.com/watch?v=HD95F0AGxlI", "HD95F0AGxlI"),
            ("https://www.youtube.com/shorts/HD95F0AGxlI", "HD95F0AGxlI"),
            ("", ""),
        ],
    )
    def test_published_reads_the_video_id_from_the_url(self, relay_env, url, video_id):
        provider = YouTubeProvider({"client_id": "i", "client_secret": "s"})
        provider._request = MagicMock(return_value=_resp(_post("published", url=url)))

        status = provider.check_publish_status("t", "bbp:post-1")

        assert status.state is PublishState.COMPLETE
        assert status.platform_post_id == video_id

    def test_failed_and_pending(self, relay_env):
        provider = YouTubeProvider({"client_id": "i", "client_secret": "s"})
        provider._request = MagicMock(return_value=_resp(_post("failed", error="Video rejected")))
        failed = provider.check_publish_status("t", "bbp:post-1")
        assert failed.state is PublishState.FAILED and "Video rejected" in failed.error

        provider._request = MagicMock(return_value=_resp(_post("publishing")))
        assert provider.check_publish_status("t", "bbp:post-1").state is PublishState.PENDING

    def test_a_direct_handle_still_has_no_status_endpoint(self, relay_env):
        provider = YouTubeProvider({"client_id": "i", "client_secret": "s"})
        with pytest.raises(NotImplementedError):
            provider.check_publish_status("t", "dQw4w9WgXcQ")


class TestMediaDimensions:
    def test_the_upload_sends_the_measured_width_and_height(self, relay_env, tmp_path, monkeypatch):
        """Without them a YouTube post stayed "publishing" on the Platform for
        over 20 minutes; with them it was live in 98 s (13-oct-2026)."""
        monkeypatch.setattr(platform_relay, "probe", lambda path: {"width": 1080, "height": 1920, "durationSec": 5.0})
        fake = FakePlatform()
        provider = _provider(fake)

        provider.publish_post("t", _content(tmp_path))

        complete = next(k["json"] for m, u, k in fake.calls if u.endswith("/complete"))
        # The engine's duration (from the media asset) wins over the probe's.
        assert complete == {
            "parts": [{"partNumber": 1, "etag": '"etag-1"'}],
            "width": 1080,
            "height": 1920,
            "durationSec": 23.9,
        }

    def test_probe_reads_ffprobes_json(self, monkeypatch):
        out = MagicMock(stdout='{"streams":[{"width":1080,"height":1920}],"format":{"duration":"23.866667"}}')
        monkeypatch.setattr(platform_relay.subprocess, "run", lambda *a, **k: out)

        assert platform_relay.probe("video.mp4") == {"width": 1080, "height": 1920, "durationSec": 23.867}

    def test_probe_gives_up_quietly(self, monkeypatch):
        def missing(*a, **k):
            raise FileNotFoundError("ffprobe")

        monkeypatch.setattr(platform_relay.subprocess, "run", missing)

        assert platform_relay.probe("video.mp4") == {}
