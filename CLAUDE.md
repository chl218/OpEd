# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

OpEd finds the repeated opening/ending (OP/ED) in every episode of a season by audio matching, lets the user review each cut, and writes trimmed copies. Originals are never modified. The design is in `docs/superpowers/specs/2026-11-12-oped-design.md` and the task-by-task plan in `docs/superpowers/plans/2026-11-12-oped.md`. The plan's "Global Constraints" and "Review Focus" sections are binding. The code is being built task by task following that plan, so check which modules exist before assuming a layer is there. The GUI (`oped.gui`, entry point `oped = "oped.gui.app:main"`) comes last.

## Commands

Python project managed with `uv`. ffmpeg and ffprobe must be on PATH; the tests generate their own synthetic media, and the `tools` fixture skips media tests when ffmpeg is missing. A skipped run does not verify anything, so make sure ffmpeg is found.

```
uv run pytest                                  # whole suite
uv run pytest tests/test_cutter.py -v          # one file
uv run pytest tests/test_cutter.py::test_short_interval_inside_one_gop   # one test
uv run oped                                    # launch the GUI (once oped.gui exists)
```

On Windows a winget install of ffmpeg only reaches new shells. In an already-open shell, prepend the winget `...\Gyan.FFmpeg_...\ffmpeg-*-full_build\bin` directory to PATH.

There is no linter configured. `pythonpath = ["tests"]` lets tests do `from fixtures import ...`.

## Architecture

A GUI-free core package `oped.*` with a PySide6 GUI (`oped.gui.*`) on top. Core modules and how they connect:

- `media.py` is the only place that shells out. `run()` takes argument lists (no shell) and raises `FfmpegError`; `find_tools()` raises `ToolsMissing` with a per-OS install hint; `probe()` and `keyframes()` return `MediaInfo`/keyframe times.
- `discovery.py` turns a folder into a `Season`: top-level videos in natural order, sidecar subtitles matched by exact stem or `stem + "."` prefix (so `E1.ass` never attaches to `E10.mkv`), font dirs, and warnings.
- `project.py` holds `Reference`/`Cut`/`EpisodeState`/`Project` and JSON persistence (`oped-project.json` in the season folder, falling back to the platformdirs data dir). Every cut starts `proposed`, and only `approved` cuts are ever exported (`approved_intervals`).
- `timeline.py` has the cut math shared by video, audio, subtitles and chapters. `merge_cuts` and `keep_intervals` produce the keep list, and `TimeMap` maps source time to output time.
- Planned, per the plan: `cutter.py` (smart cut), `subs.py`, `export.py`, `fingerprint.py`, `matching.py`, `scan.py`.

Cross-module invariants that are easy to break:

- **Smart cut**: each cut point is re-encoded only up to the next keyframe, and everything else is stream-copied. Video and audio are cut separately and then muxed with the source's tracks, subtitles, attachments, chapters and metadata. Smart-cut failure must surface as an explicit user choice (`reencode` or `keyframe`), never a silent fallback.
- **Sliver rule**: `keep_intervals` drops kept pieces under 0.05 s, but `TimeMap` does not. Whatever builds the `TimeMap` for subtitle and chapter retiming must derive its cuts as the complement of `keep_intervals`. Otherwise subtitle offsets drift from the cut video.
- Text subtitles are clipped at cut edges, image subtitles (PGS/VobSub) are kept or dropped whole, and chapters starting inside a cut are dropped.
- Output goes to the sibling folder `<Season> (trimmed)/` with the same file names. Files are written to `.part` and renamed, and a half-written file never keeps its final name.
- Paths in tests and code can contain spaces, brackets, apostrophes and Japanese characters (fansub names like `[SubsPlease] Show - 01 (1080p) [A1B2C3D4].mkv`). Never build a shell string, and mind ffmpeg concat-list escaping.
- Tests use synthetic media from `tests/fixtures.py` (`make_color_video`, `frame_color`, `dominant_freq`). Copyrighted episodes never go in the repo or CI.

## Working conventions

The subagent-driven workflow keeps its scratch state (ledger, task briefs, review diffs) in the git-ignored `.superpowers/` directory. Never commit it.
