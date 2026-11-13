from oped.timeline import TimeMap, keep_intervals, merge_cuts


def test_merge_clamps_sorts_merges():
    assert merge_cuts([(50, 60), (-5, 3), (55, 70), (70, 80), (200, 300)], 100) == [(0, 3), (50, 80)]


def test_merge_drops_empty_and_inverted():
    assert merge_cuts([(10, 10), (30, 20), (100, 120)], 100) == []


def test_merge_touching_within_epsilon():
    assert merge_cuts([(10, 20), (20.0005, 30)], 100) == [(10, 30)]
    assert merge_cuts([(10, 20), (20.01, 30)], 100) == [(10, 20), (20.01, 30)]


def test_keep_intervals_edges():
    assert keep_intervals([(0, 10), (90, 100)], 100) == [(10, 90)]
    assert keep_intervals([(10, 20)], 20.03) == [(0, 10)]


def test_keep_intervals_no_cuts_and_all_cut():
    assert keep_intervals([], 50) == [(0, 50)]
    assert keep_intervals([(0, 50)], 50) == []
    assert keep_intervals([(-1, 60)], 50) == []


def test_keep_intervals_cut_to_end_no_zero_length():
    assert keep_intervals([(40, 100)], 100) == [(0, 40)]
    assert keep_intervals([(0, 10), (10, 20)], 100) == [(20, 100)]


def test_timemap():
    tm = TimeMap([(10, 20), (50, 60)], 100)
    assert tm.out_duration == 80
    assert tm.map(5) == 5 and tm.map(15) is None and tm.map(25) == 15 and tm.map(70) == 50
    assert tm.snap(15) == 10
    assert tm.map_span(8, 12) == (8, 10)
    assert tm.map_span(18, 22) == (10, 12)
    assert tm.map_span(12, 18) is None
    assert tm.map_span(5, 25) == (5, 15)
    assert tm.map_span_whole(8, 12) == (8, 12)
    assert tm.map_span_whole(9, 13) is None


def test_timemap_boundaries():
    tm = TimeMap([(10, 20)], 100)
    assert tm.map(10) == 10 and tm.map(20) == 10
    assert tm.map(100) == 90


def test_timemap_cut_from_zero_and_to_end():
    tm = TimeMap([(0, 10), (90, 100)], 100)
    assert tm.out_duration == 80
    assert tm.map(5) is None and tm.map(10) == 0 and tm.map(50) == 40
    assert tm.snap(5) == 0
    assert tm.snap(95) == 80
    assert tm.map_span(0, 20) == (0, 10)
    assert tm.map_span(85, 100) == (75, 80)


def test_timemap_overlapping_cuts_and_no_cuts():
    tm = TimeMap([(10, 30), (20, 40)], 100)
    assert tm.out_duration == 70
    assert tm.map(45) == 15
    tm0 = TimeMap([], 100)
    assert tm0.out_duration == 100 and tm0.map(33) == 33
    assert tm0.map_span(1, 2) == (1, 2)


def test_map_span_whole_snaps_and_clamps():
    tm = TimeMap([(10, 20)], 100)
    assert tm.map_span_whole(15, 30) == (10, 25)   # 15 kept of... 10/15 outside
    tm2 = TimeMap([(90, 100)], 100)
    assert tm2.map_span_whole(80, 95) is None or tm2.map_span_whole(80, 95)[1] <= tm2.out_duration
    assert TimeMap([(0, 100)], 100).map_span_whole(10, 20) is None
