"""Self-check: plan_moves replayed through YouTube Music's move-before-successor
call must land exactly on the target order, including duplicate videos."""
import random

import yt_bpm_sort as y

y.time.sleep = lambda s: None  # skip the real pacing between moves


class FakeYTM:
    """Applies ACTION_MOVE_VIDEO_BEFORE like YouTube Music: before successor, or to the end."""

    def __init__(self, set_ids, vids):
        self.order, self.vid = list(set_ids), dict(zip(set_ids, vids))

    def get_playlist(self, pid, limit=None):
        # like YT Music, show a few tracks under their audio-only twin's id (real: 10 of 638)
        return {"tracks": [{"videoId": self.vid[s] + ("-atv" if k % 20 == 3 else ""), "setVideoId": s}
                           for k, s in enumerate(self.order)]}

    def edit_playlist(self, pid, moveItem):
        moved, succ = moveItem if isinstance(moveItem, tuple) else (moveItem, None)
        self.order.remove(moved)
        self.order.insert(self.order.index(succ) if succ else len(self.order), moved)


for seed in range(300):
    rnd = random.Random(seed)
    n = rnd.randint(1, 40)
    vids = [f"v{rnd.randint(0, n // 2)}" for _ in range(n)]  # plenty of duplicate videos
    items = [{"id": f"i{k}", "snippet": {"title": f"t{k}", "resourceId": {"videoId": vids[k]}}}
             for k in range(n)]
    ytm = FakeYTM([f"s{k}" for k in range(n)], vids)
    target = [i["id"] for i in items]
    rnd.shuffle(target)
    y._ytm_moves(ytm, "pl", items, y.plan_moves([i["id"] for i in items], target))
    assert ytm.order == ["s" + t[1:] for t in target], seed
print("ok")
