import gzip
from pathlib import Path

import pytest

from object_graph_performance_lab.errors import LabError
from object_graph_performance_lab.subset import iter_titles
from object_graph_performance_lab.tsv import (
    iter_dicts,
    parse_adult,
    parse_crew_links,
    parse_id_list,
    parse_int,
    parse_rating_row,
    parse_title_row,
)


def test_null_and_list_parsing():
    assert parse_int(r"\N") is None
    assert parse_int("") is None
    assert parse_int("1894") == 1894
    assert parse_adult("0") is False
    assert parse_adult("1") is True
    assert parse_adult(r"\N") is None
    assert parse_id_list(r"\N") == ()
    assert parse_id_list("nm0000001,nm0000001, nm0000002") == ("nm0000001", "nm0000002")
    title = parse_title_row(
        {
            "tconst": "tt1",
            "titleType": "short",
            "primaryTitle": r"\N",
            "isAdult": r"\N",
            "startYear": r"\N",
            "runtimeMinutes": r"\N",
        }
    )
    assert title is not None
    assert title.name is None
    assert title.is_adult is None
    assert title.release_year is None
    assert title.runtime_minutes is None
    rating = parse_rating_row(
        {"tconst": "tt1", "averageRating": r"\N", "numVotes": r"\N"}
    )
    assert rating is not None
    assert rating.average_rating is None
    assert rating.vote_count is None
    assert parse_crew_links({"tconst": "tt1", "directors": r"\N", "writers": r"\N"}) == ()
    links = parse_crew_links(
        {"tconst": "tt1", "directors": "nm1", "writers": "nm1,nm2"}
    )
    assert links == (
        ("tt1", "nm1", "director"),
        ("tt1", "nm1", "writer"),
        ("tt1", "nm2", "writer"),
    )


def test_invalid_number_and_flag():
    with pytest.raises(ValueError):
        parse_int("18xx")
    with pytest.raises(ValueError):
        parse_adult("2")


def test_missing_column(tmp_path: Path):
    path = tmp_path / "bad.tsv"
    path.write_text("tconst\tname\n", encoding="utf-8")
    with pytest.raises(LabError, match="missing columns"):
        list(iter_dicts(path, ("tconst", "startYear")))


def test_gzip_and_first_duplicate_wins(tmp_path: Path):
    plain = tmp_path / "title.basics.tsv"
    plain.write_text(
        "tconst\ttitleType\tprimaryTitle\toriginalTitle\tisAdult\tstartYear\t"
        "endYear\truntimeMinutes\tgenres\n"
        "tt1\tshort\tFirst\tFirst\t0\t1894\t\\N\t1\tShort\n"
        "tt1\tmovie\tSecond\tSecond\t1\t2000\t\\N\t2\tDrama\n"
        "tt2\tshort\tOther\tOther\t0\t1892\t\\N\t\\N\tShort\n",
        encoding="utf-8",
    )
    packed = tmp_path / "title.basics.tsv.gz"
    with gzip.open(packed, "wt", encoding="utf-8") as handle:
        handle.write(plain.read_text(encoding="utf-8"))
    rows = list(iter_titles(packed, 0))
    assert [row.title_id for row in rows] == ["tt1", "tt2"]
    assert rows[0].name == "First"
    assert rows[0].type == "short"
