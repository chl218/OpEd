# OpEd

OpEd finds the repeated opening and ending (OP/ED) in every episode of a season by matching audio, lets you review each proposed cut, and writes trimmed copies of the episodes.

Originals are never modified. Output goes to a sibling folder named `<Season folder> (trimmed)/` with the same file names. The only file written into the season folder is `oped-project.json` (your references, cuts and decisions). Each save writes `oped-project.json.tmp` first and renames it onto `oped-project.json`, so that temporary file exists for a moment during a save (it is removed if the save fails). If the season folder is not writable, an existing `oped-project.json` is still read, and saves go to the per-user data directory instead; the banner says where (see "Files OpEd writes").

Status: all features are implemented and the automated tests pass on Windows. It has not yet been tried on a real season, so detection thresholds are untuned (see "Known limits"). The real-media checks are still outstanding: smart cut on real files (`uv run python scripts/spike_cut.py <video> <start> <end> --out <scratch dir>`, then check the result in a player) and one full season run through the app.

## Install and run from source

1. Install ffmpeg (includes ffprobe) and make sure both are on PATH:
   - Windows: `winget install Gyan.FFmpeg` (open a new shell afterwards)
   - macOS: `brew install ffmpeg`
   - Linux (Debian/Ubuntu): `sudo apt install ffmpeg`
2. Install [uv](https://docs.astral.sh/uv/).
3. Clone and run (Python 3.10 or newer; uv fetches dependencies, including PySide6). On Linux with X11, PySide6 may also need the system package `libxcb-cursor0` (untested here):

```
git clone <repository url>
cd OpEd
uv sync
uv run oped
```

If ffmpeg is not on PATH the app tells you and offers a "Choose ffmpeg..." button. Smart cut uses a `noise` bitstream filter expression (`drop=lt(pts\,startpts)`), which needs a reasonably recent ffmpeg; development used 9.0.2. A minimum version has not been determined, so use a recent release.

## Workflow

1. Open folder: choose the season folder (episodes at its top level; subfolders are not scanned for episodes).
2. Optional: mark the OP/ED in one episode. Select the episode, add a cut (Ctrl+N), set its start and end (I/O, or type times), then "Mark as reference". Give it a kind and label.
3. Scan: OpEd fingerprints the audio and looks for the reference in every other episode. Each hit becomes a proposed cut. Candidate references found automatically are shown for you to confirm before the scan runs.
4. Review: step through episodes and cuts, preview the edges, adjust, approve or reject (shortcuts below). "Approve all high" approves every proposed cut with high confidence and also every manual cut: the reference's own cut and cuts you added by hand (Ctrl+N), even an untouched 60 s placeholder, so check or delete those first.
   - Your decisions survive a re-scan: approved, rejected and hand-added cuts are kept, and so is a proposed cut whose times you changed. Changing the times of a reference's own cut moves the reference too.
   - Deleting a reference's own cut deletes the reference (after a confirmation). Its unreviewed proposals in other episodes are removed; approved, rejected and edited cuts from it stay as hand-added cuts.
5. Export: pick the episodes. Only approved cuts are applied. Episodes with no approved cut are copied unchanged.

### Keyboard shortcuts

These match the bindings in `src/oped/gui/main_window.py`. They are ignored while a text field is being edited. While a scan or export runs, only Space, Tab, Up, Down, PageUp and PageDown work.

| Key | Action |
| --- | --- |
| Space | Play the current edge preview |
| A | Approve the selected cut |
| X | Reject the selected cut |
| Tab | Switch between the start and end edge |
| Left / Right | Nudge the edge by -0.1 s / +0.1 s |
| Shift+Left / Shift+Right | Nudge by -1 s / +1 s |
| Ctrl+Left / Ctrl+Right | Nudge by one frame back / forward |
| Up / Down | Select previous / next cut |
| PageUp / PageDown | Previous / next episode |
| I / O | Set the selected cut's start / end to the playhead |
| Ctrl+N | Add a new cut |

### Status badges (episode list)

- `✓ <kind>`: has an approved cut (the reference's kind, for example OP or ED).
- `⚠ <text>`: scan flag, such as `No <label> found` or `<label> match weak`.
- `copied unchanged`: scanned, and no cut is approved, so export copies the file as is.
- `●`: at least one proposed cut is still waiting for review.

Confidence thresholds (`src/oped/project.py`): 0.85 and above is high, 0.65 up to 0.85 is weak (flagged), below 0.65 is treated as not found.

## Export

- Smart cut is the default. Only the part of each cut point up to the next keyframe is re-encoded, and everything else is stream-copied. Video and audio are cut separately and muxed with the source's other tracks.
- If smart cut fails for a file, OpEd asks you to choose: exact re-encode (slow), fast keyframe cut (may keep about 2 s of the cut region), or skip. It never falls back silently. Sources with open-GOP x264 currently fail smart cut and take this path.
- Subtitles: text subtitles are clipped at cut edges and retimed. Image subtitles (PGS, VobSub) are kept or dropped whole; the exception is a packet without a duration (PGS "clear" packets, for example), which is moved to the cut edge when it falls inside a cut. External sidecar subtitles next to the video (extensions `.srt`, `.ass`, `.ssa`, `.vtt`, `.sup`) are matched by exact stem (`Show - 01.ass`) or by stem plus `.` (`Show - 01.en.ass`), and each file goes to one episode only: the one with the longest matching stem, so `Show - 01.5.ass` belongs to `Show - 01.5.mkv`, not `Show - 01.mkv`. Sidecars are also taken from a `Subs/` (or `subs/`) folder, either by the same name rule or from `Subs/<episode stem>/`.
- Chapters: retimed to the output; chapters that start inside a cut are dropped.
- Attachments (fonts) and cover art are carried over, and stream metadata and dispositions are preserved.
- Files are written as `.part` and renamed when complete, so a half-written file never has its final name. The video is renamed first, then its sidecars; if a rename fails (for example the old output is open in a player), the summary names the files that were already replaced.
- A season folder at a drive root (for example `E:\`) cannot be exported, because there is no place for a sibling `(trimmed)` folder. Move the episodes into a folder.

## Files OpEd writes

| What | Where |
| --- | --- |
| Project (references, cuts, decisions) | `<season folder>/oped-project.json`; if saving there fails, `<user data dir>/projects/<sha1 of folder path>.json`. When both exist, the more recently saved one is read. |
| Backup of a project file that is not valid JSON or not a project | `<user data dir>/backups/<sha1>-<timestamp>.json` (a fresh project is started). A project file that exists but cannot be read (locked, offline) is not replaced: nothing is loaded or saved until you open the folder again. |
| Saved ffmpeg/ffprobe paths | Qt INI settings file `oped/oped.ini` in the per-user location (Windows: `%APPDATA%\oped\oped.ini`) |
| Audio fingerprint cache | `<user cache dir>/fingerprints/` |
| Trimmed episodes | `<season folder> (trimmed)/` next to the season folder |

`<user data dir>` and `<user cache dir>` are `platformdirs.user_data_dir("oped")` and `platformdirs.user_cache_dir("oped")`. On Windows (checked) these are `%LOCALAPPDATA%\oped\oped` and `%LOCALAPPDATA%\oped\oped\Cache`. On macOS (`~/Library/Application Support/oped`, `~/Library/Caches/oped`) and Linux (`~/.local/share/oped`, `~/.cache/oped`) they come from platformdirs defaults and are untested.

## Tests

```
uv run pytest
```

The full suite takes about 4.5 minutes and needs ffmpeg and ffprobe on PATH; media tests are skipped when ffmpeg is missing, and a skipped run verifies nothing. Tests generate their own synthetic media, so no real episodes are needed. Verified on Windows only; Linux and macOS are untested.

## Known limits

- External `.mka` audio files are not supported (a warning is shown and the file is ignored).
- Image subtitles (PGS, VobSub) are kept or dropped whole, not clipped (packets without a duration snap to the cut edge).
- One reference match per episode per reference.
- No cancel button for a running scan or export. Closing the window asks for confirmation and then cancels cooperatively.
- Edge previews need a working Qt Multimedia backend. If playback fails, OpEd falls back to short ffmpeg-generated clips.
- Open-GOP x264 sources fail smart cut and need the re-encode or keyframe choice.
- The high/weak thresholds (0.85 / 0.65) have not been tuned on a real season.
