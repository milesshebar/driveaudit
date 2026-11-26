import itertools
import unicodedata

from driveaudit.matching import Evidence, FileRec, ReferenceIndex, compare


def rec(drive="d", rel="a.bin", size=100, partial=None, full=None):
    return FileRec(drive, rel, size, 1, partial, full)


def test_size_alone_is_never_a_match():
    assert compare(rec(rel="x/one.bin", size=5), rec(rel="y/two.bin", size=5)) == Evidence.NONE


def test_levels():
    assert (
        compare(rec(partial="p", full="f"), rec(rel="z/q.bin", partial="p", full="f"))
        == Evidence.VERIFIED
    )
    assert compare(rec(partial="p"), rec(rel="z/q.bin", partial="p", full="f")) == Evidence.PROBABLE
    assert compare(rec(), rec(drive="e")) == Evidence.NAME_SIZE
    assert compare(rec(), rec(rel="other.bin")) == Evidence.NONE


def test_conflicting_full_hashes_beat_everything_else():
    a = rec(partial="p", full="f1")
    b = rec(drive="e", partial="p", full="f2")
    assert compare(a, b) == Evidence.NONE


def test_conflicting_partials_beat_name_match():
    assert compare(rec(partial="p1"), rec(drive="e", partial="p2")) == Evidence.NONE


def test_different_sizes_never_match():
    assert compare(rec(size=1, full="f"), rec(size=2, full="f")) == Evidence.NONE


def test_names_compare_after_unicode_normalization():
    nfd = unicodedata.normalize("NFD", "café.bin")
    nfc = unicodedata.normalize("NFC", "café.bin")
    assert compare(rec(rel=nfd), rec(drive="e", rel=nfc)) == Evidence.NAME_SIZE


def test_index_agrees_with_pairwise_compare_exhaustively():
    """ReferenceIndex.best must equal max(compare) over all reference files."""
    hashes = [(None, None), ("p1", None), ("p1", "f1"), ("p1", "f2"), ("p2", None), ("p2", "f3")]
    names = ["a.bin", "b.bin"]
    sizes = [10, 20]
    pool = [
        rec(drive="r", rel=f"{n}", size=s, partial=p, full=f)
        for n, s, (p, f) in itertools.product(names, sizes, hashes)
    ]
    probes = [
        rec(drive="x", rel=f"dir/{n}", size=s, partial=p, full=f)
        for n, s, (p, f) in itertools.product(names, sizes, hashes)
    ]
    for ref_subset in (pool, pool[::2], pool[1::3], pool[:5]):
        idx = ReferenceIndex(ref_subset)
        for probe in probes:
            want = max((compare(probe, r) for r in ref_subset), default=Evidence.NONE)
            got, match = idx.best(probe)
            assert got == want, (probe, got, want)
            if got != Evidence.NONE:
                assert compare(probe, match) == got
