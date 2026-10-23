"""Centralized logging helpers for sndbx."""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo


LEVEL_VALUES = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

LEVEL_NAMES = {
    10: "DEBUG",
    20: "INFO",
    30: "WARNING",
    40: "ERROR",
    50: "ERROR",
}

LOGGER_TAG_MAP = {
    "root": "core",
    "sndbx": "core",
    "__main__": "core",
    "app": "core",
    "sandbox": "sandbox_manager",
    "mcp_server": "mcp",
    "tools": "mcp",
    "webui_server": "webui",
}


@dataclass
class LoggingState:
    """Central logging runtime state."""

    logs_dir: Path
    timezone_name: str
    tzinfo: Any
    default_level: int
    tag_levels: Dict[str, int]
    lock: threading.Lock


_state: Optional[LoggingState] = None


def _normalize_tag(tag: str) -> str:
    """Normalize a tag for display and filenames."""
    text = str(tag or "core").strip() or "core"
    return text.replace(os.sep, "_").replace(" ", "_")


def _coerce_level_value(level: Any) -> int:
    """Convert configured level to logging int value."""
    if isinstance(level, int):
        if level <= 10:
            return 10
        if level <= 20:
            return 20
        if level <= 30:
            return 30
        if level <= 40:
            return 40
        return 50

    text = str(level or "INFO").strip().upper()
    aliases = {
        "WARN": "WARNING",
        "ERR": "ERROR",
        "FATAL": "CRITICAL",
    }
    text = aliases.get(text, text)
    return LEVEL_VALUES.get(text, 20)


def _level_name(level: Any) -> str:
    """Convert level value/name to canonical output name."""
    if isinstance(level, str):
        text = level.strip().upper()
        if text == "CRITICAL":
            return "ERROR"
        if text == "WARN":
            return "WARNING"
        if text in LEVEL_VALUES:
            return text
    return LEVEL_NAMES.get(_coerce_level_value(level), "INFO")


def _resolve_timezone(name: Any):
    """Resolve configured timezone to tzinfo and label."""
    text = str(name or "local").strip() or "local"
    if text.lower() == "local":
        now = datetime.now().astimezone()
        return text, now.tzinfo
    if text.upper() == "UTC":
        return "UTC", ZoneInfo("UTC")
    try:
        return text, ZoneInfo(text)
    except Exception:
        now = datetime.now().astimezone()
        return "local", now.tzinfo


def _ensure_state() -> LoggingState:
    """Return logging state, creating a fallback if needed."""
    global _state
    if _state is None:
        tz_name, tzinfo = _resolve_timezone("local")
        logs_dir = Path(os.getcwd()).resolve() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        _state = LoggingState(
            logs_dir=logs_dir,
            timezone_name=tz_name,
            tzinfo=tzinfo,
            default_level=20,
            tag_levels={},
            lock=threading.Lock(),
        )
    return _state


def configure_logging(config: Dict[str, Any], root_dir: str) -> None:
    """Configure centralized logging from project config."""
    global _state

    logging_cfg = config.get("logging", {}) if isinstance(config, dict) else {}
    timezone_name, tzinfo = _resolve_timezone(logging_cfg.get("timezone", "local"))
    logs_dir = Path(root_dir).resolve() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    tag_levels: Dict[str, int] = {}
    for tag, level in (logging_cfg.get("levels", {}) or {}).items():
        tag_levels[_normalize_tag(tag)] = _coerce_level_value(level)

    _state = LoggingState(
        logs_dir=logs_dir,
        timezone_name=timezone_name,
        tzinfo=tzinfo,
        default_level=_coerce_level_value(logging_cfg.get("level", "info")),
        tag_levels=tag_levels,
        lock=threading.Lock(),
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(_StdlibBridgeHandler())


def _format_line(tag: str, level: Any, message: str) -> str:
    """Format one output log line."""
    state = _ensure_state()
    now = datetime.now(state.tzinfo)
    stamp = now.isoformat(timespec="milliseconds")
    return f"{stamp} [{_level_name(level)}] [{_normalize_tag(tag)}] {message}"


def _write_file(path: Path, line: str) -> None:
    """Append one line to a log file."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.write("\n")


def _emit_stream(line: str, level: Any) -> None:
    """Mirror one log line to stdout/stderr."""
    stream = sys.stderr if _coerce_level_value(level) >= 40 else sys.stdout
    stream.write(line + "\n")
    stream.flush()


def log(tag: str, level: Any, message: str, *args: Any, exc_info: Any = None) -> None:
    """Write one message to centralized logs."""
    state = _ensure_state()
    tag_name = _normalize_tag(tag)
    text = str(message)
    if args:
        try:
            text = text % args
        except Exception:
            text = " ".join([text, *[str(arg) for arg in args]])

    if exc_info:
        if exc_info is True:
            exc_info = sys.exc_info()
        if isinstance(exc_info, tuple) and exc_info[0] is not None:
            text = f"{text}\n{''.join(traceback.format_exception(*exc_info)).rstrip()}"
        elif isinstance(exc_info, BaseException):
            text = f"{text}\n{''.join(traceback.format_exception(exc_info)).rstrip()}"

    line = _format_line(tag_name, level, text)
    threshold = state.tag_levels.get(tag_name, state.default_level)

    with state.lock:
        _write_file(state.logs_dir / "all.log", line)
        if _coerce_level_value(level) >= threshold:
            _write_file(state.logs_dir / f"{tag_name}.log", line)

    _emit_stream(line, level)


def get_tag_for_logger(name: str) -> str:
    """Map a stdlib logger name to a sndbx tag."""
    raw = str(name or "core").strip() or "core"
    if raw in LOGGER_TAG_MAP:
        return LOGGER_TAG_MAP[raw]
    tail = raw.rsplit(".", 1)[-1]
    return LOGGER_TAG_MAP.get(tail, _normalize_tag(tail))


class TagLogger:
    """Small logger adapter backed by the central log() function."""

    def __init__(self, tag: str):
        self.tag = _normalize_tag(tag)

    def debug(self, message: str, *args: Any, exc_info: Any = None) -> None:
        """Write a debug log entry."""
        log(self.tag, "DEBUG", message, *args, exc_info=exc_info)

    def info(self, message: str, *args: Any, exc_info: Any = None) -> None:
        """Write an info log entry."""
        log(self.tag, "INFO", message, *args, exc_info=exc_info)

    def warning(self, message: str, *args: Any, exc_info: Any = None) -> None:
        """Write a warning log entry."""
        log(self.tag, "WARNING", message, *args, exc_info=exc_info)

    warn = warning

    def error(self, message: str, *args: Any, exc_info: Any = None) -> None:
        """Write an error log entry."""
        log(self.tag, "ERROR", message, *args, exc_info=exc_info)

    def exception(self, message: str, *args: Any) -> None:
        """Write an error log entry with traceback."""
        log(self.tag, "ERROR", message, *args, exc_info=True)

    def critical(self, message: str, *args: Any, exc_info: Any = None) -> None:
        """Write a critical log entry."""
        log(self.tag, "ERROR", message, *args, exc_info=exc_info)

    def log(self, level: Any, message: str, *args: Any, exc_info: Any = None) -> None:
        """Write a message using an explicit level."""
        log(self.tag, level, message, *args, exc_info=exc_info)


class _StdlibBridgeHandler(logging.Handler):
    """Bridge stdlib logging records into central log files."""

    def emit(self, record: logging.LogRecord) -> None:
        """Emit one logging record through central logging."""
        try:
            tag = get_tag_for_logger(record.name)
            message = record.getMessage()
            log(tag, record.levelname, message, exc_info=record.exc_info)
        except Exception:
            pass


def get_logger(tag: str) -> TagLogger:
    """Return a tag-bound logger adapter."""
    return TagLogger(tag)


def _parse_log_timestamp(line: str) -> Optional[datetime]:
    """Parse the leading ISO-8601 timestamp of one log line."""
    text = str(line or "").strip()
    if not text:
        return None
    token = text.split(" ", 1)[0]
    if not token:
        return None
    try:
        parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _rewrite_in_place(path: Path, lines: List[str]) -> None:
    """Replace a file's content without replacing its inode."""
    with path.open("r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write("".join(lines))
        handle.truncate()


def _wipe_text_log(path: Path, cutoff: datetime) -> int:
    """Drop text log entries older than the cutoff; return the removed count.

    A log message may span several lines, and only its first line carries the
    timestamp. Continuation lines inherit the timestamp of the entry they
    belong to, so a multi-line traceback is removed as a whole.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    kept: List[str] = []
    removed = 0
    entry_stamp: Optional[datetime] = None
    for line in content.splitlines(keepends=True):
        stamp = _parse_log_timestamp(line)
        if stamp is not None:
            entry_stamp = stamp
        elif entry_stamp is None:
            # An orphaned continuation line whose entry header is gone.
            removed += 1
            continue
        if entry_stamp < cutoff:
            removed += 1
            continue
        kept.append(line)
    if removed:
        _rewrite_in_place(path, kept)
    return removed


def _wipe_jsonl_log(path: Path, cutoff: datetime) -> int:
    """Drop JSONL log entries older than the cutoff; return the removed count."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    kept: List[str] = []
    removed = 0
    for line in content.splitlines(keepends=True):
        stamp = None
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            entry = None
        if isinstance(entry, dict):
            raw = entry.get("ts")
            if isinstance(raw, str):
                try:
                    stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    stamp = None
                if stamp is not None and stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
        if stamp is not None and stamp < cutoff:
            removed += 1
            continue
        kept.append(line)
    if removed:
        _rewrite_in_place(path, kept)
    return removed


def wipe_expired_logs(logs_dir: Path, max_age_seconds: float) -> Dict[str, int]:
    """Remove log entries older than the given age.

    input: log directory and the maximum age in seconds
    output: mapping of file name to the number of removed entries
    """
    if max_age_seconds <= 0:
        return {}
    directory = Path(logs_dir)
    if not directory.is_dir():
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(max_age_seconds))
    removed: Dict[str, int] = {}
    for path in sorted(directory.glob("*.log")):
        count = _wipe_text_log(path, cutoff)
        if count:
            removed[path.name] = count
    jsonl = directory / "mcp_requests.jsonl"
    if jsonl.is_file():
        count = _wipe_jsonl_log(jsonl, cutoff)
        if count:
            removed[jsonl.name] = count
    return removed