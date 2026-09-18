"""Amazon Music connector — OFFICIAL Web API tests.

The `web` backend is a client of the official Amazon Music Web API
(https://developer.amazon.com/docs/music/): LWA OAuth refresh-token auth,
POST /v1/search/tracks, POST /v1/playback/sessions, mandatory
/v1/playback/event start/stop reporting. The HTTP transport is stubbed
here — no network, no credentials in the repo.

Also covers: backend selection (auto/web/link/stub), the deep-link
backend's scheme bounding, and honest error paths.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import music_amazon as m  # noqa: E402


def _expect(cond, msg):
    print(("ok   - " if cond else "FAIL - ") + msg)
    if not cond:
        sys.exit(1)


def _reset(env_backend=None):
    """Clean module state + env between scenarios."""
    with m._STATE_LOCK:
        for k in ("session_id", "playable", "track", "play_started_at"):
            m._STATE[k] = None
        m._STATE["playing"] = False
        m._STATE["volume"] = 70
        m._STATE["last_search"] = None
    with m._LOG_LOCK:
        del m._LOG[:]
    with m._TOKEN_LOCK:
        m._TOKEN_CACHE.clear()
    m.set_notifier(None)
    m._API = None  # the memoized client snapshots Config at first use
    os.environ.pop("NEX_MUSIC_BACKEND", None)
    for k in ("NEX_AMZ_LWA_CLIENT_ID", "NEX_AMZ_LWA_CLIENT_SECRET",
              "NEX_AMZ_LWA_REFRESH_TOKEN", "NEX_AMZ_PROFILE_ID"):
        os.environ.pop(k, None)
    if env_backend:
        os.environ["NEX_MUSIC_BACKEND"] = env_backend


TRACK = {
    "_type": "Track",
    "id": "B084KVZP2G",
    "title": "One More Time",
    "artists": [{"id": "B000QJJOSO", "name": "Daft Punk"}],
    "album": {"id": "B084KYMDXT", "title": "Discovery"},
    "playParams": {"id": "mrn:1.0:catalog:track:asin:B084KVZP2G",
                   "selections": ["PLAY", "SHUFFLE", "STATION"]},
}

PLAYABLE = {
    "_type": "Playable",
    "metricId": '{"entityId":"B084KVZP2G","playQueueId":"pq-1"}',
    "title": "One More Time",
    "durationSeconds": 320,
    "mediaMetadata": {"url": "https://stream.example.com/one-more-time",
                      "drmType": "WIDEVINE", "mediaType": "DASH"},
}


class FakeTransport:
    """Routes _http_json calls like the real API would, and records
    every request for assertions."""

    def __init__(self):
        self.calls = []
        self.session_counter = 0
        self.next_responses = []      # optional queued (status, payload)
        self.fail_first_with_429 = None

    def __call__(self, method, url, headers, body=None):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(headers),
                           "body": body})
        if self.next_responses:
            return self.next_responses.pop(0)
        if url.endswith("/v1/search/tracks") and method == "POST":
            return 200, {"data": {"searchResults": {"items": [TRACK]}}}
        if url.endswith("/v1/playback/sessions") and method == "POST":
            self.session_counter += 1
            return 200, {"sessionId": "sess-%d" % self.session_counter,
                         "playables": [dict(PLAYABLE)]}
        if url.endswith("/v1/playback/sessions/active"):
            return 200, {"sessions": []}
        if url.endswith("/current"):
            return 200, {"current": dict(PLAYABLE)}
        if "/next?" in url:
            nxt = dict(PLAYABLE)
            nxt["title"] = "Aerodynamic"
            nxt["metricId"] = '{"entityId":"B084KY999"}'
            return 200, {"next": [nxt]}
        if "/previous?" in url:
            if getattr(self, "empty_previous", False):
                return 200, {"previous": []}
            prev = dict(PLAYABLE)
            prev["title"] = "Digital Love"
            prev["metricId"] = '{"entityId":"B084KY888"}'
            return 200, {"previous": [prev]}
        if url.endswith("/v1/playback/event"):
            return 200, {}
        if url.endswith("/v1/me/recentlyPlayedEntities"):
            return 200, {"entities": [TRACK]}
        return 404, {"message": "unexpected " + url}


def _configure_web():
    os.environ["NEX_AMZ_LWA_CLIENT_ID"] = "amzn1.application-oa2-client.test"
    os.environ["NEX_AMZ_LWA_CLIENT_SECRET"] = "test-secret"
    os.environ["NEX_AMZ_LWA_REFRESH_TOKEN"] = "atc-test-refresh"
    os.environ["NEX_AMZ_PROFILE_ID"] = "amzn1.application.testprofile"


# ===========================================================================
# 1. Backend selection
# ===========================================================================

_reset()
_expect(m._backend() == "stub", "auto -> stub when nothing is configured")
_expect(not m.web_configured(), "web_configured() False without credentials")

_configure_web()
_expect(m._backend() == "web", "auto -> web once LWA credentials exist")
_expect(m.web_configured(), "web_configured() True with credentials")

_reset()
os.environ["NEX_MUSIC_BACKEND"] = "web"
_expect(m._backend() == "web", "explicit web backend honored")

os.environ["NEX_MUSIC_BACKEND"] = "url"
_expect(m._backend() == "link", "legacy 'url' value maps to link backend")

os.environ["NEX_MUSIC_BACKEND"] = "nonsense"
_expect(m._backend() == "stub", "unknown backend value falls back safely")
_reset()


# ===========================================================================
# 2. Missing credentials fail FAST and honestly (no network probe)
# ===========================================================================

_reset()
os.environ["NEX_MUSIC_BACKEND"] = "web"
r = m.call_tool("am_play", {})
_expect(r.get("ok") is False and "credentials missing" in r.get("error", ""),
        "web backend without credentials: honest fast error")
_expect("NEX_AMZ_LWA_CLIENT_ID" in r.get("error", ""),
        "error names the missing env var")
_reset()


# ===========================================================================
# 3. LWA token refresh (refresh-token grant) + caching
# ===========================================================================

_reset()
_configure_web()


class FakeLWA:
    def __init__(self):
        self.calls = []

    def __call__(self, url, data=None, method=None, timeout=None, **kw):
        self.calls.append((url, data, method))
        body = json.dumps({"access_token": "TESTTOKEN", "expires_in": 3600})
        return _FakeResponse(body)


class _FakeResponse:
    def __init__(self, body):
        self._body = body.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


fake_lwa = FakeLWA()
real_urlopen = urllib.request.urlopen
urllib.request.urlopen = fake_lwa
try:
    tok1 = m._lwa_access_token(m.Config())
    tok2 = m._lwa_access_token(m.Config())
    _expect(len(fake_lwa.calls) == 1,
            "token endpoint called once (cached afterwards)")
    tok3 = m._lwa_access_token(m.Config(), force=True)
finally:
    urllib.request.urlopen = real_urlopen
_expect(tok1 == "TESTTOKEN" and tok2 == "TESTTOKEN",
        "LWA access token issued and cached")
url, data, method = fake_lwa.calls[0]
url_s = getattr(url, "full_url", str(url))
_expect("auth/o2/token" in url_s, "token endpoint is api.amazon.com/auth/o2")
raw = getattr(url, "data", None)
if raw is None:
    raw = data
form = urllib.parse.parse_qs(
    raw.decode() if isinstance(raw, bytes) else str(raw or ""))
_expect(form.get("grant_type") == ["refresh_token"]
        and form.get("refresh_token") == ["atc-test-refresh"]
        and form.get("client_id") == ["amzn1.application-oa2-client.test"],
        "refresh-token grant carries client id + refresh token")
_expect(len(fake_lwa.calls) == 2, "force=True refreshes the token")
_reset()


# ===========================================================================
# 4. search_play happy path: search -> session -> playable -> start event
# ===========================================================================

_reset()
_configure_web()
os.environ["NEX_MUSIC_BACKEND"] = "web"
with m._TOKEN_LOCK:  # authorized session (token refresh tested above)
    m._TOKEN_CACHE["token"] = "TESTTOKEN"
    m._TOKEN_CACHE["expires"] = time.time() + 3600
ft = FakeTransport()
m._http_json = ft
notified = []
m.set_notifier(lambda ev: notified.append(ev))
try:
    r = m.call_tool("am_search_play", {"query": "daft punk"})
finally:
    pass

_expect(r.get("ok") is True and r.get("backend") == "web",
        "am_search_play succeeds over the official Web API")
_expect(r.get("sessionId") == "sess-1", "playback session id surfaced")
_expect(r.get("track", {}).get("title") == "One More Time",
        "top search result is the played track")

searches = [c for c in ft.calls if c["url"].endswith("/v1/search/tracks")]
_expect(len(searches) == 1, "exactly one catalog search call")
sbody = searches[0]["body"]
_expect(sbody["searchFilters"] == [{"field": "name", "query": "daft punk"}]
        and sbody["limit"] == 5,
        "search body matches /v1/search/tracks contract")

creates = [c for c in ft.calls
           if c["url"].endswith("/v1/playback/sessions")]
_expect(len(creates) == 1, "exactly one create-session call")
cbody = creates[0]["body"]
_expect(cbody["playParams"]["id"]
        == "mrn:1.0:catalog:track:asin:B084KVZP2G",
        "session seeded with the track's MRN play id")
_expect(cbody["playbackOptions"]["playbackContinuationMode"]
        == "AUTOPLAY_ON",
        "autoplay continuation requested")

events = [c for c in ft.calls if c["url"].endswith("/v1/playback/event")]
_expect(len(events) == 1 and events[0]["body"]["event"]["type"] == "start",
        "mandatory start event reported")
_expect(events[0]["body"]["metricId"] == PLAYABLE["metricId"],
        "start event carries the playable's metricId")

_expect(ft.calls[0]["headers"].get("x-api-key")
        == "amzn1.application.testprofile",
        "x-api-key is the LWA Security Profile ID")
_expect(ft.calls[0]["headers"].get("Authorization") == "Bearer TESTTOKEN",
        "Authorization is the LWA bearer token")
play_calls = [c for c in ft.calls if "playback" in c["url"]]
_expect(all(h["headers"].get("X-Amzn-Audio-DRMType") == "WIDEVINE"
            and h["headers"].get("X-Amzn-Device-Id")
            for h in play_calls),
        "playback calls carry the documented audio/device headers")

with m._STATE_LOCK:
    playing = m._STATE["playing"]
_expect(playing is True, "player state: playing after search_play")
play_events = [e for e in notified if e.get("type") == "amazon_music"
               and e.get("action") == "play"]
_expect(len(play_events) == 1 and
        play_events[0]["playable"]["drm_type"] == "WIDEVINE",
        "UI notified with the playable (renderer event)")

# NOTE: DRM stream — the UI will honestly refuse to render it; that is
# the renderer's documented behavior (tested in the frontend contract).


# ===========================================================================
# 5. pause: stop event with userPause
# ===========================================================================

r = m.call_tool("am_pause", {})
_expect(r.get("ok") is True, "am_pause succeeds")
events = [c for c in ft.calls if c["url"].endswith("/v1/playback/event")]
last = events[-1]["body"]["event"]
_expect(last["type"] == "stop" and last["terminationReason"] == "userPause",
        "pause reports a stop event with terminationReason userPause")
with m._STATE_LOCK:
    playing = m._STATE["playing"]
_expect(playing is False, "player state: paused")


# ===========================================================================
# 6. toggle: paused -> play (resume), playing -> pause
# ===========================================================================

r = m.call_tool("am_toggle", {})
_expect(r.get("ok") is True and r.get("resumed") is True,
        "toggle resumes a paused session")
events = [c for c in ft.calls if c["url"].endswith("/v1/playback/event")]
_expect(events[-1]["body"]["event"]["type"] == "start",
        "resume reports a start event")
r = m.call_tool("am_toggle", {})
_expect(r.get("ok") is True, "toggle pauses a playing session")


# ===========================================================================
# 7. next / previous: queue navigation with correct events
# ===========================================================================

m.call_tool("am_toggle", {})          # resume
r = m.call_tool("am_next", {})
_expect(r.get("ok") is True and r.get("skipped") == "next",
        "am_next advances the queue")
events = [c for c in ft.calls if c["url"].endswith("/v1/playback/event")]
_expect(events[-1]["body"]["event"]["type"] == "start"
        and events[-1]["body"]["metricId"] == '{"entityId":"B084KY999"}',
        "next reports stop(userNext) then start for the new playable")
stops = [e["body"]["event"] for e in events
         if e["body"]["event"]["type"] == "stop"]
_expect(any(s.get("terminationReason") == "userNext" for s in stops),
        "the skipped track got a stop event with reason userNext")

ft.empty_previous = True
r = m.call_tool("am_previous", {})
_expect(r.get("ok") is False and "no previous track" in r.get("error", ""),
        "previous at queue start: honest failure")
ft.empty_previous = False
r = m.call_tool("am_previous", {})
_expect(r.get("ok") is True and r.get("skipped") == "previous",
        "am_previous works when the queue has one")


# ===========================================================================
# 8. volume: client-side, no HTTP
# ===========================================================================

calls_before = len(ft.calls)
r = m.call_tool("am_volume", {"level": 42})
_expect(r.get("ok") is True and r.get("level") == 42, "am_volume sets level")
_expect(r.get("note") and "no volume endpoint" in r["note"],
        "volume is honest about being client-side")
_expect(len(ft.calls) == calls_before,
        "volume issues NO Web API calls")
_expect(m.call_tool("am_volume", {"level": 120}).get("ok") is False,
        "volume >100 still refused")
_expect(m.call_tool("am_volume", {"level": -3}).get("ok") is False,
        "negative volume still refused")


# ===========================================================================
# 9. API error paths: closed-beta hint, 429 backoff
# ===========================================================================

ft.next_responses = [(403, {"message": "security profile not enabled"})]
r = m.call_tool("am_search_play", {"query": "something else"})
_expect(r.get("ok") is False and "closed beta" in r.get("hint", ""),
        "API 403 -> honest error naming the closed-beta requirement")

ft.next_responses = [(429, {"message": "Too Many Requests"}),
                     (200, {"data": {"searchResults": {"items": [TRACK]}}})]
sleeps = []
real_sleep = m.time.sleep
m.time.sleep = lambda s: sleeps.append(s)
try:
    r = m.call_tool("am_search_play", {"query": "backoff"})
finally:
    m.time.sleep = real_sleep
_expect(r.get("ok") is True and sleeps == [1.0],
        "429 backs off once and retries (documented behavior)")

_reset()


# ===========================================================================
# 10. link backend: bounded to the amazonmusic:// scheme
# ===========================================================================

_reset()
os.environ["NEX_MUSIC_BACKEND"] = "link"


class FakePopen:
    def __init__(self):
        self.argv = []

    def __call__(self, argv, **kw):
        self.argv.append(argv)

        class P:
            pass
        return P()


fp = FakePopen()
real_popen = __import__("subprocess").Popen
__import__("subprocess").Popen = fp
try:
    r = m.call_tool("am_search_play", {"query": "AC/DC"})
    link_search = r.get("link", "")
    r2 = m.call_tool("am_volume", {"level": 30})
finally:
    __import__("subprocess").Popen = real_popen

_expect(r.get("ok") is True and r.get("backend") == "link",
        "link backend plays via the Amazon Music app")
_expect(link_search.startswith("amazonmusic://search/"),
        "link stays inside the amazonmusic:// scheme")
_expect("AC%2FDC" in link_search, "query is URL-quoted into the deep link")
_expect(all(a[-1].startswith("amazonmusic://") for a in fp.argv)
        and all(len(a) == 2 for a in fp.argv),
        "every spawned opener argv is amazonmusic://-bounded")
_expect(r2.get("level") == 30, "link volume records the level")
_reset()


# ===========================================================================
# 11. stub backend: unchanged contract (CI / sandbox)
# ===========================================================================

_reset()
os.environ["NEX_MUSIC_BACKEND"] = "stub"
r = m.call_tool("am_play", {})
_expect(r.get("ok") is True and r.get("backend") == "stub"
        and r["link"].startswith("amazonmusic://"),
        "stub backend returns the honest structured payload")
_expect("unknown Amazon Music control" in m.call_tool("rm_rf", {})["error"],
        "stub backend: non-allowlisted control refused")
_reset()


# ===========================================================================
# 12. status(): honest backend/web_api summary, no secrets
# ===========================================================================

up = m.MusicUpstream()
st = up.status()
_expect(st["backend"] in ("auto", "web", "link", "stub"),
        "status reports the effective backend")
_expect("web_api" in st and isinstance(st["web_api"]["configured"], bool),
        "status includes web_api configuration state")
_expect(json.dumps(st).find("atc-test-refresh") == -1,
        "status never leaks credentials")


print("\nAll Amazon Music Web API tests passed.")
