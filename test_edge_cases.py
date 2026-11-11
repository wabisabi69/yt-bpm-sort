"""Self-check for edge cases found in review: dead YT Music login, unpaired set ids,
first-artist split, and the non-Latin top-hit rule. Offline: network calls are faked."""
import sys
import types

import yt_bpm_sort as y

# 1. A signed-out browser.json falls back to the Data API instead of crashing.
class DeadYTM:
    def __init__(self, auth):
        pass

    def get_library_playlists(self, limit=None):
        raise Exception("401 Unauthorized")

sys.modules["ytmusicapi"] = types.SimpleNamespace(YTMusic=DeadYTM)
real_exists = y.os.path.exists
y.os.path.exists = lambda p: True
try:
    assert y.get_ytmusic() is None
finally:
    y.os.path.exists = real_exists

# 2. Fallback pairing never hands out a missing setVideoId.
class FakeYTM:
    def get_playlist(self, pid, limit=None):
        return {"tracks": [{"videoId": "a", "setVideoId": None}, {"videoId": "a", "setVideoId": "s2"},
                           {"videoId": "c", "setVideoId": "s3"}]}  # lengths differ: pair by video

items = [{"id": f"i{k}", "snippet": {"resourceId": {"videoId": v}}} for k, v in enumerate("ab")]
assert y._ytm_set_ids(FakeYTM(), "pl", items) == {"i0": "s2"}

# 3. "Lil Nas X" stays whole; "A x B" still splits.
assert ("Lil Nas X", "Old Town Road") in y._search_variants("Lil Nas X", "Old Town Road")
assert ("A", "Song") in y._search_variants("A x B", "Song")

# 4. A non-Latin title trusts Deezer's top hit only when an artist is known.
class Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {"data": [{"id": 1, "title": "Dope", "artist": {"name": "BTS"}, "preview": "u"}]}

y.requests.get = lambda *a, **k: Resp()
assert y._deezer_hit("BTS", "쩔어")[0] is not None
assert y._deezer_hit("", "쩔어")[0] is None
print("ok")
