"""Download IMDb non-commercial dataset files into data/raw/.

The files stay on disk for local ingest. This repository does not redistribute
them. A file that is already present and readable as gzip is left in place
unless ``force`` is set.
"""

from __future__ import annotations

import gzip
import urllib.request
from pathlib import Path

from object_graph_performance_lab.config import DATASET_URLS
from object_graph_performance_lab.errors import LabError

USER_AGENT = "object-graph-performance-lab/0.1 (non-commercial IMDb dataset client)"
_CHUNK = 1024 * 1024
_REPORT_EVERY = 8 * 1024 * 1024


def gzip_is_readable(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as handle:
            handle.read(64)
        return path.stat().st_size > 0
    except (OSError, EOFError, gzip.BadGzipFile):
        return False


def fetch_file(
    url: str,
    dest: Path,
    *,
    force: bool = False,
    opener=urllib.request.urlopen,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not force and gzip_is_readable(dest):
        print(f"cached {dest.name} ({dest.stat().st_size} bytes)", flush=True)
        return
    partial = dest.with_name(dest.name + ".partial")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    print(f"downloading {dest.name}", flush=True)
    try:
        with opener(request, timeout=60) as response:
            total = int(response.headers.get("Content-Length") or 0)
            written = 0
            next_report = _REPORT_EVERY
            with partial.open("wb") as handle:
                while True:
                    chunk = response.read(_CHUNK)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
                    if written >= next_report:
                        if total:
                            print(
                                f"  {dest.name} {written // (1024 * 1024)} / "
                                f"{total // (1024 * 1024)} MiB",
                                flush=True,
                            )
                        else:
                            print(
                                f"  {dest.name} {written // (1024 * 1024)} MiB",
                                flush=True,
                            )
                        next_report += _REPORT_EVERY
    except Exception as exc:
        partial.unlink(missing_ok=True)
        raise LabError(f"download failed for {url}: {exc}") from exc
    if not gzip_is_readable(partial):
        partial.unlink(missing_ok=True)
        raise LabError(f"downloaded file is not valid gzip: {dest.name}")
    partial.replace(dest)
    print(f"saved {dest.name} ({dest.stat().st_size} bytes)", flush=True)


def download_datasets(directory: Path, *, force: bool = False, opener=urllib.request.urlopen) -> None:
    print(
        "IMDb non-commercial datasets "
        "(https://developer.imdb.com/non-commercial-datasets/).",
        flush=True,
    )
    print(
        "Files are cached locally and are not committed or redistributed.",
        flush=True,
    )
    directory.mkdir(parents=True, exist_ok=True)
    for name, url in DATASET_URLS:
        fetch_file(url, directory / name, force=force, opener=opener)
