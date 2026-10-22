"""Log the top of the main playlist from YouTube's public feed (no API quota),
to catch when something outside the sorter resets its order."""
import datetime
import html
import json
import re

import requests

PLAYLIST = json.load(open("config.json"))["playlist"]
x = requests.get(f"https://www.youtube.com/feeds/videos.xml?playlist_id={PLAYLIST}", timeout=20).text
titles = [html.unescape(t) for t in re.findall(r"<entry>[\s\S]*?<title>([^<]*)</title>", x)][:6]
state = "SORTED-ISH" if titles[:1] == ["Minecraft"] else "RESET?"
line = f"{datetime.datetime.now():%Y-%m-%d %H:%M} {state:<10} " + " | ".join(titles)
with open("order_watch.log", "a", encoding="utf-8") as f:
    f.write(line + "\n")
print(line)
