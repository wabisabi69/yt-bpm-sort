# YouTube Playlist BPM Sorter

Reorders a personal YouTube playlist by felt tempo, slowest first, and refines the order for smooth listening: compatible musical keys and rising energy within each tempo band, with copies of the same song kept apart. It can also build Chill, Mid and Hype playlists in the same order. Tempo and key data are provided by [GetSongBPM](https://getsongbpm.com).

## How it works

1. `export` fetches the playlist, guesses artist and song from each video title, looks up the tempo on GetSongBPM, and writes `playlist_bpm.csv`.
2. `fill` estimates tempo for rows GetSongBPM could not match, from the track's public 30 second preview on Deezer, analysed locally with librosa. Very fast readings (145 BPM and up) are halved to the felt tempo.
3. `analyse` adds a musical key (Camelot notation) and an energy score for each track, from GetSongBPM where it has the key and from the preview otherwise (Deezer, or iTunes for tracks Deezer lacks).
4. You review the CSV and correct anything that looks wrong.
5. `apply` reorders the live playlist to match, using the fewest moves possible.
6. `moods` keeps one private playlist per mood: Chill (under 90 BPM), Mid (90 to 120) and Hype (120 and up).

`sync` runs all of these in order and is safe to run every day.

## The order

- Felt BPM ascending.
- Tracks within 4 BPM of each other are reordered so neighbouring songs share or neighbour a key on the Camelot wheel, going quiet to loud, and keep their current order wherever the choice is close (which saves moves).
- Copies of the same song are kept at least 20 tracks apart.
- A row can name a track it must directly follow in the `follows` column.
- `apply --arc` builds to a peak mid-playlist and then winds down instead; `apply --descending` reverses the order.

## Setup

1. Create a Google Cloud project, enable the YouTube Data API v3, and create an OAuth client of type **Desktop app**. Download the client JSON and save it next to the script as `client_secret.json`.
2. While the OAuth consent screen is in testing mode, add your Google account as a test user.
3. Install dependencies, plus ffmpeg on your PATH for iTunes previews:

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

Re-running `export` keeps BPMs, keys, energy and follows rules already in the CSV, so manual corrections survive.

## Quota notes

The YouTube Data API default quota is 10,000 units per day, and it resets at midnight Pacific time. Each move, add or remove costs 50 units, so roughly 200 per day. Every command stops cleanly at the quota and picks up from the live playlist state on the next run.

## Credits

Tempo and key data from [GetSongBPM](https://getsongbpm.com).
