#!/usr/bin/env python3
"""Bounded, fail-closed retention for sealed Radar rank baselines.

This dark helper has no network, scheduler, publisher, or notification path.
Exporter and retention share one canonical exclusive lock. The internal apply
path uses inode-bound directory operations plus a durable operation journal,
but the public CLI remains dry-run-only until protected-LKG and incident
lifecycle gates are implemented and reviewed.
"""

from __future__ import annotations

import argparse
import ctypes
from dataclasses import dataclass
from datetime import UTC, datetime
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any
import uuid

try:
    from . import radar_rank_baseline as baseline
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import radar_rank_baseline as baseline


HARD_MAX_ARTIFACTS = 64
HARD_MAX_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_POINTER_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
RECEIPT_CONTRACT = "radar-rank-baseline-retention-operation-v1"
RECEIPT_POINTER_CONTRACT = "radar-rank-baseline-retention-pointer-v1"
RENAME_NOREPLACE = 1
ARTIFACT_RE = re.compile(
    rf"^{re.escape(baseline.CONTRACT)}\.([0-9a-f]{{64}})\.json$"
)


class RetentionError(RuntimeError):
    """Raised before an unsafe or unverifiable retention action."""


@dataclass(frozen=True)
class Artifact:
    path: Path
    sha256: str
    size: int
    device: int
    inode: int
    data_generated_at_utc: str
    data_generated_at: datetime


def file_sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _timestamp_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_existing(path: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise RetentionError(f"{label} is unavailable") from exc
    if resolved != absolute:
        raise RetentionError(f"{label} contains a symlink or path alias")
    return resolved


def _require_directory(path: Path, label: str) -> Path:
    resolved = _canonical_existing(path, label)
    metadata = os.lstat(resolved)
    if not stat.S_ISDIR(metadata.st_mode):
        raise RetentionError(f"{label} is not a directory")
    return resolved


def _read_regular(path: Path, label: str, maximum: int) -> tuple[bytes, os.stat_result]:
    resolved = _canonical_existing(path, label)
    metadata = os.lstat(resolved)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RetentionError(f"{label} is not a private regular file")
    if metadata.st_size > maximum:
        raise RetentionError(f"{label} exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
            or opened.st_nlink != 1
        ):
            raise RetentionError(f"{label} changed while opening")
        data = b""
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            data += block
            if len(data) > maximum:
                raise RetentionError(f"{label} exceeds its size limit")
        completed = os.fstat(descriptor)
        if (
            completed.st_dev != opened.st_dev
            or completed.st_ino != opened.st_ino
            or completed.st_size != opened.st_size
            or completed.st_mtime_ns != opened.st_mtime_ns
            or completed.st_ctime_ns != opened.st_ctime_ns
            or completed.st_nlink != 1
            or stat.S_IMODE(completed.st_mode) != 0o600
        ):
            raise RetentionError(f"{label} changed while reading")
    finally:
        os.close(descriptor)
    return data, completed


def _load_pointer(
    pointer_path: Path,
    artifact_dir: Path,
    trusted_pointer_sha256: str,
    minimum_data_generated_at_utc: str,
) -> tuple[dict[str, Any], bytes, Path]:
    trusted = baseline.require_hash(
        trusted_pointer_sha256, "trusted latest-accepted pointer SHA-256"
    )
    pointer_raw, _ = _read_regular(pointer_path, "latest-accepted pointer", MAX_POINTER_BYTES)
    if file_sha256_bytes(pointer_raw) != trusted:
        raise RetentionError("latest-accepted pointer differs from its trusted anchor")
    pointer = baseline.loads_strict(pointer_raw, "latest-accepted pointer")
    if not isinstance(pointer, dict):
        raise RetentionError("latest-accepted pointer is not an object")
    try:
        baseline.validate_latest_accepted_pointer_envelope(pointer)
        minimum = baseline.parse_utc(
            minimum_data_generated_at_utc, "trusted minimum baseline timestamp"
        )
        observed = baseline.parse_utc(
            pointer["data_generated_at_utc"], "pointer data timestamp"
        )
    except baseline.BaselineError as exc:
        raise RetentionError(str(exc)) from exc
    if observed < minimum:
        raise RetentionError("latest-accepted pointer is older than its rollback floor")
    pointed = artifact_dir / pointer["artifact_file"]
    try:
        if pointed.parent.resolve(strict=True) != artifact_dir:
            raise RetentionError("pointed artifact escapes the artifact directory")
        baseline.validate_latest_accepted_pointer(pointer, artifact_dir)
    except (baseline.BaselineError, OSError) as exc:
        raise RetentionError(f"latest-accepted pointer or artifact is invalid: {exc}") from exc
    _read_regular(pointed, "pointed artifact", MAX_ARTIFACT_BYTES)
    return pointer, pointer_raw, pointed


def _read_regular_at(
    directory_fd: int,
    name: str,
    label: str,
    maximum: int,
) -> tuple[bytes, os.stat_result]:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise RetentionError(f"{label} has an unsafe directory entry name")
    metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise RetentionError(f"{label} is not a private regular file")
    if metadata.st_size > maximum:
        raise RetentionError(f"{label} exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
        ):
            raise RetentionError(f"{label} changed while opening")
        chunks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise RetentionError(f"{label} exceeds its size limit")
        completed = os.fstat(descriptor)
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        for observed in (completed, current):
            if (
                observed.st_dev != opened.st_dev
                or observed.st_ino != opened.st_ino
                or observed.st_size != opened.st_size
                or observed.st_mtime_ns != opened.st_mtime_ns
                or observed.st_ctime_ns != opened.st_ctime_ns
                or observed.st_nlink != 1
                or stat.S_IMODE(observed.st_mode) != 0o600
            ):
                raise RetentionError(f"{label} changed while reading")
        return b"".join(chunks), completed
    finally:
        os.close(descriptor)


def _scan_artifacts_locked(
    directory: Path, directory_fd: int,
) -> tuple[list[Artifact], list[str]]:
    artifacts: list[Artifact] = []
    unknown_entries: list[str] = []
    seen_inodes: set[tuple[int, int]] = set()
    for name in sorted(os.listdir(directory_fd)):
        if not name.startswith(f"{baseline.CONTRACT}."):
            unknown_entries.append(name)
            continue
        match = ARTIFACT_RE.fullmatch(name)
        if match is None:
            raise RetentionError("artifact namespace contains a malformed name")
        path = directory / name
        raw, metadata = _read_regular_at(
            directory_fd, name, "baseline artifact", MAX_ARTIFACT_BYTES
        )
        identity = (metadata.st_dev, metadata.st_ino)
        if identity in seen_inodes:
            raise RetentionError("baseline artifact inventory contains a duplicate inode")
        seen_inodes.add(identity)
        observed_sha256 = file_sha256_bytes(raw)
        if observed_sha256 != match.group(1):
            raise RetentionError("baseline artifact differs from its content-addressed name")
        try:
            payload = baseline.loads_strict(raw, "baseline artifact")
            validated = baseline.validate_baseline_structure(payload)
            generated = baseline.parse_utc(
                validated["data_generated_at_utc"],
                "baseline artifact data timestamp",
            )
        except baseline.BaselineError as exc:
            raise RetentionError(f"baseline artifact is invalid: {exc}") from exc
        artifacts.append(
            Artifact(
                path=path,
                sha256=observed_sha256,
                size=metadata.st_size,
                device=metadata.st_dev,
                inode=metadata.st_ino,
                data_generated_at_utc=validated["data_generated_at_utc"],
                data_generated_at=generated,
            )
        )
    return artifacts, sorted(unknown_entries)


def _revalidate_candidate_locked(item: Artifact, directory_fd: int) -> None:
    raw, reopened = _read_regular_at(
        directory_fd,
        item.path.name,
        "retention candidate",
        MAX_ARTIFACT_BYTES,
    )
    if (
        reopened.st_dev != item.device
        or reopened.st_ino != item.inode
        or reopened.st_size != item.size
        or file_sha256_bytes(raw) != item.sha256
    ):
        raise RetentionError("retention candidate hash or identity changed before deletion")


def _artifact_evidence(item: Artifact) -> dict[str, Any]:
    return {
        "name": item.path.name,
        "sha256": item.sha256,
        "size": item.size,
        "device": item.device,
        "inode": item.inode,
        "data_generated_at_utc": item.data_generated_at_utc,
    }


def _inventory_evidence(artifacts: list[Artifact]) -> list[dict[str, Any]]:
    return [
        _artifact_evidence(item)
        for item in sorted(artifacts, key=lambda value: value.path.name)
    ]


def _seal_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    core = {
        key: value
        for key, value in receipt.items()
        if key != "receipt_payload_sha256"
    }
    return {**core, "receipt_payload_sha256": baseline.canonical_sha256(core)}


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise RetentionError("short write while persisting operation evidence")
        offset += written


def _write_new_private_at(
    parent_fd: int,
    name: str,
    data: bytes,
    *,
    allow_exact: bool = False,
) -> None:
    if Path(name).name != name or not name:
        raise RetentionError("unsafe operation evidence filename")
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    linked = False
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            linked = True
        except FileExistsError:
            if not allow_exact:
                raise RetentionError("operation evidence destination already exists")
            observed, _ = _read_regular_at(
                parent_fd, name, "existing immutable operation record", MAX_RECEIPT_BYTES
            )
            if observed != data:
                raise RetentionError("immutable operation record conflicts with existing bytes")
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
        if linked:
            observed, _ = _read_regular_at(
                parent_fd, name, "immutable operation evidence", MAX_RECEIPT_BYTES
            )
            if observed != data:
                raise RetentionError("immutable operation evidence did not persist exact bytes")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _replace_private_at(parent_fd: int, name: str, data: bytes) -> None:
    temporary = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
        0o600,
        dir_fd=parent_fd,
    )
    try:
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def _atomic_write_operation_receipt(
    path: Path,
    receipt: dict[str, Any],
    *,
    create: bool,
) -> tuple[Path, str]:
    absolute = Path(os.path.abspath(os.fspath(path)))
    if absolute.name != path.name or not absolute.name:
        raise RetentionError("operation receipt path is not a direct file path")
    parent = _require_directory(absolute.parent, "operation receipt directory")
    absolute = parent / absolute.name
    parent_meta = os.lstat(parent)
    if parent_meta.st_uid != os.geteuid() or stat.S_IMODE(parent_meta.st_mode) & 0o022:
        raise RetentionError("operation receipt directory is not securely owned")
    parent_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    parent_flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    parent_fd = os.open(parent, parent_flags)
    lock_fd = -1
    try:
        opened_parent = os.fstat(parent_fd)
        if (
            opened_parent.st_dev != parent_meta.st_dev
            or opened_parent.st_ino != parent_meta.st_ino
        ):
            raise RetentionError("operation receipt directory changed while opening")
        lock_name = f".{absolute.name}.lock"
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        lock_flags |= getattr(os, "O_CLOEXEC", 0)
        lock_fd = os.open(lock_name, lock_flags, 0o600, dir_fd=parent_fd)
        lock_meta = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(lock_meta.st_mode)
            or lock_meta.st_nlink != 1
            or lock_meta.st_uid != os.geteuid()
            or stat.S_IMODE(lock_meta.st_mode) != 0o600
        ):
            raise RetentionError("operation receipt lock is not private and stable")
        fcntl.flock(lock_fd, fcntl.LOCK_EX)

        prior_pointer: dict[str, Any] | None = None
        prior_pointer_raw: bytes | None = None
        prior_pointer_meta: os.stat_result | None = None
        prior_record_sha256: str | None = None
        sequence = 1
        if create:
            try:
                os.stat(absolute.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RetentionError("operation receipt already exists")
        else:
            prior_pointer_raw, prior_pointer_meta = _read_regular_at(
                parent_fd, absolute.name, "existing operation receipt pointer", MAX_RECEIPT_BYTES
            )
            prior_pointer = baseline.loads_strict(
                prior_pointer_raw, "existing operation receipt pointer"
            )
            if not isinstance(prior_pointer, dict):
                raise RetentionError("operation receipt pointer is not an object")
            prior_core = {
                key: value for key, value in prior_pointer.items()
                if key != "pointer_payload_sha256"
            }
            if (
                prior_pointer.get("contract") != RECEIPT_POINTER_CONTRACT
                or prior_pointer.get("schema_version") != 1
                or prior_pointer.get("operation_id") != receipt.get("operation_id")
                or prior_pointer.get("pointer_payload_sha256")
                != baseline.canonical_sha256(prior_core)
            ):
                raise RetentionError("operation receipt pointer seal or identity changed")
            prior_record_raw, _ = _read_regular_at(
                parent_fd,
                str(prior_pointer.get("record_file")),
                "prior immutable operation record",
                MAX_RECEIPT_BYTES,
            )
            prior_record_sha256 = file_sha256_bytes(prior_record_raw)
            if prior_record_sha256 != prior_pointer.get("record_sha256"):
                raise RetentionError("prior immutable operation record hash changed")
            prior_record = baseline.loads_strict(
                prior_record_raw, "prior immutable operation record"
            )
            if not isinstance(prior_record, dict):
                raise RetentionError("prior immutable operation record is not an object")
            prior_record_core = {
                key: value for key, value in prior_record.items()
                if key != "receipt_payload_sha256"
            }
            if (
                prior_record.get("receipt_payload_sha256")
                != baseline.canonical_sha256(prior_record_core)
                or prior_record.get("phase") != prior_pointer.get("phase")
                or prior_record.get("journal_sequence")
                != prior_pointer.get("journal_sequence")
            ):
                raise RetentionError("prior operation record seal or phase changed")
            sequence = int(prior_pointer["journal_sequence"]) + 1

        record = _seal_receipt({
            **receipt,
            "journal_sequence": sequence,
            "prior_record_sha256": prior_record_sha256,
        })
        record_data = baseline.canonical_bytes(record) + b"\n"
        if len(record_data) > MAX_RECEIPT_BYTES:
            raise RetentionError("operation receipt exceeds its size limit")
        record_sha256 = file_sha256_bytes(record_data)
        record_name = (
            f"{absolute.name}.{receipt['operation_id']}."
            f"{sequence:06d}.{record_sha256}.json"
        )
        _write_new_private_at(parent_fd, record_name, record_data, allow_exact=True)
        pointer_core = {
            "contract": RECEIPT_POINTER_CONTRACT,
            "schema_version": 1,
            "operation_id": receipt["operation_id"],
            "journal_sequence": sequence,
            "phase": receipt["phase"],
            "record_file": record_name,
            "record_sha256": record_sha256,
            "prior_record_sha256": prior_record_sha256,
        }
        pointer = {
            **pointer_core,
            "pointer_payload_sha256": baseline.canonical_sha256(pointer_core),
        }
        pointer_data = baseline.canonical_bytes(pointer) + b"\n"
        if create:
            _write_new_private_at(parent_fd, absolute.name, pointer_data)
        else:
            assert prior_pointer_raw is not None and prior_pointer_meta is not None
            current_raw, current_meta = _read_regular_at(
                parent_fd, absolute.name, "operation receipt CAS pointer", MAX_RECEIPT_BYTES
            )
            if (
                current_raw != prior_pointer_raw
                or current_meta.st_dev != prior_pointer_meta.st_dev
                or current_meta.st_ino != prior_pointer_meta.st_ino
            ):
                raise RetentionError("operation receipt pointer failed inode-bound CAS")
            _replace_private_at(parent_fd, absolute.name, pointer_data)
        os.fsync(parent_fd)
        persisted, _ = _read_regular_at(
            parent_fd, absolute.name, "operation receipt pointer", MAX_RECEIPT_BYTES
        )
        if persisted != pointer_data:
            raise RetentionError("operation receipt pointer did not persist exact bytes")
        return absolute, file_sha256_bytes(pointer_data)
    finally:
        if lock_fd >= 0:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        os.close(parent_fd)


def _rename_noreplace_at(directory_fd: int, source: str, destination: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RetentionError("renameat2 RENAME_NOREPLACE is unavailable (ENOSYS)")
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = renameat2(
        directory_fd,
        os.fsencode(source),
        directory_fd,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error == errno.ENOSYS:
        raise RetentionError("renameat2 RENAME_NOREPLACE is unavailable (ENOSYS)")
    if error == errno.EEXIST:
        raise RetentionError("retention quarantine destination exists (EEXIST)")
    raise RetentionError(
        f"renameat2 RENAME_NOREPLACE failed: {os.strerror(error)}"
    )


def _quarantine_candidate_locked(
    item: Artifact,
    directory_fd: int,
    *,
    operation_id: str,
    sequence: int,
) -> dict[str, Any]:
    _revalidate_candidate_locked(item, directory_fd)
    quarantine = f".retention-{operation_id}-{sequence:04d}.deleting"
    _rename_noreplace_at(directory_fd, item.path.name, quarantine)
    os.fsync(directory_fd)
    raw, moved = _read_regular_at(
        directory_fd, quarantine, "quarantined retention candidate", MAX_ARTIFACT_BYTES
    )
    if (
        moved.st_dev != item.device
        or moved.st_ino != item.inode
        or moved.st_size != item.size
        or file_sha256_bytes(raw) != item.sha256
    ):
        raise RetentionError("retention candidate identity changed during quarantine")
    return {
        **_artifact_evidence(item),
        "sequence": sequence,
        "quarantine_name": quarantine,
        "state": "quarantined",
    }


def _unlink_quarantined_locked(
    evidence: dict[str, Any], directory_fd: int,
) -> dict[str, Any]:
    quarantine = str(evidence["quarantine_name"])
    raw, moved = _read_regular_at(
        directory_fd, quarantine, "quarantined retention candidate", MAX_ARTIFACT_BYTES
    )
    if (
        moved.st_dev != evidence["device"]
        or moved.st_ino != evidence["inode"]
        or moved.st_size != evidence["size"]
        or file_sha256_bytes(raw) != evidence["sha256"]
    ):
        raise RetentionError("quarantined candidate changed before unlink")
    os.unlink(quarantine, dir_fd=directory_fd)
    os.fsync(directory_fd)
    try:
        os.stat(quarantine, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise RetentionError("quarantined retention candidate still exists")
    try:
        os.stat(str(evidence["name"]), dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise RetentionError("retention candidate name reappeared during deletion")
    return {**evidence, "state": "deleted"}


def _quarantine_state_locked(directory_fd: int, operation_id: str) -> list[dict[str, Any]]:
    prefix = f".retention-{operation_id}-"
    observed: list[dict[str, Any]] = []
    for name in sorted(os.listdir(directory_fd)):
        if not name.startswith(prefix):
            continue
        raw, metadata = _read_regular_at(
            directory_fd, name, "retention quarantine", MAX_ARTIFACT_BYTES
        )
        observed.append({
            "quarantine_name": name,
            "sha256": file_sha256_bytes(raw),
            "size": metadata.st_size,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
        })
    return observed


def _load_current_operation_record(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    pointer_raw, _ = _read_regular(
        path, "operation receipt pointer", MAX_RECEIPT_BYTES
    )
    pointer = baseline.loads_strict(pointer_raw, "operation receipt pointer")
    if not isinstance(pointer, dict):
        raise RetentionError("operation receipt pointer is not an object")
    pointer_core = {
        key: value for key, value in pointer.items()
        if key != "pointer_payload_sha256"
    }
    if (
        pointer.get("contract") != RECEIPT_POINTER_CONTRACT
        or pointer.get("pointer_payload_sha256")
        != baseline.canonical_sha256(pointer_core)
    ):
        raise RetentionError("operation receipt pointer seal is invalid")
    record_path = path.parent / str(pointer.get("record_file"))
    record_raw, _ = _read_regular(
        record_path, "immutable operation record", MAX_RECEIPT_BYTES
    )
    if file_sha256_bytes(record_raw) != pointer.get("record_sha256"):
        raise RetentionError("immutable operation record hash is invalid")
    record = baseline.loads_strict(record_raw, "immutable operation record")
    if not isinstance(record, dict):
        raise RetentionError("immutable operation record is not an object")
    record_core = {
        key: value for key, value in record.items()
        if key != "receipt_payload_sha256"
    }
    if (
        record.get("receipt_payload_sha256") != baseline.canonical_sha256(record_core)
        or record.get("operation_id") != pointer.get("operation_id")
        or record.get("journal_sequence") != pointer.get("journal_sequence")
        or record.get("phase") != pointer.get("phase")
    ):
        raise RetentionError("immutable operation record seal or phase is invalid")
    return pointer, record


_ARTIFACT_IDENTITY_KEYS = (
    "name",
    "sha256",
    "size",
    "device",
    "inode",
    "data_generated_at_utc",
)
_QUARANTINE_IDENTITY_KEYS = (
    "quarantine_name",
    "sha256",
    "size",
    "device",
    "inode",
)


def _same_evidence(
    left: dict[str, Any], right: dict[str, Any], keys: tuple[str, ...]
) -> bool:
    return all(left.get(key) == right.get(key) for key in keys)


def _exact_original_state_locked(
    directory_fd: int, evidence: dict[str, Any]
) -> str:
    name = evidence.get("name")
    if not isinstance(name, str):
        raise RetentionError("receipt-bound original lacks a safe name")
    try:
        raw, metadata = _read_regular_at(
            directory_fd,
            name,
            "receipt-bound original artifact",
            MAX_ARTIFACT_BYTES,
        )
    except FileNotFoundError:
        return "absent"
    if (
        file_sha256_bytes(raw) != evidence.get("sha256")
        or metadata.st_size != evidence.get("size")
        or metadata.st_dev != evidence.get("device")
        or metadata.st_ino != evidence.get("inode")
    ):
        raise RetentionError("receipt-bound original artifact changed")
    return "exact"


def _planned_match(
    planned: list[dict[str, Any]], evidence: dict[str, Any]
) -> dict[str, Any]:
    matches = [
        item
        for item in planned
        if all(
            item.get(key) == evidence.get(key)
            for key in ("sha256", "size", "device", "inode")
        )
    ]
    if len(matches) != 1:
        raise RetentionError("quarantine cannot be mapped uniquely to its original")
    return matches[0]


def reconcile_failed_quarantine(
    *,
    artifact_dir: Path,
    receipt_path: Path,
    apply: bool = False,
    allow_test_apply: bool = False,
) -> dict[str, Any]:
    """Restore only receipt-bound intent/quarantine remnants from one operation."""
    if apply and not allow_test_apply:
        raise RetentionError("quarantine reconcile apply is test-only")
    receipt_absolute = Path(os.path.abspath(os.fspath(receipt_path)))
    directory = _require_directory(artifact_dir, "artifact directory")
    restored: list[str] = []
    already_restored: list[str] = []
    verified_existing: list[str] = []
    recovered_deleted: list[dict[str, Any]] = []
    with baseline.exclusive_store_lock(directory) as held:
        _, record = _load_current_operation_record(receipt_absolute)
        phase = record.get("phase")
        if phase not in {"intent", "quarantined", "failed"}:
            raise RetentionError(
                "quarantine reconcile requires intent, quarantined, or failed evidence"
            )
        operation_id = str(record["operation_id"])
        planned_value = record.get("planned_removals")
        if not isinstance(planned_value, list) or not all(
            isinstance(item, dict) for item in planned_value
        ):
            raise RetentionError("operation lacks bounded reconcile evidence")
        planned: list[dict[str, Any]] = planned_value
        if str(directory) != record.get("artifact_dir"):
            raise RetentionError("reconcile artifact directory differs from its receipt")

        live = _quarantine_state_locked(held.artifact_dir_fd, operation_id)
        restore_actions: list[tuple[dict[str, Any], dict[str, Any]]] = []
        current = record.get("current_item")

        if phase == "failed":
            recorded_value = record.get("quarantine_state")
            if not isinstance(recorded_value, list) or not all(
                isinstance(item, dict) for item in recorded_value
            ):
                raise RetentionError("failed receipt lacks exact quarantine evidence")
            recorded: list[dict[str, Any]] = recorded_value
            live_by_name = {
                str(item.get("quarantine_name")): item for item in live
            }
            recorded_by_name = {
                str(item.get("quarantine_name")): item for item in recorded
            }
            if len(live_by_name) != len(live) or len(recorded_by_name) != len(recorded):
                raise RetentionError("failed receipt has duplicate quarantine names")
            if not set(live_by_name).issubset(recorded_by_name):
                raise RetentionError("failed receipt does not bind live quarantine state")
            for name, observed in live_by_name.items():
                if not _same_evidence(
                    observed, recorded_by_name[name], _QUARANTINE_IDENTITY_KEYS
                ):
                    raise RetentionError("live quarantine differs from failed receipt")
            for name, expected in recorded_by_name.items():
                original_evidence = _planned_match(planned, expected)
                original_state = _exact_original_state_locked(
                    held.artifact_dir_fd, original_evidence
                )
                observed = live_by_name.get(name)
                if observed is not None:
                    if original_state != "absent":
                        raise RetentionError(
                            "failed receipt has both original and quarantine"
                        )
                    restore_actions.append((observed, original_evidence))
                elif original_state == "exact":
                    already_restored.append(str(original_evidence["name"]))
                else:
                    raise RetentionError(
                        "failed receipt lost both original and quarantine"
                    )
            if not recorded:
                if not isinstance(current, dict):
                    raise RetentionError("failed receipt lacks an active bound item")
                if not any(
                    _same_evidence(current, item, _ARTIFACT_IDENTITY_KEYS)
                    for item in planned
                ):
                    raise RetentionError("failed receipt current item is not planned")
                original_state = _exact_original_state_locked(
                    held.artifact_dir_fd, current
                )
                current_quarantine = record.get("current_quarantine")
                if isinstance(current_quarantine, dict):
                    if not _same_evidence(
                        current, current_quarantine, _ARTIFACT_IDENTITY_KEYS
                    ):
                        raise RetentionError(
                            "failed receipt quarantine identity is inconsistent"
                        )
                    if original_state == "absent":
                        recovered_deleted.append(
                            {**current_quarantine, "state": "deleted"}
                        )
                    else:
                        already_restored.append(str(current["name"]))
                elif current_quarantine is None and original_state == "exact":
                    verified_existing.append(str(current["name"]))
                else:
                    raise RetentionError(
                        "failed receipt filesystem state is ambiguous"
                    )
        elif phase == "quarantined":
            expected = record.get("current_quarantine")
            if not isinstance(expected, dict) or not isinstance(current, dict):
                raise RetentionError("quarantined receipt lacks exact item evidence")
            if not any(
                _same_evidence(current, item, _ARTIFACT_IDENTITY_KEYS)
                for item in planned
            ) or not _same_evidence(
                current, expected, _ARTIFACT_IDENTITY_KEYS
            ):
                raise RetentionError("quarantined receipt item identity is inconsistent")
            if len(live) > 1:
                raise RetentionError("quarantined receipt has extra live entries")
            if live and not _same_evidence(
                live[0], expected, _QUARANTINE_IDENTITY_KEYS
            ):
                raise RetentionError("live quarantine differs from quarantined receipt")
            original_state = _exact_original_state_locked(
                held.artifact_dir_fd, current
            )
            if live:
                if original_state != "absent":
                    raise RetentionError(
                        "quarantined receipt has both original and quarantine"
                    )
                restore_actions.append((live[0], current))
            elif original_state == "absent":
                recovered_deleted.append({**expected, "state": "deleted"})
            else:
                already_restored.append(str(current["name"]))
        else:
            expected_name = record.get("expected_quarantine_name")
            if not isinstance(current, dict) or not isinstance(expected_name, str):
                raise RetentionError("intent receipt lacks exact candidate evidence")
            if not any(
                _same_evidence(current, item, _ARTIFACT_IDENTITY_KEYS)
                for item in planned
            ):
                raise RetentionError("intent receipt current item is not planned")
            if len(live) > 1 or (live and live[0]["quarantine_name"] != expected_name):
                raise RetentionError("live quarantine is not the intent-bound destination")
            if live:
                if not _same_evidence(
                    live[0], current, ("sha256", "size", "device", "inode")
                ):
                    raise RetentionError("intent quarantine identity differs from receipt")
                if _exact_original_state_locked(
                    held.artifact_dir_fd, current
                ) != "absent":
                    raise RetentionError("intent receipt has both original and quarantine")
                restore_actions.append((live[0], current))
            else:
                if _exact_original_state_locked(
                    held.artifact_dir_fd, current
                ) != "exact":
                    raise RetentionError("intent receipt lost its bound artifact")
                verified_existing.append(str(current["name"]))

        for observed, original_evidence in restore_actions:
            original = str(original_evidence["name"])
            if apply:
                _rename_noreplace_at(
                    held.artifact_dir_fd,
                    str(observed["quarantine_name"]),
                    original,
                )
                os.fsync(held.artifact_dir_fd)
                raw, metadata = _read_regular_at(
                    held.artifact_dir_fd,
                    original,
                    "reconciled baseline artifact",
                    MAX_ARTIFACT_BYTES,
                )
                if (
                    file_sha256_bytes(raw) != original_evidence["sha256"]
                    or metadata.st_size != original_evidence["size"]
                    or metadata.st_dev != original_evidence["device"]
                    or metadata.st_ino != original_evidence["inode"]
                ):
                    raise RetentionError("reconciled artifact identity is not exact")
                restored.append(original)
        if apply:
            completed_value = record.get("completed_removals")
            if not isinstance(completed_value, list) or not all(
                isinstance(item, dict) for item in completed_value
            ):
                raise RetentionError("operation lacks completed-removal evidence")
            completed = list(completed_value)
            completed_identities = {
                (item.get("name"), item.get("sha256"), item.get("inode"))
                for item in completed
            }
            for item in recovered_deleted:
                identity = (item.get("name"), item.get("sha256"), item.get("inode"))
                if identity not in completed_identities:
                    completed.append(item)
                    completed_identities.add(identity)
            updated = {
                **record,
                "phase": "reconciled",
                "updated_at_utc": _timestamp_now(),
                "completed_removals": completed,
                "current_item": None,
                "current_quarantine": None,
                "quarantine_state": [],
                "reconciled_artifacts": sorted(set(restored + already_restored)),
                "verified_existing_artifacts": verified_existing,
                "recovered_deleted_artifacts": recovered_deleted,
                "error": None,
            }
            _atomic_write_operation_receipt(
                receipt_absolute, updated, create=False
            )
    return {
        "result": "RADAR_RANK_BASELINE_RETENTION_RECONCILE_V1_PASS",
        "applied": apply,
        "operation_id": operation_id,
        "source_phase": phase,
        "restored": restored,
        "already_restored": already_restored,
        "recovered_deleted": [str(item["name"]) for item in recovered_deleted],
        "verified_existing": verified_existing,
        "production_ready": False,
    }


def _validate_limits(max_artifacts: int, max_bytes: int) -> None:
    if not 1 <= max_artifacts <= HARD_MAX_ARTIFACTS:
        raise RetentionError("artifact-count limit is outside the hard bound")
    if not 1 <= max_bytes <= HARD_MAX_BYTES:
        raise RetentionError("artifact-byte limit is outside the hard bound")


def retain(
    *,
    artifact_dir: Path,
    pointer_path: Path,
    trusted_pointer_sha256: str,
    minimum_data_generated_at_utc: str,
    max_artifacts: int = HARD_MAX_ARTIFACTS,
    max_bytes: int = HARD_MAX_BYTES,
    apply: bool = False,
    allow_unlocked_test_apply: bool = False,
    receipt_path: Path | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Plan or test-apply retention under the exporter's canonical lock."""
    _validate_limits(max_artifacts, max_bytes)
    if apply and not allow_unlocked_test_apply:
        raise RetentionError(
            "internal apply is disabled without explicit temp-fixture authorization"
        )
    if apply and receipt_path is None:
        raise RetentionError("internal apply requires a durable operation receipt path")
    operation_id = operation_id or uuid.uuid4().hex
    if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise RetentionError("operation id is not a lowercase 128-bit hex value")
    directory = _require_directory(artifact_dir, "artifact directory")

    with baseline.exclusive_store_lock(directory) as held:
        held.assert_current()
        pointer, pointer_raw, pointed = _load_pointer(
            pointer_path,
            directory,
            trusted_pointer_sha256,
            minimum_data_generated_at_utc,
        )
        artifacts, unknown_entries = _scan_artifacts_locked(
            directory, held.artifact_dir_fd
        )
        if apply and unknown_entries:
            raise RetentionError("apply refuses an artifact directory with unknown entries")
        by_path = {item.path: item for item in artifacts}
        if pointed not in by_path:
            raise RetentionError("pointed artifact is absent from the retention inventory")
        ordered = sorted(
            (item for item in artifacts if item.path != pointed),
            key=lambda item: (item.data_generated_at, item.path.name),
        )
        remaining_count = len(artifacts)
        remaining_bytes = sum(item.size for item in artifacts)
        removals: list[Artifact] = []
        for item in ordered:
            if remaining_count <= max_artifacts and remaining_bytes <= max_bytes:
                break
            removals.append(item)
            remaining_count -= 1
            remaining_bytes -= item.size
        if remaining_count > max_artifacts or remaining_bytes > max_bytes:
            raise RetentionError(
                "hard retention bounds cannot be met while preserving the pointer"
            )

        lock_evidence = {
            "path": str(held.lock_path),
            "device": held.lock_device,
            "inode": held.lock_inode,
            "exclusive": True,
        }
        summary: dict[str, Any] = {
            "result": "RADAR_RANK_BASELINE_RETENTION_V1_PASS",
            "applied": apply,
            "hard_max_artifacts": HARD_MAX_ARTIFACTS,
            "hard_max_bytes": HARD_MAX_BYTES,
            "configured_max_artifacts": max_artifacts,
            "configured_max_bytes": max_bytes,
            "before_artifacts": len(artifacts),
            "before_bytes": sum(item.size for item in artifacts),
            "after_artifacts": remaining_count,
            "after_bytes": remaining_bytes,
            "pointed_artifact": pointed.name,
            "pointed_artifact_sha256": pointer["artifact_sha256"],
            "unknown_entries": unknown_entries,
            "removed": [item.path.name for item in removals],
            "production_ready": False,
            "shared_exporter_lock_verified": True,
            "inode_bound_deletion_verified": apply,
            "durable_operation_receipt_verified": False,
            "exact_post_rescan_verified": False,
            "unlocked_apply_test_only": False,
            "test_only_apply_authorized": apply,
            "store_lock": lock_evidence,
            "residual_production_gates": [
                "protected_lkg_watermark",
                "durable_incident_lifecycle_and_dedupe",
                "private_runtime_owner_and_scheduler",
                "terminal_two_pass_benchmark",
            ],
        }
        if not apply:
            return summary

        assert receipt_path is not None
        receipt_absolute = Path(os.path.abspath(os.fspath(receipt_path)))
        receipt_parent = receipt_absolute.parent
        if receipt_parent == directory or directory in receipt_parent.parents:
            raise RetentionError("operation receipt must be outside the artifact directory")
        started = _timestamp_now()
        completed: list[dict[str, Any]] = []
        journal: dict[str, Any] = {
            "contract": RECEIPT_CONTRACT,
            "schema_version": 1,
            "operation_id": operation_id,
            "phase": "prepared",
            "started_at_utc": started,
            "updated_at_utc": started,
            "artifact_dir": str(directory),
            "store_lock": lock_evidence,
            "trusted_pointer_sha256": trusted_pointer_sha256,
            "minimum_data_generated_at_utc": minimum_data_generated_at_utc,
            "pointed_artifact": pointed.name,
            "pointed_artifact_sha256": pointer["artifact_sha256"],
            "configured_max_artifacts": max_artifacts,
            "configured_max_bytes": max_bytes,
            "before_inventory": _inventory_evidence(artifacts),
            "planned_removals": _inventory_evidence(removals),
            "completed_removals": completed,
            "post_inventory": None,
            "error": None,
            "production_ready": False,
        }
        persisted_path, persisted_sha256 = _atomic_write_operation_receipt(
            receipt_absolute, journal, create=True
        )
        expected_remaining = {
            item.path.name: item for item in artifacts if item not in removals
        }
        try:
            current_pointer, current_raw, current_pointed = _load_pointer(
                pointer_path,
                directory,
                trusted_pointer_sha256,
                minimum_data_generated_at_utc,
            )
            if (
                current_raw != pointer_raw
                or current_pointer != pointer
                or current_pointed != pointed
            ):
                raise RetentionError("latest-accepted pointer changed before retention")
            for item in removals:
                _revalidate_candidate_locked(item, held.artifact_dir_fd)
            for sequence, item in enumerate(removals, start=1):
                held.assert_current()
                current_pointer, current_raw, current_pointed = _load_pointer(
                    pointer_path,
                    directory,
                    trusted_pointer_sha256,
                    minimum_data_generated_at_utc,
                )
                if (
                    current_raw != pointer_raw
                    or current_pointer != pointer
                    or current_pointed != pointed
                ):
                    raise RetentionError("latest-accepted pointer changed during retention")
                journal = {
                    **journal,
                    "phase": "intent",
                    "updated_at_utc": _timestamp_now(),
                    "completed_removals": list(completed),
                    "current_item": _artifact_evidence(item),
                    "current_quarantine": None,
                    "retention_sequence": sequence,
                    "expected_quarantine_name": (
                        f".retention-{operation_id}-{sequence:04d}.deleting"
                    ),
                }
                persisted_path, persisted_sha256 = _atomic_write_operation_receipt(
                    persisted_path, journal, create=False
                )
                quarantined = _quarantine_candidate_locked(
                    item,
                    held.artifact_dir_fd,
                    operation_id=operation_id,
                    sequence=sequence,
                )
                journal = {
                    **journal,
                    "phase": "quarantined",
                    "updated_at_utc": _timestamp_now(),
                    "current_quarantine": quarantined,
                }
                persisted_path, persisted_sha256 = _atomic_write_operation_receipt(
                    persisted_path, journal, create=False
                )
                completed.append(
                    _unlink_quarantined_locked(quarantined, held.artifact_dir_fd)
                )
                journal = {
                    **journal,
                    "phase": "deleted",
                    "updated_at_utc": _timestamp_now(),
                    "completed_removals": list(completed),
                    "current_item": None,
                    "current_quarantine": None,
                }
                persisted_path, persisted_sha256 = _atomic_write_operation_receipt(
                    persisted_path, journal, create=False
                )

            held.assert_current()
            post_artifacts, post_unknown = _scan_artifacts_locked(
                directory, held.artifact_dir_fd
            )
            if post_unknown:
                raise RetentionError("post-retention scan found unknown entries")
            if _inventory_evidence(post_artifacts) != _inventory_evidence(
                list(expected_remaining.values())
            ):
                raise RetentionError("post-retention inventory differs from the exact plan")
            final_pointer, final_raw, final_pointed = _load_pointer(
                pointer_path,
                directory,
                trusted_pointer_sha256,
                minimum_data_generated_at_utc,
            )
            if (
                final_raw != pointer_raw
                or final_pointer != pointer
                or final_pointed != pointed
            ):
                raise RetentionError("latest-accepted pointer changed before final receipt")
            journal = {
                **journal,
                "phase": "complete",
                "updated_at_utc": _timestamp_now(),
                "completed_removals": list(completed),
                "post_inventory": _inventory_evidence(post_artifacts),
            }
            persisted_path, persisted_sha256 = _atomic_write_operation_receipt(
                persisted_path, journal, create=False
            )
        except (RetentionError, baseline.BaselineError, OSError) as exc:
            failed = {
                **journal,
                "phase": "failed",
                "updated_at_utc": _timestamp_now(),
                "completed_removals": list(completed),
                "quarantine_state": _quarantine_state_locked(
                    held.artifact_dir_fd, operation_id
                ),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            try:
                _atomic_write_operation_receipt(persisted_path, failed, create=False)
            except (RetentionError, baseline.BaselineError, OSError) as receipt_exc:
                raise RetentionError(
                    "retention failed and its durable receipt could not be finalized"
                ) from receipt_exc
            if isinstance(exc, RetentionError):
                raise
            raise RetentionError(str(exc)) from exc

        return {
            **summary,
            "receipt_path": str(persisted_path),
            "receipt_sha256": persisted_sha256,
            "durable_operation_receipt_verified": True,
            "exact_post_rescan_verified": True,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--latest-accepted-manifest", type=Path, required=True)
    parser.add_argument("--trusted-pointer-sha256", required=True)
    parser.add_argument("--minimum-data-generated-at-utc", required=True)
    parser.add_argument("--max-artifacts", type=int, default=HARD_MAX_ARTIFACTS)
    parser.add_argument("--max-bytes", type=int, default=HARD_MAX_BYTES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        receipt = retain(
            artifact_dir=args.artifact_dir,
            pointer_path=args.latest_accepted_manifest,
            trusted_pointer_sha256=args.trusted_pointer_sha256,
            minimum_data_generated_at_utc=args.minimum_data_generated_at_utc,
            max_artifacts=args.max_artifacts,
            max_bytes=args.max_bytes,
            apply=False,
        )
        print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
        return 0
    except (RetentionError, baseline.BaselineError, OSError) as exc:
        print(f"RADAR_RANK_BASELINE_RETENTION_V1_FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
