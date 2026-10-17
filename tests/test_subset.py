from object_graph_performance_lab.config import DatasetPaths
from object_graph_performance_lab.subset import iter_crew, iter_ratings, iter_titles


def test_prefix_is_file_order_not_sorted_order(datasets: DatasetPaths):
    ids: set[str] = set()
    titles = list(iter_titles(datasets.basics, 3, ids))
    assert [title.title_id for title in titles] == ["tt0000009", "tt0000001", "tt0000003"]
    assert ids == {"tt0000009", "tt0000001", "tt0000003"}
    ratings = [row.title_id for row in iter_ratings(datasets.ratings, ids)]
    assert ratings == ["tt0000009", "tt0000001", "tt0000003"]
    crew = list(iter_crew(datasets.crew, ids))
    assert crew == [
        ("tt0000009", "nm0000001", "director"),
        ("tt0000001", "nm0000002", "director"),
        ("tt0000001", "nm0000002", "writer"),
        ("tt0000001", "nm0000003", "writer"),
    ]


def test_limit_zero_reads_every_fixture_title(datasets: DatasetPaths):
    ids: set[str] = set()
    titles = list(iter_titles(datasets.basics, 0, ids))
    assert [title.title_id for title in titles] == [
        "tt0000009",
        "tt0000001",
        "tt0000003",
        "tt0000002",
        "tt0000008",
    ]
    rating_ids = [row.title_id for row in iter_ratings(datasets.ratings, ids)]
    assert "tt9999999" not in rating_ids
    assert len(rating_ids) == 5
    assert len(list(iter_crew(datasets.crew, ids))) == 9
