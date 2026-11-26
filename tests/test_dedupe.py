import json

from driveaudit.dedupe import find_duplicates, render_json, render_text
from driveaudit.matching import Evidence

from .conftest import blob, make_tree


def test_verified_duplicates_across_and_within_drives(tmp_path, conn, cat):
    make_tree(
        tmp_path / "a",
        {"x/film.mov": blob("film", 50_000), "x/copy.mov": blob("film", 50_000), "u.txt": "unique"},
    )
    make_tree(
        tmp_path / "b",
        {"backup/film_renamed.mov": blob("film", 50_000), "other.bin": blob("o", 30_000)},
    )
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    cat.hash_all()
    rep = find_duplicates(conn)
    assert len(rep.verified) == 1 and not rep.probable
    g = rep.verified[0]
    assert g.storage_units == 3 and g.wasted_bytes == 100_000
    assert {(f.drive, f.rel) for f in g.copies} == {
        ("a", "x/film.mov"),
        ("a", "x/copy.mov"),
        ("b", "backup/film_renamed.mov"),
    }
    assert rep.unhashed_candidates == 0


def test_same_size_and_same_ends_but_different_middle_is_not_a_duplicate(tmp_path, conn, cat):
    a, b = bytearray(blob("m", 90_000)), bytearray(blob("m", 90_000))
    b[45_000] ^= 0xFF
    make_tree(tmp_path / "a", {"clip.bin": bytes(a)})
    make_tree(tmp_path / "b", {"clip.bin": bytes(b)})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    cat.hash_all()  # partial hashes collide, so full hashes are computed and differ
    assert find_duplicates(conn).groups == []


def test_partial_only_matches_are_reported_as_probable_not_verified(tmp_path, conn, cat):
    from driveaudit.catalog import hash_drive

    make_tree(tmp_path / "a", {"clip.bin": blob("p", 90_000)})
    make_tree(tmp_path / "b", {"clip.bin": blob("p", 90_000)})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    for d in ("a", "b"):  # partial stage only: no full hashes yet
        hash_drive(
            conn,
            d,
            str(tmp_path / d),
            expected_uuid_check=False,
            stages=("partial",),
            log=lambda *_: None,
        )
    rep = find_duplicates(conn)
    assert not rep.verified and len(rep.probable) == 1
    assert rep.probable[0].evidence == Evidence.PROBABLE
    assert "NOT proven" in render_text(rep)


def test_same_size_different_content_never_groups(tmp_path, conn, cat):
    make_tree(tmp_path / "a", {"f.bin": blob("one", 40_000)})
    make_tree(tmp_path / "b", {"f.bin": blob("two", 40_000)})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    cat.hash_all()
    assert find_duplicates(conn).groups == []


def test_unhashed_files_are_surfaced_not_guessed(tmp_path, conn, cat):
    make_tree(tmp_path / "a", {"f.bin": blob("one", 40_000), "g.bin": blob("g", 777)})
    make_tree(tmp_path / "b", {"renamed.bin": blob("one", 40_000)})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    rep = find_duplicates(conn)  # nothing hashed yet
    assert rep.groups == [] and rep.unhashed_candidates == 2
    weak = find_duplicates(conn, weak=True)
    assert weak.weak_groups == []  # different names: not even a weak match
    make_tree(tmp_path / "b", {"f.bin": blob("two", 40_000)})
    cat(tmp_path / "b", "b")
    weak = find_duplicates(conn, weak=True)
    assert len(weak.weak_groups) == 1 and weak.groups == []


def test_hard_links_do_not_count_as_wasted_space(tmp_path, conn, cat):
    import os

    make_tree(tmp_path / "a", {"one.bin": blob("hl", 40_000)})
    os.link(tmp_path / "a" / "one.bin", tmp_path / "a" / "two.bin")
    make_tree(tmp_path / "b", {"three.bin": blob("hl", 40_000)})
    cat(tmp_path / "a", "a")
    cat(tmp_path / "b", "b")
    cat.hash_all()
    g = find_duplicates(conn).verified[0]
    assert len(g.copies) == 3 and g.storage_units == 2 and g.wasted_bytes == 40_000


def test_zero_byte_files_cost_nothing(tmp_path, conn, cat):
    make_tree(tmp_path / "a", {"e1": b"", "e2": b""})
    cat(tmp_path / "a", "a")
    assert find_duplicates(conn, min_size=1).groups == []


def test_json_output_is_valid_and_complete(tmp_path, conn, cat):
    make_tree(tmp_path / "a", {"f.bin": blob("j", 40_000), "g.bin": blob("j", 40_000)})
    cat(tmp_path / "a", "a")
    cat.hash_all()
    data = json.loads(render_json(find_duplicates(conn)))
    assert data["summary"]["verified_groups"] == 1
    assert data["groups"][0]["evidence"] == "verified"
