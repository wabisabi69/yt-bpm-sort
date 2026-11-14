# YouTube Playlist BPM Sorter

Reorders a personal YouTube playlist by felt tempo, slowest first, and refines the order for smooth listening: compatible musical keys and rising energy within each tempo band, with copies of the same song kept apart. It can also build Chill, Mid and Hype playlists in the same order. Tempo and key data are provided by [GetSongBPM](https://getsongbpm.com).

## How it works

1. `export` fetches the playlist, guesses artist and song from each video title, looks up the tempo on GetSongBPM, and writes `playlist_bpm.csv`. Half-time readings under 70 BPM are doubled.
2. `fill` finds a tempo for rows GetSongBPM could not match. It tries, in order: Deezer's own measured tempo, the track's 30 second preview on Deezer and then iTunes analysed locally with librosa, and last the video's own audio. Readings above 145 BPM are halved to the felt tempo. `fill` also re-checks earlier audio estimates against Deezer; `fill --blanks-only` skips that.
3. `analyse` adds a musical key (Camelot notation) and an energy score for each track. Energy is measured on the video's whole audio: the track is cut into 15 second windows and scored on its louder sections (75th percentile), so a slow intro, breakdown or outro does not make a hype song read as calm. Tempos estimated from audio are re-measured on the 4 most energetic windows. Keys come from GetSongBPM where it has them, otherwise from the preview. A halved tempo is doubled back when the track is energetic (energy 0.5 or more) and marked "kept fast". `analyse --full` re-measures every track this way; it is resumable.
4. `lyrics` sets `lyrics` = `no` for instrumentals, for tracks with fewer than 20 sung words a minute (background voices, a sampled hook), and for tracks with fewer than 10 different words a minute (one hook on repeat). Words a minute come from the lyrics YouTube Music links to the video (needs `browser.json`) and the track's length. Without those, LRCLIB's instrumental tags decide, then LRCLIB's lyrics. A title saying "Instrumental" always counts. Songs neither source knows stay blank. `lyrics --recheck` checks every row again except ones set by hand.
5. You review the CSV and correct anything that looks wrong (see below).
6. `apply` reorders the live playlist to match, using the fewest moves possible.
7. `moods` keeps one private playlist per mood: Chill (under 90 BPM), Mid (90 to 120) and Hype (120 and up). A track at Hype tempo with below-average energy goes to Mid, unless it is marked "kept fast"; a track at Chill tempo with high energy (0.5 or more) goes to Mid too. It also keeps an Instrumental playlist of every track whose `lyrics` is `no`, in the main playlist's order.

`sync` runs all of these in order and is safe to run every day.

## Reviewing the CSV

- `energy_source` says whether energy came from the full track or only a preview.
- `bpm_source` says where each tempo came from. Tempos estimated from audio (`deezer_preview:librosa`, `youtube_audio:librosa`) are the least reliable; rows marked "windows disagree" read differently in different parts of the clip and are the first to check by ear.
- To correct a tempo, change `bpm` and set `bpm_source` to something starting with `manual`, so `fill` never replaces it.
- `mood` forces a track into `Chill`, `Mid` or `Hype` whatever its tempo.
- `follows` names a track this one must directly follow.
- `lyrics` set to `no` puts a track in the Instrumental playlist, `yes` keeps it out. A blank `lyrics` with `lyrics_source` "not found" means neither source knew the song. Adding or removing a track in the Instrumental playlist in YouTube does the same: the next `moods` run notices and sets `lyrics` with a `manual` source. A different upload of a song already in the main playlist is swapped for the main playlist's copy. A track that is not in the main playlist at all stays, unsorted, at the end.

## The order

- Felt BPM ascending.
- Tracks within 4 BPM of each other are reordered so neighbouring songs share or neighbour a key on the Camelot wheel, going quiet to loud, and keep their current order wherever the choice is close (which saves moves).
- Copies of the same song are kept at least 20 tracks apart.
- A row can name a track it must directly follow in the `follows` column.
- `apply --arc` builds to a peak mid-playlist and then winds down instead; `apply --descending` reverses the order.

## Setup

1. Create a Google Cloud project, enable the YouTube Data API v3, and create an OAuth client of type **Desktop app**. Download the client JSON and save it next to the script as `client_secret.json`.
2. While the OAuth consent screen is in testing mode, add your Google account as a test user.
3. Install dependencies, plus ffmpeg on your PATH for iTunes previews and YouTube audio. Use a Python version librosa has wheels for (3.11 works; the newest release may not yet):

   ```
   pip install -r requirements.txt
   ```

4. Get a free API key from [GetSongBPM](https://getsongbpm.com/api).
5. In YouTube, set the playlist's **Default ordering** to **Manual** in the playlist settings. Otherwise YouTube accepts position changes but keeps showing its own order. `apply` checks this after moving and warns if the order did not stick.

## Usage

Step by step:

```
python yt_bpm_sort.py export "<playlist URL or ID>" --api-key <GETSONGBPM_KEY>
python yt_bpm_sort.py fill
python yt_bpm_sort.py analyse --api-key <GETSONGBPM_KEY>
python yt_bpm_sort.py lyrics
python yt_bpm_sort.py apply --dry-run
python yt_bpm_sort.py apply
python yt_bpm_sort.py moods
```

Or all at once, with a `config.json` next to the script:

```json
{"playlist": "<playlist URL or ID>", "api_key": "<GETSONGBPM_KEY>"}
```

```
python yt_bpm_sort.py sync
```

Re-running `export` keeps BPMs, keys, energy, moods and follows rules already in the CSV, so manual corrections survive, also when a video is removed from the playlist and added back.

If the Google login expires, `python yt_bpm_sort.py login` signs in again.

## Quota notes

The YouTube Data API default quota is 10,000 units per day, and it resets at midnight Pacific time. Each move, add or remove costs 50 units, so roughly 200 per day. Every command stops cleanly at the quota and picks up from the live playlist state on the next run.

## Sorting without the quota (optional)

A first sort of a long playlist takes days at 200 moves a day. With a `browser.json` next to the script, `apply` and `moods` write through YouTube Music's web API instead, which is not billed against the quota (reads still use the Data API, at about 1 unit per 50 tracks).

1. Sign in at music.youtube.com, open the browser's developer tools, and copy the request headers of any `browse` POST request. The [ytmusicapi guide](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html) shows how.
2. Run `ytmusicapi browser` next to the script and paste the headers. It writes `browser.json`.

`browser.json` holds your login cookies: treat it like a password. It is in `.gitignore`. Signing out of YouTube Music in that browser ends it; the script then says so and goes back to the Data API.

## Credits

Tempo and key data from [GetSongBPM](https://getsongbpm.com).
