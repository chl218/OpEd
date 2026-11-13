import functools
from pathlib import Path

import numpy as np
import pytest

import oped.scan as scan_mod
from fixtures import episode, episode_with
from oped.fingerprint import features_from_pcm
from oped.matching import Match
from oped.project import Cut, EpisodeState, Project, Reference
from oped.scan import make_feature_source, propose_references, scan

TOTAL = 200


@functools.lru_cache(maxsize=None)
def ep_feats(op_at=None, seed=0, **kw):
    return features_from_pcm(episode(op_at, total=TOTAL, seed=seed, **kw))


@functools.lru_cache(maxsize=None)
def ed_feats(op_at, ed_at, seed):
    return features_from_pcm(episode_with(op_at, ed_at, seed, total=400))


def project_with(names):
    return Project(Path("."), [], {n: EpisodeState(n) for n in names})


def test_scan_proposes_flags_and_respects_decisions():
    p = project_with(["E1.mkv", "E2.mkv", "E3.mkv"])
    fs = {"E1.mkv": ep_feats(None), "E2.mkv": ep_feats(90, 1), "E3.mkv": ep_feats(100, 2)}
    p.references.append(Reference("r", "OP", "OP 1", "E2.mkv", 90, 150))
    p.episodes["E3.mkv"].cuts.append(Cut("keep", 100, 160, "rejected", "r", 0.9))
    scan(p, fs.__getitem__)
    assert p.episodes["E1.mkv"].cuts == [] and "No OP 1 found" in p.episodes["E1.mkv"].flags
    (c2,) = p.episodes["E2.mkv"].cuts
    assert (c2.start, c2.end, c2.confidence, c2.status) == (90, 150, None, "proposed")
    assert [c.id for c in p.episodes["E3.mkv"].cuts] == ["keep"]
    assert all(s.scanned for s in p.episodes.values())


def test_unconfirmed_references_are_ignored():
    p = project_with(["E1.mkv"])
    p.references.append(Reference("r", "OP", "OP?", "E1.mkv", 0, 60, confirmed=False))
    scan(p, lambda f: ep_feats(0))
    assert p.episodes["E1.mkv"].cuts == []


def _three():
    p = project_with(["E1.mkv", "E2.mkv", "E3.mkv"])
    p.references.append(Reference("r", "OP", "OP 1", "E1.mkv", 90, 150))
    fs = {"E1.mkv": ep_feats(90, 1), "E2.mkv": ep_feats(40, 2), "E3.mkv": ep_feats(None, 3)}
    return p, fs


def _snapshot(p):
    return {n: ([(c.start, c.end, c.confidence) for c in s.cuts], list(s.flags))
            for n, s in p.episodes.items()}


def test_matched_episode_gets_proposed_cut_with_confidence():
    p, fs = _three()
    scan(p, fs.__getitem__)
    (c,) = p.episodes["E2.mkv"].cuts
    assert (c.status, c.reference_id) == ("proposed", "r")
    assert c.start == pytest.approx(40, abs=0.5) and c.end == pytest.approx(100, abs=0.5)
    assert c.confidence >= 0.85 and p.episodes["E2.mkv"].flags == []
    assert p.episodes["E3.mkv"].flags == ["No OP 1 found"]


def test_rescan_is_idempotent():
    p, fs = _three()
    scan(p, fs.__getitem__)
    first = _snapshot(p)
    scan(p, fs.__getitem__)
    assert _snapshot(p) == first


def test_hand_added_and_approved_cuts_survive_and_block_duplicates():
    p, fs = _three()
    p.episodes["E2.mkv"].cuts += [
        Cut("hand", 10, 20, "proposed", None, None),
        Cut("ok", 41, 101, "approved", "r", 0.9),
    ]
    scan(p, fs.__getitem__)
    assert [c.id for c in p.episodes["E2.mkv"].cuts] == ["hand", "ok"]


def test_stale_proposal_removed_when_reference_no_longer_found():
    p, fs = _three()
    p.episodes["E3.mkv"].cuts.append(Cut("stale", 5, 10, "proposed", "r", 0.9))
    scan(p, fs.__getitem__)
    assert p.episodes["E3.mkv"].cuts == []


def test_flags_not_accumulated():
    p, fs = _three()
    scan(p, fs.__getitem__)
    scan(p, fs.__getitem__)
    assert p.episodes["E3.mkv"].flags == ["No OP 1 found"]


def test_weak_match_flagged(monkeypatch):
    p, fs = _three()
    monkeypatch.setattr(scan_mod, "find_reference", lambda r, e: Match(40, 100, 0.7))
    scan(p, fs.__getitem__)
    (c,) = p.episodes["E2.mkv"].cuts
    assert c.confidence == 0.7 and "OP 1 match weak" in p.episodes["E2.mkv"].flags


def test_below_weak_is_no_match(monkeypatch):
    p, fs = _three()
    monkeypatch.setattr(scan_mod, "find_reference", lambda r, e: Match(40, 100, 0.5))
    scan(p, fs.__getitem__)
    assert p.episodes["E2.mkv"].cuts == [] and "No OP 1 found" in p.episodes["E2.mkv"].flags


def test_reference_episode_not_in_project():
    p = project_with(["E2.mkv"])
    p.references.append(Reference("r", "OP", "OP 1", "gone.mkv", 90, 150))
    scan(p, {"E2.mkv": ep_feats(40, 2)}.__getitem__)
    st = p.episodes["E2.mkv"]
    assert st.cuts == [] and st.flags[0].startswith("Scan failed") and st.scanned


def test_feature_failure_flags_episode_and_continues():
    p, fs = _three()

    def src(f):
        if f == "E2.mkv":
            raise RuntimeError("boom")
        return fs[f]

    scan(p, src)
    assert p.episodes["E2.mkv"].flags == ["Scan failed: boom"]
    assert p.episodes["E2.mkv"].cuts == []
    assert p.episodes["E3.mkv"].flags == ["No OP 1 found"] and p.episodes["E3.mkv"].scanned


def test_reference_beyond_features_is_guarded():
    p, fs = _three()
    p.references[0].start, p.references[0].end = 5000, 5060
    scan(p, fs.__getitem__)
    assert p.episodes["E2.mkv"].flags[0].startswith("Scan failed")


def test_progress_callback():
    p, fs = _three()
    calls = []
    scan(p, fs.__getitem__, lambda f, x: calls.append((f, x)))
    assert [f for f, _ in calls] == ["E1.mkv", "E2.mkv", "E3.mkv"]
    assert calls[-1][1] == pytest.approx(1.0)
    assert all(0 <= x <= 1 for _, x in calls)


def test_propose_references_appends_unconfirmed():
    p = project_with(["E1.mkv", "E2.mkv", "E3.mkv"])
    fs = {"E1.mkv": ed_feats(30, 320, 1), "E2.mkv": ed_feats(50, 300, 2),
          "E3.mkv": ed_feats(40, 330, 3)}
    new = propose_references(p, fs.__getitem__)
    assert {r.kind for r in new} == {"OP", "ED"}
    assert p.references == new and all(not r.confirmed for r in new)
    assert all(r.label == f"{r.kind}?" for r in new)
    assert len({r.id for r in new}) == len(new)


def test_reference_candidates_report_support_without_mutating():
    from oped.scan import reference_candidates

    p = project_with(["E1.mkv", "E2.mkv", "E3.mkv"])
    fs = {"E1.mkv": ed_feats(30, 320, 1), "E2.mkv": ed_feats(50, 300, 2),
          "E3.mkv": ed_feats(40, 330, 3)}
    cands = reference_candidates(p, fs.__getitem__)
    assert {r.kind for r, _ in cands} == {"OP", "ED"}
    assert all(2 <= s <= 3 for _, s in cands) and all(not r.confirmed for r, _ in cands)
    assert p.references == []


def test_make_feature_source_memoises(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(scan_mod, "probe", lambda tools, path: ("info", Path(path).name))
    monkeypatch.setattr(scan_mod, "episode_features",
                        lambda tools, info, cache: calls.append(info) or np.zeros((3, 12)))

    class Ep:
        def __init__(self, n):
            self.video = tmp_path / n

    class S:
        episodes = [Ep("a.mkv"), Ep("b.mkv")]

    src = make_feature_source("tools", S(), tmp_path / "cache")
    src("a.mkv")
    src("a.mkv")
    src("b.mkv")
    assert len(calls) == 2
    with pytest.raises(KeyError):
        src("nope.mkv")


def _two_refs():
    p, fs = _three()
    p.references.append(Reference("r2", "OP", "OP 2", "E1.mkv", 90, 150))
    return p, fs


def test_failed_episode_one_flag_and_one_fetch_with_two_references():
    p, fs = _two_refs()
    calls = []

    def src(f):
        calls.append(f)
        if f == "E2.mkv":
            raise RuntimeError("boom")
        return fs[f]

    scan(p, src)
    assert p.episodes["E2.mkv"].flags == ["Scan failed: boom"]
    assert calls.count("E2.mkv") == 1


def test_two_references_to_missing_episode_no_duplicate_flags():
    p, fs = _three()
    p.references[:] = [Reference("a", "OP", "OP 1", "gone.mkv", 0, 60),
                       Reference("b", "ED", "ED 1", "gone.mkv", 100, 160)]
    scan(p, fs.__getitem__)
    for n in ("E1.mkv", "E2.mkv", "E3.mkv"):
        assert len(p.episodes[n].flags) == len(set(p.episodes[n].flags)) == 1


def test_failed_reference_leaves_other_reference_and_old_proposals_alone():
    p, fs = _three()
    p.references.append(Reference("bad", "ED", "ED 1", "gone.mkv", 0, 60))
    p.episodes["E2.mkv"].cuts.append(Cut("old", 150, 190, "proposed", "bad", 0.9))
    scan(p, fs.__getitem__)
    ids = {c.id: c for c in p.episodes["E2.mkv"].cuts}
    assert "old" in ids
    (good,) = [c for c in ids.values() if c.reference_id == "r"]
    assert good.start == pytest.approx(40, abs=0.5)
    assert any(f.startswith("Scan failed") for f in p.episodes["E2.mkv"].flags)


def test_edited_proposal_survives_rescan_without_duplicate():
    """M2: an edge edit on a proposal is a user decision; a re-scan keeps it as is."""
    p, fs = _three()
    scan(p, fs.__getitem__)
    (c,) = p.episodes["E2.mkv"].cuts
    c.start, c.edited = c.start + 1.0, True
    edited = (c.id, c.start, c.end)
    scan(p, fs.__getitem__)
    assert [(k.id, k.start, k.end) for k in p.episodes["E2.mkv"].cuts] == [edited]
    assert p.episodes["E2.mkv"].cuts[0].status == "proposed"


def test_rescan_sorts_cuts_by_start():
    p, fs = _three()
    p.episodes["E2.mkv"].cuts.append(Cut("hand", 150, 160, "proposed"))
    scan(p, fs.__getitem__)
    starts = [c.start for c in p.episodes["E2.mkv"].cuts]
    assert len(starts) == 2 and starts == sorted(starts)
