"""Self-check: energy decides whether a halved tempo is undone."""
from yt_bpm_sort import unfold_if_energetic as unfold

src = "deezer_preview:librosa: X / Y (raw 166.7)"
hot = {"bpm": "83.4", "bpm_source": src, "energy": "0.9"}
assert unfold(hot) and hot["bpm"] == "166.8" and "kept fast" in hot["bpm_source"]
assert not unfold(hot)  # idempotent on re-runs
assert not unfold({"bpm": "83.4", "bpm_source": src, "energy": "-0.2"})  # calm: stays halved
assert not unfold({"bpm": "83.4", "bpm_source": src, "energy": ""})  # no energy yet
assert not unfold({"bpm": "120", "bpm_source": "deezer_preview:librosa: X / Y (raw 60.0)", "energy": "1"})  # doubled up, not halved
assert not unfold({"bpm": "128", "bpm_source": "getsongbpm", "energy": "2"})  # never folded
assert not unfold({"bpm": "124", "bpm_source": src, "energy": "0.9"})  # corrected by hand: not doubled to 248
assert unfold({"bpm": "80.7", "bpm_source": "x (raw 161.5)", "energy": "0.9"})  # 161.5 was rounded for show
print("ok")
