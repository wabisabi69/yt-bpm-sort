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

# 5. Short artist names match as whole words, not as a substring of another name.
assert not y.artist_ok("IU", "Sirius") and not y.artist_ok("V", "Avicii")
assert y.artist_ok("IU", "IU & SUGA") and y.artist_ok("U2", "u2") and y.artist_ok("Run DMC", "Run-D.M.C.")
assert y.artist_ok("Louis Armstrong", "Louis Armstrong And His All Stars")

# 6. GetSongBPM half-time readings are doubled into the felt range; fast ones are left alone.
class Song:
    def __init__(self, tempo):
        self.tempo = tempo

    def raise_for_status(self):
        pass

    def json(self):
        return {"search": [{"title": "Lean On", "tempo": self.tempo, "artist": {"name": "Major Lazer"}}]}

y.requests.get = lambda *a, **k: Song("49")
assert y._lookup_once("k", "Major Lazer", "Lean On") == ("98", "getsongbpm: Lean On by Major Lazer (raw 49)")
y.requests.get = lambda *a, **k: Song("174")
assert y._lookup_once("k", "Major Lazer", "Lean On") == ("174", "getsongbpm: Lean On by Major Lazer")
y.requests.get = lambda *a, **k: Song(None)
assert y._lookup_once("k", "Major Lazer", "Lean On") == ("", "match had no tempo")

# 7. Windows that read over 5% apart in felt tempo are flagged; half/double readings agree.
assert y._spread([83.0, 166.0, 84.0]) == ""
assert y._spread([124.0, 166.7]) == ", windows disagree: felt 83.3-124"

# 8. _ytm_moves counts the moves it could not make (here: a track YouTube Music does not list).
class HidingYTM:
    def get_playlist(self, pid, limit=None):
        return {"tracks": [{"videoId": "b", "setVideoId": "sb"}]}

    def edit_playlist(self, pid, moveItem):
        pass

two = [{"id": f"i{k}", "snippet": {"title": v, "resourceId": {"videoId": v}}} for k, v in enumerate("ab")]
y.time.sleep = lambda s: None
assert y._ytm_moves(HidingYTM(), "pl", two, [(1, "i0")]) == 1
assert y._ytm_moves(HidingYTM(), "pl", two, [(0, "i1")]) == 0

# 9. A slow quiet intro does not decide the song: energy and tempo come from the loud part.
import numpy as np
import librosa

sr = y.FULL_SR
intro = 0.05 * librosa.clicks(times=np.arange(0, 60, 1.0), sr=sr, length=60 * sr)  # 60 BPM, quiet
rng = np.random.default_rng(0)
drop = 0.8 * librosa.clicks(times=np.arange(0, 60, 60 / 128), sr=sr, length=60 * sr) \
    + 0.1 * rng.standard_normal(60 * sr)  # 128 BPM, loud
y._youtube_audio = lambda vid, sr=sr: (np.concatenate([intro, drop]).astype("float32"), sr)
energy, raw, _, _ = y.full_track("v")
assert energy > y.estimate_energy(intro, sr) + 1, energy
assert abs(y.fold_bpm(raw)[0] - 128) < 4, raw

# 10. Moods: a slow but energetic track is Mid, never Chill; a 'mood' value still wins.
assert y.mood_for({"bpm": "80", "energy": "0.6", "bpm_source": ""}) == "Mid"
assert y.mood_for({"bpm": "80", "energy": "0.4", "bpm_source": ""}) == "Chill"
assert y.mood_for({"bpm": "80", "energy": "0.9", "bpm_source": "", "mood": "Chill"}) == "Chill"

# 11. A re-measure undoes 'kept fast' so fresh energy decides again; manual rows are left alone.
r = {"bpm": "161.4", "bpm_source": "deezer_bpm: X (raw 161.5) kept fast (energy 0.51)"}
y._undo_kept_fast(r)
assert r == {"bpm": "80.7", "bpm_source": "deezer_bpm: X (raw 161.5)"}, r
r = {"bpm": "166.8", "bpm_source": "manual: user counts double-time of 83.4, kept fast"}
y._undo_kept_fast(r)
assert r["bpm"] == "166.8"
print("ok")
