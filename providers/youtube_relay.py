"""Publish to YouTube through BrightBean Platform (a local patch, not upstream).

Every video uploaded through the YouTube Data API by an unverified Google Cloud
project is locked private until Google audits the project. BrightBean Platform's
project is audited: on 13-oct-2026 a test upload sent through it as "unlisted"
stayed unlisted (read back with this instance's own token, then deleted).

With the relay configured (see ``platform_relay``; the channel is
``PLATFORM_YOUTUBE_RELAY_ACCOUNT_ID``), publishing goes through the Platform:
the video (and the custom thumbnail, if any) is uploaded there, a draft is
created with the title, description and YouTube options, and it is published
now. The publish is asynchronous here: the handle ``bbp:<platform post id>`` is
settled by :func:`check_status`, which reads the video id from the post's URL.

Comments, statistics and the connection itself stay on this instance's own
YouTube connection: the relay only uploads.
"""

from __future__ import annotations

import re

from . import platform_relay
from .exceptions import PublishError
from .types import PublishContent, PublishResult, PublishState, PublishStatus

NETWORK = "youtube"
VIDEO_ID = re.compile(r"(?:youtu\.be/|[?&]v=|/shorts/)([\w-]{11})")


def enabled() -> bool:
    return platform_relay.enabled(NETWORK)


def is_relay_handle(handle: str) -> bool:
    return platform_relay.is_relay_handle(handle)


def title_for(content: PublishContent) -> str:
    """The video's title, without the ``#Shorts`` the direct path appends.

    On 13-oct-2026 the two relayed posts whose title ended in " #Shorts" stayed
    "publishing" on the Platform for over 20 minutes and never reached YouTube,
    while the two without it were live in about 90 s. YouTube already treats a
    vertical video under three minutes as a Short, so the tag adds nothing.
    """
    return (content.title or content.text or "")[:100]


def youtube_options(content: PublishContent) -> dict:
    """Map Studio's YouTube extras onto the Platform's ``options.youtube``,
    with the direct path's defaults (public, not made for kids)."""
    extra = content.extra or {}
    options: dict = {
        "privacyStatus": extra.get("privacy_status") or "public",
        "madeForKids": bool(extra.get("self_declared_made_for_kids", False)),
    }
    tags = [str(tag) for tag in (extra.get("tags") or []) if str(tag).strip()]
    if tags:
        options["tags"] = tags
    if extra.get("category_id"):
        options["categoryId"] = str(extra["category_id"])
    return options


def publish(provider, content: PublishContent) -> PublishResult:
    """Hand the video to BrightBean Platform and have it publish now."""
    if not content.media_files:
        raise PublishError(
            "The YouTube relay needs the video as a local file (local media storage).",
            platform=NETWORK,
            retryable=False,
        )
    thumbnail = (content.extra or {}).get("thumbnail_file")
    return platform_relay.publish(
        provider,
        NETWORK,
        caption=(content.description or content.text or "")[:5000],
        title=title_for(content),
        media_file=content.media_files[0],
        options=youtube_options(content),
        duration_sec=content.video_duration_sec,
        extra_uploads={"thumbnail": thumbnail} if thumbnail else None,
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
        return PublishStatus(state=PublishState.FAILED, error=f"YouTube (via BrightBean Platform): {error}", raw=post)
    return PublishStatus(state=PublishState.PENDING, raw=post)
