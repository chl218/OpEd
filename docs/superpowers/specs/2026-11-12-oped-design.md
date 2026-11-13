# OpEd — Season OP/ED Trimmer: Design

Date: 2026-11-12
Status: Approved 2026-11-12

## Intent

The user watches TV seasons back to back in VLC: anime, and live-action shows
such as Star Trek: TNG and Westworld. They want trimmed copies of every episode
in a season with the repeated opening (OP) and ending (ED) removed, so they can
watch without hearing the theme song every episode.

The OP/ED is usually not at the very start or end of the file. TNG and Westworld
start with a cold open before the titles. Anime often has a short scene before
the OP and a next-episode preview or post-credits scene after the ED. So the app
removes **segments wherever they sit** and keeps everything around them. It does
not just chop off the head and tail of each file.

## Goals

- Point the app at a season folder and get trimmed copies of every episode.
- Find the OP/ED in each episode automatically by matching audio.
- Let the user give **hints**: mark the OP and/or ED in one episode
  (e.g. OP 2:34–3:45, ED 44:34–45:15). Those marked sections become the
  reference audio that the app searches for in every other episode.
- **Preview every cut before anything is written.** The user reviews each
  episode's proposed cuts, adjusts or rejects them, then exports.
- Never modify the original files.
- Run on Windows, Linux and macOS.

## Non-goals

- Skipping during playback, or integrating with Plex, Jellyfin or Kodi.
  The output is plain files for VLC.
- General video editing. The only operation is removing segments.
- Removing anything other than OP/ED-style repeated segments, such as recaps
  or ads. (The data model below doesn't rule this out later.)
- Processing several seasons in one session. One folder at a time.

## Anti-goals (failure even if it "works")

- Silently cutting story content. Every cut is shown to the user, and nothing
  is exported without their approval.
- Trimmed files with audio, subtitles or chapters out of sync with the video.
- A visible drop in quality, or an export that takes as long as re-encoding
  the whole season.
- Dropping audio or subtitle tracks. Anime releases often carry several
  (e.g. Japanese and English audio, multiple subtitle tracks), and every one
  must survive.

## Constraints

- Cross-platform desktop app: Windows, Linux, macOS.
- The user picked a **desktop window app** (Approach B below).
- Input is commonly MKV with H.264 or HEVC video, AAC/FLAC/AC3/Opus audio, and
  ASS/SRT subtitles (anime fansub releases), or MP4.
- Depends on ffmpeg/ffprobe, which isn't currently installed on the user's
  Windows machine.

## Approaches considered

All three use the same engine: ffmpeg for decoding and cutting, and audio
fingerprint matching for detection.

- **A. Local app in the browser (localhost web UI).** The season's review is
  on one page, and edges are previewed with generated clips because browsers
  can't play MKV/HEVC/FLAC. Not picked.
- **B. Desktop window app (Python + Qt).** The same workflow as A, in a native
  window. **Picked by the user**, with the requirement that it run on Windows,
  Linux and macOS.
- **C. Command-line tool with VLC for preview.** Writes a text cut list and edge
  clips. The user reviews them in VLC, edits the text, then exports. Fastest to
  build, clunkiest to review 24 episodes. Not picked.

## Decided (by the user)

- Output is **trimmed copies**. Originals are kept untouched.
- Playback is in **VLC**.
- **Preview all cuts** before export.
- **Manual hints on one episode** are used as the reference for the rest.
- **Desktop app, cross-platform.**
- **Personal tool**, shared with others only via git clone.
- **Episodes can have external subtitle files**, which must stay in sync.

## Design

### Workflow

1. **Open season folder.** The app lists the video files in natural sort order
   (E01, E02, … E10, not E1, E10, E2).
2. **Hints (optional).** In any episode, the user marks an OP range and/or an
   ED range, either by typing times or by setting in/out points while scrubbing
   the player. Each marked range becomes a named **reference** (e.g. "OP 1",
   "ED 1"). The user can add more references in other episodes. For example,
   when an anime switches to a new OP halfway through the season, they mark it
   once in the first episode that has it and it becomes "OP 2".
3. **Scan.** For every episode, the app searches for every reference and
   proposes a cut wherever a reference matches confidently. With no hints, the
   app first discovers references on its own by finding audio that repeats
   across episodes (see Detection).
4. **Review.** Every episode shows its proposed cuts on a timeline, each with a
   confidence level. For each cut the user can:
   - play a few seconds just before the cut's start, then jump to just after its
     end, to hear exactly how the trimmed episode will flow;
   - nudge the start or end (frame/0.1 s/1 s steps) or type exact times;
   - approve, reject, or add a cut by hand.
   Low-confidence and missing matches are flagged so the user spots them, e.g.
   "No OP found in E01" or "ED match weak in E12 (finale?)". All cuts start
   unapproved, so the user explicitly approves each one (with a quick
   "approve all high-confidence" button for convenience).
5. **Export.** Writes trimmed copies of the episodes the user selects, with
   per-episode progress shown. Episodes with no approved cuts are **copied
   unchanged** into the output folder (plain file copy, no links), so the
   trimmed folder is a complete season playlist in VLC. Shown as
   "copied unchanged" in the review list (my call).

### Window sketch (my proposal)

```
+--------------------------------------------------------------------------+
| Season: D:\Anime\Frieren S1           [Scan]  [Approve all ✓high]  [Export]|
+-------------------+------------------------------------------------------+
| E01  ⚠ no OP      |  [            video player                         ] |
| E02  ✓ OP ✓ ED    |  |◀ -5s  ▶ play edge  +5s ▶|   02:34.120 → 03:45.080  |
| E03  ✓ OP ✓ ED    |                                                      |
| E04  ✓ OP ⚠ ED    |  timeline: [cold open][██ OP 1 ██][ episode ...][█ED█][prev] |
| ...               |                                                      |
| E14  ✓ OP2 ✓ ED   |  Cuts:  OP 1  02:34.1–03:45.1  conf 0.97  [✓][✗][nudge]|
|                   |         ED 1  44:34.0–45:15.2  conf 0.62  [✓][✗][nudge]|
| References:       |  [+ add cut]  [mark as reference]                    |
|  OP 1 (E02) OP 2  |                                                      |
|  ED 1 (E02)       |                                                      |
+-------------------+------------------------------------------------------+
```

### Tech choices (my calls)

- **Python 3.11+ with PySide6 (Qt 6).** Cross-platform, and the user's machine
  already has Python.
- **Playback inside the app with Qt Multimedia.** On Qt 6.5+ it uses an FFmpeg
  backend on all three platforms, so MKV, HEVC and most audio codecs play
  directly. No preview clips are needed. If a codec doesn't play, fall back to
  generating a short H.264/AAC preview clip of the cut edge with ffmpeg.
- **ffmpeg/ffprobe** for probing, decoding audio for analysis, and cutting.
  Use the binaries on PATH, with a setting to point at specific binaries. If
  they're missing, the app says so on startup with per-OS install hints
  (e.g. `winget install ffmpeg`, `brew install ffmpeg`, `apt install ffmpeg`).
  ffmpeg is not bundled.
- **Distribution (decided by the user): a personal tool, shared by git clone.**
  Run from source (e.g. `uv run oped`) on Windows, Linux and macOS. There are no
  packaged installers or release builds, and licensing doesn't need settling
  (ffmpeg is the user's own install). The README covers setup on all three OSes.

### Detection

- Decode each episode's audio once to mono at low sample rate (e.g. 11–16 kHz)
  via ffmpeg, and compute a compact fingerprint over time (chroma or spectral
  peak features at ~10–20 frames/s). Cache the fingerprints per file, keyed on
  path, size and mtime, so rescans are fast.
- **With references:** slide each reference's fingerprint along each episode
  and find the best-matching offset. The match's similarity is the confidence.
  Matches are tolerant of slight edits: the OP may be a few seconds shorter in
  some episodes, or have different sound effects mixed over it. Refine the
  start and end edges by trimming where similarity drops off.
- **Without references:** compare episodes pairwise (or each to a few
  neighbours) to find long (≥ ~30 s) stretches of matching audio. Stretches that
  recur in most episodes become proposed references, labeled "OP?" and "ED?"
  by position. The user confirms or renames them before they count as
  references.
- A reference may match zero or one time per episode (v1). Zero matches is a
  normal, flagged result (e.g. an episode that skips the OP).
- Thresholds for "confident", "weak" and "no match" are tuned by the builder
  against real episodes. See "Left to the builder".

### Export / cutting

- **Exact cuts without re-encoding the whole file ("smart cut").** Stream-copy
  everything between keyframes, and re-encode only the short stretch from each
  cut point to the next keyframe, so the cut lands on the exact frame. Audio is
  cut at the exact time. If smart cut fails for a file, e.g. an unusual codec,
  fall back to the user's choice of (a) a full re-encode at high quality, which
  is exact and slow, or (b) a keyframe-aligned stream copy, which is fast but
  may keep up to a couple of seconds of song. The fallback is shown to the user
  and isn't applied silently.
- **Keep every stream:** all video, audio and subtitle tracks, attachments
  (fonts embedded in MKVs for ASS subtitles are essential), and track metadata
  such as language, default and forced flags.
- **Subtitles** are shifted to match the removed time. Lines that fall entirely
  inside a cut are dropped, and lines spanning a cut edge are clipped.
- **Image-based subtitles** (PGS/VobSub, common in Blu-ray rips of TNG and
  Westworld) are cut at the packet level and shifted. Events inside a cut are
  dropped, and an event spanning a cut edge is kept or dropped whole (my call:
  clipping images isn't worth it). The smart-cut spike must include a PGS
  sample.
- **External subtitle files (decided by the user: episodes can have them).**
  Subtitle files next to an episode (`.ass`, `.ssa`, `.srt`, `.vtt`, `.sup`) whose
  name starts with the episode's base name (e.g. `E01.mkv` →
  `E01.ass`, `E01.en.srt`, `E01.eng.forced.ass`) are matched to that episode.
  They're shifted the same way as embedded ones and written next to the trimmed
  copy under the same name, so VLC still auto-loads them. A `Fonts/` (or
  `fonts/`, `Attachments/`) folder in the season folder is copied to the output
  folder as is. Subtitles in a `Subs/` subfolder use the same name matching and
  keep the same relative path (my call). Matched side files are shown under
  each episode in the review list. External audio (`.mka`) is out of scope for
  v1 (my call): the app shows a warning if it finds any.
- **Chapters** are shifted. Chapters inside a removed segment are dropped
  (or moved to the cut point).
- **Container is preserved:** MKV stays MKV and MP4 stays MP4.
- **Output** goes to a sibling folder, `<Season folder> (trimmed)/`, with the
  same file names, so VLC playlists and sort order still work (my call). If it
  already exists, ask before overwriting files.

### Saved state (my call)

The review state (references, cuts, approvals) is saved automatically to a small
`oped-project.json` in the season folder, so the user can close the app midway
through reviewing 24 episodes and pick up later. This is the only file the app
writes into the source folder, and it doesn't touch the videos.
(Alternative if the source folder is read-only: store it in the app's data
directory, keyed by folder path.)

## Left to the builder

- Fingerprint algorithm and matching thresholds. Chromaprint (via `fpcalc`) or
  a custom numpy chroma approach are both acceptable, provided matching works
  on segments that sit anywhere in the file and handles slightly shortened OPs.
- Exact smart-cut mechanism, e.g. ffmpeg segment re-encode + concat, or a
  combination with mkvmerge for MKV. Requirements: exact cuts, A/V/subtitle
  sync, all streams kept. **This is the riskiest part.** Spike it on real
  MKV (HEVC + FLAC + ASS with attached fonts) and MP4 (H.264 + AAC) samples
  early.
- Test fixtures (my call): copyrighted episodes never go in the repo or CI.
  Automated tests use synthetic videos generated with ffmpeg (a repeated audio
  clip spliced in at varying positions and lengths, plus multiple audio and
  subtitle tracks). The smart-cut spike and threshold tuning run locally on the
  user's own episodes.
- Project structure, test strategy and internal architecture. Detection and
  cutting should be usable and testable without the GUI.
- How references discovered without hints are presented for confirmation.
- Keyboard shortcuts for the review screen (strongly encouraged, because
  reviewing a whole season is repetitive).
