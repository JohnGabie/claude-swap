"""Incremental cache of weighted tokens per 5-minute bucket (FP11, FP7).

Built from ``<claude config>/projects/**/*.jsonl``. For each bucket it keeps
the weighted tokens per model and per project key. Only the timestamp, the
message and request ids, the model name, the integer token counts and the
project key (never the path) are read or stored. No message text, no path and
no email reach the cache file.

File: ``<backup_root>/cache/usage_history.json``, mode 0600, atomic write.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import settings, token_model, usage_log

HISTORY_FILENAME = "usage_history.json"
BUCKET_S = 300
KEEP_S = 31 * 86400
KEYS_KEPT = 500
SCHEMA_VERSION = 1
OTHER_KEY = "__other__"

KEY_PERSONAL = "pasta pessoal"
KEY_MEMORY = "claude (memória)"
KEY_TEMP = "temporários"
KEY_OTHER = "outras pastas"
WORKTREES_SUFFIX = ".worktrees"
SYSTEM_TEMP_DIRS = ("/tmp", "/var/tmp")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty() -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "updatedAt": _iso(0), "files": {}, "buckets": {}}


def _parts(path: str, windows: bool) -> list[str]:
    text = path.replace("\\", "/") if windows else path
    parts = [p for p in text.split("/") if p]
    return [p.casefold() for p in parts] if windows else parts


def _under(parts: list[str], base: list[str]) -> bool:
    return bool(base) and parts[: len(base)] == base


def project_key(cwd, home: str, temp_dirs: tuple[str, ...] = ()) -> str:
    """Project key of a ``cwd``: the FP7 rule. Only the key is ever stored."""
    if not isinstance(cwd, str) or not cwd:
        return KEY_OTHER
    windows = "\\" in cwd or "\\" in (home or "")
    parts = _parts(cwd, windows)
    raw = [p for p in (cwd.replace("\\", "/") if windows else cwd).split("/") if p]
    home_parts = _parts(home, windows) if home else []
    if _under(parts, home_parts + ["projects"]):
        depth = len(home_parts) + 1
        if len(parts) == depth:
            return KEY_PERSONAL
        name = raw[depth]
        if name.endswith(WORKTREES_SUFFIX) and len(name) > len(WORKTREES_SUFFIX):
            name = name[: -len(WORKTREES_SUFFIX)]
        return name
    if _under(parts, home_parts + [".claude"]):
        return KEY_MEMORY
    for temp in (*SYSTEM_TEMP_DIRS, *temp_dirs):
        if temp and _under(parts, _parts(temp, windows)):
            return KEY_TEMP
    if _under(parts, home_parts):
        return KEY_PERSONAL
    return KEY_OTHER


def _temp_dirs(env=None) -> tuple[str, ...]:
    env = os.environ if env is None else env
    found = [env.get(name) for name in ("TMPDIR", "TEMP", "TMP")]
    found.append(tempfile.gettempdir())
    return tuple(dict.fromkeys(d for d in found if d))


def _cache_path(backup_root: Path) -> Path:
    return Path(backup_root) / "cache" / HISTORY_FILENAME


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def load(backup_root: Path) -> dict:
    """The stored history; an empty one for a missing, bad or other-version file."""
    try:
        data = json.loads(_cache_path(backup_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict) or data.get("schemaVersion") != SCHEMA_VERSION:
        return _empty()
    files, buckets = data.get("files"), data.get("buckets")
    if not isinstance(files, dict) or not isinstance(buckets, dict):
        return _empty()
    clean_files = {}
    for key, entry in files.items():
        if isinstance(entry, dict) and _is_int(entry.get("offset")) and isinstance(entry.get("keys"), list):
            clean_files[key] = {"offset": entry["offset"], "keys": [k for k in entry["keys"] if isinstance(k, str)]}
    clean_buckets = {}
    for key, entry in buckets.items():
        if isinstance(key, str) and key.isdigit() and isinstance(entry, dict):
            clean_buckets[key] = {
                "m": entry["m"] if isinstance(entry.get("m"), dict) else {},
                "p": entry["p"] if isinstance(entry.get("p"), dict) else {},
            }
    updated = data.get("updatedAt")
    return {
        "schemaVersion": SCHEMA_VERSION,
        "updatedAt": updated if isinstance(updated, str) else _iso(0),
        "files": clean_files,
        "buckets": clean_buckets,
    }


def _message_key(message: dict, row: dict, ts_text) -> str:
    mid, rid = message.get("id"), row.get("requestId")
    if mid is None and rid is None:
        return f"|{ts_text}"
    return f"{mid}|{rid}"


def _read_new(path: Path, entry: dict, size: int, buckets: dict, home: str, temps: tuple, cutoff: float) -> None:
    """Add the whole new lines of ``path`` to ``buckets``; move ``entry["offset"]``."""
    if size < entry["offset"]:
        entry["offset"], entry["keys"] = 0, []
    try:
        with path.open("rb") as handle:
            handle.seek(entry["offset"])
            data = handle.read()
    except OSError:
        return
    last = data.rfind(b"\n")
    if last < 0:
        return
    seen = set(entry["keys"])
    for raw in data[: last + 1].decode("utf-8", errors="replace").splitlines():
        if '"usage"' not in raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) else None
        usage = message.get("usage") if isinstance(message, dict) else None
        ts = usage_log.parse_ts(row.get("timestamp")) if isinstance(row, dict) else None
        if not isinstance(usage, dict) or ts is None:
            continue
        key = _message_key(message, row, row.get("timestamp"))
        if key in seen:
            continue
        seen.add(key)
        entry["keys"].append(key)
        model = message.get("model") if isinstance(message.get("model"), str) else None
        weighted = token_model.weighted_tokens(model, usage)
        if weighted <= 0 or ts < cutoff:
            continue
        bucket = buckets.setdefault(str(int(ts // BUCKET_S * BUCKET_S)), {"m": {}, "p": {}})
        name = model or "unknown"
        bucket["m"][name] = bucket["m"].get(name, 0.0) + weighted
        project = project_key(row.get("cwd"), home, temps)
        bucket["p"][project] = bucket["p"].get(project, 0.0) + weighted
    entry["keys"] = entry["keys"][-KEYS_KEPT:]
    entry["offset"] += last + 1


def update(backup_root: Path, projects_dir: Path, now: float, home: str | None = None) -> dict:
    """Read the new log lines into the history, drop the old, write the file, return it."""
    history = load(backup_root)
    home = str(Path.home()) if home is None else home
    temps = _temp_dirs()
    cutoff = now - KEEP_S
    files, buckets = history["files"], history["buckets"]
    kept: dict = {}
    try:
        paths = sorted(Path(projects_dir).rglob("*.jsonl"))
    except OSError:
        paths = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime < cutoff:
            continue
        name = hashlib.sha256(str(path).encode("utf-8")).hexdigest()
        entry = files.get(name) or {"offset": 0, "keys": []}
        kept[name] = entry
        if stat.st_size != entry["offset"]:
            _read_new(path, entry, stat.st_size, buckets, home, temps, cutoff)
    history["files"] = kept
    history["buckets"] = {k: v for k, v in buckets.items() if int(k) >= cutoff}
    history["updatedAt"] = _iso(now)
    settings.atomic_write_json(_cache_path(backup_root), history)
    return history


def _bucket_items(history: dict, start: float, end: float):
    for key, value in history.get("buckets", {}).items():
        b = int(key)
        if start <= b < end:
            yield b, value


def series(history: dict, start: float, end: float, step: int) -> list[float]:
    """Weighted tokens per minute for each step of ``[start, end)``."""
    out = [0.0] * max(0, math.ceil((end - start) / step))
    for b, value in _bucket_items(history, start, end):
        out[int((b - start) // step)] += sum(value["m"].values()) * 60 / step
    return out


def totals(history: dict, start: float, end: float, field: str) -> dict[str, float]:
    """Weighted tokens in ``[start, end)`` by model (``"m"``) or project key (``"p"``)."""
    out: dict[str, float] = {}
    for _, value in _bucket_items(history, start, end):
        for name, weighted in value[field].items():
            out[name] = out.get(name, 0.0) + weighted
    return out


def grid(history: dict, start: float, end: float, step: int, keys: list[str]) -> dict[str, list[float]]:
    """Weighted tokens per step for each project key; other keys sum under ``__other__``."""
    count = max(0, math.ceil((end - start) / step))
    out = {key: [0.0] * count for key in keys}
    out[OTHER_KEY] = [0.0] * count
    for b, value in _bucket_items(history, start, end):
        index = int((b - start) // step)
        for name, weighted in value["p"].items():
            out[name if name in keys else OTHER_KEY][index] += weighted
    return out
