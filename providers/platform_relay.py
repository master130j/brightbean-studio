"""Publish through BrightBean Platform's audited apps (a local patch, not upstream).

Some networks only let an audited API client post publicly, and audit only
apps built for a wide audience — not a self-hosted Studio posting to its
owner's own accounts:

- TikTok: unaudited clients post SELF_ONLY, to private accounts only;
- YouTube: uploads from an unverified Google project are locked private.

BrightBean Platform (api-platform.brightbean.xyz) runs audited apps for both and
has a public REST API. This module is the shared half of the relay: settings,
calls, the presigned media upload, the "never post twice" lookup and reading a
Platform post's channel. ``tiktok_relay`` and ``youtube_relay`` hold what is
specific to each network.

Settings (environment, read at call time):

- ``PLATFORM_RELAY_KEY``: a Platform API key with ``create_posts``,
  ``upload_media`` and ``publish_directly`` on the relayed channels
  (``PLATFORM_TIKTOK_RELAY_KEY`` is still read, for the first deployment);
- ``PLATFORM_<NETWORK>_RELAY_ACCOUNT_ID``: the channel id on the Platform;
- ``PLATFORM_RELAY_URL``: the base URL, if not the default.

Nothing here publishes on its own: it runs only when this instance's engine
publishes a post a person scheduled.

API reference: https://github.com/brightbeanxyz/brightbean-platform-agent
"""

from __future__ import annotations

import json as jsonlib
import logging
import os
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

from .exceptions import APIError, PublishError
from .types import PublishResult

logger = logging.getLogger(__name__)

HANDLE_PREFIX = "bbp:"
DEFAULT_BASE_URL = "https://api-platform.brightbean.xyz"
# Cloudflare in front of the Platform rejects some default user agents (1010).
USER_AGENT = "brightbean-studio-platform-relay/1.0"

# How far back a Platform post with the same text counts as this one. The
# engine retries a failed publish after 1, 5 and 30 minutes, and an earlier
# attempt may have reached the Platform even though it raised here.
RECENT_WINDOW = timedelta(hours=6)

# Platform channel statuses that mean the post is (or is about to be) live.
LIVE_OR_ON_ITS_WAY = {"publishing", "published", "scheduled", "queued"}

MEDIA_TYPE_BY_EXTENSION = {
    ".mp4": "video",
    ".mov": "video",
    ".webm": "video",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
}


def settings(network: str) -> tuple[str, str, str]:
    """``(api_base, key, account_id)`` for one network."""
    base = (
        os.environ.get("PLATFORM_RELAY_URL", "").strip()
        or os.environ.get("PLATFORM_TIKTOK_RELAY_URL", "").strip()
        or DEFAULT_BASE_URL
    )
    key = os.environ.get("PLATFORM_RELAY_KEY", "").strip() or os.environ.get("PLATFORM_TIKTOK_RELAY_KEY", "").strip()
    account_id = os.environ.get(f"PLATFORM_{network.upper()}_RELAY_ACCOUNT_ID", "").strip()
    return f"{base.rstrip('/')}/api/v1", key, account_id


def enabled(network: str) -> bool:
    _, key, account_id = settings(network)
    return bool(key and account_id)


def is_relay_handle(handle: str) -> bool:
    return bool(handle) and handle.startswith(HANDLE_PREFIX)


def call(
    provider,
    network: str,
    method: str,
    path: str,
    *,
    json: dict | None = None,
    idempotency_key: str | None = None,
    timeout: float = 60.0,
) -> dict:
    api_base, key, _ = settings(network)
    if not key:
        raise PublishError(
            "PLATFORM_RELAY_KEY is not set, so this post can't be handed to BrightBean Platform.",
            platform=network,
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
                f"BrightBean Platform refused the {network} post: {detail}",
                platform=network,
                raw_response=getattr(exc, "raw_response", None),
                retryable=False,
            ) from exc
        raise
    return response.json() if response.content else {}


def probe(path: str) -> dict:
    """Width, height and duration of a video or image, read with ffprobe.

    The Platform's ``complete`` takes them as optional, but a YouTube post whose
    video arrived without them stayed "publishing" for over 20 minutes on
    13-oct-2026, while the same post with them was live in 98 s. Missing
    ffprobe or an unreadable file returns ``{}``: the upload goes on without.
    """
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        data = jsonlib.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.warning("ffprobe could not read %s; uploading without its dimensions", path)
        return {}
    stream = (data.get("streams") or [{}])[0]
    found: dict = {}
    if stream.get("width") and stream.get("height"):
        found["width"], found["height"] = int(stream["width"]), int(stream["height"])
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration > 0:
        found["durationSec"] = round(duration, 3)
    return found


def upload(
    provider,
    network: str,
    path: str,
    *,
    duration_sec: float | None = None,
) -> str:
    """Upload a file with the Platform's presigned multipart flow; return the asset id."""
    media_type = MEDIA_TYPE_BY_EXTENSION.get(os.path.splitext(path)[1].lower(), "video")
    size = os.path.getsize(path)
    plan = call(
        provider,
        network,
        "POST",
        "/media/uploads",
        json={"mediaType": media_type, "sizeBytes": size},
        # A fresh key per attempt: replaying an old plan would hand back part
        # URLs that expire after 15 minutes, before the engine's next retry.
        idempotency_key=f"studio-upload-{uuid.uuid4().hex}",
    )
    asset_id = plan["mediaAssetId"]
    part_size = int(plan["partSize"])
    part_count = int(plan["partCount"])
    urls = {u["partNumber"]: u["url"] for u in plan.get("urls", [])}
    parts = []
    with open(path, "rb") as media:
        for number in range(1, part_count + 1):
            if number not in urls:
                more = call(
                    provider,
                    network,
                    "POST",
                    f"/media/uploads/{asset_id}/parts",
                    json={"fromPart": number, "count": 16},
                )
                urls.update({u["partNumber"]: u["url"] for u in more.get("urls", [])})
            chunk = media.read(part_size)
            # Presigned storage URL: no Authorization header (access_token=None).
            put = provider._request("PUT", urls[number], data=chunk, timeout=300.0)
            parts.append({"partNumber": number, "etag": put.headers.get("ETag", "")})
    body: dict = {"parts": parts}
    measured = probe(path)
    if media_type == "video":
        if duration_sec:
            measured["durationSec"] = round(float(duration_sec), 3)
    else:
        measured.pop("durationSec", None)
    body.update(measured)
    asset = call(provider, network, "POST", f"/media/uploads/{asset_id}/complete", json=body)
    return asset.get("id") or asset_id


def channel(post: dict, account_id: str) -> dict:
    channels = post.get("channels") or []
    for candidate in channels:
        if candidate.get("accountId") == account_id:
            return candidate
    return channels[0] if channels else {}


def channel_status(post: dict, account_id: str) -> str:
    return (channel(post, account_id).get("status") or post.get("status") or "").lower()


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def earlier_attempt(provider, network: str, caption: str, title: str | None = None) -> dict | None:
    """A recent Platform post with this text, if an earlier attempt made one.

    This is what keeps a retry from posting the same video twice: if the
    previous attempt got as far as the Platform, its post is reused (published
    again only if it is still a draft), never duplicated.
    """
    _, _, account_id = settings(network)
    since = datetime.now(UTC) - RECENT_WINDOW
    listing = call(provider, network, "GET", f"/posts/?accountId={account_id}&limit=20")
    for post in listing.get("data") or listing.get("posts") or []:
        created = _parse_time(post.get("createdAt"))
        if (post.get("caption") or "") != caption or not created or created < since:
            continue
        if title is not None and (post.get("title") or "") != title:
            continue
        status = channel_status(post, account_id)
        if status == "draft" or status in LIVE_OR_ON_ITS_WAY:
            return post
    return None


def publish(
    provider,
    network: str,
    *,
    caption: str,
    media_file: str,
    options: dict,
    title: str | None = None,
    duration_sec: float | None = None,
    extra_uploads: dict | None = None,
) -> PublishResult:
    """Hand one post to the Platform and have it publish now.

    ``extra_uploads`` maps a dotted path inside ``options`` to a local file to
    upload first (e.g. a YouTube thumbnail); its asset id is put there.
    """
    _, _, account_id = settings(network)
    post = earlier_attempt(provider, network, caption, title)
    if post is None:
        asset_id = upload(provider, network, media_file, duration_sec=duration_sec)
        for dotted, path in (extra_uploads or {}).items():
            target = options
            *parents, leaf = dotted.split(".")
            for name in parents:
                target = target.setdefault(name, {})
            target[leaf] = {"assetId": upload(provider, network, path)}
        body: dict = {
            "accounts": [account_id],
            "caption": caption,
            "mediaAssetIds": [asset_id],
            "perAccount": {account_id: {"options": {network: options}}},
        }
        if title is not None:
            body["title"] = title
        created = call(
            provider, network, "POST", "/posts/", json=body, idempotency_key=f"studio-draft-{uuid.uuid4().hex}"
        )
        post = created.get("post") or created

    post_id = post["id"]
    if channel_status(post, account_id) not in LIVE_OR_ON_ITS_WAY:
        published = call(
            provider,
            network,
            "POST",
            f"/posts/{post_id}/publish",
            # Stable per Platform post: a retry of this exact call is replayed
            # by the Platform, never published a second time.
            idempotency_key=f"studio-publish-{post_id}",
        )
        post = published.get("post") or post
    logger.info("%s post handed to BrightBean Platform as %s", network, post_id)
    return PublishResult(platform_post_id=f"{HANDLE_PREFIX}{post_id}", extra={"relay_post": post})


def fetch(provider, network: str, handle: str) -> tuple[dict, dict]:
    """``(post, channel)`` of a relayed publish, from its handle."""
    _, _, account_id = settings(network)
    post = call(provider, network, "GET", f"/posts/{handle[len(HANDLE_PREFIX) :]}")
    return post, channel(post, account_id)
