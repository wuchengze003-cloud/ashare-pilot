import io
import tarfile

import pytest

from ashare_research.qlib_bootstrap import extract_qlib_archive, latest_complete_release


def test_release_selector_skips_empty_latest_release():
    release = latest_complete_release(
        [
            {"tag_name": "2026-07-15", "published_at": "x", "assets": []},
            {
                "tag_name": "2026-07-14",
                "published_at": "y",
                "assets": [
                    {
                        "name": "qlib_bin.tar.gz",
                        "size": 123,
                        "browser_download_url": "https://example.test/data",
                    }
                ],
            },
        ]
    )
    assert release.tag == "2026-07-14"
    assert release.size == 123


def test_archive_extraction_strips_root_and_rejects_traversal(tmp_path):
    archive = tmp_path / "valid.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        for name in ("qlib/calendars/day.txt", "qlib/features/sh600000/close.day.bin"):
            payload = b"data"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            handle.addfile(info, io.BytesIO(payload))
    target = tmp_path / "target"
    extract_qlib_archive(archive, target)
    assert (target / "calendars" / "day.txt").exists()

    unsafe = tmp_path / "unsafe.tar.gz"
    with tarfile.open(unsafe, "w:gz") as handle:
        info = tarfile.TarInfo("../outside")
        info.size = 1
        handle.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsafe"):
        extract_qlib_archive(unsafe, tmp_path / "bad")
