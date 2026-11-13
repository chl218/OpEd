"""Project scan: locate confirmed OP/ED references in every episode and propose cuts.

User decisions are sacred. A re-scan replaces only an episode's *proposed* cuts that came
from a reference; approved, rejected and hand-added (``reference_id is None``) cuts are
kept, and a new proposal that overlaps (> 50 % of the shorter interval) an approved or
rejected cut of the same reference is skipped.

Failure handling: if features cannot be built for an episode, or the reference itself is
unusable (its episode is missing from the project, or its span is empty or beyond the
episode), the episode gets the flag ``Scan failed: <reason>``, its existing proposals for
that reference are left untouched, and the scan continues with the other episodes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from oped.discovery import Season
from oped.fingerprint import FPS, episode_features
from oped.matching import discover_references, find_reference
from oped.media import Tools, probe
from oped.project import HIGH, WEAK, Cut, Project, Reference, new_id

FeatureSource = Callable[[str], np.ndarray]


def make_feature_source(tools: Tools, season: Season, cache_dir: Path | None) -> FeatureSource:
    """File name -> features, via ``probe`` + ``episode_features``, memoised in memory."""
    paths = {ep.video.name: ep.video for ep in season.episodes}
    memo: dict[str, np.ndarray] = {}

    def source(name: str) -> np.ndarray:
        if name not in memo:
            memo[name] = episode_features(tools, probe(tools, paths[name]), cache_dir)
        return memo[name]

    return source


def _overlap_fraction(a0: float, a1: float, b0: float, b1: float) -> float:
    inter = min(a1, b1) - max(a0, b0)
    shorter = min(a1 - a0, b1 - b0)
    return inter / shorter if inter > 0 and shorter > 0 else 0.0


def _reason(exc: Exception) -> str:
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def scan(project: Project, feats: FeatureSource,
         progress: Callable[[str, float], None] | None = None) -> None:
    refs = [r for r in project.references if r.confirmed]
    ref_cache: dict[str, np.ndarray | Exception] = {}
    feat_cache: dict[str, np.ndarray | Exception] = {}

    def episode_feats(name: str) -> np.ndarray:
        """Features of one episode, fetched at most once per scan (failures included)."""
        if name not in feat_cache:
            try:
                feat_cache[name] = np.asarray(feats(name))
            except Exception as exc:  # noqa: BLE001 - reported as a flag
                feat_cache[name] = exc
        got = feat_cache[name]
        if isinstance(got, Exception):
            raise got
        return got

    def reference_features(ref: Reference) -> np.ndarray:
        if ref.id not in ref_cache:
            try:
                if ref.episode not in project.episodes:
                    raise ValueError(f"reference episode {ref.episode} is not in the project")
                piece = episode_feats(ref.episode)[
                    slice(round(ref.start * FPS), round(ref.end * FPS))]
                if len(piece) == 0:
                    raise ValueError(f"{ref.label} reference is empty or beyond its episode")
                ref_cache[ref.id] = piece
            except Exception as exc:  # noqa: BLE001 - reported as a flag
                ref_cache[ref.id] = exc
        got = ref_cache[ref.id]
        if isinstance(got, Exception):
            raise got
        return got

    names = list(project.episodes)
    for i, name in enumerate(names):
        state = project.episodes[name]
        flags: list[str] = []
        proposals: list[Cut] = []
        evaluated: set[str] = set()   # references whose stale proposals may be replaced
        for ref in refs:
            try:
                if ref.episode == name:
                    proposals.append(Cut(new_id(), ref.start, ref.end, "proposed", ref.id, None))
                else:
                    piece = reference_features(ref)
                    m = find_reference(piece, episode_feats(name))
                    if m is None or m.confidence < WEAK:
                        flags.append(f"No {ref.label} found")
                    else:
                        if m.confidence < HIGH:
                            flags.append(f"{ref.label} match weak")
                        proposals.append(
                            Cut(new_id(), m.start, m.end, "proposed", ref.id, m.confidence))
            except Exception as exc:  # noqa: BLE001
                msg = f"Scan failed: {_reason(exc)}"
                if msg not in flags:
                    flags.append(msg)
                continue
            evaluated.add(ref.id)

        kept = [c for c in state.cuts
                if not (c.status == "proposed" and c.reference_id in evaluated)]
        for c in proposals:
            decided = (k for k in kept
                       if k.reference_id == c.reference_id and k.status in ("approved", "rejected"))
            if any(_overlap_fraction(c.start, c.end, k.start, k.end) > 0.5 for k in decided):
                continue
            kept.append(c)
        state.cuts = kept
        state.flags = flags
        state.scanned = True
        if progress is not None:
            progress(name, (i + 1) / len(names))


def propose_references(project: Project, feats: FeatureSource) -> list[Reference]:
    """Append unconfirmed references found by ``discover_references``; return the new ones."""
    features = {name: feats(name) for name in project.episodes}
    new = [Reference(new_id(), c.kind, f"{c.kind}?", c.episode, c.start, c.end, confirmed=False)
           for c in discover_references(features)]
    project.references.extend(new)
    return new
