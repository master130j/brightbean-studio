"""Publish to TikTok through BrightBean Platform (a local patch, not upstream).

TikTok lets an API client post publicly only after an audit, and its Content
Sharing Guidelines rule out "a utility tool to help upload contents to the
account(s) you or your team manages" — which is what a self-hosted Studio
connected to its owner's own account is. Until audited, every post is
``SELF_ONLY`` and the account itself has to be private.

BrightBean Platform's TikTok app is audited. With the relay configured (see
``platform_relay``; the channel is ``PLATFORM_TIKTOK_RELAY_ACCOUNT_ID``), this
instance keeps its own TikTok connection, but:

- the composer's creator info (privacy options, interaction limits, the
  nickname it shows) comes from the Platform's ``tiktok-creator-info``;
- at the scheduled time the video goes to the Platform with the post's TikTok
  options and is published now; the handle is ``bbp:<platform post id>``;
- :func:`check_status` settles that handle from the Platform's post.
"""

from __future__ import annotations

import re

from . import platform_relay
from .exceptions import PublishError
from .types import PublishContent, PublishResult, PublishState, PublishStatus

NETWORK = "tiktok"
HANDLE_PREFIX = platform_relay.HANDLE_PREFIX
VIDEO_ID = re.compile(r"/video/(\d+)")

# Studio's composer stores "disable_*"; the Platform takes "allow*".
INTERACTION_FLAGS = (
    ("disable_comment", "allowComment"),
    ("disable_duet", "allowDuet"),
    ("disable_stitch", "allowStitch"),
)


def enabled() -> bool:
    return platform_relay.enabled(NETWORK)


def is_relay_handle(handle: str) -> bool:
    return platform_relay.is_relay_handle(handle)


def creator_info(provider) -> dict:
    """The Platform's creator info, in the shape of TikTok's own response."""
    _, _, account_id = platform_relay.settings(NETWORK)
    data = platform_relay.call(provider, NETWORK, "GET", f"/accounts/{account_id}/tiktok-creator-info")
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


def publish(provider, content: PublishContent, privacy_level: str) -> PublishResult:
    """Hand the video to BrightBean Platform and have it publish now."""
    if not content.media_files:
        raise PublishError(
            "The TikTok relay needs the video as a local file (local media storage).",
            platform=NETWORK,
            retryable=False,
        )
    return platform_relay.publish(
        provider,
        NETWORK,
        caption=(content.title or content.text or "")[:2200],
        media_file=content.media_files[0],
        options=tiktok_options(content, privacy_level),
        duration_sec=content.video_duration_sec,
    )


def check_status(provider, handle: str) -> PublishStatus:
    """Settle a relayed publish from the Platform's post."""
    post, channel = platform_relay.fetch(provider, NETWORK, handle)
    status = (channel.get("status") or post.get("status") or "").lower()
    if status == "published":
        match = VIDEO_ID.search(channel.get("url") or "")
        return PublishStatus(state=PublishState.COMPLETE, platform_post_id=match.group(1) if match else "", raw=post)
    if status in ("failed", "partially_published"):
        error = channel.get("error") or "BrightBean Platform could not publish the video."
        return PublishStatus(state=PublishState.FAILED, error=f"TikTok (via BrightBean Platform): {error}", raw=post)
    return PublishStatus(state=PublishState.PENDING, raw=post)
