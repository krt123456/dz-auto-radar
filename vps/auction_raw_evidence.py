#!/usr/bin/env python3
"""Immutable raw-response evidence capture shared by auction connectors."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any


UTC = dt.timezone.utc
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


class RawEvidenceCapture:
    """Capture public HTTP responses without recording credentials or cookies."""

    def __init__(self, root: Path, source: str, captured_at: dt.datetime) -> None:
        if not SAFE_NAME.fullmatch(source):
            raise ValueError("invalid evidence source key")
        if captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        captured_at = captured_at.astimezone(UTC)
        self.source = source
        self.captured_at = captured_at
        self.capture_id = captured_at.strftime("%Y%m%dT%H%M%S.%fZ")
        self.root = root / source / self.capture_id
        self.responses = self.root / "responses"
        self.responses.mkdir(parents=True, exist_ok=False)
        self._records: list[dict[str, Any]] = []
        self._names: set[str] = set()
        self._lock = threading.Lock()

    @property
    def reference(self) -> str:
        return f"{self.source}/{self.capture_id}/manifest.json"

    def get(self, session: Any, name: str, url: str, **kwargs: Any) -> Any:
        response = session.get(url, **kwargs)
        self.record(name, "GET", response)
        return response

    def post(self, session: Any, name: str, url: str, **kwargs: Any) -> Any:
        response = session.post(url, **kwargs)
        self.record(name, "POST", response)
        return response

    def record(self, name: str, method: str, response: Any) -> None:
        if not SAFE_NAME.fullmatch(name):
            raise ValueError("invalid evidence response name")
        body = bytes(response.content)
        content_type = str(response.headers.get("content-type") or "").lower()
        extension = ".json" if "json" in content_type else ".html" if "html" in content_type else ".bin"
        file_name = name + extension
        with self._lock:
            if name in self._names:
                raise ValueError(f"duplicate evidence response name: {name}")
            self._names.add(name)
            (self.responses / file_name).write_bytes(body)
            self._records.append({
                "name": name,
                "method": method,
                "url": str(response.url),
                "status": int(response.status_code),
                "content_type": content_type,
                "bytes": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "file": f"responses/{file_name}",
            })

    def finish(self, source_report: dict[str, Any]) -> Path:
        manifest = {
            "schema_version": 1,
            "source": self.source,
            "capture_id": self.capture_id,
            "captured_at_utc": self.captured_at.isoformat(),
            "responses": sorted(self._records, key=lambda record: record["name"]),
            "source_report": source_report,
        }
        path = self.root / "manifest.json"
        atomic_write_json(path, manifest)
        return path
