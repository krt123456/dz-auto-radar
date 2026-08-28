#!/usr/bin/env python3
"""Publish a freshly validated official-auction watch without changing data.enc.

This narrow fallback is used when the broad radar payload cannot be republished
because its independently verified regular-offer snapshot is stale.  The
dashboard separately validates this public watch against the bound registry and
shows its own freshness state, so this command never upgrades the regular lane
or makes any claim about it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from publish_radar_dashboard import (  # noqa: E402
    OFFICIAL_AUCTION_WATCH_PART_DIRECTORY,
    OFFICIAL_AUCTION_WATCH_PART_NAME,
    build_published_official_auction_watch,
    load_published_official_auction_watch,
    validate_official_auction_watch,
    write_published_official_auction_watch,
)


DEFAULT_WATCH = Path("/home/krt/car_deal_finder/mobile_site_local/official_auction_watch.json")
DEFAULT_SITE = Path("/srv/sonardeals-radar/site")


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def git(site: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(site), *arguments],
        check=False,
        text=True,
        capture_output=True,
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", type=Path, default=DEFAULT_WATCH)
    parser.add_argument("--site", type=Path, default=DEFAULT_SITE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    raw = args.watch.read_bytes()
    try:
        watch = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"official auction watch is not valid UTF-8 JSON: {exc}")
    validate_official_auction_watch(watch)
    row_count = watch.get("row_count")
    manifest_bytes, part_bytes, public_manifest = build_published_official_auction_watch(watch)
    if args.dry_run:
        print(json.dumps({
            "result": "OFFICIAL_AUCTION_WATCH_ONLY_VALID",
            "row_count": row_count,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "public_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "part_count": len(part_bytes),
        }, sort_keys=True))
        return 0

    if not (args.site / ".git").exists():
        raise SystemExit(f"publication directory is not a Git checkout: {args.site}")
    public_manifest = write_published_official_auction_watch(args.site, watch)
    target = args.site / "official_auction_watch.json"
    staged_paths = [target.name]
    tracked_parts = [
        line.strip()
        for line in git(args.site, "ls-files", "--", OFFICIAL_AUCTION_WATCH_PART_DIRECTORY).stdout.splitlines()
        if line.strip()
    ]
    if (args.site / OFFICIAL_AUCTION_WATCH_PART_DIRECTORY).is_dir():
        staged_paths.append(OFFICIAL_AUCTION_WATCH_PART_DIRECTORY)
    staged_paths.extend(tracked_parts)
    git(args.site, "add", "-A", "--", *sorted(set(staged_paths)))
    staged = {
        line.strip()
        for line in git(args.site, "diff", "--cached", "--name-only").stdout.splitlines()
        if line.strip()
    }
    unexpected = {
        path for path in staged
        if path != target.name
        and not (
            path.startswith(f"{OFFICIAL_AUCTION_WATCH_PART_DIRECTORY}/")
            and OFFICIAL_AUCTION_WATCH_PART_NAME.fullmatch(Path(path).name)
        )
    }
    if unexpected:
        raise SystemExit(f"refusing to publish unexpected files: {sorted(unexpected)}")
    if not staged:
        print(json.dumps({
            "result": "OFFICIAL_AUCTION_WATCH_ONLY_NO_CHANGE",
            "row_count": row_count,
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "public_manifest_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "part_count": len(public_manifest["parts"]),
        }, sort_keys=True))
        return 0
    git(args.site, "diff", "--cached", "--check")
    generated = str(watch.get("generated_at_utc") or "unknown").replace(" ", "T")[:32]
    git(args.site, "commit", "-m", f"official auction watch {generated}")
    git(args.site, "push", "origin", "HEAD:main")
    reconstructed, _ = load_published_official_auction_watch(args.site)
    if reconstructed.get("row_count") != row_count:
        raise SystemExit("published official auction watch count mismatch")
    print(json.dumps({
        "result": "OFFICIAL_AUCTION_WATCH_ONLY_PUSHED",
        "row_count": row_count,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "public_manifest_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "part_count": len(public_manifest["parts"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
