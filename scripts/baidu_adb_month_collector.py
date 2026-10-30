from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import baidu_adb_collector as core
from PIL import Image


ROOT = core.ROOT
STATE_DIR = core.STATE_DIR
DB_PATH = core.DB_PATH
YEAR_WHEEL_X = 230
MONTH_WHEEL_X = 510
WHEEL_Y = 990
WHEEL_STEP = 100
WINDOWS_POWERSHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
OCR_SCRIPT = Path(__file__).with_name("win_ocr.ps1")
YEAR_CROP = (40, 900, 382, 1020)
MONTH_CROP = (382, 900, 680, 1020)


def previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12
    return year, month - 1


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def month_index(year: int, month: int) -> int:
    return year * 12 + month - 1


def from_month_index(value: int) -> tuple[int, int]:
    year, month_zero = divmod(value, 12)
    return year, month_zero + 1


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    return from_month_index(month_index(year, month) + delta)


def exact_text_nodes(root: ET.Element, text: str) -> list[ET.Element]:
    return [
        node
        for node in root.iter("node")
        if node.attrib.get("text", "").strip() == text
    ]


def tap_text(device: str, text: str, timeout: float = 30) -> ET.Element:
    deadline = time.monotonic() + timeout
    last_root = None
    while time.monotonic() < deadline:
        last_root = core.dump_ui(device)
        nodes = exact_text_nodes(last_root, text)
        if nodes:
            node = min(
                nodes,
                key=lambda item: (
                    lambda bounds: (bounds[2] - bounds[0])
                    * (bounds[3] - bounds[1])
                )(core.parse_bounds(item.attrib["bounds"])),
            )
            left, top, right, bottom = core.parse_bounds(
                node.attrib["bounds"]
            )
            core.tap(device, (left + right) // 2, (top + bottom) // 2)
            return last_root
        time.sleep(0.4)
    current = "|".join(core.texts_in(last_root))[:500] if last_root else ""
    raise TimeoutError(f"Text {text!r} was not found. Current UI: {current}")


def tap_text_prefix(
    device: str, prefix: str, timeout: float = 30
) -> ET.Element:
    deadline = time.monotonic() + timeout
    last_root = None
    while time.monotonic() < deadline:
        last_root = core.dump_ui(device)
        nodes = [
            node
            for node in last_root.iter("node")
            if node.attrib.get("text", "").strip().startswith(prefix)
        ]
        if nodes:
            node = min(
                nodes,
                key=lambda item: (
                    lambda bounds: (bounds[2] - bounds[0])
                    * (bounds[3] - bounds[1])
                )(core.parse_bounds(item.attrib["bounds"])),
            )
            left, top, right, bottom = core.parse_bounds(
                node.attrib["bounds"]
            )
            core.tap(device, (left + right) // 2, (top + bottom) // 2)
            return last_root
        time.sleep(0.4)
    current = "|".join(core.texts_in(last_root))[:500] if last_root else ""
    raise TimeoutError(
        f"Text prefix {prefix!r} was not found. Current UI: {current}"
    )


def wait_for_text(
    device: str, text: str, timeout: float = 60
) -> ET.Element:
    deadline = time.monotonic() + timeout
    last_root = None
    while time.monotonic() < deadline:
        last_root = core.dump_ui(device)
        if exact_text_nodes(last_root, text):
            return last_root
        time.sleep(0.4)
    current = "|".join(core.texts_in(last_root))[:500] if last_root else ""
    raise TimeoutError(f"Timed out waiting for {text!r}. Current UI: {current}")


def normalize_to_footprint_list(device: str, timeout: float) -> ET.Element:
    try:
        return core.wait_until_list(device, timeout=min(timeout, 8))
    except Exception:
        root = core.dump_ui(device)
        joined = "|".join(core.texts_in(root))
        handled_overlay = False
        if "取消" in joined and "确定" in joined:
            tap_text(device, "取消")
            time.sleep(0.5)
            root = core.dump_ui(device)
            joined = "|".join(core.texts_in(root))
            handled_overlay = True
        if "重置" in joined and "确认" in joined:
            tap_text(device, "重置")
            time.sleep(0.2)
            tap_text(device, "确认")
            time.sleep(0.8)
            handled_overlay = True
        if handled_overlay:
            try:
                return core.wait_until_list(device, timeout=12)
            except Exception:
                pass
        if not handled_overlay:
            try:
                return core.wait_until_list(device, timeout=1)
            except Exception:
                core.keyevent(device, 4)
                time.sleep(0.8)
                try:
                    return core.wait_until_list(device, timeout=5)
                except Exception:
                    pass

        # Relaunch the app if the emulator returned to its launcher.
        core.shell(
            device,
            "monkey",
            "-p",
            "com.baidu.BaiduMap",
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        )
        time.sleep(4)
        root = core.dump_ui(device)
        if core.is_footprint_list(root):
            return root
        current_texts = set(core.texts_in(root))
        if any(text.startswith("跳过") for text in current_texts):
            tap_text_prefix(device, "跳过", timeout=10)
        wait_for_text(device, "我的", timeout=30)
        tap_text(device, "我的")
        time.sleep(5)
        try:
            wait_for_text(device, "历史行程", timeout=30)
        except Exception:
            core.shell(device, "input", "swipe", "360", "1100", "360", "500", "500")
            wait_for_text(device, "历史行程", timeout=30)
        tap_text(device, "历史行程")
        time.sleep(3)
        try:
            return core.wait_until_list(device, timeout=timeout)
        except Exception:
            if "查看全部历史轨迹" in set(
                core.texts_in(core.dump_ui(device))
            ):
                tap_text(device, "查看全部历史轨迹")
                return core.wait_until_list(device, timeout=timeout)
            raise


def open_empty_filter(device: str, timeout: float) -> ET.Element:
    root = normalize_to_footprint_list(device, timeout)
    texts = set(core.texts_in(root))
    if "取消" in texts and "确定" in texts:
        tap_text(device, "取消")
        time.sleep(0.5)
        root = core.dump_ui(device)
        texts = set(core.texts_in(root))
    if "重置" not in texts or "确认" not in texts:
        tap_text(device, "筛选")
        root = wait_for_text(device, "重置", timeout=timeout)
    return root


def wheel_swipe(
    device: str, x: int, steps: int, direction: str
) -> None:
    if steps <= 0:
        return
    if direction == "previous":
        start_y, end_y = WHEEL_Y, WHEEL_Y + WHEEL_STEP
    elif direction == "next":
        start_y, end_y = WHEEL_Y + WHEEL_STEP, WHEEL_Y
    else:
        raise ValueError(f"Unknown wheel direction: {direction}")
    for _ in range(steps):
        core.shell(
            device,
            "input",
            "swipe",
            str(x),
            str(start_y),
            str(x),
            str(end_y),
            "260",
        )
        time.sleep(0.3)


def ocr_picker_crop(image: Image.Image, crop: tuple[int, int, int, int], name: str) -> str:
    output_path = STATE_DIR / f"picker_{name}.png"
    image.crop(crop).resize(
        ((crop[2] - crop[0]) * 3, (crop[3] - crop[1]) * 3)
    ).save(output_path)
    result = subprocess.run(
        [
            str(WINDOWS_POWERSHELL),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(OCR_SCRIPT),
            "-Path",
            str(output_path.resolve()),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=20,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            result.stderr.decode("utf-8", errors="replace")
            or f"Windows OCR failed for {name}"
        )
    return result.stdout.decode("utf-8", errors="replace").strip()


def read_picker_selection(device: str) -> tuple[int, int]:
    screenshot = core.run_adb(
        device, "exec-out", "screencap", "-p", timeout=20
    )
    image = Image.open(io.BytesIO(screenshot)).convert("RGB")
    year_text = ocr_picker_crop(
        image, YEAR_CROP, "year"
    )
    month_text = ocr_picker_crop(
        image, MONTH_CROP, "month"
    )
    year_match = re.search(r"(20\d{2})", year_text)
    month_match = re.search(r"(\d{1,2})", month_text)
    if not year_match or not month_match:
        raise RuntimeError(
            f"Could not OCR picker selection: {year_text!r}, {month_text!r}"
        )
    selected_year = int(year_match.group(1))
    selected_month = int(month_match.group(1))
    if not 1 <= selected_month <= 12:
        raise RuntimeError(
            f"OCR returned invalid month: {selected_month} ({month_text!r})"
        )
    return selected_year, selected_month


def choose_picker_target(device: str, year: int, month: int) -> None:
    for _ in range(40):
        selected_year, selected_month = read_picker_selection(device)
        print(
            f"PICKER selected={selected_year}-{selected_month:02d} "
            f"target={year}-{month:02d}",
            flush=True,
        )
        if selected_year == year and selected_month == month:
            return
        if selected_year != year:
            direction = "next" if selected_year < year else "previous"
            wheel_swipe(device, YEAR_WHEEL_X, 1, direction)
            time.sleep(0.8)
            continue

        # The month wheel clamps at January instead of rolling over into
        # the adjacent year, so move directly through the same-year range.
        month_delta = month - selected_month
        direction = "next" if month_delta > 0 else "previous"
        wheel_swipe(device, MONTH_WHEEL_X, 1, direction)
        time.sleep(0.8)
    selected_year, selected_month = read_picker_selection(device)
    raise RuntimeError(
        "Picker did not converge to target: "
        f"wanted {year}-{month:02d}, got {selected_year}-{selected_month:02d}"
    )


def wait_for_filtered_month(
    device: str, year: int, month: int, timeout: float
) -> ET.Element:
    label = f"{year}年{month:02d}月"
    month_prefix = f"{month:02d}月"
    deadline = time.monotonic() + timeout
    last_root = None
    while time.monotonic() < deadline:
        last_root = core.dump_ui(device)
        joined = "|".join(core.texts_in(last_root))
        cards = core.record_cards(last_root, str(year))
        matching = [card for card in cards if card["base_fingerprint"].startswith(month_prefix)]
        if (
            matching
            or "暂无数据" in joined
            or "暂时没有数据" in joined
            or "暂时没有更多数据哦" in joined
        ):
            return last_root
        time.sleep(0.8)
    current = "|".join(core.texts_in(last_root))[:500] if last_root else ""
    raise TimeoutError(
        f"Timed out applying {label}. Current UI: {current}"
    )


def selected_filter_label(
    root: ET.Element,
) -> tuple[int, int, str] | None:
    for text in core.texts_in(root):
        match = re.fullmatch(r"(20\d{2})年(\d{2})月", text)
        if match:
            year, month = int(match.group(1)), int(match.group(2))
            return year, month, text
    return None


def apply_month_filter(
    device: str, year: int, month: int, timeout: float
) -> ET.Element:
    sheet = open_empty_filter(device, timeout)
    if "选择年月" in set(core.texts_in(sheet)):
        tap_text(device, "选择年月")
    else:
        current_labels = [
            text
            for text in core.texts_in(sheet)
            if re.fullmatch(r"20\d{2}年\d{2}月", text)
        ]
        if not current_labels:
            raise RuntimeError("Filter panel did not expose a date button")
        tap_text(device, current_labels[0])
    wait_for_text(device, "确定", timeout=timeout)

    adjusted_year, adjusted_month = year, month
    sheet = None
    for _ in range(4):
        choose_picker_target(device, adjusted_year, adjusted_month)
        tap_text(device, "确定")
        sheet = wait_for_text(device, "确认", timeout=timeout)
        selected = selected_filter_label(sheet)
        if selected is None:
            raise RuntimeError("Month picker did not expose a selected date")
        selected_year, selected_month, selected_text = selected
        error = (
            month_index(selected_year, selected_month)
            - month_index(year, month)
        )
        if error == 0:
            break
        adjusted_year, adjusted_month = shift_month(
            adjusted_year, adjusted_month, -error
        )
        tap_text(device, selected_text)
        wait_for_text(device, "确定", timeout=timeout)
    else:
        raise RuntimeError(
            f"Could not select target month {year}-{month:02d}; "
            f"last selected {selected_year}-{selected_month:02d}"
        )

    tap_text(device, "确认")
    return wait_for_filtered_month(device, year, month, timeout)


def create_month_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS collection_months (
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            status TEXT NOT NULL,
            records_before INTEGER NOT NULL DEFAULT 0,
            records_after INTEGER NOT NULL DEFAULT 0,
            had_cards INTEGER NOT NULL DEFAULT 0,
            started_at_utc TEXT,
            completed_at_utc TEXT,
            error TEXT,
            PRIMARY KEY (year, month)
        )
        """
    )
    connection.commit()


def begin_month(
    connection: sqlite3.Connection, year: int, month: int
) -> int:
    count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    connection.execute(
        """
        INSERT INTO collection_months (
            year, month, status, records_before, records_after,
            had_cards, started_at_utc, completed_at_utc, error
        ) VALUES (?, ?, 'running', ?, 0, 0, ?, NULL, NULL)
        ON CONFLICT(year, month) DO UPDATE SET
            status = 'running',
            records_before = excluded.records_before,
            records_after = 0,
            had_cards = 0,
            started_at_utc = excluded.started_at_utc,
            completed_at_utc = NULL,
            error = NULL
        """,
        (year, month, count, core.now_utc()),
    )
    connection.commit()
    return count


def finish_month(
    connection: sqlite3.Connection,
    year: int,
    month: int,
    had_cards: bool,
) -> None:
    count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    connection.execute(
        """
        UPDATE collection_months
        SET status = 'complete', records_after = ?, had_cards = ?,
            completed_at_utc = ?, error = NULL
        WHERE year = ? AND month = ?
        """,
        (count, int(had_cards), core.now_utc(), year, month),
    )
    connection.commit()


def fail_month(
    connection: sqlite3.Connection,
    year: int,
    month: int,
    error: Exception,
) -> None:
    connection.execute(
        """
        UPDATE collection_months
        SET status = 'failed', error = ?
        WHERE year = ? AND month = ?
        """,
        (repr(error), year, month),
    )
    connection.commit()


def run_month(
    device: str,
    connection: sqlite3.Connection,
    year: int,
    month: int,
    args: argparse.Namespace,
    session_started: str,
    session_state: dict,
) -> tuple[bool, int, bool]:
    label = f"{year}-{month:02d}"
    print(f"MONTH_START {label}", flush=True)
    root = apply_month_filter(
        device, year, month, timeout=args.ui_timeout
    )
    had_cards = bool(core.record_cards(root, str(year)))
    month_new = 0
    until_qgis = args.qgis_every

    while True:
        processed = core.load_processed(connection)
        cards = core.record_cards(root, str(year))
        had_cards = had_cards or bool(cards)
        available = [
            card
            for card in cards
            if not core.card_is_processed(card, processed)
        ]
        if not available:
            joined = "|".join(core.texts_in(root))
            if not cards and any(text in joined for text in ("暂无数据", "暂时没有数据", "暂时没有更多数据哦")):
                break
            root, exhausted, _ = core.advance_to_unprocessed(
                device,
                root,
                processed,
                str(year),
                args.stagnant_scrolls,
                month_mode=True,
            )
            if exhausted:
                break
            continue

        card = available[0]
        current = {
            "month": label,
            "fingerprint": card["fingerprint"],
            "stable_fingerprint": card["stable_fingerprint"],
        }
        core.write_status(
            connection,
            device,
            session_started,
            current,
            "opening_record",
            f"month={label}",
        )
        result = core.process_card(
            device,
            connection,
            card,
            args.file_timeout,
            args.poll_seconds,
        )
        session_state["last_result"] = result
        month_new += 1
        session_state["actions"] += 1
        core.keyevent(device, 4)
        root = normalize_to_footprint_list(device, args.ui_timeout)
        root = wait_for_filtered_month(
            device, year, month, timeout=args.ui_timeout
        )
        core.write_status(
            connection,
            device,
            session_started,
            result,
            "running",
            f"month={label}",
        )

        if args.max_records and session_state["actions"] >= args.max_records:
            return had_cards, month_new, False

        if not args.no_qgis and args.qgis_every > 0:
            until_qgis -= 1
            if until_qgis <= 0:
                core.write_status(
                    connection,
                    device,
                    session_started,
                    result,
                    "rebuilding_qgis",
                    f"month={label}",
                )
                core.rebuild_qgis()
                until_qgis = args.qgis_every

    finish_month(connection, year, month, had_cards)
    print(
        f"MONTH_COMPLETE {label} actions={month_new} had_cards={had_cards}",
        flush=True,
    )
    return had_cards, month_new, True

