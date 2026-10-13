"""Tests for the BrightBean Platform relay behind TikTokProvider (local patch)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from providers import tiktok_relay
from providers.exceptions import APIError, PublishError
from providers.tiktok import TikTokProvider
from providers.types import PostType, PublishContent, PublishState

API = "https://api-platform.brightbean.xyz/api/v1"
ACCOUNT = "acc-tiktok-1"


@pytest.fixture
def relay_env(monkeypatch):
    monkeypatch.setenv("PLATFORM_TIKTOK_RELAY_KEY", "bb_live_test")
    monkeypatch.setenv("PLATFORM_TIKTOK_RELAY_ACCOUNT_ID", ACCOUNT)
    monkeypatch.delenv("PLATFORM_TIKTOK_RELAY_URL", raising=False)


def _resp(payload=None, headers=None):
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload or {})
    resp.content = b"x" if payload is not None else b""
    resp.headers = headers or {}
    return resp


CREATOR_INFO = {
    "username": "micondominio.co",
    "nickname": "micondominio",
    "avatarUrl": "https://cdn.example/a.jpg",
    "privacyLevelOptions": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
    "commentDisabled": False,
    "duetDisabled": True,
    "stitchDisabled": False,
    "maxVideoPostDurationSec": 3600,
    "canPost": True,
}


def _post(status, *, url="", error="", caption="Hola", created=None):
    return {
        "id": "post-1",
        "caption": caption,
        "status": status,
        "createdAt": (created or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "channels": [{"accountId": ACCOUNT, "platform": "tiktok", "status": status, "url": url, "error": error}],
    }


class FakePlatform:
    """Routes ``provider._request`` calls like the Platform would, and records them."""

    def __init__(self, listing=None, publish_status="publishing"):
        self.calls = []
        self.listing = listing or []
        self.publish_status = publish_status

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.startswith("https://storage.example/"):
            return _resp(headers={"ETag": '"etag-1"'})
        path = url[len(API) :]
        if path.endswith("/tiktok-creator-info"):
            return _resp(CREATOR_INFO)
        if path.startswith("/posts/?"):
            return _resp({"data": self.listing})
        if path == "/media/uploads":
            return _resp(
                {
                    "mediaAssetId": "asset-1",
                    "partSize": 64 * 1024 * 1024,
                    "partCount": 1,
                    "urls": [{"partNumber": 1, "url": "https://storage.example/part-1"}],
                }
            )
        if path == "/media/uploads/asset-1/complete":
            return _resp({"id": "asset-1", "processingStatus": "ready"})
        if path == "/posts/":
            return _resp({"post": _post("draft"), "routed": "draft"})
        if path == "/posts/post-1/publish":
            return _resp({"post": _post(self.publish_status), "routed": "enacted"})
        raise AssertionError(f"unexpected call {method} {url}")

    def paths(self):
        return [(m, u[len(API) :] if u.startswith(API) else u) for m, u, _ in self.calls]


def _content(tmp_path, **extra):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"\x00" * 1024)
    return PublishContent(
        text="Hola",
        post_type=PostType.VIDEO,
        media_files=[str(video)],
        video_duration_sec=23.9,
        extra={"privacy_level": "PUBLIC_TO_EVERYONE", **extra},
    )


def _provider(fake):
    provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
    provider._request = MagicMock(side_effect=fake)
    return provider


class TestDisabled:
    def test_off_without_key_and_account(self, monkeypatch):
        monkeypatch.delenv("PLATFORM_TIKTOK_RELAY_KEY", raising=False)
        monkeypatch.delenv("PLATFORM_TIKTOK_RELAY_ACCOUNT_ID", raising=False)
        assert tiktok_relay.enabled() is False

    def test_a_native_handle_is_not_a_relay_handle(self):
        assert tiktok_relay.is_relay_handle("v_pub_file~123") is False
        assert tiktok_relay.is_relay_handle("") is False


class TestCreatorInfo:
    def test_the_composer_gets_the_platforms_options_in_tiktoks_shape(self, relay_env):
        provider = _provider(FakePlatform())

        info = provider.query_creator_info("sandbox-token")

        assert info["privacy_level_options"] == ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]
        assert info["creator_nickname"] == "micondominio.co"
        assert info["duet_disabled"] is True and info["comment_disabled"] is False
        assert info["max_video_post_duration_sec"] == 3600
        method, url, kwargs = provider._request.call_args_list[0][0] + (provider._request.call_args_list[0][1],)
        assert url == f"{API}/accounts/{ACCOUNT}/tiktok-creator-info"
        assert kwargs["access_token"] == "bb_live_test"  # the Platform key, not the sandbox token

    def test_cannot_post_means_no_options(self, relay_env):
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider._request = MagicMock(return_value=_resp({**CREATOR_INFO, "canPost": False}))

        assert provider.query_creator_info("t")["privacy_level_options"] == []


class TestPublish:
    def test_uploads_creates_and_publishes_with_the_posts_options(self, relay_env, tmp_path):
        fake = FakePlatform()
        provider = _provider(fake)
        content = _content(
            tmp_path,
            disable_comment=False,
            disable_duet=True,
            disable_stitch=True,
            brand_organic_toggle=True,
            is_aigc=False,
            video_cover_timestamp_ms=22400,
        )

        result = provider.publish_post("sandbox-token", content)

        assert result.platform_post_id == "bbp:post-1"
        assert [p for _, p in fake.paths() if not p.startswith("https://")] == [
            f"/accounts/{ACCOUNT}/tiktok-creator-info",
            f"/posts/?accountId={ACCOUNT}&limit=20",
            "/media/uploads",
            "/media/uploads/asset-1/complete",
            "/posts/",
            "/posts/post-1/publish",
        ]
        create = next(k for m, u, k in fake.calls if u == f"{API}/posts/")
        assert create["json"]["perAccount"][ACCOUNT]["options"]["tiktok"] == {
            "privacyLevel": "PUBLIC_TO_EVERYONE",
            "allowComment": True,
            "allowDuet": False,
            "allowStitch": False,
            "disclose": True,
            "brandOrganic": True,
            "brandContent": False,
            "aiGenerated": False,
            "coverMs": 22400,
        }
        assert create["json"]["mediaAssetIds"] == ["asset-1"]
        publish = next(k for m, u, k in fake.calls if u.endswith("/publish"))
        assert publish["headers"]["Idempotency-Key"] == "studio-publish-post-1"
        put = next(k for m, u, k in fake.calls if u.startswith("https://storage.example/"))
        assert put.get("access_token") is None  # presigned URL: no Authorization
        complete = next(k for m, u, k in fake.calls if u.endswith("/complete"))
        assert complete["json"] == {"parts": [{"partNumber": 1, "etag": '"etag-1"'}], "durationSec": 23.9}

    def test_a_retry_reuses_a_post_already_on_its_way_instead_of_posting_twice(self, relay_env, tmp_path):
        fake = FakePlatform(listing=[_post("publishing")])
        provider = _provider(fake)

        result = provider.publish_post("t", _content(tmp_path))

        assert result.platform_post_id == "bbp:post-1"
        assert not any(p in ("/media/uploads", "/posts/") or p.endswith("/publish") for _, p in fake.paths())

    def test_a_retry_publishes_a_draft_an_earlier_attempt_left(self, relay_env, tmp_path):
        fake = FakePlatform(listing=[_post("draft")])
        provider = _provider(fake)

        provider.publish_post("t", _content(tmp_path))

        paths = [p for _, p in fake.paths()]
        assert "/posts/post-1/publish" in paths
        assert "/media/uploads" not in paths and "/posts/" not in paths

    def test_an_old_or_failed_post_with_the_same_caption_is_not_reused(self, relay_env, tmp_path):
        old = _post("published", created=datetime.now(UTC) - timedelta(days=2))
        failed = _post("failed")
        fake = FakePlatform(listing=[old, failed])
        provider = _provider(fake)

        provider.publish_post("t", _content(tmp_path))

        assert "/posts/" in [p for _, p in fake.paths()]

    def test_a_refusal_is_reported_once_in_the_platforms_words(self, relay_env, tmp_path):
        fake = FakePlatform()

        def refuse(method, url, **kwargs):
            if url == f"{API}/posts/":
                err = APIError(
                    "422",
                    platform="tiktok",
                    status_code=422,
                    raw_response={"error": "invalid", "detail": "coverMs is past the end of the video"},
                )
                raise err
            return fake(method, url, **kwargs)

        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider._request = MagicMock(side_effect=refuse)

        with pytest.raises(PublishError) as raised:
            provider.publish_post("t", _content(tmp_path))
        assert raised.value.retryable is False
        assert "coverMs is past the end of the video" in str(raised.value)

    def test_without_a_local_file_it_fails_clearly(self, relay_env):
        provider = _provider(FakePlatform())
        content = PublishContent(
            text="Hola",
            post_type=PostType.VIDEO,
            media_urls=["https://cdn.example/v.mp4"],
            extra={"privacy_level": "PUBLIC_TO_EVERYONE"},
        )
        with pytest.raises(PublishError):
            provider.publish_post("t", content)


class TestStatus:
    @pytest.mark.parametrize(
        ("post", "state", "video_id"),
        [
            (
                _post("published", url="https://www.tiktok.com/@micondominio.co/video/7561234567890123456"),
                PublishState.COMPLETE,
                "7561234567890123456",
            ),
            (_post("publishing"), PublishState.PENDING, ""),
            (_post("failed", error="Video too short"), PublishState.FAILED, ""),
        ],
    )
    def test_maps_the_platforms_channel_status(self, relay_env, post, state, video_id):
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider._request = MagicMock(return_value=_resp(post))

        status = provider.check_publish_status("t", "bbp:post-1")

        assert status.state is state
        assert status.platform_post_id == video_id
        if state is PublishState.FAILED:
            assert "Video too short" in status.error
        assert provider._request.call_args[0][1] == f"{API}/posts/post-1"

    def test_a_relayed_handle_settles_even_with_the_relay_turned_off(self, monkeypatch):
        # A post handed over before the relay was switched off still has to
        # settle; only the key is needed to ask.
        monkeypatch.setenv("PLATFORM_TIKTOK_RELAY_KEY", "bb_live_test")
        monkeypatch.delenv("PLATFORM_TIKTOK_RELAY_ACCOUNT_ID", raising=False)
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider._request = MagicMock(return_value=_resp(_post("publishing")))

        assert provider.check_publish_status("t", "bbp:post-1").state is PublishState.PENDING

    def test_analytics_resolve_a_relayed_post_to_its_video_id(self, relay_env):
        provider = TikTokProvider({"client_key": "k", "client_secret": "s"})
        provider._request = MagicMock(
            return_value=_resp(
                _post("published", url="https://www.tiktok.com/@micondominio.co/video/7561234567890123456")
            )
        )

        assert provider._resolve_video_id("t", "bbp:post-1") == "7561234567890123456"
