"""Bootstrap Qlib's community CN dataset for reproducible cold-start benchmarks."""

from __future__ import annotations

import json
import os
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

RELEASES_API = "https://api.github.com/repos/chenditc/investment_data/releases?per_page=20"
ASSET_NAME = "qlib_bin.tar.gz"


@dataclass(frozen=True)
class QlibRelease:
    tag: str
    published_at: str
    url: str
    size: int


def latest_complete_release(api_payload: list[dict]) -> QlibRelease:
    for release in api_payload:
        for asset in release.get("assets", []):
            if asset.get("name") == ASSET_NAME and int(asset.get("size", 0)) > 0:
                return QlibRelease(
                    tag=str(release["tag_name"]),
                    published_at=str(release["published_at"]),
                    url=str(asset["browser_download_url"]),
                    size=int(asset["size"]),
                )
    raise RuntimeError("no complete qlib_bin.tar.gz asset found in recent releases")


def fetch_release() -> QlibRelease:
    request = urllib.request.Request(
        RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ashare-research-v2"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return latest_complete_release(json.load(response))


def _safe_relative(name: str, common_root: str | None) -> Path | None:
    value = PurePosixPath(name)
    parts = (
        value.parts[1:]
        if common_root and value.parts and value.parts[0] == common_root
        else value.parts
    )
    if not parts:
        return None
    if value.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe archive member: {name}")
    return Path(*parts)


def extract_qlib_archive(archive_path: Path | str, target: Path | str) -> None:
    archive_path = Path(archive_path)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f"{target.name}-", dir=target.parent))
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = [member for member in archive.getmembers() if member.name]
            roots = {PurePosixPath(member.name).parts[0] for member in members}
            root = next(iter(roots)) if len(roots) == 1 else None
            common_root = root if root not in {None, "", ".", ".."} else None
            for member in members:
                relative = _safe_relative(member.name, common_root)
                if relative is None:
                    continue
                destination = temporary / relative
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeError(f"unable to read archive member: {member.name}")
                with source, destination.open("wb") as handle:
                    shutil.copyfileobj(source, handle)
        if not (temporary / "calendars").exists() or not (temporary / "features").exists():
            raise RuntimeError("archive does not contain a valid Qlib data layout")
        backup = target.with_name(f"{target.name}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if target.exists():
            os.replace(target, backup)
        os.replace(temporary, target)
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def bootstrap_qlib_dataset(runtime_root: Path | str, refresh: bool = False) -> dict:
    runtime_root = Path(runtime_root)
    release = fetch_release()
    target = runtime_root / "qlib" / "cn_data"
    metadata_path = target / "source.json"
    if metadata_path.exists() and not refresh:
        existing = json.loads(metadata_path.read_text("utf-8"))
        if existing.get("release_tag") == release.tag:
            return {**existing, "status": "current"}
    downloads = runtime_root / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    archive = downloads / f"qlib-bin-{release.tag}.tar.gz"
    if not archive.exists() or archive.stat().st_size != release.size:
        temporary = archive.with_suffix(".partial")
        request = urllib.request.Request(release.url, headers={"User-Agent": "ashare-research-v2"})
        with (
            urllib.request.urlopen(request, timeout=60) as response,
            temporary.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        if temporary.stat().st_size != release.size:
            raise RuntimeError(
                f"download size mismatch: got {temporary.stat().st_size}, expected {release.size}"
            )
        os.replace(temporary, archive)
    extract_qlib_archive(archive, target)
    metadata = {
        "status": "downloaded",
        "source": "chenditc/investment_data",
        "release_tag": release.tag,
        "published_at": release.published_at,
        "asset_url": release.url,
        "asset_size": release.size,
        "installed_at": datetime.now(UTC).isoformat(),
        "usage": "cold-start benchmark only",
        "quality_warning": "Community crawler data; production promotion requires Tushare validation.",
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return metadata


def validate_qlib_dataset(runtime_root: Path | str) -> dict:
    import qlib
    from qlib.config import REG_CN
    from qlib.contrib.data.loader import Alpha158DL
    from qlib.data import D

    target = Path(runtime_root) / "qlib" / "cn_data"
    qlib.init(provider_uri=str(target.resolve()), region=REG_CN, kernels=1)
    calendar = D.calendar(start_time="2018-01-01", freq="day")
    instruments = D.list_instruments(
        D.instruments("all"),
        start_time=str(calendar[-10])[:10],
        end_time=str(calendar[-1])[:10],
        as_list=True,
    )
    sample = D.features(
        instruments[:3],
        ["$close", "$volume"],
        start_time=str(calendar[-10])[:10],
        end_time=str(calendar[-1])[:10],
        freq="day",
    )
    _, alpha158_names = Alpha158DL.get_feature_config()
    result = {
        "passed": len(calendar) >= 2_000 and len(instruments) >= 4_000 and not sample.empty,
        "calendar_start": str(calendar[0])[:10],
        "calendar_end": str(calendar[-1])[:10],
        "calendar_days": len(calendar),
        "active_instruments": len(instruments),
        "sample_rows": len(sample),
        "alpha158_features": len(alpha158_names),
    }
    output = target / "health.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return result
