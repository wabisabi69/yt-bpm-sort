"""Self-check: window tempos find a beat, skip silence, and fall back from Deezer to iTunes."""
import numpy as np
import librosa

import yt_bpm_sort as y

sr = 22050
clicks = librosa.clicks(times=np.arange(0, 30, 0.5), sr=sr, length=30 * sr)  # 120 BPM
tempos = y._window_tempos(clicks, sr)
assert len(tempos) == 4 and all(abs(y.fold_bpm(t)[0] - 120) < 4 for t in tempos), tempos
assert y._window_tempos(np.zeros(30 * sr, dtype="float32"), sr) == []

# Deezer clip is silent, iTunes clip has the beat: fallback must reach iTunes
audio = {"dz": np.zeros(30 * sr, dtype="float32"), "it": clicks}
y.deezer_find_preview = lambda a, s: ("dz", "Song / Artist")
y.itunes_find_preview = lambda a, s: ("it", "Song / Artist (itunes)")
y._download_preview = lambda url: (audio[url], sr)
y._ffmpeg = lambda: "ffmpeg"
raw, label = y.preview_tempo("Artist", "Song")
assert abs(y.fold_bpm(raw)[0] - 120) < 4 and "(itunes)" in label, (raw, label)

audio["it"] = audio["dz"]  # nothing anywhere: no tempo, reason kept
raw, why = y.preview_tempo("Artist", "Song")
assert raw is None and "no beat in any window" in why, why
print("ok")
