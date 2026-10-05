# YouTube Playlist BPM Sorter

Reorders a personal YouTube playlist by BPM, ascending by default, with an option for descending. Tempo data is provided by [GetSongBPM](https://getsongbpm.com).

## How it works

The tool runs in two steps with a human review in between:

1. `export` fetches the playlist, guesses artist and song from each video title, looks up the tempo on GetSongBPM, and writes `playlist_bpm.csv`.
2. You review the CSV and correct or fill in any BPM values.
3. `apply` sorts the CSV by BPM and reorders the live playlist to match.

## Setup

1. Create a Google Cloud project, enable the YouTube Data API v3, and create an OAuth client of type **Desktop app**. Download the client JSON and save it next to the script as `client_secret.json`.
2. While the OAuth consent screen is in testing mode, add your Google account as a test user.
3. Install dependencies:

   ```
   pip install -r requirements.txt
   ```

4. Get a free API key from [GetSongBPM](https://getsongbpm.com/api). Without a key, `export` still runs and leaves the `bpm` column blank for manual entry.
5. In YouTube, set the playlist sort order to **Manual**, otherwise position updates are ignored.

## Usage

Export the playlist and look up tempos:

```
python yt_bpm_sort.py export "<playlist URL or ID>" --api-key <GETSONGBPM_KEY>
```

Review `playlist_bpm.csv`. Check the `bpm` and `bpm_source` columns, fix mismatches, and watch for half or double time errors (for example 85 vs 170). Re-running `export` preserves BPMs already in the CSV, so manual corrections survive.

Preview the reorder, then apply it:

```
python yt_bpm_sort.py apply --dry-run
python yt_bpm_sort.py apply
python yt_bpm_sort.py apply --descending
```

## Quota notes

The YouTube Data API default quota is 10,000 units per day. Each position update costs 50 units, so roughly 200 moves per day. If the quota runs out, `apply` stops cleanly; re-running it the next day resumes from the live playlist state.

## Credits

Tempo data from [GetSongBPM](https://getsongbpm.com).
