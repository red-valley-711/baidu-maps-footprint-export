from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path.cwd()
ADB = Path("adb")
CACHE_DIR = ""  # Required: discover and verify this on the user's device.
REMOTE_UI = "/sdcard/baidu_footprint_export_ui.xml"
RAW_DIR = ROOT / "raw"
STATE_DIR = ROOT / "state"
PULL_DIR = STATE_DIR / "pulls"
DB_PATH = STATE_DIR / "collector.sqlite3"
LOG_PATH = STATE_DIR / "events.jsonl"
STATUS_PATH = STATE_DIR / "status.json"
LIST_SETTLE_SECONDS = 30.0



class AccountActionRequired(BaseException):
    """Stops automatic navigation immediately; handled only by the entrypoint."""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def run_adb(
    device: str, *args: str, timeout: int = 30, check: bool = True
) -> bytes:
    result = subprocess.run(
        [str(ADB), "-s", device, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"adb {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.decode('utf-8', errors='replace')}"
        )
    return result.stdout


def shell(device: str, *args: str, timeout: int = 30, check: bool = True) -> str:
    return run_adb(
        device, "shell", *args, timeout=timeout, check=check
    ).decode("utf-8", errors="replace")


def dump_ui(device: str) -> ET.Element:
    run_adb(
        device, "shell", "uiautomator", "dump", REMOTE_UI, timeout=20
    )
    xml = run_adb(device, "exec-out", "cat", REMOTE_UI, timeout=20)
    root = ET.fromstring(xml)
    joined = "|".join(texts_in(root))
    if any(marker in joined for marker in (
        "旋转图片", "拖动滑块", "安全验证", "请完成验证", "账号登录", "密码登录"
    )):
        raise AccountActionRequired("Account verification/login requires the user")
    return root


def parse_bounds(value: str) -> tuple[int, int, int, int]:
    match = re.fullmatch(r"\[(\d+),(\d+)]\[(\d+),(\d+)]", value)
    if not match:
        raise ValueError(f"Bad bounds: {value}")
    return tuple(map(int, match.groups()))


def texts_in(node: ET.Element) -> list[str]:
    return [
        child.attrib["text"].strip()
        for child in node.iter()
        if child.attrib.get("text", "").strip()
    ]


def year_headers(root: ET.Element) -> list[dict]:
    headers = []
    for node in root.iter("node"):
        text = node.attrib.get("text", "").strip()
        match = re.search(r"(?<!\d)(20\d{2})年(?:\d{1,2}月)?", text)
        if not match:
            continue
        try:
            left, top, right, bottom = parse_bounds(node.attrib["bounds"])
        except (KeyError, ValueError):
            continue
        headers.append(
            {
                "year": match.group(1),
                "top": top,
                "bottom": bottom,
                "text": text,
            }
        )
    headers.sort(key=lambda item: item["top"])
    return headers


def current_year(root: ET.Element, fallback: str | None) -> str | None:
    headers = year_headers(root)
    return headers[-1]["year"] if headers else fallback


def record_cards(
    root: ET.Element, default_year: str | None = None
) -> list[dict]:
    cards = []
    headers = year_headers(root)
    try:
        _, screen_top, _, screen_bottom = parse_bounds(root.attrib["bounds"])
    except (KeyError, ValueError):
        screen_top, screen_bottom = 0, 1280
    safe_top = screen_top + 180
    # Baidu's final month card often ends a few pixels inside the bottom
    # edge. A 30 px cutoff skipped exactly the last card of many months.
    # Cards that are genuinely clipped extend to or beyond the screen edge.
    safe_bottom = screen_bottom - 5
    for node in root.iter("node"):
        if (
            node.attrib.get("package") != "com.baidu.BaiduMap"
            or node.attrib.get("clickable") != "true"
            or node.attrib.get("long-clickable") != "true"
        ):
            continue
        try:
            left, top, right, bottom = parse_bounds(node.attrib["bounds"])
        except (KeyError, ValueError):
            continue
        width = right - left
        height = bottom - top
        if not (500 <= width <= 720 and 150 <= height <= 500):
            continue
        if top < safe_top or bottom > safe_bottom:
            continue
        texts = texts_in(node)
        joined = "|".join(texts)
        if "公里" not in joined or not re.search(
            r"\d{1,2}月\d{1,2}日\s+\d{2}:\d{2}", joined
        ):
            continue
        preceding = [
            item for item in headers if item["bottom"] <= top
        ]
        year = preceding[-1]["year"] if preceding else default_year
        cards.append(
            {
                "base_fingerprint": joined,
                "year_label": year,
                "fingerprint": f"{year}|{joined}" if year else joined,
                "stable_fingerprint": (
                    f"{year}|{'|'.join(texts[:2])}"
                    if year and len(texts) >= 2
                    else f"{year}|{joined}" if year else joined
                ),
                "bounds": [left, top, right, bottom],
                "x": (left + right) // 2,
                "y": (top + bottom) // 2,
                "texts": texts,
            }
        )
    cards.sort(key=lambda card: card["bounds"][1])
    return cards


def is_footprint_list(root: ET.Element) -> bool:
    joined = "|".join(texts_in(root))
    return "我的足迹记录" in joined and (
        "累计导航" in joined or bool(record_cards(root))
    )


def cache_listing(device: str) -> list[dict]:
    output = shell(device, "ls", "-la", CACHE_DIR)
    files = []
    for line in output.splitlines():
        if not line or line.startswith("total "):
            continue
        parts = line.split(maxsplit=7)
        if len(parts) < 8 or parts[0].startswith("d"):
            continue
        name = parts[7]
        if not (name.endswith(".csv") or name.endswith(".csv.gz")):
            continue
        files.append({"name": name, "size": int(parts[4])})
    return files


def tap(device: str, x: int, y: int) -> None:
    shell(device, "input", "tap", str(x), str(y))


def keyevent(device: str, code: int) -> None:
    shell(device, "input", "keyevent", str(code))


def scroll_list(device: str) -> None:
    shell(
        device,
        "input",
        "swipe",
        "360",
        "1160",
        "360",
        "430",
        "500",
    )


def wait_until_list(
    device: str, timeout: float = 60, include_rounds: int = 20
) -> ET.Element:
    deadline = time.monotonic() + timeout
    last_root = None
    round_number = 1
    while time.monotonic() < deadline:
        last_root = dump_ui(device)
        joined = "|".join(texts_in(last_root))
        if is_footprint_list(last_root):
            return last_root
        if any(
            marker in joined
            for marker in (
                "旋转图片",
                "拖动滑块",
                "安全验证",
                "请完成验证",
                "账号登录",
                "密码登录",
            )
        ):
            raise RuntimeError(
                f"Account verification or login is required: {joined[:300]}"
            )
        if include_rounds and any(
            marker in joined for marker in ("加载中", "正在加载")
        ):
            time.sleep(0.6)
            continue
        round_number += 1
        time.sleep(0.6)
    suffix = (
        "|".join(texts_in(last_root))[:500] if last_root is not None else ""
    )
    if round_number == 1:
        suffix = "No UI root was returned. " + suffix
    raise TimeoutError(
        f"Timed out waiting for the footprint list. Current UI: {suffix}"
    )


def wait_for_new_file(
    device: str,
    before: set[str],
    timeout: float,
    poll_seconds: float,
    stable_checks: int,
) -> dict | None:
    deadline = time.monotonic() + timeout
    candidate = None
    stable_size = None
    stable_count = 0
    while time.monotonic() < deadline:
        files = cache_listing(device)
        new_files = [item for item in files if item["name"] not in before]
        if len(new_files) > 1:
            raise RuntimeError("Multiple new cache files appeared; stop and check association before retrying")
        if new_files:
            new_files.sort(key=lambda item: item["name"])
            current = new_files[-1]
            if candidate is not None and current["name"] == candidate["name"]:
                if current["size"] == stable_size and current["size"] > 0:
                    stable_count += 1
                else:
                    stable_size = current["size"]
                    stable_count = 0
                if stable_count >= stable_checks:
                    return current
            else:
                candidate = current
                stable_size = current["size"]
                stable_count = 0
        time.sleep(poll_seconds)
    return None  # A nonempty but still-growing file is not a completed cache.


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pull_new_file(device: str, info: dict) -> tuple[Path, str, float]:
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\.csv(?:\.gz)?", info["name"]):
        raise ValueError("Refusing an unexpected cache filename")
    raw, pulls = RAW_DIR.resolve(), PULL_DIR.resolve()
    if raw == pulls or raw in pulls.parents or pulls in raw.parents:
        raise ValueError("Raw cache and temporary pull directories must not overlap")
    PULL_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    remote = f"{CACHE_DIR}/{info['name']}"
    descriptor, temp_name = tempfile.mkstemp(prefix=info["name"] + ".", suffix=".part", dir=PULL_DIR)
    os.close(descriptor)
    temporary = Path(temp_name)
    started = time.monotonic()
    run_adb(device, "pull", remote, str(temporary), timeout=90)
    pull_seconds = time.monotonic() - started

    destination = RAW_DIR / info["name"]
    if destination.exists():
        same = (
            destination.stat().st_size == temporary.stat().st_size
            and sha256(destination) == sha256(temporary)
        )
        if same:
            temporary.unlink()
            return destination, "duplicate", pull_seconds
        raise RuntimeError(
            f"Refusing to overwrite a different existing file: {destination}"
        )

    if temporary.stat().st_size != info["size"]:
        raise RuntimeError("Remote cache changed during pull; retain the temporary file and retry")
    with temporary.open("rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    temporary.unlink()
    return destination, "pulled", pull_seconds


@contextmanager
def collector_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "collector.lock"
    lock_file = lock_path.open("a+b")
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            lock_file.close()
            raise RuntimeError(
                f"Another collector is already running: {lock_path}"
            ) from exc
    try:
        yield
    finally:
        if os.name == "nt":
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        lock_file.close()


def open_db(legacy_year: str | None = None) -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=30)
    # Reject unknown legacy years before journal/schema changes; inspection must
    # not silently mutate an old database simply because a migration will fail.
    previous_columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
    if previous_columns:
        year_field = "year_label" if "year_label" in previous_columns else "NULL"
        if not legacy_year:
            for fingerprint, year in connection.execute("SELECT fingerprint," + year_field + " FROM records"):
                if not year and not re.fullmatch(r"20\d{2}\|.*", fingerprint):
                    connection.close()
                    raise ValueError("Legacy records have no year; back up the DB and supply --legacy-year explicitly")
        backup_dir = STATE_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        descriptor, backup_name = tempfile.mkstemp(prefix="collector-before-write-", suffix=".sqlite3", dir=backup_dir)
        os.close(descriptor)
        backup = sqlite3.connect(backup_name)
        try:
            connection.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Database backup integrity check failed; collection was not started")
        except BaseException:
            connection.close()
            raise
        finally:
            backup.close()
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS records (
            fingerprint TEXT PRIMARY KEY,
            legacy_fingerprint TEXT,
            year_label TEXT,
            stable_fingerprint TEXT,
            sequence INTEGER UNIQUE,
            status TEXT NOT NULL,
            new_file TEXT,
            new_file_size INTEGER,
            local_file TEXT,
            detail_seconds REAL,
            pull_seconds REAL,
            total_seconds REAL,
            started_at_utc TEXT,
            finished_at_utc TEXT,
            error TEXT
        )
        """
    )
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(records)")
    }
    if "legacy_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN legacy_fingerprint TEXT"
        )
    if "year_label" not in columns:
        connection.execute("ALTER TABLE records ADD COLUMN year_label TEXT")
    if "stable_fingerprint" not in columns:
        connection.execute(
            "ALTER TABLE records ADD COLUMN stable_fingerprint TEXT"
        )

    legacy_rows = connection.execute(
        """
        SELECT fingerprint, legacy_fingerprint, year_label, stable_fingerprint
        FROM records
        WHERE legacy_fingerprint IS NULL
           OR year_label IS NULL
           OR stable_fingerprint IS NULL
        """
    ).fetchall()
    for fingerprint, legacy, year, stable_fingerprint in legacy_rows:
        versioned = re.fullmatch(r"(20\d{2})\|(.*)", fingerprint)
        if versioned:
            year = year or versioned.group(1)
            legacy = legacy or versioned.group(2)
            versioned_fingerprint = fingerprint
        else:
            year = year or legacy_year
            if not year:
                raise ValueError("Legacy records have no year; back up the DB and supply --legacy-year explicitly")
            legacy = legacy or fingerprint
            versioned_fingerprint = f"{year}|{legacy}"
        stable_parts = legacy.split("|")
        stable_fingerprint = stable_fingerprint or (
            f"{year}|{'|'.join(stable_parts[:2])}"
            if len(stable_parts) >= 2
            else f"{year}|{legacy}"
        )
        connection.execute(
            """
            UPDATE records
            SET fingerprint = ?, legacy_fingerprint = ?, year_label = ?,
                stable_fingerprint = ?
            WHERE fingerprint = ?
            """,
            (
                versioned_fingerprint,
                legacy,
                year,
                stable_fingerprint,
                fingerprint,
            ),
        )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS records_legacy_year_idx
        ON records (legacy_fingerprint, year_label)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS records_stable_fingerprint_idx
        ON records (stable_fingerprint)
        """
    )

    duplicate_groups = connection.execute(
        """
        SELECT stable_fingerprint
        FROM records
        WHERE stable_fingerprint IS NOT NULL
        GROUP BY stable_fingerprint
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    for (stable_fingerprint,) in duplicate_groups:
        rows = connection.execute(
            """
            SELECT sequence, fingerprint, legacy_fingerprint, year_label,
                   status, new_file
            FROM records
            WHERE stable_fingerprint = ?
            """,
            (stable_fingerprint,),
        ).fetchall()
        keeper = max(
            rows,
            key=lambda row: (
                row[4] in ("duplicate", "pulled", "benchmark_pulled"),
                bool(row[5]),
                len(row[2] or row[1]),
            ),
        )
        best_legacy = max(
            (row[2] or row[1] for row in rows),
            key=len,
        )
        year = keeper[3]
        archive_record(connection, keeper[0], "fingerprint_dedup_keeper")
        for row in rows:
            if row[0] != keeper[0]:
                archive_record(connection, row[0], "fingerprint_dedup")
                connection.execute(
                    "DELETE FROM records WHERE sequence = ?", (row[0],)
                )
        connection.execute(
            """
            UPDATE records
            SET fingerprint = ?, legacy_fingerprint = ?
            WHERE sequence = ?
            """,
            (f"{year}|{best_legacy}", best_legacy, keeper[0]),
        )
    connection.commit()
    return connection


def archive_record(connection: sqlite3.Connection, sequence: int, reason: str) -> None:
    """Retain the complete prior row in the same transaction before consolidation."""
    connection.execute("CREATE TABLE IF NOT EXISTS record_history (id INTEGER PRIMARY KEY, archived_at_utc TEXT NOT NULL, reason TEXT NOT NULL, record_json TEXT NOT NULL)")
    cursor = connection.execute("SELECT * FROM records WHERE sequence=?", (sequence,))
    row = cursor.fetchone()
    if row:
        snapshot = dict(zip((column[0] for column in cursor.description), row))
        connection.execute("INSERT INTO record_history(archived_at_utc,reason,record_json) VALUES(?,?,?)", (now_utc(), reason, json.dumps(snapshot, ensure_ascii=False)))


def append_event(event: dict) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        fh.flush()


def write_status(
    connection: sqlite3.Connection,
    device: str,
    session_started: str,
    current: dict | None,
    state: str,
    note: str = "",
) -> None:
    counts = {
        row[0]: row[1]
        for row in connection.execute(
            "SELECT status, COUNT(*) FROM records GROUP BY status"
        )
    }
    total = sum(counts.values())
    last_record = connection.execute(
        """
        SELECT sequence, status, fingerprint, legacy_fingerprint,
               year_label, stable_fingerprint, new_file, finished_at_utc
        FROM records
        ORDER BY sequence DESC
        LIMIT 1
        """
    ).fetchone()
    status = {
        "updated_at_utc": now_utc(),
        "session_started_utc": session_started,
        "pid": os.getpid(),
        "device": device,
        "state": state,
        "note": note,
        "records_processed": total,
        "status_counts": counts,
        "last_record": (
            {
                "sequence": last_record[0],
                "status": last_record[1],
                "fingerprint": last_record[2],
                "legacy_fingerprint": last_record[3],
                "year_label": last_record[4],
                "stable_fingerprint": last_record[5],
                "new_file": last_record[6],
                "finished_at_utc": last_record[7],
            }
            if last_record
            else None
        ),
        "current_record": current,
        "raw_files": len(list(RAW_DIR.glob("*.csv")))
        + len(list(RAW_DIR.glob("*.csv.gz"))),
    }
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(STATUS_PATH)


def rebuild_qgis() -> None:
    raise RuntimeError("Use footprint_export.py to configure the exporter")


def load_processed(connection: sqlite3.Connection) -> dict:
    exact = set()
    stable = set()
    by_year_and_base = set()
    legacy_bases = set()
    for fingerprint, legacy, year, stable_fingerprint in connection.execute(
        """
        SELECT fingerprint, legacy_fingerprint, year_label,
               stable_fingerprint
        FROM records
        WHERE status != 'retry_pending'
        """
    ):
        base = legacy or fingerprint
        exact.add(fingerprint)
        legacy_bases.add(base)
        if stable_fingerprint:
            stable.add(stable_fingerprint)
        if year:
            by_year_and_base.add((year, base))
    return {
        "exact": exact,
        "stable": stable,
        "by_year_and_base": by_year_and_base,
        "legacy_bases": legacy_bases,
    }


def card_is_processed(card: dict, processed: dict) -> bool:
    if card["stable_fingerprint"] in processed["stable"]:
        return True
    if card["fingerprint"] in processed["exact"]:
        return True
    year = card.get("year_label")
    base = card["base_fingerprint"]
    if year:
        return (year, base) in processed["by_year_and_base"]
    return base in processed["legacy_bases"]


def advance_to_unprocessed(
    device: str,
    root: ET.Element,
    processed: dict,
    active_year: str | None,
    max_stagnant_scrolls: int,
    month_mode: bool = False,
) -> tuple[ET.Element, bool, str | None]:
    stagnant = 0
    for _ in range(200):
        active_year = current_year(root, active_year)
        cards = record_cards(root, active_year)
        if any(not card_is_processed(card, processed) for card in cards):
            return root, False, active_year

        before = {card["fingerprint"] for card in cards}
        scroll_list(device)
        time.sleep(0.5)
        deadline = time.monotonic() + LIST_SETTLE_SECONDS
        changed = False
        while time.monotonic() < deadline:
            root = dump_ui(device)
            joined = "|".join(texts_in(root))
            if any(
                marker in joined
                for marker in (
                    "旋转图片",
                    "拖动滑块",
                    "安全验证",
                    "请完成验证",
                )
            ):
                raise RuntimeError("Account verification appeared while scrolling")
            if not is_footprint_list(root):
                time.sleep(0.8)
                continue
            active_year = current_year(root, active_year)
            cards = record_cards(root, active_year)
            after = {card["fingerprint"] for card in cards}
            if any(not card_is_processed(card, processed) for card in cards):
                return root, False, active_year
            if after != before:
                changed = True
                break
            if after and any(
                marker in joined
                for marker in ("没有更多", "已加载全部", "到底了")
            ):
                return root, True, active_year
            time.sleep(0.8)
        if changed:
            stagnant = 0
            continue
        stagnant += 1
        if stagnant >= max_stagnant_scrolls:
            if month_mode and is_footprint_list(root) and cards and not any(
                marker in "|".join(texts_in(root)) for marker in ("加载中", "正在加载")
            ):
                append_event({"event": "month_end_inferred", "at_utc": now_utc(),
                              "stagnant_scrolls": stagnant})
                return root, True, active_year
            current = "|".join(texts_in(root))[:500]
            raise RuntimeError(
                "Footprint list stopped loading before an explicit end marker. "
                f"Current UI: {current}"
            )
    raise RuntimeError("Scrolling did not expose another record")


def process_card(
    device: str,
    connection: sqlite3.Connection,
    card: dict,
    file_timeout: float,
    poll_seconds: float,
) -> dict:
    fingerprint = card["fingerprint"]
    base_fingerprint = card["base_fingerprint"]
    year_label = card.get("year_label")
    stable_fingerprint = card["stable_fingerprint"]
    before = {item["name"] for item in cache_listing(device)}
    next_sequence = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM records"
    ).fetchone()[0]
    started_at = now_utc()
    started = time.monotonic()
    tap(device, card["x"], card["y"])
    new_file = wait_for_new_file(
        device,
        before,
        timeout=file_timeout,
        poll_seconds=poll_seconds,
        stable_checks=2,
    )
    detail_seconds = time.monotonic() - started

    local_file = ""
    pull_seconds = 0.0
    status = "server_only_or_slow"
    if new_file:
        destination, pull_status, pull_seconds = pull_new_file(
            device, new_file
        )
        status = pull_status
        local_file = str(destination)

    total_seconds = time.monotonic() - started
    result = {
        "sequence": next_sequence,
        "fingerprint": fingerprint,
        "legacy_fingerprint": base_fingerprint,
        "year_label": year_label,
        "stable_fingerprint": stable_fingerprint,
        "status": status,
        "new_file": new_file["name"] if new_file else None,
        "new_file_size": new_file["size"] if new_file else None,
        "local_file": local_file,
        "detail_seconds": round(detail_seconds, 3),
        "pull_seconds": round(pull_seconds, 3),
        "total_seconds": round(total_seconds, 3),
        "started_at_utc": started_at,
        "finished_at_utc": now_utc(),
    }

    existing = connection.execute(
        "SELECT sequence FROM records WHERE stable_fingerprint = ? AND status = 'retry_pending'",
        (stable_fingerprint,),
    ).fetchone()
    if new_file and existing is None:
        existing = connection.execute(
            """
            SELECT sequence
            FROM records
            WHERE new_file = ? AND fingerprint <> ?
            ORDER BY sequence
            LIMIT 1
            """,
            (new_file["name"], fingerprint),
        ).fetchone()
    if existing is None:
        slow_matches = connection.execute(
            """
            SELECT sequence
            FROM records
            WHERE legacy_fingerprint = ? AND year_label = ?
              AND status IN ('server_only_or_slow', 'retry_pending')
            ORDER BY sequence
            """,
            (base_fingerprint, year_label),
        ).fetchall()
        if len(slow_matches) == 1:
            existing = slow_matches[0]
    if existing is not None:
        existing_sequence = existing[0]
        archive_record(connection, existing_sequence, "record_update")
        connection.execute(
            """
            UPDATE records
            SET fingerprint = ?, legacy_fingerprint = ?, year_label = ?,
                stable_fingerprint = ?, status = ?, new_file = ?,
                new_file_size = ?, local_file = ?, detail_seconds = ?,
                pull_seconds = ?, total_seconds = ?, started_at_utc = ?,
                finished_at_utc = ?, error = NULL
            WHERE sequence = ?
            """,
            (
                fingerprint,
                base_fingerprint,
                year_label,
                stable_fingerprint,
                status,
                result["new_file"],
                result["new_file_size"],
                local_file,
                result["detail_seconds"],
                result["pull_seconds"],
                result["total_seconds"],
                started_at,
                result["finished_at_utc"],
                existing_sequence,
            ),
        )
        connection.commit()
        result["sequence"] = existing_sequence
        result["status"] = "year_corrected" if status == "duplicate" else status
        result["merged_into_sequence"] = existing_sequence
        append_event(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    connection.execute(
        """
        INSERT INTO records (
            fingerprint, legacy_fingerprint, year_label,
            stable_fingerprint, sequence, status, new_file,
            new_file_size, local_file, detail_seconds, pull_seconds,
            total_seconds, started_at_utc, finished_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fingerprint,
            base_fingerprint,
            year_label,
            stable_fingerprint,
            next_sequence,
            status,
            result["new_file"],
            result["new_file_size"],
            local_file,
            result["detail_seconds"],
            result["pull_seconds"],
            result["total_seconds"],
            started_at,
            result["finished_at_utc"],
        ),
    )
    connection.commit()
    append_event(result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result

