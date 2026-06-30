"""Resolve the stream URL Eventyay exposes for a room."""

from __future__ import annotations

NATIVE_LIVESTREAM = "livestream.native"
YOUTUBE_LIVESTREAM = "livestream.youtube"
IFRAME_LIVESTREAM = "livestream.iframe"

STREAM_MODULES = frozenset({
    NATIVE_LIVESTREAM,
    YOUTUBE_LIVESTREAM,
    IFRAME_LIVESTREAM,
})

# SUSI ``YouTubeSource``: yt-dlp for platform URLs, ffmpeg for direct ``.m3u8``.
SUSI_STREAM_TYPE = "youtube"

# Embed-only schedules have no direct audio URL for SUSI to ingest.
_SKIP_SCHEDULE_TYPES = frozenset({"iframe"})


def _strip(url: str) -> str:
    return (url or "").strip()


def _youtube_url(value: str) -> str:
    value = _strip(value)
    if not value:
        return ""
    if "://" in value:
        return value
    return f"https://www.youtube.com/watch?v={value}"


def _url_from_module(module: dict) -> str:
    module_type = module.get("type")
    config = module.get("config") or {}
    if module_type == NATIVE_LIVESTREAM:
        return _strip(config.get("hls_url"))
    if module_type == YOUTUBE_LIVESTREAM:
        return _youtube_url(config.get("ytid", ""))
    if module_type == IFRAME_LIVESTREAM:
        return _strip(config.get("url"))
    return ""


def get_module_stream_url(room) -> str:
    """URL from the room's stage module (native HLS, YouTube, or iframe player)."""
    for module in room.module_config or []:
        if not isinstance(module, dict):
            continue
        url = _url_from_module(module)
        if url:
            return url
    return ""


def _schedules(room):
    schedules = getattr(room, "stream_schedules", None)
    if schedules is None:
        return None
    if hasattr(schedules, "exclude"):
        return schedules.exclude(stream_type__in=_SKIP_SCHEDULE_TYPES)
    return schedules


def get_schedule_stream_url(room, at_time=None) -> str:
    """URL from timed stream schedules (YouTube, Vimeo, HLS, native, …)."""
    schedules = _schedules(room)
    if schedules is None:
        return ""

    items = list(schedules)
    active = [s for s in items if s.is_active(at_time) and _strip(s.url)]
    if active:
        return _strip(active[0].url)

    dated = [s for s in items if _strip(s.url)]
    if not dated:
        return ""
    latest = max(dated, key=lambda s: s.start_time)
    return _strip(latest.url)


def get_room_stream_url(room, at_time=None) -> str:
    """Best stream URL for a room: stage module first, then stream schedule."""
    return get_module_stream_url(room) or get_schedule_stream_url(room, at_time)


def set_module_interpretation(room, info: dict) -> bool:
    """Write caption discovery info into the room's active stream module."""
    modules = room.module_config or []
    for module in modules:
        if isinstance(module, dict) and module.get("type") in STREAM_MODULES:
            config = module.setdefault("config", {})
            config["interpretation"] = info
            room.module_config = modules
            return True
    return False


def clear_module_interpretation(room) -> bool:
    """Remove caption discovery info from the room's stream module."""
    modules = room.module_config or []
    changed = False
    for module in modules:
        if isinstance(module, dict) and module.get("type") in STREAM_MODULES:
            config = module.get("config") or {}
            if "interpretation" in config:
                config.pop("interpretation", None)
                changed = True
    if changed:
        room.module_config = modules
    return changed
