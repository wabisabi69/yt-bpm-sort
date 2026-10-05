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
          "channel", "artist_guess", "song_guess", "bpm", "bpm_source"]
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

    # Keep BPMs you already entered if the CSV exists
    previous = {}
    if os.path.exists(args.out):
        with open(args.out, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("bpm"):
                    previous[row["playlist_item_id"]] = (row["bpm"], row.get("bpm_source", ""))
        print(f"Reusing {len(previous)} BPM values from existing {args.out}")

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
        if it["id"] in previous:
            bpm, source = previous[it["id"]]
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
        })
        print(f"{sn['position']:>4}  {bpm or '---':>6}  {title}")

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    missing = sum(1 for r in rows if not r["bpm"])
    print(f"\nWrote {args.out}. {missing} of {len(rows)} rows have no BPM.")
    print("Check the bpm column (watch for half/double-time values), then run 'apply'.")


def build_target(rows, descending):
    def key(r):
        try:
            b = float(r["bpm"])
            return (0, -b if descending else b)
        except (TypeError, ValueError):
            return (1, 0)  # no BPM: keep at the end, in current order
    return sorted(rows, key=key)


def plan_moves(current, target_ids):
    current = list(current)
    moves = []
    for pos, item_id in enumerate(target_ids):
        if current[pos] == item_id:
            continue
        current.remove(item_id)
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

    target_ids = [r["playlist_item_id"] for r in build_target(rows, args.descending)
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
            yt.playlistItems().update(part="snippet", body=body).execute()
        except HttpError as e:
            if e.resp.status == 403 and "quota" in str(e).lower():
                print(f"\nHit the daily quota after {done} moves. Run 'apply' again tomorrow to continue.")
                return
            raise
        done += 1
        print(f"  [{done}/{len(moves)}] {pos:>4}: {it['snippet']['title']}")
    print("\nDone. Playlist is sorted.")


def main():
    p = argparse.ArgumentParser(description="Sort a YouTube playlist by BPM")
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("export", help="Fetch playlist and look up BPMs into a CSV")
    e.add_argument("playlist", help="Playlist URL or ID")
    e.add_argument("--api-key", help="GetSongBPM API key (or set GETSONGBPM_API_KEY)")
    e.add_argument("--out", default="playlist_bpm.csv")
    e.add_argument("--delay", type=float, default=0.5, help="Seconds between BPM lookups")
    e.set_defaults(func=cmd_export)

    a = sub.add_parser("apply", help="Reorder the playlist using the CSV")
    a.add_argument("csv", nargs="?", default="playlist_bpm.csv")
    a.add_argument("--descending", action="store_true", help="Fastest first instead")
    a.add_argument("--dry-run", action="store_true", help="Show the planned moves only")
    a.set_defaults(func=cmd_apply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
