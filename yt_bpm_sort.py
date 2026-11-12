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
import functools
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
          "key", "key_source", "energy", "mood", "energy_source"]
# Columns a re-export carries over from the previous CSV for the same item
KEPT = ["bpm", "bpm_source", "follows", "key", "key_source", "energy", "mood", "artist_guess",
        "energy_source"]
SKIP_TITLES = {"Deleted video", "Private video"}


# ---------- YouTube ----------

def get_youtube():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        refreshed = False
        if creds and creds.expired and creds.refresh_token:
            from google.auth.exceptions import RefreshError
            try:
                creds.refresh(Request())
                refreshed = True
            except RefreshError:
                # Apps left in Testing mode get logins that expire after 7 days.
                # Never open a browser here: the scheduled run would hang on it.
                sys.exit("GOOGLE LOGIN EXPIRED: run 'python yt_bpm_sort.py login' to sign in "
                         "again. To stop this recurring every 7 days, publish the app "
                         "(Google Auth Platform > Audience > Publish app).")
        if not refreshed:
            if not os.path.exists(CLIENT_SECRET):
                sys.exit(f"Missing {CLIENT_SECRET}. Download your OAuth Desktop client JSON from Google Cloud.")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def cmd_login(args):
    """Sign in again in the browser and save a fresh token."""
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(port=0)
    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())
    print("Signed in. Saved a fresh token.")


YTM_AUTH = "browser.json"


def get_ytmusic():
    """YouTube Music's web API, used for playlist writes when browser.json exists:
    it is not billed against the Data API's daily quota."""
    if not os.path.exists(YTM_AUTH):
        return None
    from ytmusicapi import YTMusic
    try:
        ytm = YTMusic(YTM_AUTH)
        ytm.get_library_playlists(limit=1)  # fails fast once the browser session is signed out
        return ytm
    except Exception as e:
        print(f"YouTube Music login in {YTM_AUTH} no longer works ({e}); using the quota-billed API.")
        return None


def _ytm_set_ids(ytm, pid, items):
    """Map Data API playlist item ids to YouTube Music setVideoIds, pairing the
    k-th copy of each video in both listings (YT Music can hide unavailable videos)."""
    tracks = ytm.get_playlist(pid, limit=None)["tracks"]
    if len(tracks) == len(items):
        # Same playlist order in both listings, but YT Music shows some videos under their
        # audio-only twin's id, so pair by position once most ids agree.
        same = sum(t.get("videoId") == i["snippet"]["resourceId"].get("videoId")
                   for t, i in zip(tracks, items))
        if len(items) - same <= max(1, len(items) // 10):
            return {i["id"]: t["setVideoId"] for t, i in zip(tracks, items) if t.get("setVideoId")}
    by_vid = {}
    for t in tracks:
        if t.get("setVideoId"):
            by_vid.setdefault(t.get("videoId"), []).append(t["setVideoId"])
    out = {}
    for i in items:
        q = by_vid.get(i["snippet"]["resourceId"].get("videoId"))
        if q:
            out[i["id"]] = q.pop(0)
    return out


def _ytm_moves(ytm, pid, items, moves):
    """Apply plan_moves output through YouTube Music. It moves an item before a
    successor rather than to a position, so replay the moves to find each successor.
    Returns how many moves could not be made."""
    sids = _ytm_set_ids(ytm, pid, items)
    title = {i["id"]: i["snippet"]["title"] for i in items}
    cur = [i["id"] for i in items]
    fails = skipped = 0
    for n, (pos, item_id) in enumerate(moves, 1):
        cur.remove(item_id)
        cur.insert(pos, item_id)
        if item_id not in sids:
            print(f"  skipped {title[item_id]}: not listed in YouTube Music")
            skipped += 1
            continue
        succ = next((sids[x] for x in cur[pos + 1:] if x in sids), None)
        for wait in (2, 10, None):  # back off on rate limits before giving up on this move
            try:
                ytm.edit_playlist(pid, moveItem=(sids[item_id], succ) if succ else sids[item_id])
                fails = 0
                break
            except Exception as e:
                if wait:
                    time.sleep(wait)
                    continue
                fails += 1
                if fails >= 5:  # login expired or the endpoint changed: stop, don't spam
                    raise
                print(f"  skipped {title[item_id]}: {e}")
        else:
            skipped += 1  # gave up on this move
            continue
        time.sleep(0.2)
        print(f"  [{n}/{len(moves)}] {pos:>4}: {title[item_id]}", flush=True)
    return skipped


def _ytm_add(ytm, pid, vids):
    try:
        r = ytm.add_playlist_items(pid, vids, duplicates=True)
        return isinstance(r, dict) and r.get("status") == "STATUS_SUCCEEDED"
    except Exception:
        return False


def parse_playlist_id(s):
    if "list=" in s:
        return parse_qs(urlparse(s).query)["list"][0]
    return s.strip()


def fetch_items(yt, playlist_id):
    items, token = [], None
    while True:
        req = yt.playlistItems().list(
            part="snippet", playlistId=playlist_id, maxResults=50, pageToken=token)
        for wait in (5, 15, 30, None):
            try:
                resp = req.execute()
                break
            except HttpError as e:
                # YouTube occasionally answers 404 or 5xx for an existing playlist
                if wait is None or e.resp.status not in (404, 409, 500, 502, 503, 504):
                    raise
                time.sleep(wait)
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


def topic_artists(yt, video_ids):
    """Real artist for auto-generated ("- Topic") uploads, read from the
    'Provided to YouTube by' description line 'Title · Artist · Artist'.
    Channel names can be placeholders such as 'Release - Topic'."""
    out = {}
    ids = [v for v in video_ids if v]
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(part="snippet", id=",".join(ids[i:i + 50])).execute()
        for v in resp.get("items", []):
            for line in v["snippet"].get("description", "").splitlines():
                parts = [p.strip() for p in line.split(" · ")]
                if len(parts) >= 2 and parts[1]:
                    out[v["id"]] = parts[1]
                    break
    return out


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
    # Never fall back from a remix or mashup to the original: it has another tempo
    if bare and bare != song and not VARIANT.search(song):
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
    if artist:
        best = next((x for x in results if artist_ok(x.get("artist", {}).get("name"), artist)), None)
        if best is None:
            return "", "only other artists' songs found"
    else:
        best = results[0]
    tempo = best.get("tempo")
    try:
        # Double half-time readings ('Lean On' comes back as 49) like the other sources do.
        # Fast ones stay: the energy rule in mood_for already sends calm ones to Mid.
        bpm, was_folded = fold_bpm(float(tempo or 0), hi=float("inf"))
    except (TypeError, ValueError):
        bpm = None
    if bpm is None:
        return "", "match had no tempo"
    matched = f"{best.get('title', '?')} by {best.get('artist', {}).get('name', '?')}"
    return f"{bpm:g}", f"getsongbpm: {matched}" + (f" (raw {tempo})" if was_folded else "")


# ---------- Commands ----------

def cmd_export(args):
    api_key = args.api_key or os.environ.get("GETSONGBPM_API_KEY")
    if not api_key:
        print("No GetSongBPM key given, so the BPM column will be left blank for you to fill in.")

    # Keep BPMs, keys, energy and follows rules already in the CSV
    kept, kept_vid = {}, {}
    if os.path.exists(args.out):
        with open(args.out, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                kept[row["playlist_item_id"]] = {k: row.get(k, "") for k in KEPT}
                if row.get("video_id"):  # a video removed and re-added comes back under a new item id
                    kept_vid[row["video_id"]] = kept[row["playlist_item_id"]]
        print(f"Reusing {sum(1 for v in kept.values() if v['bpm'])} BPM values from existing {args.out}")

    yt = get_youtube()
    pid = parse_playlist_id(args.playlist)
    items = fetch_items(yt, pid)
    print(f"Fetched {len(items)} items")

    new_topic = [it["snippet"]["resourceId"].get("videoId") for it in items
                 if it["id"] not in kept
                 and it["snippet"].get("videoOwnerChannelTitle", "").endswith(" - Topic")]
    credited = topic_artists(yt, new_topic) if new_topic else {}

    rows = []
    for it in items:
        sn = it["snippet"]
        title = sn["title"]
        channel = sn.get("videoOwnerChannelTitle", "")
        artist, song = guess_artist_song(title, channel)
        artist = credited.get(sn["resourceId"].get("videoId"), artist)
        old = kept.get(it["id"]) or kept_vid.get(sn["resourceId"].get("videoId"), {})
        artist = old.get("artist_guess") or artist  # keep corrections made in the CSV
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
            "mood": old.get("mood", ""),
            "energy_source": old.get("energy_source", ""),
        })
        print(f"{sn['position']:>4}  {bpm or '---':>6}  {title}")

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    missing = sum(1 for r in rows if not r["bpm"])
    print(f"\nWrote {args.out}. {missing} of {len(rows)} rows have no BPM.")
    print("Check the bpm column (watch for half/double-time values), then run 'apply'.")

    if kept:
        current = {r["playlist_item_id"] for r in rows}
        added = [r for r in rows if r["playlist_item_id"] not in kept]
        removed = [k for k in kept if k not in current]
        print(f"Since the last export: {len(added)} added, {len(removed)} removed.")
        for r in added:
            print(f"  + {r['title']}")
        return [r["playlist_item_id"] for r in added], len(removed)
    return [], 0


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
    Rows should arrive in current playlist order (ties keep that order).

    Bands lean towards the current order, so sorting an already sorted list
    can still shuffle it a little, and a big change would then cost moves on
    the following runs too. Re-sort until the order stops changing, so one
    apply reaches the order later runs agree with."""
    ordered = _build_once(rows, descending, arc)
    for _ in range(10):
        again = _build_once(ordered, descending, arc)
        if all(a is b for a, b in zip(again, ordered)):
            break
        ordered = again
    return ordered


def _build_once(rows, descending, arc):
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
    cost = "" if os.path.exists(YTM_AUTH) else (f", about {len(moves) * UPDATE_COST} quota units "
                                                "(default daily quota is 10,000)")
    print(f"{len(moves)} moves needed{cost}.")
    if args.dry_run:
        for pos, item_id in moves:
            print(f"  move to {pos:>4}: {live_by_id[item_id]['snippet']['title']}")
        print("Dry run only, nothing changed.")
        return
    if not moves:
        print("Already sorted.")
        return "done"

    done = skipped = 0
    ytm = get_ytmusic()
    if ytm:
        print("Moving through YouTube Music (no quota).")
        skipped = _ytm_moves(ytm, pid, live, moves)
        moves = []  # all made; skip the quota-billed path and go straight to the check
    if len(moves) > 20:
        if not _probe_manual(yt, pid, live_by_id, moves[0]):
            print("\nWARNING: a test move did not stick. Set the playlist's Default ordering to "
                  "Manual in YouTube, then run again. No further quota was spent.")
            return "unsorted"
        done = 1
        print(f"  [1/{len(moves)}] {moves[0][0]:>4}: {live_by_id[moves[0][1]]['snippet']['title']}"
              " (test move stuck)")
    for pos, item_id in moves[done:]:
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
        fix = (f"{skipped} moves could not be made through YouTube Music (see 'skipped' above). "
               f"Run 'apply' again, or remove {YTM_AUTH} to move them through the Data API."
               if skipped else
               "Set the playlist's Default ordering to Manual in YouTube, then run 'apply' again.")
        print(f"\nWARNING: YouTube reports only {stuck}/{len(target_ids)} tracks in the "
              f"planned position. {fix}")
        return "unsorted"
    print("\nDone. Playlist is sorted.")
    return "done"


def _probe_manual(yt, pid, live_by_id, move):
    """Make the first move and confirm it stuck before spending quota on a big
    reorder. YouTube accepts moves on playlists that are not set to Manual
    ordering and then ignores them; listings can also lag, so poll briefly."""
    pos, item_id = move
    it = live_by_id[item_id]
    _run(yt.playlistItems().update(part="snippet", body={"id": item_id, "snippet": {
        "playlistId": pid, "resourceId": it["snippet"]["resourceId"], "position": pos}}))
    for wait in (5, 15, 30, 60):
        time.sleep(wait)
        ids = [i["id"] for i in fetch_items(yt, pid)]
        if item_id in ids and ids.index(item_id) == pos:
            return True
    return False


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
# Felt BPM ranges, in the order they are built (each finishes before the next starts)
MOODS = [("Hype", 120, None), ("Mid", 90, 120), ("Chill", None, 90)]


HYPE_MIN_ENERGY = 0.0  # Hype must also be at least averagely energetic


def _mood_of(bpm):
    for name, lo, hi in MOODS:
        if (lo is None or bpm >= lo) and (hi is None or bpm < hi):
            return name
    return None


def mood_for(r):
    """A 'mood' column value wins. Otherwise tempo decides, except that a
    fast but calm track goes to Mid rather than Hype, and a slow but energetic one to Mid
    rather than Chill."""
    if r.get("mood") in {m[0] for m in MOODS}:
        return r["mood"]
    b = _bpm(r)
    if b is None:
        return None
    m = _mood_of(b)
    if m == "Hype" and _energy(r) < HYPE_MIN_ENERGY and "kept fast" not in r.get("bpm_source", ""):
        return "Mid"
    if m == "Chill" and _energy(r) >= UNFOLD_ENERGY:
        return "Mid"  # slow but intense (half-time trap, a big build): never Chill
    return m


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
    ordered = build_target(rows)  # tracks without a BPM come last, so they end their mood
    # A track that follows another also joins its anchor's mood playlist, right
    # after it, while keeping its own measured place in its own mood.
    moods = {r["playlist_item_id"]: {mood_for(r)} - {None} for r in ordered}
    for _ in range(len(ordered)):  # settle chains (C follows B follows A)
        changed = False
        for r in ordered:
            want = (r.get("follows") or "").strip().lower()
            if not want:
                continue
            anchor = next((a for a in ordered if a is not r and want in (
                a["title"].strip().lower(), a["song_guess"].strip().lower(),
                a["video_id"].lower(), a["playlist_item_id"].lower())), None)
            if anchor and not moods[anchor["playlist_item_id"]] <= moods[r["playlist_item_id"]]:
                moods[r["playlist_item_id"]] |= moods[anchor["playlist_item_id"]]
                changed = True
        if not changed:
            break

    def anchored_by(r):
        want = (r.get("follows") or "").strip().lower()
        return next((a for a in ordered if a is not r and want and want in (
            a["title"].strip().lower(), a["song_guess"].strip().lower(),
            a["video_id"].lower(), a["playlist_item_id"].lower())), None)

    for name in desired:
        seen = set()
        for r in ordered:
            vid = r.get("video_id")
            if name not in moods[r["playlist_item_id"]] or not vid or vid in seen \
                    or r["title"] in SKIP_TITLES:
                continue
            seen.add(vid)
            desired[name].append(vid)
        # In its own mood a follower sits at its measured tempo, not glued to an
        # anchor that is not in this playlist; build_target glued it, so unglue.
        for r in ordered:
            a = anchored_by(r)
            vid = r.get("video_id")
            if a and vid in desired[name] and name not in moods[a["playlist_item_id"]]:
                desired[name].remove(vid)
                solo = [x for x in build_target([dict(x, follows="") for x in rows])
                        if name in moods[x["playlist_item_id"]] and x.get("video_id")]
                order = [x["video_id"] for x in solo]
                k = order.index(vid)
                after = next((order[j] for j in range(k - 1, -1, -1) if order[j] in desired[name]), None)
                desired[name].insert(desired[name].index(after) + 1 if after else 0, vid)

    store = json.load(open(MOODS_FILE)) if os.path.exists(MOODS_FILE) else {}
    ids = store.setdefault(pid, {})
    main_title = yt.playlists().list(part="snippet", id=pid).execute()["items"][0]["snippet"]["title"]
    ytm = get_ytmusic()

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
            _sync_mood(yt, ids[name], name, desired[name], ytm)
    except HttpError as e:
        if _is_quota(e):
            print("\nHit the daily quota while building mood playlists. "
                  "The next run continues where this stopped.")
            return "quota"
        raise
    return "done"


def _sync_mood(yt, mpid, name, want, ytm=None):
    # A just-created playlist can 404 for a short while before it is listable
    for wait in (0, 5, 15, 30, 60):
        time.sleep(wait)
        try:
            items = fetch_items(yt, mpid)
            break
        except HttpError as e:
            if e.resp.status != 404 or wait == 60:
                raise

    # YouTube sometimes accepts a delete without doing it, and a retried insert
    # can land twice, so re-read and repeat until the contents match.
    unaddable = set()
    for rnd in range(4):
        have, extra = {}, []
        for i in items:
            vid = i["snippet"]["resourceId"].get("videoId")
            if vid in want and vid not in have:
                have[vid] = i
            else:
                extra.append(i)
        missing = [v for v in want if v not in have and v not in unaddable]
        if not extra and not missing:
            break
        print(f"{name}: {len(want)} tracks, {len(have)} present, adding {len(missing)}, "
              f"removing {len(extra)}" + (f" (check {rnd + 1})" if rnd else ""), flush=True)
        if ytm:  # no quota
            sids = _ytm_set_ids(ytm, mpid, items)
            gone = [{"videoId": i["snippet"]["resourceId"].get("videoId"), "setVideoId": sids[i["id"]]}
                    for i in extra if i["id"] in sids]
            if gone:
                ytm.remove_playlist_items(mpid, gone)
            failed = 0
            for k in range(0, len(missing), 50):
                chunk = missing[k:k + 50]
                if _ytm_add(ytm, mpid, chunk):
                    continue
                for vid in chunk:  # one bad video fails its whole batch, so retry singly
                    if not _ytm_add(ytm, mpid, [vid]):
                        unaddable.add(vid)
                        failed += 1
                        print(f"  could not add {vid}")
            print(f"  {name}: added {len(missing) - failed}", flush=True)
        else:
            for i in extra:
                _run(yt.playlistItems().delete(id=i["id"]))
            for n, vid in enumerate(missing, 1):
                try:
                    _run(yt.playlistItems().insert(part="snippet", body={"snippet": {
                        "playlistId": mpid, "resourceId": {"kind": "youtube#video", "videoId": vid}}}))
                except HttpError as e:
                    if _is_quota(e):
                        raise
                    unaddable.add(vid)
                    print(f"  could not add {vid}: {e.resp.status}")
                if n % 25 == 0:
                    print(f"  {name}: added {n}/{len(missing)}", flush=True)
        time.sleep(20)
        items = fetch_items(yt, mpid)
    else:
        print(f"  {name}: WARNING contents still differ after 4 checks; the next run retries")
    by_vid = {i["snippet"]["resourceId"].get("videoId"): i for i in items}
    current = [i["id"] for i in items]
    target = [by_vid[v]["id"] for v in want if v in by_vid]
    target += [i for i in current if i not in set(target)]
    moves = plan_moves(current, target)
    unmoved = _ytm_moves(ytm, mpid, items, moves) if ytm else 0
    for pos, item_id in ([] if ytm else moves):
        it = next(i for i in items if i["id"] == item_id)
        _run(yt.playlistItems().update(part="snippet", body={"id": item_id, "snippet": {
            "playlistId": mpid, "resourceId": it["snippet"]["resourceId"], "position": pos}}))
    if moves:
        print(f"  {name}: {len(moves)} moves to fix order")
    print(f"  {name}: " + (f"WARNING {unmoved} moves failed; the next run retries them"
                           if unmoved else "in order"), flush=True)


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
    added, removed = [], 0
    try:
        print("== export")
        added, removed = cmd_export(ns(playlist=cfg["playlist"], api_key=cfg.get("api_key"),
                                       out=args.csv, delay=0.5))
        # blanks only: re-asking Deezer about every audio estimate daily is slow and finds nothing new
        print("== fill"); cmd_fill(ns(csv=args.csv, delay=0.5, blanks_only=True))
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
    finally:
        report_new_tracks(args.csv, added, removed)


def report_new_tracks(csv_path, added_ids, removed):
    """Say where each newly added track ended up, and flag any that need a hand."""
    print(f"\n== new tracks: {len(added_ids)} added, {removed} removed since the last run")
    if not added_ids:
        return
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = {r["playlist_item_id"]: r for r in csv.DictReader(f)}
    for item_id in added_ids:
        r = rows.get(item_id)
        if not r:
            continue
        mood = mood_for(r)
        if _bpm(r) is None and not mood:
            where = "NEEDS INPUT: no BPM found, sits at the end; give it a BPM or a mood"
        else:
            where = f"{r['bpm'] or '?'} BPM, key {r.get('key') or '?'}, {mood or 'no mood'} playlist"
        print(f"  + {r['title'][:50]:<50} -> {where}")


VARIANT = re.compile(
    r"sped\s*up|speed\s*up|slowed|reverb|nightcore|remix|\bedit\b|\blive\b|"
    r"acoustic|instrumental|extended|cover|karaoke|8d|\bvs\b\.?|\bflip\b|bootleg|mashup",
    re.I,
)


def _artist_tokens(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", " and ")
    return re.sub(r"[\W_]+", " ", s).split()


def artist_ok(a, b):
    """Same artist allowing for spelling: accents, '&' vs 'and', word order,
    and a credited name inside a longer one ('Louis Armstrong And His All Stars')."""
    import difflib
    ta, tb = _artist_tokens(a), _artist_tokens(b)
    if not ta or not tb:
        return False
    ja, jb = "".join(ta), "".join(tb)
    if min(len(ja), len(jb)) < 4:  # 'IU' is inside 'Sirius': short names must match as whole words
        return ja == jb or set(ta) <= set(tb) or set(tb) <= set(ta)
    return (ja in jb or jb in ja or set(ta) == set(tb)
            or difflib.SequenceMatcher(None, ja, jb).ratio() >= 0.75)


def title_ok(found, wanted):
    """Same song title, ignoring bracketed extras, case and punctuation."""
    import difflib

    def base(s):
        t = re.sub(r"[\W_]+", "", re.sub(r"[\(\[（【][^\)\]）】]*[\)\]）】]", "", s or "").lower())
        return t or re.sub(r"[\W_]+", "", (s or "").lower())  # all-bracket titles like '[oops]'
    a, b = base(found), base(wanted)
    if not a or not b:
        return False
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _match_score(track, artist, song, tags=None):
    """Rank a Deezer result: right artist, exact title, and no version tag
    (sped up, slowed, remix...) that the YouTube title (`tags`, default the song) does not also have."""
    title = track.get("title", "")
    score = 0
    if artist and artist_ok(track["artist"]["name"], artist):
        score += 4
    if _norm(title) == _norm(song):
        score += 2
    wanted = {m.lower() for m in VARIANT.findall(tags or song)}
    got = {m.lower() for m in VARIANT.findall(title)}
    score -= 3 * len(got - wanted)
    return score


def deezer_find_preview(artist, song):
    """Return (preview_url, 'Title / Artist') for the best Deezer match, or (None, why)."""
    hit, why = _deezer_hit(artist, song)
    if not hit:
        return None, why
    if not hit.get("preview"):
        return None, "deezer: match has no preview"
    return hit["preview"], why


def deezer_bpm(artist, song):
    """Deezer's own tempo for the best match: (bpm, label), or (None, why). Deezer reports
    0 for tracks it has not measured."""
    hit, why = _deezer_hit(artist, song)
    if not hit:
        return None, why
    try:
        bpm = float(requests.get(f"https://api.deezer.com/track/{hit['id']}", timeout=15).json().get("bpm") or 0)
    except (requests.RequestException, ValueError) as e:
        return None, f"deezer error: {e}"
    return (bpm if bpm > 0 else None), why


_BRACKETED = r"[\(\[（【]([^\)\]）】]*)[\)\]）】]"


def _search_variants(artist, song, cap=8):
    """(artist, song) pairs to search, most literal first: as given, bracketed extras dropped,
    first credited artist only, each side of a '〜', then bracketed aliases ('BTS (방탄소년단)')."""
    def strip(s):
        return re.sub(r"\s+", " ", re.sub(_BRACKETED, " ", s or "")).strip()
    first = re.split(r",|&|\s+x\s+|\bfeat\.?|\bft\.?|\bwith\b", artist or "", flags=re.I)[0].strip()
    a0, s0, a1, s1, a2 = artist or "", song or "", strip(artist), strip(song), strip(first)
    pairs = [(a0, s0), (a1, s1), (a2, s1)]
    pairs += [(a2, p.strip()) for p in re.split(r"[〜~]", s1)]
    pairs += [(alias.strip(), s1) for alias in re.findall(_BRACKETED, first)]
    pairs += [(a2, alias.strip()) for alias in re.findall(_BRACKETED, s0)]
    out = []
    for p in pairs:
        if p[1] and p not in out:
            out.append(p)
    return out[:cap]


def _has_latin(s):
    return bool(re.search(r"[A-Za-z]", s or ""))


@functools.lru_cache(maxsize=None)  # the tempo, preview and key lookups all ask for the same track
def _deezer_hit(artist, song):
    """Return (track, 'Title / Artist') for the best Deezer match over several search
    wordings, or (None, why) giving the reason from the most literal search."""
    # shortcut: a network error is cached too, for the rest of this run; the next run retries.
    first_why = None
    for a, s in _search_variants(artist, song):
        try:
            r = requests.get("https://api.deezer.com/search",
                             params={"q": f"{s} {a}".strip(), "limit": 5}, timeout=15)
            r.raise_for_status()
            data = r.json().get("data") or []
        except (requests.RequestException, ValueError) as e:
            return None, f"deezer error: {e}"
        hit = max(data, key=lambda t: _match_score(t, a, s, song)) if data else None
        if not hit:
            why = "deezer: no result"
        elif _match_score(hit, a, s, song) < 0:
            why = f"deezer: only other versions found, e.g. '{hit['title']}'"
        elif a and not artist_ok(hit["artist"]["name"], a):
            why = f"deezer: only other artists, e.g. '{hit['title']} / {hit['artist']['name']}'"
        elif title_ok(hit["title"], s) or (a and not _has_latin(s) and hit is data[0]):
            # Deezer files many K-pop/J-pop songs under an English title ('쩔어' is 'Dope');
            # for a non-Latin title, trust the top hit once the artist matches.
            return hit, f"{hit['title']} / {hit['artist']['name']}"
        else:
            why = f"deezer: only other songs, e.g. '{hit['title']} / {hit['artist']['name']}'"
        first_why = first_why or why
    return None, first_why or "deezer: no result"


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
    # Blank rows, plus rows estimated from audio: Deezer's measured tempo beats both
    blanks = [r for r in rows if r["title"] not in SKIP_TITLES and (
        not (r.get("bpm") or "").strip() or (r.get("bpm_source", "").startswith("deezer_preview:librosa")
                                             and not getattr(args, "blanks_only", False)))]
    print(f"{len(blanks)} rows without a measured BPM. Trying Deezer, then previews...", flush=True)

    def save():
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            w.writeheader()
            w.writerows(rows)

    filled = 0
    for n, r in enumerate(blanks, 1):
        label = f"[{n}/{len(blanks)}] {r['artist_guess']} - {r['song_guess']}"
        dz, matched = deezer_bpm(r["artist_guess"], r["song_guess"])
        if dz:
            bpm, was_folded = fold_bpm(dz)
            note = f" (raw {dz:.1f})" if was_folded else ""
            r["bpm"], r["bpm_source"] = str(bpm), f"deezer_bpm: {matched}{note}"
            filled += 1
            print(f"  {bpm:>6}  {label}  <- deezer {matched}{note}", flush=True)
            if filled % 20 == 0:
                save()
            time.sleep(args.delay)
            continue
        if (r.get("bpm") or "").strip():
            time.sleep(args.delay)
            continue  # already estimated from audio; keep it
        try:
            raw, matched = preview_tempo(r["artist_guess"], r["song_guess"])
        except Exception as e:
            raw, matched = None, f"analysis failed: {e}"
        source = "deezer_preview:librosa"
        if raw is None and r.get("video_id"):
            try:
                raw, why = youtube_tempo(r["video_id"])
            except Exception as e:  # unreadable download: skip this row, keep filling
                raw, why = None, f"youtube: {e}"
            matched, source = (why, "youtube_audio:librosa") if raw else (f"{matched}; {why}", source)
        bpm, was_folded = fold_bpm(raw)
        if bpm is None:
            print(f"  skip  {label}  ({matched})", flush=True)
            time.sleep(args.delay)
            continue
        note = f" (raw {raw:.1f})" if was_folded else ""
        r["bpm"] = str(bpm)
        r["bpm_source"] = f"{source}: {matched}{note}"
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
            if _match_score(best, artist, song) >= 2 and title_ok(best["title"], song) and (
                    not artist or artist_ok(best["artist"]["name"], artist)):
                return best["preview"], f"{best['title']} / {best['artist']['name']} (itunes)"
    return None, "itunes: no match"


def load_preview(artist, song):
    """Return (samples, sample_rate, label) for the middle of a 30s preview, or (None, None, why)."""
    url, label = deezer_find_preview(artist, song)
    if not url and _ffmpeg():
        url, label = itunes_find_preview(artist, song)
    if not url:
        return None, None, label
    y, sr = _download_preview(url)
    if len(y) > 20 * sr:
        y = y[5 * sr:-5 * sr]
    return y, sr, label


def preview_tempo(artist, song):
    """Tempo from several windows of the Deezer preview, then of the iTunes one (often a
    different part of the song). Returns (raw_bpm, label) or (None, why)."""
    whys = []
    for find in (deezer_find_preview, itunes_find_preview):
        if find is itunes_find_preview and not _ffmpeg():
            break
        url, label = find(artist, song)
        if not url:
            whys.append(label)
            continue
        try:
            tempos = _window_tempos(*_download_preview(url))
        except Exception as e:
            whys.append(f"{label}: {e}")
            continue
        if tempos:
            return _median_felt(tempos), f"{label}, {len(tempos)}/4 windows{_spread(tempos)}"
        whys.append(f"{label}: no beat in any window")
    return None, "; ".join(whys)


def _median_felt(tempos):
    """Median by felt tempo, so 83 and 166 readings of one song count as agreeing."""
    return sorted(tempos, key=lambda t: fold_bpm(t)[0])[len(tempos) // 2]


def _spread(tempos):
    """Note for the CSV when windows read over 5% apart in felt tempo: a row to check by ear."""
    felt = sorted(fold_bpm(t)[0] for t in tempos)
    return f", windows disagree: felt {felt[0]:g}-{felt[-1]:g}" if felt[-1] > felt[0] * 1.05 else ""


def _youtube_audio(video_id, sr=22050):
    """The video's own audio as (mono samples, sample_rate). Raises if it cannot be fetched."""
    import shutil
    import subprocess
    import tempfile
    import soundfile as sf
    d = tempfile.mkdtemp()
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
                        "-f", "bestaudio", "-o", os.path.join(d, "a.%(ext)s"),
                        f"https://www.youtube.com/watch?v={video_id}"], check=True, timeout=300)
        src = os.path.join(d, os.listdir(d)[0])
        wav = os.path.join(d, "a.wav")
        subprocess.run([_ffmpeg(), "-y", "-loglevel", "error", "-i", src, "-ac", "1", "-ar", str(sr), wav],
                       check=True, timeout=300)
        return sf.read(wav, dtype="float32")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def youtube_tempo(video_id):
    """Last resort: the video's own audio, exactly the version in the playlist. Tempo from
    20s windows at 20/40/60/80% through it. Returns (raw_bpm, label) or (None, why)."""
    if not _ffmpeg():
        return None, "youtube: needs ffmpeg"
    try:
        y, sr = _youtube_audio(video_id)
    except Exception as e:
        return None, f"youtube: {e}"
    w = 20 * sr
    starts = [int(len(y) * f) for f in (0.2, 0.4, 0.6, 0.8)]
    tempos = [t for s in starts if s + w <= len(y) for t in [estimate_tempo(y[s:s + w], sr)] if t > 0]
    if not tempos:
        return None, "youtube: no beat in any window"
    return _median_felt(tempos), f"youtube audio, {len(tempos)}/4 windows{_spread(tempos)}"


def _window_tempos(y, sr):
    """librosa tempo for the middle, whole, start and end of a preview; beatless windows dropped."""
    n, w = len(y), 12 * sr
    windows = [y[5 * sr:-5 * sr] if n > 20 * sr else y, y, y[:w], y[-w:]]
    return [t for s in windows if len(s) >= 6 * sr for t in [estimate_tempo(s, sr)] if t > 0]


def _download_preview(url):
    """Download a preview and return (mono samples, sample_rate), untrimmed."""
    import tempfile
    import subprocess
    import soundfile as sf
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
    return y.mean(axis=1), sr


def estimate_tempo(y, sr):
    import numpy as np
    import librosa
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    return float(np.atleast_1d(tempo)[0])


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
    env = librosa.onset.onset_strength(y=y, sr=sr)  # computed once: onset_detect would redo it
    feats = {
        "rms": float(np.sqrt(np.mean(y ** 2))),
        "onsets": len(librosa.onset.onset_detect(onset_envelope=env, sr=sr)) / (len(y) / sr),
        "flux": float(np.mean(env)),
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
    hit = next((x for x in res if artist_ok(x.get("artist", {}).get("name"), artist)), None)
    return camelot_from_open_key(hit.get("open_key")) if hit else ""


UNFOLD_ENERGY = 0.5  # a halved track at least this energetic is heard fast, so undo the halving


def unfold_if_energetic(r):
    """fill halves tempos of FELT_MAX and up; energy says whether the track is really heard
    that fast. Doubles back one step, marked 'kept fast' so mood_for keeps it in Hype."""
    m = re.search(r"\(raw ([\d.]+)\)", r.get("bpm_source", ""))
    b = _bpm(r)
    if not m or b is None or not r.get("energy") or "kept fast" in r["bpm_source"] \
            or float(m.group(1)) <= b or _energy(r) < UNFOLD_ENERGY:
        return False
    if abs(fold_bpm(float(m.group(1)))[0] - b) > 0.2:
        return False  # bpm corrected by hand since: it is no longer the halved reading
    r["bpm"] = str(round(b * 2, 1))
    r["bpm_source"] += f" kept fast (energy {r['energy']})"
    return True


def _undo_kept_fast(r):
    """Halve a tempo analyse doubled, so it is decided again from fresh energy (never manual rows)."""
    m = re.search(r" kept fast \(energy [^)]*\)", r.get("bpm_source", ""))
    if m and not r["bpm_source"].startswith("manual") and _bpm(r) is not None:
        r["bpm_source"] = r["bpm_source"].replace(m.group(0), "")
        r["bpm"] = f"{round(_bpm(r) / 2, 2):g}"


FULL_SR = 44100  # previews are 44.1 kHz and energy features depend on the rate: match them
FULL_TRACK = "youtube: full track"
AUDIO_TEMPO = ("deezer_preview:librosa", "youtube_audio:librosa")  # tempos estimated from audio


def full_track(video_id, win=15, tempo=True):
    """Energy and tempo from the video's whole audio, so a slow intro, breakdown or outro
    does not make a hype song read as calm. Energy is the 75th percentile over back-to-back
    windows (the loud sections define the song); tempo comes from the 4 most energetic windows.
    Returns (energy, raw_tempo or None, spread note, main-section samples). Raises on failure."""
    import numpy as np
    y, sr = _youtube_audio(video_id, sr=FULL_SR)
    w = win * sr
    wins = [y[s:s + w] for s in range(0, len(y) - w + 1, w)] or [y]
    energies = [estimate_energy(x, sr) for x in wins]
    main = [wins[i] for i in sorted(range(len(wins)), key=lambda i: -energies[i])[:4]]
    # tempo=False when a database tempo is kept anyway: beat tracking is the slow part
    tempos = [t for x in main for t in [estimate_tempo(x, sr)] if t > 0] if tempo else []
    return (round(float(np.percentile(energies, 75)), 2), _median_felt(tempos) if tempos else None,
            _spread(tempos) if tempos else "", np.concatenate(main))


def cmd_analyse(args):
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    api_key = getattr(args, "api_key", None) or os.environ.get("GETSONGBPM_API_KEY")
    full = getattr(args, "full", False)
    todo = [r for r in rows if r["title"] not in SKIP_TITLES
            and (not r.get("key") or not r.get("energy")
                 or (full and r.get("energy_source") != FULL_TRACK and r.get("video_id")))]
    print(f"{len(todo)} rows need key or energy" + (" or a full-track measure" if full else "") + ".",
          flush=True)

    def save():
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    fails = 0
    for n, r in enumerate(todo, 1):
        label = f"[{n}/{len(todo)}] {r['title'][:45]}"
        if not r.get("key") and api_key and r.get("bpm_source", "").startswith("getsongbpm"):
            k = getsongbpm_key(api_key, r["artist_guess"], r["song_guess"])
            if k:
                r["key"], r["key_source"] = k, "getsongbpm"
            time.sleep(args.delay)
        whole, note = None, ""
        if r.get("video_id") and _ffmpeg():
            try:
                whole = full_track(r["video_id"], tempo=r["bpm_source"].startswith(AUDIO_TEMPO))
                fails = 0
            except Exception as e:
                fails += 1
                note = f"  (full track failed: {str(e)[:80]})"
        if whole:
            energy, raw, spread, main = whole
            r["energy"], r["energy_source"] = str(energy), FULL_TRACK
            _undo_kept_fast(r)  # fresh energy decides it again below
            if raw and r["bpm_source"].startswith(AUDIO_TEMPO):
                bpm, folded = fold_bpm(raw)
                if abs(bpm - (_bpm(r) or 0)) > 0.05 * bpm:
                    note = f"  (tempo {r['bpm']} -> {bpm})"
                r["bpm"] = str(bpm)
                r["bpm_source"] = (f"youtube_audio:librosa: full track, main sections{spread}"
                                   + (f" (raw {raw:.1f})" if folded else ""))
        y = None
        if not r.get("key") or not r.get("energy"):
            try:
                y, sr, why = load_preview(r["artist_guess"], r["song_guess"])
            except Exception as e:
                y, why = None, f"preview failed: {e}"
            if y is None and whole:  # no preview anywhere: the track's own main sections
                y, sr, why = whole[3], FULL_SR, "youtube audio"
            if y is not None:
                if not r.get("key"):
                    r["key"], r["key_source"] = estimate_key(y, sr), f"preview: {why}"
                if not r.get("energy"):
                    r["energy"], r["energy_source"] = str(estimate_energy(y, sr)), f"preview: {why}"
            else:
                note += f"  ({why})"
        print(f"  {r.get('key') or '-':>3}  {r.get('energy') or '-':>6}  {label}{note}", flush=True)
        if n % 20 == 0:
            save()
        if fails >= 10:
            print("YouTube downloads keep failing (blocked for a while?). Stopping; "
                  "run 'analyse --full' again later to continue.")
            break
        time.sleep(args.delay)
    fast = [r for r in rows if unfold_if_energetic(r)]
    for r in fast:
        print(f"  kept fast {r['bpm']:>6}  {r['title'][:45]}  (energy {r['energy']})")
    save()
    print(f"Analysis saved. {len(fast)} halved tempos kept fast for high energy.")


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
    fl.add_argument("--blanks-only", action="store_true",
                    help="Only rows with no BPM; skip re-checking audio estimates against Deezer")
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
    an.add_argument("--full", action="store_true",
                    help="Re-measure every track from its whole YouTube audio (resumable)")
    an.set_defaults(func=cmd_analyse)

    mo = sub.add_parser("moods", help="Build Chill / Mid / Hype playlists in the same order")
    mo.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    mo.set_defaults(func=cmd_moods)

    sy = sub.add_parser("sync", help="export, fill, analyse, apply and moods in one go (uses config.json)")
    sy.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    sy.set_defaults(func=cmd_sync)

    lg = sub.add_parser("login", help="Sign in to Google again (after the login expires)")
    lg.set_defaults(func=cmd_login)

    args = p.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows falls back to cp1252 when output is
        stream.reconfigure(encoding="utf-8", errors="replace")  # redirected: Korean titles crash it
    args.func(args)


if __name__ == "__main__":
    main()
