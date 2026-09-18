"""Amazon Music — the dedicated, explicitly allowlisted music integration.

This is one of exactly TWO things Nex's AI can touch:

    1. explicitly connected MCP servers
    2. THIS module's music controls

It is a first-class connector that duck-types an MCP Upstream, so it flows
through the same discovery -> registry -> policy -> executor pipeline as
every MCP server — there is no special path, no side door.

The ALLOWLIST is the whole surface (nothing else exists here):

    am_play         start/resume playback
    am_pause        pause playback
    am_toggle       play/pause toggle
    am_next         next track
    am_previous     previous track
    am_volume       set volume 0..100
    am_search_play  search and play the best match

## The official Web API backend

Under the `web` backend this module is a client of the OFFICIAL
Amazon Music Web API (https://developer.amazon.com/docs/music/):

    base        https://api.music.amazon.dev
    auth        Login With Amazon OAuth 2.0 (refresh-token grant)
    headers     Authorization: Bearer <token> + x-api-key: <profile ID>
                (playback calls add WIDEVINE / device-capability headers)
    search      POST /v1/search/tracks          (scope music::catalog)
    playback    POST /v1/playback/sessions      (scope music::playback)
                GET  /v1/playback/sessions/{id}/current|next|previous
                POST /v1/playback/event         (mandatory start/stop
                                                 reporting with metricId)

Configuration (env — no secrets are ever exposed through the AI surface):

    NEX_AMZ_LWA_CLIENT_ID      amzn1.application-oa2-client....
    NEX_AMZ_LWA_CLIENT_SECRET
    NEX_AMZ_LWA_REFRESH_TOKEN  long-term LWA refresh token
    NEX_AMZ_PROFILE_ID         x-api-key; the LWA *Security Profile ID*
                               (amzn1.application....) — NOT the client id
    NEX_AMZ_DEVICE_ID          stable device id (default: hashed hostname)
    NEX_AMZ_API_BASE           default https://api.music.amazon.dev

NOTE: the Amazon Music Web API is in closed beta — the Security Profile
must be enabled by the Amazon Music service. Until that access exists the
connector stays honest about it (errors name the missing piece) and Nex
can still drive the installed Amazon Music app via the `link` backend.

Backends (NEX_MUSIC_BACKEND = auto | web | link | url | stub):

    auto (default) — official Web API when LWA credentials are present,
                     otherwise `stub`.
    web            — official Web API (requires the credentials above).
    link / url     — explicit opt-in: opens amazonmusic:// deep links in
                     the installed Amazon Music app. Bounded to that scheme.
    stub           — records commands, returns honest structured results
                     (sandboxes/CI).

Playback rendering: the Web API model is "the client renders audio and
MUST report start/stop events". Nex's renderer is its own UI (an event
pushed over the BUS carries the playable); the backend owns auth, search,
queue sessions and event reporting. No renderer is faked — if a stream is
DRM-protected and the UI cannot render it, the UI says so.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

SERVER_NAME = "amazon-music"
DEEP_LINK = "amazonmusic://"

WEB_API_BASE = os.environ.get("NEX_AMZ_API_BASE",
                              "https://api.music.amazon.dev")
LWA_TOKEN_URL = os.environ.get("NEX_AMZ_LWA_TOKEN_URL",
                               "https://api.amazon.com/auth/o2/token")

REQUEST_TIMEOUT = 8.0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v.strip()
    return ""


class Config:
    """Snapshot of the Web API credentials. Never contains values exposed
    to the AI — only booleans cross that line."""

    def __init__(self) -> None:
        self.client_id = _env("NEX_AMZ_LWA_CLIENT_ID")
        self.client_secret = _env("NEX_AMZ_LWA_CLIENT_SECRET")
        self.refresh_token = _env("NEX_AMZ_LWA_REFRESH_TOKEN")
        self.profile_id = _env("NEX_AMZ_PROFILE_ID",
                               "NEX_AMZ_SECURITY_PROFILE_ID")
        self.device_id = _env("NEX_AMZ_DEVICE_ID") or _default_device_id()
        self.api_base = _env("NEX_AMZ_API_BASE") or WEB_API_BASE
        self.token_url = _env("NEX_AMZ_LWA_TOKEN_URL") or LWA_TOKEN_URL

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret
                    and self.refresh_token and self.profile_id)

    def missing(self) -> List[str]:
        need = [("NEX_AMZ_LWA_CLIENT_ID", self.client_id),
                ("NEX_AMZ_LWA_CLIENT_SECRET", self.client_secret),
                ("NEX_AMZ_LWA_REFRESH_TOKEN", self.refresh_token),
                ("NEX_AMZ_PROFILE_ID", self.profile_id)]
        return [n for (n, v) in need if not v]


def _default_device_id() -> str:
    """Stable, non-identifying device id per the API recommendation."""
    raw = (socket.gethostname() + "/nex-music").encode()
    return base64.urlsafe_b64encode(
        __import__("hashlib").sha256(raw).digest()).decode().rstrip("=")[:32]


# ---------------------------------------------------------------------------
# Login With Amazon token manager (refresh-token grant, cached, thread-safe)
# ---------------------------------------------------------------------------

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: Dict[str, Any] = {}


class AuthError(Exception):
    pass


def _lwa_access_token(cfg: Config, force: bool = False) -> str:
    if not (cfg.client_id and cfg.client_secret and cfg.refresh_token):
        raise AuthError("LWA credentials missing: " + ", ".join(cfg.missing()))
    with _TOKEN_LOCK:
        tok = _TOKEN_CACHE.get("token")
        exp = _TOKEN_CACHE.get("expires", 0.0)
        if tok and not force and time.time() < exp - 60:
            return str(tok)
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": cfg.refresh_token,
            "client_id": cfg.client_id,
            "client_secret": cfg.client_secret,
        }).encode()
        req = urllib.request.Request(
            cfg.token_url, data=body, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
                payload = json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            raise AuthError("LWA token refresh failed: " + repr(exc)) from exc
        access = payload.get("access_token")
        if not access:
            raise AuthError("LWA token response missing access_token")
        try:
            ttl = float(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600.0
        _TOKEN_CACHE["token"] = access
        _TOKEN_CACHE["expires"] = time.time() + ttl
        return str(access)


# ---------------------------------------------------------------------------
# Official Web API client
# ---------------------------------------------------------------------------

class ApiError(Exception):
    pass


def _http_json(method: str, url: str, headers: Dict[str, str],
               body: Optional[Dict[str, Any]] = None
               ) -> tuple:
    """One HTTP request returning (status, parsed-json-or-{}). Isolated so
    tests can stub the transport without touching the network."""
    data = (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        raw = ""
        try:
            raw = exc.read().decode()
        except Exception:  # noqa: BLE001
            pass
        try:
            return exc.code, (json.loads(raw) if raw.strip() else {})
        except ValueError:
            return exc.code, {"raw": raw[:400]}


class MusicWebApi:
    """Official Amazon Music Web API client (V1)."""

    def __init__(self, cfg: Optional[Config] = None) -> None:
        self.cfg = cfg or Config()

    # -- plumbing -----------------------------------------------------------
    def _headers(self, playback: bool = False) -> Dict[str, str]:
        token = _lwa_access_token(self.cfg)
        h = {"x-api-key": self.cfg.profile_id,
             "Authorization": "Bearer " + token}
        if playback:
            h["X-Amzn-Audio-DRMType"] = "WIDEVINE"
            h["X-Amzn-Audio-Device-Capability"] = "STD_RES"
            h["X-Amzn-Device-Id"] = self.cfg.device_id
        return h

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] =
              None, playback: bool = False) -> Dict[str, Any]:
        url = self.cfg.api_base.rstrip("/") + path
        headers = self._headers(playback=playback)
        status, payload = _http_json(method, url, headers, body)
        if status == 429:  # documented TPS limiting -> back off once
            time.sleep(1.0)
            status, payload = _http_json(method, url, headers, body)
        if status >= 400:
            msg = payload.get("message") or payload.get("error") or \
                json.dumps(payload)[:200]
            raise ApiError("HTTP %s from %s: %s" % (status, path, msg))
        return payload

    # -- endpoints ----------------------------------------------------------
    def search_tracks(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        payload = self._call(
            "POST", "/v1/search/tracks",
            {"searchFilters": [{"field": "name", "query": query}],
             "limit": limit, "sortBy": "relevance"})
        return _extract_tracks(payload)

    def create_session(self, track_mrn: str) -> Dict[str, Any]:
        return self._call(
            "POST", "/v1/playback/sessions",
            {"playParams": {"id": track_mrn},
             "playbackOptions": {
                 "playbackContinuationMode": "AUTOPLAY_ON",
                 "shuffleMode": "SHUFFLE_OFF",
                 "loopMode": "LOOP_OFF",
                 "startAsStation": False},
             "limit": 3},
            playback=True)

    def active_sessions(self) -> List[Dict[str, Any]]:
        payload = self._call("GET", "/v1/playback/sessions/active")
        out = payload.get("sessions")
        return out if isinstance(out, list) else []

    def current_playable(self, session_id: str) -> Optional[Dict[str, Any]]:
        payload = self._call(
            "GET", "/v1/playback/sessions/%s/current" % session_id,
            playback=True)
        return _first_playable(payload)

    def next_playable(self, session_id: str) -> Optional[Dict[str, Any]]:
        payload = self._call(
            "GET", "/v1/playback/sessions/%s/next?limit=1" % session_id,
            playback=True)
        return _first_playable(payload)

    def previous_playable(self, session_id: str) -> Optional[Dict[str, Any]]:
        payload = self._call(
            "GET", "/v1/playback/sessions/%s/previous?limit=1" % session_id,
            playback=True)
        return _first_playable(payload)

    def recently_played(self) -> List[Dict[str, Any]]:
        try:
            payload = self._call("GET", "/v1/me/recentlyPlayedEntities")
        except ApiError:
            return []
        return _extract_tracks(payload)

    def report_event(self, metric_id: str, kind: str, *,
                     reason: Optional[str] = None,
                     progress_seconds: int = 0,
                     duration_seconds: int = 0) -> None:
        """Mandatory playback-event reporting (start / stop)."""
        tz = time.strftime("%z") or "+00:00"
        event: Dict[str, Any] = {
            "type": kind,
            "clientTimestampInMilliseconds": int(time.time() * 1000),
            "initialPlaybackDelayMilliseconds": 0,
            "deviceTimezone": tz,
            "playbackCurrentSpeed": 1.0,
            "durationSeconds": int(duration_seconds or 0),
            "entityProgressSeconds": int(progress_seconds or 0),
            "rebufferCount": 0,
        }
        if kind == "stop" and reason:
            event["terminationReason"] = reason
        self._call("POST", "/v1/playback/event",
                   {"metricId": metric_id, "event": event}, playback=True)


# ---------------------------------------------------------------------------
# Defensive response parsing (shapes are documented by example, not schema)
# ---------------------------------------------------------------------------

def _extract_tracks(payload: Any) -> List[Dict[str, Any]]:
    """Collect Track entities ({asin,title,artists,album,playParams})
    from an arbitrary search/views payload."""
    found: List[Dict[str, Any]] = []

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 12 or isinstance(node, (str, int, float, bool)):
            return
        if isinstance(node, dict):
            if node.get("_type") == "Track" and node.get("playParams"):
                artists = ", ".join(a.get("name", "")
                                    for a in node.get("artists", [])
                                    if isinstance(a, dict))
                album = node.get("album") or {}
                found.append({
                    "asin": node.get("id") or "",
                    "title": node.get("title", ""),
                    "artist": artists,
                    "album": (album.get("title", "")
                              if isinstance(album, dict) else ""),
                    "play_params_id": (node.get("playParams") or {})
                    .get("id", ""),
                })
                return
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)

    _walk(payload)
    seen, out = set(), []
    for t in found:
        key = t["asin"] or t["title"]
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _first_playable(payload: Any) -> Optional[Dict[str, Any]]:
    """Pull the first playable object out of a session/current/next
    response. Playables carry the stream reference + metricId used for
    mandatory event reporting."""
    playables: List[Any] = []

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 12 or isinstance(node, (str, int, float, bool)):
            return
        if isinstance(node, dict):
            if node.get("_type") in ("Playable", "TrackPlayable") or \
                    ("metricId" in node and "mediaMetadata" in node):
                playables.append(node)
                return
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)

    _walk(payload)
    if not playables:
        return None
    p = playables[0]
    media = p.get("mediaMetadata") or {}
    url = (media.get("url") or p.get("url") or "")
    return {
        "metric_id": p.get("metricId") or p.get("metrics") or "",
        "url": url if isinstance(url, str) else "",
        "drm_type": (media.get("drmType") or p.get("drmType") or ""),
        "media_type": (media.get("mediaType") or p.get("mediaType") or ""),
        "title": p.get("title", ""),
        "duration_seconds": int(p.get("durationSeconds") or 0),
        "raw": {k: p.get(k) for k in ("_type", "id", "title")
                if p.get(k) is not None},
    }


def _session_id(payload: Dict[str, Any]) -> str:
    for key in ("sessionId", "session_id", "id", "playQueueId"):
        v = payload.get(key)
        if isinstance(v, str) and v:
            return v
    data = payload.get("data") if isinstance(payload.get("data"), dict) \
        else {}
    for key in ("sessionId", "id", "playQueueId"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


# ---------------------------------------------------------------------------
# Player state (Nex-side; the AI sees only honest summaries of it)
# ---------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    "session_id": None,
    "playable": None,
    "track": None,
    "playing": False,
    "volume": 70,
    "last_search": None,
    "play_started_at": None,
}

# UI notifier hook (set by server.py to BUS.publish). The connector never
# imports the server — infra wiring flows one way.
_NOTIFY: Optional[Callable[[Dict[str, Any]], None]] = None
_NOTIFY_LOCK = threading.Lock()


def set_notifier(fn: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    global _NOTIFY
    with _NOTIFY_LOCK:
        _NOTIFY = fn


def _notify(action: str, extra: Optional[Dict[str, Any]] = None) -> None:
    with _NOTIFY_LOCK:
        fn = _NOTIFY
    if fn is None:
        return
    try:
        ev: Dict[str, Any] = {"type": "amazon_music", "action": action}
        with _STATE_LOCK:
            ev["volume"] = _STATE["volume"]
            if _STATE.get("track"):
                ev["track"] = dict(_STATE["track"])
            if _STATE.get("playable"):
                # Only what a renderer needs — never credentials.
                p = _STATE["playable"]
                ev["playable"] = {"url": p.get("url", ""),
                                  "drm_type": p.get("drm_type", ""),
                                  "media_type": p.get("media_type", ""),
                                  "title": p.get("title", "")}
        if extra:
            ev.update(extra)
        fn(ev)
    except Exception:  # noqa: BLE001
        pass


def _snapshot() -> Dict[str, Any]:
    with _STATE_LOCK:
        return {k: (dict(v) if isinstance(v, dict) else v)
                for k, v in _STATE.items()}


# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

def _backend() -> str:
    raw = os.environ.get("NEX_MUSIC_BACKEND", "auto").strip().lower()
    if raw in ("url", "deep-link", "deeplink"):
        raw = "link"
    if raw not in ("auto", "web", "link", "stub"):
        raw = "auto"
    if raw == "auto":
        return "web" if Config().configured() else "stub"
    return raw


def web_configured() -> bool:
    return Config().configured()


# ---------------------------------------------------------------------------
# The allowlisted operations — the ENTIRE capability surface.
# Each returns an honest structured result; the AI can never inject a
# different action here (dispatch is by name against this table only).
# ---------------------------------------------------------------------------

_API: Optional[MusicWebApi] = None
_API_LOCK = threading.Lock()


def _api() -> MusicWebApi:
    global _API
    with _API_LOCK:
        if _API is None:
            _API = MusicWebApi()
        return _API


def _elapsed_seconds() -> int:
    with _STATE_LOCK:
        started = _STATE.get("play_started_at")
    if not started:
        return 0
    return int(time.time() - started)


def _err_web(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, AuthError):
        hint = "check NEX_AMZ_LWA_* credentials"
    elif isinstance(exc, ApiError):
        hint = ("the Amazon Music Web API is in closed beta — the security "
                "profile must be enabled by Amazon Music; alternatively set "
                "NEX_MUSIC_BACKEND=link to drive the Amazon Music app")
    else:
        hint = "unexpected transport failure"
    return {"ok": False, "backend": "web", "error": str(exc), "hint": hint}


def op_play(args: Dict[str, Any]) -> Dict[str, Any]:
    backend = _backend()
    if backend == "web":
        try:
            r = _web_play()
        except (AuthError, ApiError) as exc:
            r = _err_web(exc)
        _record("am_play", args, r)
        return r
    if backend == "link":
        r = _open_deep_link("play")
        r["backend"] = "link"
        _record("am_play", args, r)
        return r
    r = {"ok": True, "backend": "stub", "link": DEEP_LINK + "play",
         "note": "stub backend (set NEX_AMZ_LWA_* for the official Web API "
                 "or NEX_MUSIC_BACKEND=link to open the Amazon Music app)"}
    _record("am_play", args, r)
    return r


def _web_play() -> Dict[str, Any]:
    with _STATE_LOCK:
        playable = _STATE["playable"]
        playing = _STATE["playing"]
    api = _api()
    if playing:
        return {"ok": True, "backend": "web",
                "track": _STATE.get("track"),
                "note": "already playing"}
    if playable:
        metric = playable.get("metric_id") or ""
        if metric:
            api.report_event(str(metric), "start",
                             progress_seconds=_elapsed_seconds(),
                             duration_seconds=int(
                                 playable.get("duration_seconds") or 0))
        with _STATE_LOCK:
            _STATE["playing"] = True
            _STATE["play_started_at"] = time.time()
        _notify("play")
        return {"ok": True, "backend": "web", "resumed": True,
                "track": _STATE.get("track")}
    # Nothing locally queued: adopt an active session, else recently played.
    for sess in api.active_sessions():
        sid = sess.get("sessionId") or sess.get("id") or ""
        if sid:
            playable = api.current_playable(str(sid))
            if playable:
                return _web_start(str(sid), playable, resumed=True)
    recent = api.recently_played()
    if recent:
        return _web_search_start(recent[0])
    return {"ok": False, "backend": "web",
            "error": "nothing to play — use am_search_play first"}


def _web_start(session_id: str, playable: Dict[str, Any],
               resumed: bool = False) -> Dict[str, Any]:
    metric = playable.get("metric_id") or ""
    if metric:
        _api().report_event(str(metric), "start")
    with _STATE_LOCK:
        _STATE["session_id"] = session_id
        _STATE["playable"] = playable
        _STATE["playing"] = True
        _STATE["play_started_at"] = time.time()
    _notify("play")
    return {"ok": True, "backend": "web", "sessionId": session_id,
            "resumed": resumed, "track": _STATE.get("track"),
            "note": "renderer: Nex UI (DRM streams need a Widevine-capable "
                    "viewer; otherwise open the Amazon Music app)"}


def _web_search_start(track: Dict[str, Any]) -> Dict[str, Any]:
    api = _api()
    mrn = track.get("play_params_id") or (
        "mrn:1.0:catalog:track:asin:" + (track.get("asin") or ""))
    if not track.get("play_params_id") and not track.get("asin"):
        return {"ok": False, "backend": "web",
                "error": "search result has no playable id"}
    payload = api.create_session(mrn)
    sid = _session_id(payload)
    playable = _first_playable(payload)
    if not sid or not playable:
        return {"ok": False, "backend": "web",
                "error": "playback session did not return a playable",
                "track": track}
    with _STATE_LOCK:
        _STATE["track"] = track
    result = _web_start(sid, playable)
    result["track"] = track
    result["query"] = _STATE.get("last_search")
    return result


def op_pause(args: Dict[str, Any]) -> Dict[str, Any]:
    backend = _backend()
    if backend == "web":
        try:
            r = _web_pause()
        except (AuthError, ApiError) as exc:
            r = _err_web(exc)
        _record("am_pause", args, r)
        return r
    if backend == "link":
        r = _open_deep_link("pause")
        r["backend"] = "link"
        _record("am_pause", args, r)
        return r
    r = {"ok": True, "backend": "stub", "link": DEEP_LINK + "pause",
         "note": "stub backend"}
    _record("am_pause", args, r)
    return r


def _web_pause() -> Dict[str, Any]:
    with _STATE_LOCK:
        playing = _STATE["playing"]
        playable = _STATE["playable"]
    if not playing:
        return {"ok": True, "backend": "web", "note": "not playing"}
    metric = (playable or {}).get("metric_id") or ""
    if metric:
        _api().report_event(str(metric), "stop", reason="userPause",
                            progress_seconds=_elapsed_seconds(),
                            duration_seconds=_elapsed_seconds())
    with _STATE_LOCK:
        _STATE["playing"] = False
    _notify("pause")
    return {"ok": True, "backend": "web",
            "track": _STATE.get("track")}


def op_toggle(args: Dict[str, Any]) -> Dict[str, Any]:
    if _backend() == "web":
        with _STATE_LOCK:
            playing = _STATE["playing"]
        return op_pause(args) if playing else op_play(args)
    if _backend() == "link":
        r = _open_deep_link("toggle")
        r["backend"] = "link"
        _record("am_toggle", args, r)
        return r
    r = {"ok": True, "backend": "stub", "link": DEEP_LINK + "toggle",
         "note": "stub backend"}
    _record("am_toggle", args, r)
    return r


def _web_skip(direction: str) -> Dict[str, Any]:
    with _STATE_LOCK:
        sid = _STATE["session_id"]
        playable = _STATE["playable"]
        playing = _STATE["playing"]
    if not sid or not playable:
        return {"ok": False, "backend": "web",
                "error": "no active playback session — play something first"}
    api = _api()
    metric = playable.get("metric_id") or ""
    if metric and playing:
        api.report_event(str(metric), "stop",
                         reason="userNext" if direction == "next"
                         else "userPrev",
                         progress_seconds=_elapsed_seconds(),
                         duration_seconds=_elapsed_seconds())
    fetcher = api.next_playable if direction == "next" \
        else api.previous_playable
    nxt = fetcher(str(sid))
    if not nxt:
        return {"ok": False, "backend": "web",
                "error": "no %s track in the queue" % direction}
    with _STATE_LOCK:
        _STATE["playable"] = nxt
        _STATE["playing"] = True
        _STATE["play_started_at"] = time.time()
    nmetric = nxt.get("metric_id") or ""
    if nmetric:
        api.report_event(str(nmetric), "start")
    _notify("play")
    return {"ok": True, "backend": "web", "skipped": direction,
            "track": _STATE.get("track")}


def op_next(args: Dict[str, Any]) -> Dict[str, Any]:
    if _backend() == "web":
        try:
            r = _web_skip("next")
        except (AuthError, ApiError) as exc:
            r = _err_web(exc)
        _record("am_next", args, r)
        return r
    if _backend() == "link":
        r = _open_deep_link("next")
        r["backend"] = "link"
        _record("am_next", args, r)
        return r
    r = {"ok": True, "backend": "stub", "link": DEEP_LINK + "next",
         "note": "stub backend"}
    _record("am_next", args, r)
    return r


def op_previous(args: Dict[str, Any]) -> Dict[str, Any]:
    if _backend() == "web":
        try:
            r = _web_skip("previous")
        except (AuthError, ApiError) as exc:
            r = _err_web(exc)
        _record("am_previous", args, r)
        return r
    if _backend() == "link":
        r = _open_deep_link("previous")
        r["backend"] = "link"
        _record("am_previous", args, r)
        return r
    r = {"ok": True, "backend": "stub", "link": DEEP_LINK + "previous",
         "note": "stub backend"}
    _record("am_previous", args, r)
    return r


def op_volume(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        level = int(args.get("level"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "am_volume needs integer 'level' 0..100"}
    if not 0 <= level <= 100:
        return {"ok": False, "error": "volume must be 0..100"}
    with _STATE_LOCK:
        _STATE["volume"] = level
    _notify("volume", {"volume": level})
    backend = _backend()
    if backend == "web":
        # Volume is a renderer-side property; the Web API has no endpoint
        # for it. Honest about that.
        r = {"ok": True, "backend": "web", "level": level,
             "note": "client-side volume (Web API has no volume endpoint)"}
    elif backend == "link":
        r = _open_deep_link("volume/%d" % level)
        r["backend"] = "link"
        r["level"] = level
    else:
        r = {"ok": True, "backend": "stub", "link": DEEP_LINK
             + ("volume/%d" % level), "level": level, "note": "stub backend"}
    _record("am_volume", {"level": level}, r)
    return r


def op_search_play(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "am_search_play needs 'query'"}
    if len(query) > 200:
        return {"ok": False, "error": "query too long (max 200 chars)"}
    backend = _backend()
    if backend == "web":
        try:
            tracks = _api().search_tracks(query)
            with _STATE_LOCK:
                _STATE["last_search"] = query
            if not tracks:
                r = {"ok": False, "backend": "web",
                     "error": "no results for: " + query}
            else:
                r = _web_search_start(tracks[0])
        except (AuthError, ApiError) as exc:
            r = _err_web(exc)
        r["query"] = query
        _record("am_search_play", {"query": query}, r)
        return r
    if backend == "link":
        r = _open_deep_link("search/" + urllib.parse.quote(query, safe=""))
        r["backend"] = "link"
        r["query"] = query
        _record("am_search_play", {"query": query}, r)
        return r
    r = {"ok": True, "backend": "stub",
         "link": DEEP_LINK + "search/" + urllib.parse.quote(query, safe=""),
         "query": query, "note": "stub backend"}
    _record("am_search_play", {"query": query}, r)
    return r


# ---------------------------------------------------------------------------
# Deep-link backend — the ONLY OS touch in this module, bounded to the
# amazonmusic:// scheme, and only via the explicit `link` backend.
# ---------------------------------------------------------------------------

def _open_deep_link(path: str) -> Dict[str, Any]:
    import subprocess
    import sys
    link = DEEP_LINK + path
    try:
        if hasattr(os, "startfile"):          # Windows
            os.startfile(link)  # type: ignore[attr-defined]
            opener = "startfile"
        elif sys.platform == "darwin":
            subprocess.Popen(["open", link],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            opener = "open"
        else:
            subprocess.Popen(["xdg-open", link],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            opener = "xdg-open"
        return {"ok": True, "backend": "link", "link": link, "opener": opener}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "backend": "link", "link": link,
                "error": "could not open Amazon Music: " + repr(exc)}


# ---------------------------------------------------------------------------
# Command log (Nex's own telemetry for its music actions — infra).
# ---------------------------------------------------------------------------

_LOG_LOCK = threading.Lock()
_LOG: List[Dict[str, Any]] = []
_LOG_CAP = 100


def _record(op: str, args: Dict[str, Any], result: Dict[str, Any]) -> None:
    with _LOG_LOCK:
        _LOG.append({"op": op, "args": args, "result": result,
                     "ts": time.time()})
        del _LOG[:-_LOG_CAP]


def recent_commands(limit: int = 20) -> List[Dict[str, Any]]:
    with _LOG_LOCK:
        return list(_LOG[-limit:])


# (name, description, schema, handler) — same tuple shape tools.py uses.
MUSIC_TOOLS: List[Any] = [
    ("am_play",
     "Amazon Music: start/resume playback.",
     {"type": "object", "properties": {}, "required": []},
     op_play),
    ("am_pause",
     "Amazon Music: pause playback.",
     {"type": "object", "properties": {}, "required": []},
     op_pause),
    ("am_toggle",
     "Amazon Music: toggle play/pause.",
     {"type": "object", "properties": {}, "required": []},
     op_toggle),
    ("am_next",
     "Amazon Music: skip to the next track.",
     {"type": "object", "properties": {}, "required": []},
     op_next),
    ("am_previous",
     "Amazon Music: go to the previous track.",
     {"type": "object", "properties": {}, "required": []},
     op_previous),
    ("am_volume",
     "Amazon Music: set playback volume (0-100).",
     {"type": "object",
      "properties": {"level": {"type": "integer",
                               "description": "0..100"}},
      "required": ["level"]},
     op_volume),
    ("am_search_play",
     "Amazon Music: search for a song/artist/album and play the best match.",
     {"type": "object",
      "properties": {"query": {"type": "string"}},
      "required": ["query"]},
     op_search_play),
]

MUSIC_TOOL_NAMES = frozenset(name for (name, _d, _s, _h) in MUSIC_TOOLS)


def tool_definitions() -> List[Dict[str, Any]]:
    return [{"name": n, "description": d, "inputSchema": s}
            for (n, d, s, _h) in MUSIC_TOOLS]


def call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    for (n, _d, _s, handler) in MUSIC_TOOLS:
        if n == name:
            return handler(arguments or {})
    return {"error": "unknown Amazon Music control: " + name}


# ---------------------------------------------------------------------------
# MusicUpstream — duck-types upstream.Upstream so the Amazon Music
# connector flows through discovery -> CapabilityRegistry -> policy ->
# executor exactly like a real MCP server. No special-casing anywhere.
# ---------------------------------------------------------------------------

class MusicUpstream:
    """The explicitly-implemented Amazon Music connector, as a connector."""

    def __init__(self) -> None:
        self.name = SERVER_NAME
        self.label = "Amazon Music"
        self.url = DEEP_LINK
        self.probe_timeout = 0.2
        self.call_timeout = 8.0
        self._initialized = True
        self._server_info = {"serverInfo": {
            "name": SERVER_NAME, "version": "2.0",
            "integration": "amazon-music-web-api"}}
        self._last_error = None
        self._tools_cache = list(tool_definitions())

    # -- lifecycle ----------------------------------------------------------
    def connect(self) -> Dict[str, Any]:
        self._initialized = True
        return {"serverInfo": self._server_info["serverInfo"]}

    # -- surface ------------------------------------------------------------
    def tools(self) -> List[Dict[str, Any]]:
        return [dict(t) for t in self._tools_cache]

    def call(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        # Return the MCP content envelope directly (tunnels._call_upstream
        # passes it through), so refusals keep isError=True instead of
        # being swallowed as empty results.
        result = call_tool(tool, args or {})
        text = json.dumps(result, ensure_ascii=False, indent=2,
                          sort_keys=True)
        return {"result": {"content": [{"type": "text", "text": text}],
                           "isError": "error" in result}}

    def status(self) -> Dict[str, Any]:
        cfg = Config()
        return {
            "name": self.name,
            "label": self.label,
            "online": True,
            "connected": True,
            "server_info": self._server_info,
            "tools_count": len(self._tools_cache),
            "url": self.url,
            "last_error": self._last_error,
            "health": "ok",
            "backend": _backend(),
            "web_api": {"configured": cfg.configured(),
                        "base": cfg.api_base,
                        "authorized": bool(_TOKEN_CACHE.get("token"))},
            "player": {"playing": bool(_STATE.get("playing")),
                       "volume": _STATE.get("volume"),
                       "track": _STATE.get("track")},
        }

    def __repr__(self) -> str:  # pragma: no cover
        return ("<MusicUpstream %s (%d controls, backend=%s)>"
                % (self.name, len(self._tools_cache), _backend()))
