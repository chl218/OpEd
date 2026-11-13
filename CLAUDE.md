# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpEd finds the repeated opening/ending (OP/ED) in every episode of a season by audio matching, lets the user review each cut, and writes trimmed copies. Originals are never modified. The design is in `docs/superpowers/specs/2026-11-12-oped-design.md` and the task-by-task plan in `docs/superpowers/plans/2026-11-12-oped.md`. The plan's "Global Constraints" and "Review Focus" sections are binding. All plan tasks are implemented; the GUI entry point is `oped = "oped.gui.app:main"`. Real media, on-screen GUI behaviour and Linux/macOS are still unverified.

## Commands

Python project managed with `uv`. ffmpeg and ffprobe must be on PATH; the tests generate their own synthetic media, and the `tools` fixture skips media tests when ffmpeg is missing. A skipped run does not verify anything, so make sure ffmpeg is found.

```
uv run pytest                                  # whole suite
uv run pytest tests/test_cutter.py -v          # one file
uv run pytest tests/test_cutter.py::test_short_interval_inside_one_gop   # one test
uv run oped                                    # launch the GUI
```

The full suite takes about 4.5 minutes; `tests/test_matching.py` alone is about 2 minutes (fixture feature building), so run focused files while iterating.

On Windows a winget install of ffmpeg only reaches new shells. In an already-open shell, prepend the winget `...\Gyan.FFmpeg_...\ffmpeg-*-full_build\bin` directory to PATH.

There is no linter configured. `pythonpath = ["tests"]` lets tests do `from fixtures import ...`.

Settings isolation: the app reads and writes settings only through `main_window._settings()` (IniFormat, UserScope), and the autouse `_isolated_qsettings` fixture redirects that path to a temp dir. Never use `QSettings("oped", "oped")` in code or tests: it is NativeFormat (the real HKCU registry on Windows) and `setPath` does not redirect it.

## Architecture

A GUI-free core package `oped.*` with a PySide6 GUI (`oped.gui.*`) on top. Core modules and how they connect:

- `media.py` is the only place that shells out. `run()` takes argument lists (no shell) and raises `FfmpegError`; `find_tools()` raises `ToolsMissing` with a per-OS install hint; `probe()` and `keyframes()` return `MediaInfo`/keyframe times.
- `discovery.py` turns a folder into a `Season`: top-level videos in natural order, sidecar subtitles matched by exact stem or `stem + "."` prefix, each sidecar given to exactly one video (`Subs/<stem>/` folder, then exact stem, then longest prefix stem, so `E1.ass` never attaches to `E10.mkv` and `01.5.ass` never to `01.mkv`), font dirs, and warnings.
- `project.py` holds `Reference`/`Cut`/`EpisodeState`/`Project` and JSON persistence. `save_project` tries `oped-project.json` in the season folder (atomic `.tmp` + replace, no probe files) and falls back to the platformdirs data dir on error; `load_project` reads whichever of the two exists and is newer, and raises `OSError` for an unreadable file (the GUI then attaches no project rather than treating it as corrupt). Every cut starts `proposed`, and only `approved` cuts are ever exported (`approved_intervals`). `Cut.edited` marks a user time edit.
- `timeline.py` has the cut math shared by video, audio, subtitles and chapters: `merge_cuts`, `keep_intervals`, `complement`, and `TimeMap` (source time to output time).
- `cutter.py` (smart/reencode/keyframe cut, `snap_to_frames`, `keyframe_intervals`), `subs.py` (subtitle/chapter retiming), `export.py` (per-episode pipeline and final mux), `fingerprint.py` + `matching.py` (audio features and reference search), `scan.py` (references to proposed cuts; keeps approved, rejected, hand-added and edited cuts; leaves cuts sorted).
- GUI: `main_window.py` (folder/project, banner, autosave, toolbar actions, the patchable module-level dialogs), `cut_panel.py` (cut rows, edge edits, Mark as reference, deleting a reference via its own cut), `player.py` (Qt Multimedia with an ffmpeg preview-clip fallback), `episode_list.py` (badges), `workers.py` (`Worker` on the global thread pool, cooperative cancel at progress reports).
- Threading: a worker never sees the live `Project`. Scan and export run on a `copy.deepcopy` taken on the GUI thread, and scan results (cuts, flags, scanned) are copied back on the GUI thread. While a worker runs the window is busy (`_busy`): Open/Scan/Approve/Export and the cut panel are disabled and editing shortcuts are ignored, so the copy-back cannot lose edits.

Cross-module invariants that are easy to break:

- **Smart cut**: each cut point is re-encoded only up to the next keyframe, and everything else is stream-copied. Video and audio are cut separately and then muxed with the source's tracks, subtitles, attachments, chapters and metadata. Smart-cut failure must surface as an explicit user choice (`reencode` or `keyframe`), never a silent fallback.
- **TimeMap from the snapped keep**: the cut video keeps the frame-snapped intervals (`snap_to_frames`), or the keyframe intervals (`keyframe_intervals`) in keyframe mode, not the raw `keep_intervals` (which also drops slivers under 0.05 s). The `TimeMap` for subtitles and chapters must be built from the complement of that snapped keep (`export._final_keep` / `_time_map`). Otherwise subtitle and chapter times drift from the video.
- Text subtitles are clipped at cut edges, image subtitles (PGS/VobSub) are kept or dropped whole (packets without a duration snap to the cut edge), and chapters starting inside a cut are dropped.
- Output goes to the sibling folder `<Season> (trimmed)/` with the same file names (a drive root has none: `OutputFolderError`). Files are written to `.part` and renamed, video first and then sidecars, and a half-written file never keeps its final name.
- User decisions survive a re-scan, and the deleted reference stays deleted; "Approve all high" also approves manual cuts (`confidence is None`) by design.
- Paths in tests and code can contain spaces, brackets, apostrophes and Japanese characters (fansub names like `[SubsPlease] Show - 01 (1080p) [A1B2C3D4].mkv`). Never build a shell string, and mind ffmpeg concat-list escaping.
- Tests use synthetic media from `tests/fixtures.py` (`make_color_video`, `frame_color`, `dominant_freq`). Copyrighted episodes never go in the repo or CI.

## Working conventions

The subagent-driven workflow keeps its scratch state (ledger, task briefs, review diffs) in the git-ignored `.superpowers/` directory. Never commit it.
