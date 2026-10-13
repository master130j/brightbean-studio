"""Publish to TikTok through BrightBean Platform (a local patch, not upstream).

TikTok lets an API client post publicly only after an audit, and its Content
Sharing Guidelines rule out "a utility tool to help upload contents to the
account(s) you or your team manages" — which is what a self-hosted Studio
connected to its owner's own account is. Until audited, every post is
``SELF_ONLY`` and the account itself has to be private.

BrightBean Platform (api-platform.brightbean.xyz) runs an audited TikTok app.
With ``PLATFORM_TIKTOK_RELAY_KEY`` and ``PLATFORM_TIKTOK_RELAY_ACCOUNT_ID`` set,
this instance keeps its own TikTok connection, but:

- the composer's creator info (privacy options, interaction limits, the
  nickname it shows) comes from the Platform's ``tiktok-creator-info``;
- at the scheduled time the video is uploaded to the Platform, a draft is
  created there with the post's TikTok options, and it is published now;
- the publish handle is ``bbp:<platform post id>``, and
  :func:`check_status` settles it from the Platform's post.

Nothing here publishes on its own: it runs only when this instance's engine
publishes a post a person scheduled. The key needs ``create_posts``,
``upload_media`` and ``publish_directly``, limited to the TikTok channel.

API reference: https://github.com/brightbeanxyz/brightbean-platform-agent
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

from .exceptions import APIError, PublishError
from .types import PublishContent, PublishResult, PublishState, PublishStatus

logger = logging.getLogger(__name__)

HANDLE_PREFIX = "bbp:"
DEFAULT_BASE_URL = "https://api-platform.brightbean.xyz"
# Cloudflare in front of the Platform rejects some default user agents (1010).
USER_AGENT = "brightbean-studio-tiktok-relay/1.0"
VIDEO_ID = re.compile(r"/video/(\d+)")

# How far back a post on the Platform with the same caption counts as this one.
# The engine retries a failed publish after 1, 5 and 30 minutes; an earlier
# attempt may have reached the Platform even though it raised here.
RECENT_WINDOW = timedelta(hours=6)

# Studio's composer stores "disable_*"; the Platform takes "allow*".
INTERACTION_FLAGS = (
    ("disable_comment", "allowComment"),
    ("disable_duet", "allowDuet"),
    ("disable_stitch", "allowStitch"),
)

# Platform channel statuses that mean the video is (or is about to be) live.
LIVE_OR_ON_ITS_WAY = {"publishing", "published", "scheduled", "queued"}


def _settings() -> tuple[str, str, str]:
    """``(api_base, key, account_id)``, read at call time so tests and a
    restarted worker always see the current environment."""
    base = os.environ.get("PLATFORM_TIKTOK_RELAY_URL", "").strip() or DEFAULT_BASE_URL
    return (
        f"{base.rstrip('/')}/api/v1",
        os.environ.get("PLATFORM_TIKTOK_RELAY_KEY", "").strip(),
        os.environ.get("PLATFORM_TIKTOK_RELAY_ACCOUNT_ID", "").strip(),
    )


def enabled() -> bool:
    _, key, account_id = _settings()
    return bool(key and account_id)


def is_relay_handle(handle: str) -> bool:
    return bool(handle) and handle.startswith(HANDLE_PREFIX)


def _call(
    provider,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    idempotency_key: str | None = None,
    timeout: float = 60.0,
) -> dict:
    api_base, key, _ = _settings()
    if not key:
        raise PublishError(
            "PLATFORM_TIKTOK_RELAY_KEY is not set, so this post can't be handed to BrightBean Platform.",
            platform="tiktok",
            retryable=False,
        )
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        response = provider._request(
            method, f"{api_base}{path}", access_token=key, headers=headers, json=json, timeout=timeout
        )
    except APIError as exc:
        status = getattr(exc, "status_code", None)
        # A 4xx other than a timeout or a conflict will be the same on every
        # retry (a bad option, an unfinished upload, a revoked key): say so
        # once, in the Platform's own words, instead of burning the backoff.
        if status and 400 <= status < 500 and status not in (408, 409):
            detail = (getattr(exc, "raw_response", None) or {}).get("detail") or str(exc)
            raise PublishError(
                f"BrightBean Platform refused the TikTok post: {detail}",
                platform="tiktok",
                raw_response=getattr(exc, "raw_response", None),
                retryable=False,
            ) from exc
        raise
    return response.json() if response.content else {}


def creator_info(provider) -> dict:
    """The Platform's creator info, in the shape of TikTok's own response."""
    _, _, account_id = _settings()
    data = _call(provider, "GET", f"/accounts/{account_id}/tiktok-creator-info")
    can_post = data.get("canPost", True) is not False
    return {
        "creator_nickname": data.get("username") or data.get("nickname") or "",
        "creator_avatar_url": data.get("avatarUrl") or "",
        # TikTok's own way of saying "you can't post right now" is an empty
        # list of options; the composer then shows nothing to pick.
        "privacy_level_options": list(data.get("privacyLevelOptions") or []) if can_post else [],
        "comment_disabled": bool(data.get("commentDisabled")),
        "duet_disabled": bool(data.get("duetDisabled")),
        "stitch_disabled": bool(data.get("stitchDisabled")),
        "max_video_post_duration_sec": data.get("maxVideoPostDurationSec"),
        "can_post": can_post,
    }


def tiktok_options(content: PublishContent, privacy_level: str) -> dict:
    """Map Studio's TikTok extras onto the Platform's ``options.tiktok``.

    Only what the creator set is sent; anything absent keeps the Platform's
    default, as it would keep TikTok's on the direct path.
    """
    extra = content.extra or {}
    options: dict = {"privacyLevel": privacy_level}
    for ours, theirs in INTERACTION_FLAGS:
        if ours in extra:
            options[theirs] = not bool(extra[ours])
    organic = bool(extra.get("brand_organic_toggle"))
    branded = bool(extra.get("brand_content_toggle"))
    if organic or branded:
        options.update(disclose=True, brandOrganic=organic, brandContent=branded)
    if "is_aigc" in extra:
        options["aiGenerated"] = bool(extra["is_aigc"])
    cover_ms = extra.get("video_cover_timestamp_ms")
    if cover_ms is not None and not isinstance(cover_ms, bool):
        try:
            cover_ms = int(cover_ms)
        except (TypeError, ValueError):
            cover_ms = -1
        if cover_ms >= 0:
            options["coverMs"] = cover_ms
    return options


def _upload_video(provider, path: str, duration_sec: float | None) -> str:
    """Upload the file with the Platform's presigned multipart flow; return the asset id."""
    size = os.path.getsize(path)
    plan = _call(
        provider,
        "POST",
        "/media/uploads",
        json={"mediaType": "video", "sizeBytes": size},
        # A fresh key per attempt: replaying an old plan would hand back part
        # URLs that expire after 15 minutes, before the engine's next retry.
        idempotency_key=f"studio-upload-{uuid.uuid4().hex}",
    )
    asset_id = plan["mediaAssetId"]
    part_size = int(plan["partSize"])
    part_count = int(plan["partCount"])
    urls = {u["partNumber"]: u["url"] for u in plan.get("urls", [])}
    parts = []
    with open(path, "rb") as video:
        for number in range(1, part_count + 1):
            if number not in urls:
                more = _call(
                    provider, "POST", f"/media/uploads/{asset_id}/parts", json={"fromPart": number, "count": 16}
                )
                urls.update({u["partNumber"]: u["url"] for u in more.get("urls", [])})
            chunk = video.read(part_size)
            # Presigned storage URL: no Authorization header (access_token=None).
            put = provider._request("PUT", urls[number], data=chunk, timeout=300.0)
            parts.append({"partNumber": number, "etag": put.headers.get("ETag", "")})
    body: dict = {"parts": parts}
    if duration_sec:
        body["durationSec"] = round(float(duration_sec), 3)
    asset = _call(provider, "POST", f"/media/uploads/{asset_id}/complete", json=body)
    return asset.get("id") or asset_id


def _channel(post: dict) -> dict:
    _, _, account_id = _settings()
    channels = post.get("channels") or []
    for channel in channels:
        if channel.get("accountId") == account_id:
            return channel
    return channels[0] if channels else {}


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _earlier_attempt(provider, caption: str) -> dict | None:
    """A recent Platform post with this caption, if an earlier attempt made one.

    This is what keeps a retry from posting the same video twice: if the
    previous attempt got as far as the Platform, its post is reused (published
    again only if it is still a draft), never duplicated.
    """
    _, _, account_id = _settings()
    since = datetime.now(UTC) - RECENT_WINDOW
    listing = _call(provider, "GET", f"/posts/?accountId={account_id}&limit=20")
    for post in listing.get("data") or listing.get("posts") or []:
        created = _parse_time(post.get("createdAt"))
        if (post.get("caption") or "") != caption or not created or created < since:
            continue
        status = (_channel(post).get("status") or post.get("status") or "").lower()
        if status == "draft" or status in LIVE_OR_ON_ITS_WAY:
            return post
    return None


def publish(provider, content: PublishContent, privacy_level: str) -> PublishResult:
    """Hand the video to BrightBean Platform and have it publish now."""
    if not content.media_files:
        raise PublishError(
            "The TikTok relay needs the video as a local file (local media storage).",
            platform="tiktok",
            retryable=False,
        )
    _, _, account_id = _settings()
    caption = (content.title or content.text or "")[:2200]

    post = _earlier_attempt(provider, caption)
    if post is None:
        asset_id = _upload_video(provider, content.media_files[0], content.video_duration_sec)
        created = _call(
            provider,
            "POST",
            "/posts/",
            json={
                "accounts": [account_id],
                "caption": caption,
                "mediaAssetIds": [asset_id],
                "perAccount": {account_id: {"options": {"tiktok": tiktok_options(content, privacy_level)}}},
            },
            idempotency_key=f"studio-draft-{uuid.uuid4().hex}",
        )
        post = created.get("post") or created

    post_id = post["id"]
    status = (_channel(post).get("status") or post.get("status") or "").lower()
    if status not in LIVE_OR_ON_ITS_WAY:
        published = _call(
            provider,
            "POST",
            f"/posts/{post_id}/publish",
            # Stable per Platform post: a retry of this exact call is replayed
            # by the Platform, never published a second time.
            idempotency_key=f"studio-publish-{post_id}",
        )
        post = published.get("post") or post
    logger.info("TikTok post handed to BrightBean Platform as %s", post_id)
    return PublishResult(platform_post_id=f"{HANDLE_PREFIX}{post_id}", extra={"relay_post": post})


def check_status(provider, handle: str) -> PublishStatus:
    """Settle a relayed publish from the Platform's post."""
    post_id = handle[len(HANDLE_PREFIX) :]
    post = _call(provider, "GET", f"/posts/{post_id}")
    channel = _channel(post)
    status = (channel.get("status") or post.get("status") or "").lower()
    if status == "published":
        match = VIDEO_ID.search(channel.get("url") or "")
        return PublishStatus(
            state=PublishState.COMPLETE,
            platform_post_id=match.group(1) if match else "",
            raw=post,
        )
    if status in ("failed", "partially_published"):
        error = channel.get("error") or "BrightBean Platform could not publish the video."
        return PublishStatus(state=PublishState.FAILED, error=f"TikTok (via BrightBean Platform): {error}", raw=post)
    return PublishStatus(state=PublishState.PENDING, raw=post)
