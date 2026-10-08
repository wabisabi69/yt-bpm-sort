#!/usr/bin/env python3
"""
Reorder a YouTube playlist by BPM.

Step 1 (export): pull the playlist, guess artist/song from each title, look up
BPM on GetSongBPM, and write everything to a CSV you can check and correct.

Step 2 (apply): read the CSV, sort by BPM and push the new order to YouTube.
Apply is resumable: it re-reads the live playlist and skips anything already
in place, so if you hit the daily API quota just run it again tomorrow.

Usage:
  python yt_bpm_sort.py export "https://www.youtube.com/playlist?list=PLxxxx" --api-key YOUR_GETSONGBPM_KEY
  python yt_bpm_sort.py apply playlist_bpm.csv --dry-run
  python yt_bpm_sort.py apply playlist_bpm.csv
"""
import argparse
import csv
import os
import re
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/youtube"]
CLIENT_SECRET = "client_secret.json"
TOKEN_FILE = "token.json"
GETSONGBPM_URL = "https://api.getsong.co/search/"
UPDATE_COST = 50  # quota units per playlistItems.update
FIELDS = ["playlist_id", "position", "playlist_item_id", "video_id", "title",
          "channel", "artist_guess", "song_guess", "bpm", "bpm_source", "follows",
          "key", "key_source", "energy"]
# Columns a re-export carries over from the previous CSV for the same item
KEPT = ["bpm", "bpm_source", "follows", "key", "key_source", "energy"]
SKIP_TITLES = {"Deleted video", "Private video"}


# ---------- YouTube ----------

def get_youtube():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                sys.exit(f"Missing {CLIENT_SECRET}. Download your OAuth Desktop client JSON from Google Cloud.")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def parse_playlist_id(s):
    if "list=" in s:
        return parse_qs(urlparse(s).query)["list"][0]
    return s.strip()


def fetch_items(yt, playlist_id):
    items, token = [], None
    while True:
        resp = yt.playlistItems().list(
            part="snippet", playlistId=playlist_id, maxResults=50, pageToken=token
        ).execute()
        items.extend(resp["items"])
        token = resp.get("nextPageToken")
        if not token:
            break
    items.sort(key=lambda i: i["snippet"]["position"])
    return items


# ---------- Title parsing ----------

NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|video|audio|lyric|visuali[sz]er|\bhd\b|4k|remaster|"
    r"\blive\b|\bmv\b|feat|\bft\b)[^\)\]]*[\)\]]",
    re.I,
)
FEAT = re.compile(r"\s+(ft\.?|feat\.?|featuring)\s+.*$", re.I)
SPLIT = re.compile(r"\s+[-\u2013\u2014|]\s+")


def guess_artist_song(title, channel):
    t = NOISE.sub("", title).strip()
    parts = SPLIT.split(t, maxsplit=1)
    if len(parts) == 2:
        artist, song = parts
    else:
        # YouTube Music "Artist - Topic" channels put just the song in the title
        artist = re.sub(r"(\s*-\s*Topic|VEVO)$", "", channel or "").strip()
        song = t
    artist = FEAT.sub("", artist).strip(" \"'")
    song = FEAT.sub("", song).strip(" \"'")
    return artist, song


# ---------- BPM lookup ----------

def _norm(x):
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def lookup_bpm(api_key, artist, song):
    bpm, source = _lookup_once(api_key, artist, song)
    if bpm:
        return bpm, source
    # Retry with parenthetical or bracketed text stripped, e.g.
    # "Sunflower (Spider-Man: Into the Spider-Verse)" -> "Sunflower"
    bare = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", song).strip()
    if bare and bare != song:
        time.sleep(1)
        return _lookup_once(api_key, artist, bare)
    return bpm, source


def _lookup_once(api_key, artist, song):
    if artist:
        params = {"api_key": api_key, "type": "both", "lookup": f"song:{song} artist:{artist}"}
    else:
        params = {"api_key": api_key, "type": "song", "lookup": song}
    try:
        r = requests.get(GETSONGBPM_URL, params=params, timeout=15)
        r.raise_for_status()
        results = r.json().get("search")
    except (requests.RequestException, ValueError) as e:
        return "", f"error: {e}"
    if not isinstance(results, list) or not results:
        return "", "not found"
    best = next(
        (x for x in results if artist and _norm(x.get("artist", {}).get("name")) == _norm(artist)),
        results[0],
    )
    tempo = best.get("tempo")
    if not tempo:
        return "", "match had no tempo"
    matched = f"{best.get('title', '?')} by {best.get('artist', {}).get('name', '?')}"
    return str(tempo), f"getsongbpm: {matched}"


# ---------- Commands ----------

def cmd_export(args):
    api_key = args.api_key or os.environ.get("GETSONGBPM_API_KEY")
    if not api_key:
        print("No GetSongBPM key given, so the BPM column will be left blank for you to fill in.")

    # Keep BPMs, keys, energy and follows rules already in the CSV
    kept = {}
    if os.path.exists(args.out):
        with open(args.out, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                kept[row["playlist_item_id"]] = {k: row.get(k, "") for k in KEPT}
        print(f"Reusing {sum(1 for v in kept.values() if v['bpm'])} BPM values from existing {args.out}")

    yt = get_youtube()
    pid = parse_playlist_id(args.playlist)
    items = fetch_items(yt, pid)
    print(f"Fetched {len(items)} items")

    rows = []
    for it in items:
        sn = it["snippet"]
        title = sn["title"]
        channel = sn.get("videoOwnerChannelTitle", "")
        artist, song = guess_artist_song(title, channel)
        old = kept.get(it["id"], {})
        if old.get("bpm"):
            bpm, source = old["bpm"], old["bpm_source"]
        elif api_key and title not in SKIP_TITLES:
            bpm, source = lookup_bpm(api_key, artist, song)
            time.sleep(args.delay)
        else:
            bpm, source = "", ""
        rows.append({
            "playlist_id": pid,
            "position": sn["position"],
            "playlist_item_id": it["id"],
            "video_id": sn["resourceId"].get("videoId", ""),
            "title": title,
            "channel": channel,
            "artist_guess": artist,
            "song_guess": song,
            "bpm": bpm,
            "bpm_source": source,
            "follows": old.get("follows", ""),
            "key": old.get("key", ""),
            "key_source": old.get("key_source", ""),
            "energy": old.get("energy", ""),
        })
        print(f"{sn['position']:>4}  {bpm or '---':>6}  {title}")

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    missing = sum(1 for r in rows if not r["bpm"])
    print(f"\nWrote {args.out}. {missing} of {len(rows)} rows have no BPM.")
    print("Check the bpm column (watch for half/double-time values), then run 'apply'.")


BAND_WIDTH = 4    # BPM: tracks this close are free to reorder for key and energy
DUP_GAP = 20      # copies of the same song sit at least this many tracks apart
STAY_WEIGHT = 0.3  # how strongly a band keeps its current order (saves moves)


def _bpm(r):
    try:
        return float(r["bpm"])
    except (TypeError, ValueError, KeyError):
        return None


def _energy(r):
    try:
        return float(r["energy"])
    except (TypeError, ValueError, KeyError):
        return 0.0


def _camelot(k):
    m = re.fullmatch(r"(\d{1,2})([AB])", (k or "").strip().upper())
    return (int(m.group(1)), m.group(2)) if m else None


def key_cost(a, b):
    """0 for the same key, 1 for a Camelot neighbour (smooth), more for a clash."""
    ka, kb = _camelot(a.get("key")), _camelot(b.get("key"))
    if not ka or not kb:
        return 1.0
    step = min((ka[0] - kb[0]) % 12, (kb[0] - ka[0]) % 12)
    if ka[1] == kb[1]:
        return float(step)
    return 1.0 + step


def _order_band(band, prev):
    """Chain a band of similar-tempo tracks: compatible keys, quiet to loud,
    and current playlist order wherever the choice is close."""
    remaining = list(band)
    out = []
    while remaining:
        energies = [_energy(r) for r in remaining]
        lo, hi = min(energies), max(energies)
        lives = sorted(r["_live"] for r in remaining)

        def cost(r):
            k = key_cost(prev, r) if prev else 0.0
            e = (_energy(r) - lo) / (hi - lo) if hi > lo else 0.0
            stay = lives.index(r["_live"]) / max(len(lives) - 1, 1)
            return k + e + STAY_WEIGHT * stay * len(remaining) ** 0.5

        best = min(remaining, key=lambda r: (cost(r), r["_live"]))
        out.append(best)
        remaining.remove(best)
        prev = best
    return out


def dup_key(r):
    t = re.sub(r"[\(\[（【][^\)\]）】]*[\)\]）】]", "", r["title"])
    t = re.sub(r"^.*?\s[-–]\s", "", t)
    t = VARIANT.sub("", t)
    return re.sub(r"[\W_]+", "", t.lower())


def spread_duplicates(ordered, gap=DUP_GAP):
    """Hold back a copy of a song until it is at least `gap` tracks after the
    previous copy, so copies never play close together. Held copies are placed
    as soon as they are clear, which keeps them near their tempo."""
    out, last, held = [], {}, []

    def clear(r):
        k = dup_key(r)
        return not k or k not in last or len(out) - last[k] >= gap

    def place(r):
        k = dup_key(r)
        if k:
            last[k] = len(out)
        out.append(r)

    def release():
        for h in list(held):
            if clear(h):
                held.remove(h)
                place(h)

    for r in ordered:
        release()
        if clear(r):
            place(r)
        else:
            held.append(r)
    while held:
        release()
        if held:
            place(held.pop(0))  # end of list: nowhere further to push it
    return out


def arc_order(ordered):
    """Rise to a peak in the middle, then wind down."""
    return ordered[0::2] + ordered[1::2][::-1]


def build_target(rows, descending=False, arc=False):
    """Final order: felt BPM ascending, refined within small tempo bands for
    key and energy, duplicates spread out, then follows rules applied.
    Rows should arrive in current playlist order (ties keep that order)."""
    for n, r in enumerate(rows):
        r["_live"] = n
    timed = sorted((r for r in rows if _bpm(r) is not None), key=_bpm)
    untimed = [r for r in rows if _bpm(r) is None]  # no BPM: end, current order

    ordered, band, prev = [], [], None
    for r in timed:
        if band and _bpm(r) - _bpm(band[0]) > BAND_WIDTH:
            ordered += _order_band(band, prev)
            prev = ordered[-1]
            band = []
        band.append(r)
    if band:
        ordered += _order_band(band, prev)

    ordered = spread_duplicates(ordered)
    if arc:
        ordered = arc_order(ordered)
    if descending:
        ordered.reverse()
    return apply_follows(ordered + untimed)


def apply_follows(ordered):
    """Move any row with a 'follows' value to directly after its anchor.

    The follows column can name the anchor's title, song_guess, video_id or
    playlist_item_id (case-insensitive). Unknown anchors are reported and
    skipped. Chains work (C follows B follows A) as long as the CSV lists
    them in a consistent direction; a row never follows itself.
    """
    def norm(s):
        return (s or "").strip().lower()

    for _ in range(len(ordered)):  # enough passes to settle any chain
        moved = False
        for row in list(ordered):
            want = norm(row.get("follows"))
            if not want:
                continue
            anchor = next(
                (a for a in ordered if a is not row and want in (
                    norm(a["title"]), norm(a["song_guess"]),
                    norm(a["video_id"]), norm(a["playlist_item_id"]))),
                None,
            )
            if anchor is None:
                print(f"follows: no track matching '{row['follows']}' for '{row['title']}', ignoring")
                row["follows"] = ""
                continue
            ai = ordered.index(anchor)
            ri = ordered.index(row)
            if ri == ai + 1:
                continue
            ordered.remove(row)
            ordered.insert(ordered.index(anchor) + 1, row)
            moved = True
        if not moved:
            break
    return ordered


def _longest_increasing_subsequence(seq):
    """Indices into seq forming one longest strictly increasing subsequence."""
    import bisect
    tails, tails_idx, prev = [], [], [-1] * len(seq)
    for i, v in enumerate(seq):
        k = bisect.bisect_left(tails, v)
        if k == len(tails):
            tails.append(v)
            tails_idx.append(i)
        else:
            tails[k] = v
            tails_idx[k] = i
        prev[i] = tails_idx[k - 1] if k else -1
    out, i = [], tails_idx[-1] if tails_idx else -1
    while i != -1:
        out.append(i)
        i = prev[i]
    return out[::-1]


def plan_moves(current, target_ids):
    """Minimal moves: items already in correct relative order (the longest
    increasing subsequence) stay put; every other item is moved to sit
    directly after its predecessor in the target order."""
    rank = {item_id: i for i, item_id in enumerate(target_ids)}
    seq = [rank[i] for i in current]
    keep = {current[i] for i in _longest_increasing_subsequence(seq)}

    current = list(current)
    moves = []
    for i, item_id in enumerate(target_ids):
        if item_id in keep:
            continue
        current.remove(item_id)
        pos = current.index(target_ids[i - 1]) + 1 if i else 0
        current.insert(pos, item_id)
        moves.append((pos, item_id))
    return moves


def cmd_apply(args):
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("CSV is empty")
    pid = rows[0]["playlist_id"]

    yt = get_youtube()
    live = fetch_items(yt, pid)
    live_by_id = {i["id"]: i for i in live}
    current = [i["id"] for i in live]

    # Order rows by live position first: the sort is stable, so tracks with
    # equal BPM keep their current relative order and cost no moves.
    live_pos = {item_id: n for n, item_id in enumerate(current)}
    rows.sort(key=lambda r: live_pos.get(r["playlist_item_id"], len(current)))

    target_ids = [r["playlist_item_id"]
                  for r in build_target(rows, args.descending, getattr(args, "arc", False))
                  if r["playlist_item_id"] in live_by_id]
    seen = set(target_ids)
    target_ids += [i for i in current if i not in seen]  # items added since export go last

    moves = plan_moves(current, target_ids)
    print(f"{len(moves)} moves needed, about {len(moves) * UPDATE_COST} quota units "
          f"(default daily quota is 10,000).")
    if args.dry_run:
        for pos, item_id in moves:
            print(f"  move to {pos:>4}: {live_by_id[item_id]['snippet']['title']}")
        print("Dry run only, nothing changed.")
        return

    done = 0
    for pos, item_id in moves:
        it = live_by_id[item_id]
        body = {
            "id": item_id,
            "snippet": {"playlistId": pid, "resourceId": it["snippet"]["resourceId"], "position": pos},
        }
        try:
            _run(yt.playlistItems().update(part="snippet", body=body))
        except HttpError as e:
            if _is_quota(e):
                print(f"\nHit the daily quota after {done} moves. Run 'apply' again after the quota "
                      "resets at midnight Pacific time to continue.")
                return "quota"
            if e.resp.status in (409, 500, 502, 503, 504):
                print(f"  skipped {it['snippet']['title']}: YouTube kept failing ({e.resp.status}); "
                      "the next run retries it")
                continue
            raise
        done += 1
        print(f"  [{done}/{len(moves)}] {pos:>4}: {it['snippet']['title']}")
    # YouTube accepts position updates even when the playlist's default
    # ordering is not Manual, then keeps showing its own order. Verify.
    # Listings lag behind updates by a little while, so retry before warning.
    for wait in (15, 45, 90):
        time.sleep(wait)
        after = [i["id"] for i in fetch_items(yt, pid)]
        if after == target_ids:
            break
    if after != target_ids:
        stuck = sum(1 for a, b in zip(after, target_ids) if a == b)
        print(f"\nWARNING: YouTube reports only {stuck}/{len(target_ids)} tracks in the "
              "planned position. Set the playlist's Default ordering to Manual in "
              "YouTube, then run 'apply' again.")
        return "unsorted"
    print("\nDone. Playlist is sorted.")
    return "done"


def _is_quota(e):
    return isinstance(e, HttpError) and e.resp.status == 403 and "quota" in str(e).lower()


def _run(request, tries=6):
    """Execute an API request, retrying YouTube's transient errors (409
    SERVICE_UNAVAILABLE, 5xx) with backoff. Quota and other errors raise."""
    for n in range(tries):
        try:
            return request.execute()
        except HttpError as e:
            if e.resp.status in (409, 500, 502, 503, 504) and n < tries - 1:
                time.sleep(2 ** n)
                continue
            raise


# ---------- Mood playlists ----------

MOODS_FILE = "moods.json"
MOODS = [("Chill", None, 90), ("Mid", 90, 120), ("Hype", 120, None)]  # felt BPM ranges


def _mood_of(bpm):
    for name, lo, hi in MOODS:
        if (lo is None or bpm >= lo) and (hi is None or bpm < hi):
            return name
    return None


def cmd_moods(args):
    """Keep one private playlist per mood (Chill, Mid, Hype) in the same smart
    order as the main playlist. New playlists are filled by appending in final
    order, so they need inserts only, no moves."""
    import json
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    pid = rows[0]["playlist_id"]
    yt = get_youtube()

    live = [i["id"] for i in fetch_items(yt, pid)]
    pos = {item_id: n for n, item_id in enumerate(live)}
    rows.sort(key=lambda r: pos.get(r["playlist_item_id"], len(live)))
    desired = {name: [] for name, _, _ in MOODS}
    seen = set()
    for r in build_target(rows):
        b, vid = _bpm(r), r.get("video_id")
        if b is None or not vid or vid in seen or r["title"] in SKIP_TITLES:
            continue
        seen.add(vid)
        desired[_mood_of(b)].append(vid)

    store = json.load(open(MOODS_FILE)) if os.path.exists(MOODS_FILE) else {}
    ids = store.setdefault(pid, {})
    main_title = yt.playlists().list(part="snippet", id=pid).execute()["items"][0]["snippet"]["title"]

    try:
        for name, lo, hi in MOODS:
            rng = f"under {hi}" if lo is None else (f"{lo}+" if hi is None else f"{lo}-{hi}")
            if name not in ids:
                body = {"snippet": {"title": f"{main_title} - {name} ({rng} BPM)",
                                    "description": f"{name} tracks from '{main_title}', "
                                                   "ordered by felt tempo, key and energy."},
                        "status": {"privacyStatus": "private"}}
                ids[name] = _run(yt.playlists().insert(part="snippet,status", body=body))["id"]
                json.dump(store, open(MOODS_FILE, "w"), indent=1)
                print(f"Created playlist '{body['snippet']['title']}'")
            _sync_mood(yt, ids[name], name, desired[name])
    except HttpError as e:
        if _is_quota(e):
            print("\nHit the daily quota while building mood playlists. "
                  "The next run continues where this stopped.")
            return "quota"
        raise
    return "done"


def _sync_mood(yt, mpid, name, want):
    items = fetch_items(yt, mpid)
    have, extra = {}, []
    for i in items:
        vid = i["snippet"]["resourceId"].get("videoId")
        if vid in want and vid not in have:
            have[vid] = i
        else:
            extra.append(i)
    for i in extra:
        _run(yt.playlistItems().delete(id=i["id"]))
    missing = [v for v in want if v not in have]
    print(f"{name}: {len(want)} tracks, {len(have)} present, adding {len(missing)}, "
          f"removing {len(extra)}", flush=True)
    for n, vid in enumerate(missing, 1):
        try:
            _run(yt.playlistItems().insert(part="snippet", body={"snippet": {
                "playlistId": mpid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}))
        except HttpError as e:
            if _is_quota(e):
                raise
            print(f"  could not add {vid}: {e.resp.status}")
        if n % 25 == 0:
            print(f"  {name}: added {n}/{len(missing)}", flush=True)
    if not missing and not extra:
        items = fetch_items(yt, mpid)
    else:
        time.sleep(15)
        items = fetch_items(yt, mpid)
    by_vid = {i["snippet"]["resourceId"].get("videoId"): i for i in items}
    current = [i["id"] for i in items]
    target = [by_vid[v]["id"] for v in want if v in by_vid]
    target += [i for i in current if i not in set(target)]
    moves = plan_moves(current, target)
    for pos, item_id in moves:
        it = next(i for i in items if i["id"] == item_id)
        _run(yt.playlistItems().update(part="snippet", body={"id": item_id, "snippet": {
            "playlistId": mpid, "resourceId": it["snippet"]["resourceId"], "position": pos}}))
    if moves:
        print(f"  {name}: {len(moves)} moves to fix order")
    print(f"  {name}: in order", flush=True)


# ---------- Everything in one go ----------

CONFIG_FILE = "config.json"


def cmd_sync(args):
    """export, fill, analyse, apply, moods. Safe to run daily: each step only
    does what is outstanding and stops cleanly at the quota."""
    import json
    if not os.path.exists(CONFIG_FILE):
        sys.exit(f'Create {CONFIG_FILE} with {{"playlist": "<url or id>", "api_key": "<getsongbpm key>"}}')
    cfg = json.load(open(CONFIG_FILE))
    ns = argparse.Namespace
    try:
        print("== export"); cmd_export(ns(playlist=cfg["playlist"], api_key=cfg.get("api_key"),
                                          out=args.csv, delay=0.5))
        print("== fill"); cmd_fill(ns(csv=args.csv, delay=0.5))
        print("== analyse"); cmd_analyse(ns(csv=args.csv, api_key=cfg.get("api_key"), delay=0.4))
        print("== apply")
        if cmd_apply(ns(csv=args.csv, descending=False, dry_run=False, arc=False)) != "done":
            return
        print("== moods"); cmd_moods(ns(csv=args.csv))
    except HttpError as e:
        if _is_quota(e):
            print("\nDaily quota used up. The next run continues from here.")
            return
        raise


VARIANT = re.compile(
    r"sped\s*up|speed\s*up|slowed|reverb|nightcore|remix|\bedit\b|\blive\b|"
    r"acoustic|instrumental|extended|cover|karaoke|8d",
    re.I,
)


def _match_score(track, artist, song):
    """Rank a Deezer result: right artist, exact title, and no version tag
    (sped up, slowed, remix...) that the YouTube title does not also have."""
    title = track.get("title", "")
    score = 0
    if artist and _norm(track["artist"]["name"]) == _norm(artist):
        score += 4
    if _norm(title) == _norm(song):
        score += 2
    wanted = {m.lower() for m in VARIANT.findall(song)}
    got = {m.lower() for m in VARIANT.findall(title)}
    score -= 3 * len(got - wanted)
    return score


def deezer_find_preview(artist, song):
    """Return (preview_url, 'Title / Artist') for the best Deezer match, or (None, why)."""
    q = f"{song} {artist}".strip()
    try:
        r = requests.get("https://api.deezer.com/search",
                         params={"q": q, "limit": 5}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data") or []
    except (requests.RequestException, ValueError) as e:
        return None, f"deezer error: {e}"
    if not data:
        return None, "deezer: no result"
    hit = max(data, key=lambda t: _match_score(t, artist, song))
    if _match_score(hit, artist, song) < 0:
        return None, f"deezer: only other versions found, e.g. '{hit['title']}'"
    if not hit.get("preview"):
        return None, "deezer: match has no preview"
    return hit["preview"], f"{hit['title']} / {hit['artist']['name']}"


def estimate_bpm_from_url(url):
    """Download a Deezer 30s preview and estimate tempo from its middle 20s."""
    import os
    import tempfile
    import librosa
    import soundfile as sf

    audio = requests.get(url, timeout=30).content
    # libsndfile's mp3 sniffing fails on BytesIO, so go through a temp file
    fd, path = tempfile.mkstemp(suffix=".mp3")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        y, sr = sf.read(path, dtype="float32", always_2d=True)
    finally:
        os.unlink(path)
    y = y.mean(axis=1)
    # Previews are mid-song clips; trim another 5s each side to dodge fades
    if len(y) > 20 * sr:
        edge = 5 * sr
        y = y[edge:-edge]
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    # librosa may return a 1-element array
    import numpy as np
    return float(np.atleast_1d(tempo)[0])


FELT_MAX = 145  # sort by felt tempo: 160 "feels like" 80 to most listeners


def fold_bpm(bpm, lo=70, hi=FELT_MAX):
    """Fold into the felt-tempo range; returns (folded, was_folded).

    Returns (None, False) for a non-positive tempo, which librosa reports
    for silent or beatless audio."""
    if not bpm or bpm <= 0:
        return None, False
    folded = bpm
    while folded < lo:
        folded *= 2
    while folded > hi:
        folded /= 2
    return round(folded, 1), folded != bpm


def cmd_fill(args):
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    blanks = [r for r in rows if not (r.get("bpm") or "").strip()
              and r["title"] not in SKIP_TITLES]
    print(f"{len(blanks)} rows without BPM. Trying Deezer previews...", flush=True)

    def save():
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)

    filled = 0
    for n, r in enumerate(blanks, 1):
        label = f"[{n}/{len(blanks)}] {r['artist_guess']} - {r['song_guess']}"
        url, matched = deezer_find_preview(r["artist_guess"], r["song_guess"])
        if not url:
            print(f"  skip  {label}  ({matched})", flush=True)
            time.sleep(args.delay)
            continue
        try:
            raw = estimate_bpm_from_url(url)
        except Exception as e:
            print(f"  skip  {label}  (analysis failed: {e})", flush=True)
            time.sleep(args.delay)
            continue
        bpm, was_folded = fold_bpm(raw)
        if bpm is None:
            print(f"  skip  {label}  (no beat detected)", flush=True)
            time.sleep(args.delay)
            continue
        note = f" (raw {raw:.1f})" if was_folded else ""
        r["bpm"] = str(bpm)
        r["bpm_source"] = f"deezer_preview:librosa: {matched}{note}"
        filled += 1
        print(f"  {bpm:>6}  {label}  <- {matched}{note}", flush=True)
        if filled % 20 == 0:
            save()  # a crash or kill keeps the work done so far
        time.sleep(args.delay)

    save()
    print(f"\nFilled {filled} of {len(blanks)} blank rows. Review the CSV, then run 'apply'.")


# ---------- Key and energy ----------

KK_MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
KK_MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
# Fixed reference levels so energy scores stay comparable as songs are added
ENERGY_REF = {"rms": (0.25, 0.10), "onsets": (5.0, 1.6), "flux": (1.1, 0.25)}


def camelot_from_pitch(pc, minor):
    if minor:
        pc = (pc + 3) % 12  # relative major shares the Camelot number
    return f"{(7 * pc + 7) % 12 + 1}{'A' if minor else 'B'}"


def camelot_from_open_key(ok):
    m = re.fullmatch(r"(\d{1,2})([dm])", (ok or "").strip())
    if not m:
        return ""
    return f"{(int(m.group(1)) + 6) % 12 + 1}{'A' if m.group(2) == 'm' else 'B'}"


def _ffmpeg():
    import shutil
    return shutil.which("ffmpeg")


def itunes_find_preview(artist, song):
    """Apple's catalogue covers some tracks Deezer lacks (e.g. Chinese indie)."""
    for country in ("US", "TW", "JP", "KR", "AU"):
        try:
            res = requests.get("https://itunes.apple.com/search",
                               params={"term": f"{artist} {song}", "entity": "song",
                                       "limit": 5, "country": country}, timeout=15).json()
        except (requests.RequestException, ValueError):
            continue
        cands = [{"title": t.get("trackName", ""), "artist": {"name": t.get("artistName", "")},
                  "preview": t.get("previewUrl")} for t in res.get("results", [])]
        cands = [c for c in cands if c["preview"]]
        if cands:
            best = max(cands, key=lambda c: _match_score(c, artist, song))
            if _match_score(best, artist, song) >= 2:
                return best["preview"], f"{best['title']} / {best['artist']['name']} (itunes)"
    return None, "itunes: no match"


def load_preview(artist, song):
    """Return (samples, sample_rate, label) for the middle of a 30s preview, or (None, None, why)."""
    import tempfile
    import subprocess
    import soundfile as sf

    url, label = deezer_find_preview(artist, song)
    if not url and _ffmpeg():
        url, label = itunes_find_preview(artist, song)
    if not url:
        return None, None, label
    d = tempfile.mkdtemp()
    src = os.path.join(d, "p.mp3" if "dzcdn" in url else "p.m4a")
    wav = os.path.join(d, "p.wav")
    try:
        with open(src, "wb") as f:
            f.write(requests.get(url, timeout=30).content)
        if src.endswith(".m4a"):
            subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", src, wav], check=True)
            src = wav
        y, sr = sf.read(src, dtype="float32", always_2d=True)
    finally:
        for p in (src, wav, os.path.join(d, "p.mp3"), os.path.join(d, "p.m4a")):
            if os.path.exists(p):
                os.unlink(p)
        os.rmdir(d)
    y = y.mean(axis=1)
    if len(y) > 20 * sr:
        y = y[5 * sr:-5 * sr]
    return y, sr, label


def estimate_key(y, sr):
    """Krumhansl-Kessler key estimate from average chroma, as a Camelot code."""
    import numpy as np
    import librosa
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    best, best_r = None, -2.0
    for minor, prof in ((False, KK_MAJOR), (True, KK_MINOR)):
        for pc in range(12):
            r = np.corrcoef(chroma, np.roll(prof, pc))[0, 1]
            if r > best_r:
                best, best_r = (pc, minor), r
    return camelot_from_pitch(*best)


def estimate_energy(y, sr):
    """Loudness, beat density and attack strength, as one score around 0."""
    import numpy as np
    import librosa
    feats = {
        "rms": float(np.sqrt(np.mean(y ** 2))),
        "onsets": len(librosa.onset.onset_detect(y=y, sr=sr)) / (len(y) / sr),
        "flux": float(np.mean(librosa.onset.onset_strength(y=y, sr=sr))),
    }
    z = [(feats[k] - m) / s for k, (m, s) in ENERGY_REF.items()]
    return round(sum(z) / len(z), 2)


def getsongbpm_key(api_key, artist, song):
    params = {"api_key": api_key, "type": "both", "lookup": f"song:{song} artist:{artist}"}
    try:
        res = requests.get(GETSONGBPM_URL, params=params, timeout=15).json().get("search")
    except (requests.RequestException, ValueError):
        return ""
    if not isinstance(res, list):
        return ""
    hit = next((x for x in res if _norm(x.get("artist", {}).get("name")) == _norm(artist)), None)
    return camelot_from_open_key(hit.get("open_key")) if hit else ""


def cmd_analyse(args):
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    api_key = getattr(args, "api_key", None) or os.environ.get("GETSONGBPM_API_KEY")
    todo = [r for r in rows if r["title"] not in SKIP_TITLES
            and (not r.get("key") or not r.get("energy"))]
    print(f"{len(todo)} rows need key or energy.", flush=True)

    def save():
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    for n, r in enumerate(todo, 1):
        label = f"[{n}/{len(todo)}] {r['title'][:45]}"
        if not r.get("key") and api_key and r.get("bpm_source", "").startswith("getsongbpm"):
            k = getsongbpm_key(api_key, r["artist_guess"], r["song_guess"])
            if k:
                r["key"], r["key_source"] = k, "getsongbpm"
            time.sleep(args.delay)
        try:
            y, sr, why = load_preview(r["artist_guess"], r["song_guess"])
        except Exception as e:
            y, why = None, f"preview failed: {e}"
        if y is not None:
            if not r.get("key"):
                r["key"], r["key_source"] = estimate_key(y, sr), f"preview: {why}"
            if not r.get("energy"):
                r["energy"] = str(estimate_energy(y, sr))
        print(f"  {r.get('key') or '-':>3}  {r.get('energy') or '-':>6}  {label}"
              + ("" if y is not None else f"  ({why})"), flush=True)
        if n % 20 == 0:
            save()
        time.sleep(args.delay)
    save()
    print("Analysis saved.")


def main():
    p = argparse.ArgumentParser(description="Sort a YouTube playlist by BPM")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="Fetch playlist and look up BPMs into a CSV")
    e.add_argument("playlist", help="Playlist URL or ID")
    e.add_argument("--api-key", help="GetSongBPM API key (or set GETSONGBPM_API_KEY)")
    e.add_argument("--out", default="playlist_bpm.csv")
    e.add_argument("--delay", type=float, default=0.5, help="Seconds between BPM lookups")
    e.set_defaults(func=cmd_export)

    fl = sub.add_parser("fill", help="Fill blank BPMs by analysing Deezer 30s previews locally")
    fl.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    fl.add_argument("--delay", type=float, default=0.5, help="Seconds between Deezer lookups")
    fl.set_defaults(func=cmd_fill)

    a = sub.add_parser("apply", help="Reorder the playlist using the CSV")
    a.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    a.add_argument("--descending", action="store_true", help="Fastest first instead")
    a.add_argument("--dry-run", action="store_true", help="Show the planned moves only")
    a.add_argument("--arc", action="store_true", help="Rise to a peak mid-playlist, then wind down")
    a.set_defaults(func=cmd_apply)

    an = sub.add_parser("analyse", help="Add musical key and energy for each track from previews")
    an.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    an.add_argument("--api-key", help="GetSongBPM API key, used for keys where it has them")
    an.add_argument("--delay", type=float, default=0.4)
    an.set_defaults(func=cmd_analyse)

    mo = sub.add_parser("moods", help="Build Chill / Mid / Hype playlists in the same order")
    mo.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    mo.set_defaults(func=cmd_moods)

    sy = sub.add_parser("sync", help="export, fill, analyse, apply and moods in one go (uses config.json)")
    sy.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    sy.set_defaults(func=cmd_sync)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
