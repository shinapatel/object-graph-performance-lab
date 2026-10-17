import gzip
from pathlib import Path

from object_graph_performance_lab import config
from object_graph_performance_lab.cli import main
from object_graph_performance_lab.config import redact_database_url
from object_graph_performance_lab.download import download_datasets


def test_redact_hides_password():
    url = "postgresql://demo:supersecret@ep-example.neon.tech:5432/neondb?sslmode=require"
    described = redact_database_url(url)
    assert "supersecret" not in described
    assert "demo" in described
    assert "ep-example.neon.tech" in described
    assert "neondb" in described
    assert "sslmode" not in described


class _Response:
    def __init__(self, payload: bytes):
        self._payload = payload
        self._offset = 0
        self.headers = {"Content-Length": str(len(payload))}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = len(self._payload) - self._offset
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_download_caches_and_replaces_invalid_gzip(tmp_path: Path):
    payload = gzip.compress(b"tconst\ttt\n")
    calls = {"count": 0}

    def opener(request, timeout=0):
        calls["count"] += 1
        return _Response(payload)

    download_datasets(tmp_path, opener=opener)
    assert calls["count"] == 3
    download_datasets(tmp_path, opener=opener)
    assert calls["count"] == 3
    basics = tmp_path / "title.basics.tsv.gz"
    basics.write_bytes(b"not-gzip")

    def fail_if_ratings(request, timeout=0):
        calls["count"] += 1
        if not str(request.full_url).endswith("title.basics.tsv.gz"):
            raise AssertionError(request.full_url)
        return _Response(payload)

    download_datasets(tmp_path, opener=fail_if_ratings)
    assert gzip.decompress(basics.read_bytes()) == b"tconst\ttt\n"


def test_init_inspect_ingest_verify_and_reset(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setattr(config, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(config, "database_url", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "")
    fixtures = Path(__file__).parent / "fixtures"
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)
    for name in ("title.basics.tsv", "title.ratings.tsv", "title.crew.tsv"):
        data = (fixtures / name).read_bytes()
        with gzip.open(raw / f"{name}.gz", "wb") as handle:
            handle.write(data)

    assert main(["init"]) == 0
    init_out = capsys.readouterr().out
    assert "SELECT 1 ok" in init_out
    assert "DATABASE_URL is not set" in init_out
    assert (tmp_path / "data" / "local" / "imdb.sqlite").is_file()

    assert main(["ingest", "--limit", "3"]) == 0
    capsys.readouterr()
    assert main(["verify"]) == 0
    verify_out = capsys.readouterr().out
    assert "result: ok" in verify_out
    assert "postgres: skipped" in verify_out

    assert main(["inspect"]) == 0
    inspect_out = capsys.readouterr().out
    assert "titles: 3" in inspect_out
    assert "ratings: 3" in inspect_out
    assert "crew relationships: 4" in inspect_out
    assert "SELECT 1 ok" in inspect_out

    assert main(["reset"]) == 2
    refused = capsys.readouterr().out
    assert "without --yes" in refused
    assert (tmp_path / "data" / "local" / "imdb.sqlite").is_file()

    assert main(["reset", "--yes"]) == 0
    assert not (tmp_path / "data" / "local" / "imdb.sqlite").exists()
    assert (raw / "title.basics.tsv.gz").is_file()
