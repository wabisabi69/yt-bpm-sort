"""Log the top of the main playlist from YouTube's public feed (no API quota),
to catch when something outside the sorter resets its order."""
import csv
import datetime
import html
import json
import re
import sys

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # redirected output is cp1252 on Windows

PLAYLIST = json.load(open("config.json"))["playlist"]
x = requests.get(f"https://www.youtube.com/feeds/videos.xml?playlist_id={PLAYLIST}", timeout=20).text
titles = [html.unescape(t) for t in re.findall(r"<entry>[\s\S]*?<title>([^<]*)</title>", x)][:6]

# Sorted means the slowest tracks lead. Allow 10 BPM: the very slowest can be missing from the feed.
bpm = {}
for r in csv.DictReader(open("playlist_bpm.csv", newline="", encoding="utf-8-sig")):
    try:
        bpm[r["video_id"]] = float(r["bpm"])
    except ValueError:
        pass
first = re.search(r"<yt:videoId>([^<]+)", x)
ok = bool(first and bpm) and bpm.get(first.group(1), 1e9) <= min(bpm.values()) + 10
state = "SORTED-ISH" if ok else "RESET?"
line = f"{datetime.datetime.now():%Y-%m-%d %H:%M} {state:<10} " + " | ".join(titles)
with open("order_watch.log", "a", encoding="utf-8") as f:
    f.write(line + "\n")
print(line)
