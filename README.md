# OpEd

OpEd finds the repeated opening and ending (OP/ED) in every episode of a season by matching audio, lets you review each proposed cut, and writes trimmed copies of the episodes.

Originals are never modified. Output goes to a sibling folder named `<Season folder> (trimmed)/` with the same file names. The only file written into the season folder is `oped-project.json` (your references, cuts and decisions). If the season folder is not writable, the project file goes to the per-user data directory instead (see "Files OpEd writes").

Status: all features are implemented and the automated tests pass on Windows. It has not yet been tried on a real season, so detection thresholds are untuned (see "Known limits").

## Install and run from source

1. Install ffmpeg (includes ffprobe) and make sure both are on PATH:
   - Windows: `winget install ffmpeg` (open a new shell afterwards)
   - macOS: `brew install ffmpeg`
   - Linux (Debian/Ubuntu): `sudo apt install ffmpeg`
2. Install [uv](https://docs.astral.sh/uv/).
3. Clone and run (Python 3.10 or newer; uv fetches dependencies, including PySide6):

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
4. Review: step through episodes and cuts, preview the edges, adjust, approve or reject (shortcuts below). "Approve all high" approves every proposed cut with high confidence.
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
- Subtitles: text subtitles are clipped at cut edges and retimed. Image subtitles (PGS, VobSub) are kept or dropped whole. External sidecar subtitles next to the video (same stem, for example `Show - 01.en.ass`; extensions `.srt`, `.ass`, `.ssa`, `.vtt`, `.sup`) are matched and handled the same way. Sidecars are also taken from a `Subs/` (or `subs/`) folder, either directly by matching stem or from `Subs/<episode stem>/`.
- Chapters: retimed to the output; chapters that start inside a cut are dropped.
- Attachments (fonts) and cover art are carried over, and stream metadata and dispositions are preserved.
- Files are written as `.part` and renamed when complete, so a half-written file never has its final name.

## Files OpEd writes

| What | Where |
| --- | --- |
| Project (references, cuts, decisions) | `<season folder>/oped-project.json`; if that folder is not writable, `<user data dir>/projects/<sha1 of folder path>.json` |
| Backup of an unreadable project file | `<user data dir>/backups/<sha1>-<timestamp>.json` (a fresh project is started) |
| Audio fingerprint cache | `<user cache dir>/fingerprints/` |
| Trimmed episodes | `<season folder> (trimmed)/` next to the season folder |

`<user data dir>` and `<user cache dir>` are `platformdirs.user_data_dir("oped")` and `platformdirs.user_cache_dir("oped")`. On Windows (checked) these are `%LOCALAPPDATA%\oped\oped` and `%LOCALAPPDATA%\oped\oped\Cache`. On macOS (`~/Library/Application Support/oped`, `~/Library/Caches/oped`) and Linux (`~/.local/share/oped`, `~/.cache/oped`) they come from platformdirs defaults and are untested.

## Tests

```
uv run pytest
```

The full suite takes several minutes and needs ffmpeg and ffprobe on PATH; media tests are skipped when ffmpeg is missing, and a skipped run verifies nothing. Tests generate their own synthetic media, so no real episodes are needed. Verified on Windows only; Linux and macOS are untested.

## Known limits

- External `.mka` audio files are not supported (a warning is shown and the file is ignored).
- Image subtitles (PGS, VobSub) are kept or dropped whole, not clipped.
- One reference match per episode per reference.
- No cancel button for a running scan or export. Closing the window asks for confirmation and then cancels cooperatively.
- Edge previews need a working Qt Multimedia backend. If playback fails, OpEd falls back to short ffmpeg-generated clips.
- Open-GOP x264 sources fail smart cut and need the re-encode or keyframe choice.
- The high/weak thresholds (0.85 / 0.65) have not been tuned on a real season.
