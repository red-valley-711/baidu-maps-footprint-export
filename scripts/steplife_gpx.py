"""Build an optional Android StepLife GPX import package.

The footprint CSV has no per-point timestamps. This module therefore requires an
explicit ``matched-start-order`` opt-in. It preserves every source coordinate,
uses only a verified trip-record start as real time, and assigns later points a
monotonic compatibility sequence so Android StepLife does not merge points that
all share one timestamp.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import parse_baidu_hex_tracks as convert

NS = "http://www.topografix.com/GPX/1/1"
UUID_CSV = re.compile(
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\.csv(?:\.gz)?"
)
FINGERPRINT_TIME = re.compile(
    r"^(20\d{2})\|(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2})(?::(\d{2}))?\|"
)
DEFAULT_BATCH_POINTS = 100_000


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def track_id(path: Path) -> str:
    return path.name.removesuffix(".gz").removesuffix(".csv").lower()


def raw_variants(folder: Path) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = {}
    if not folder.exists():
        return result
    for path in sorted(folder.iterdir()):
        if path.is_file() and UUID_CSV.fullmatch(path.name):
            result.setdefault(track_id(path), []).append(path)
    return result


def selected_raw_files(folder: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    variants = raw_variants(folder)
    selected: dict[str, Path] = {}
    for key, paths in variants.items():
        grouped = {path.name.endswith(".gz") for path in paths}
        if len(grouped) != len(paths):
            raise ValueError(f"Ambiguous UUID with the same compression type: {key}")
        if len(paths) > 2:
            raise ValueError(f"Too many CSV variants for UUID: {key}")
        selected[key] = next(
            (path for path in paths if not path.name.endswith(".gz")),
            paths[0],
        )
    return selected, variants


def load_trip_mappings(db_path: Path, offset_hours: float) -> dict[str, dict]:
    if not db_path.exists():
        raise ValueError(f"State database does not exist: {db_path}")
    local_tz = timezone(timedelta(hours=offset_hours))
    connection = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(records)")
        }
        if not {"new_file", "fingerprint"} <= columns:
            raise ValueError("records must contain new_file and fingerprint columns")
        rows = connection.execute(
            "SELECT new_file, fingerprint FROM records "
            "WHERE new_file IS NOT NULL AND fingerprint IS NOT NULL"
        ).fetchall()
    finally:
        connection.close()

    mappings: dict[str, dict] = {}
    for new_file, fingerprint in rows:
        key = track_id(Path(new_file))
        match = FINGERPRINT_TIME.match(fingerprint)
        if not match:
            continue
        year, month, day, hour, minute, second = match.groups()
        try:
            local = datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second or 0),
                tzinfo=local_tz,
            )
        except ValueError:
            continue
        mapping = {
            "trip_local": local.isoformat(timespec="seconds"),
            "trip_utc": local.astimezone(timezone.utc),
        }
        previous = mappings.get(key)
        if previous and (
            previous["trip_local"] != mapping["trip_local"]
            or previous["trip_utc"] != mapping["trip_utc"]
        ):
            raise ValueError(f"Conflicting trip times for UUID: {key}")
        mappings[key] = mapping
    return mappings


def parse_uuid_track(path: Path, variants: list[Path]) -> dict:
    track = convert.parse_track(path)
    if len(variants) > 1:
        for other in variants:
            candidate = convert.parse_track(other)
            if candidate["points"] != track["points"]:
                raise ValueError(f"Conflicting CSV/gzip copies for UUID {track_id(path)}")
    return track


def format_epoch(value: int) -> str:
    return (
        datetime.fromtimestamp(value, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def allocate_times(
    key: str,
    points: list[dict],
    mapping: dict,
    reserved: set[int],
    protected_starts: set[int],
    local_tz: timezone,
) -> tuple[list[str], dict]:
    base = int(mapping["trip_utc"].timestamp())
    assigned: list[int] = []
    previous_assigned = None
    max_drift = 0
    for offset in range(len(points)):
        desired = base + offset
        if previous_assigned is None:
            # Preserve the verified trip-record start even when another track
            # legitimately has the same start second.
            candidate = base
        else:
            candidate = max(desired, previous_assigned + 1)
            while candidate in reserved or candidate in protected_starts:
                candidate += 1
        assigned.append(candidate)
        reserved.add(candidate)
        max_drift = max(max_drift, candidate - desired)
        previous_assigned = candidate

    if len(set(assigned)) != len(points):
        raise RuntimeError(f"Timestamp allocation failed for {key}")
    if any(right <= left for left, right in zip(assigned, assigned[1:])):
        raise RuntimeError(f"Timestamps are not increasing for {key}")
    first_local = datetime.fromtimestamp(assigned[0], timezone.utc).astimezone(local_tz)
    last_local = datetime.fromtimestamp(assigned[-1], timezone.utc).astimezone(local_tz)
    return [format_epoch(value) for value in assigned], {
        "compatibility_span_seconds": len(points) - 1,
        "assigned_span_seconds": assigned[-1] - assigned[0],
        "max_forward_drift_seconds": max_drift,
        "crosses_local_midnight": first_local.date() != last_local.date(),
    }


class GpxWriter:
    def __init__(self, path: Path, title: str, description: str, semantics: str):
        self.path = path
        self.handle = path.open("x", encoding="utf-8", newline="\n")
        self.track_count = 0
        self.point_count = 0
        self.timed_point_count = 0
        self.handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        self.handle.write(
            f'<gpx version="1.1" creator="Baidu footprint to StepLife" xmlns="{NS}">\n'
        )
        self.handle.write("  <metadata>\n")
        self.handle.write(f"    <name>{escape(title)}</name>\n")
        self.handle.write(f"    <desc>{escape(description)}</desc>\n")
        self.handle.write(f"    <keywords>{escape(semantics)}</keywords>\n")
        self.handle.write("  </metadata>\n")

    def add_track(self, label: str, points: list[dict], point_times: list[str] | None):
        if point_times is not None and len(point_times) != len(points):
            raise ValueError("Every matched point must have one timestamp")
        self.handle.write("  <trk>\n")
        self.handle.write(f"    <name>{escape(label)}</name>\n")
        self.handle.write(
            f"    <desc>{escape('Optional StepLife compatibility output; '
                                'later timestamps are monotonic compatibility '
                                'offsets, not observed sample times.')}</desc>\n"
        )
        self.handle.write("    <trkseg>\n")
        for index, point in enumerate(points):
            self.handle.write(
                f'      <trkpt lat="{point["lat_wgs84"]:.8f}" '
                f'lon="{point["lon_wgs84"]:.8f}">\n'
            )
            if point_times is not None:
                self.handle.write(f"        <time>{point_times[index]}</time>\n")
                self.timed_point_count += 1
            self.handle.write("      </trkpt>\n")
        self.handle.write("    </trkseg>\n")
        self.handle.write("  </trk>\n")
        self.track_count += 1
        self.point_count += len(points)

    def close(self):
        self.handle.write("</gpx>\n")
        self.handle.close()


def validate_gpx(path: Path, expected: dict) -> dict:
    root = ET.parse(path).getroot()
    tracks = root.findall(f".//{{{NS}}}trk")
    points = root.findall(f".//{{{NS}}}trkpt")
    times = root.findall(f".//{{{NS}}}trkpt/{{{NS}}}time")
    if len(tracks) != expected["track_count"]:
        raise RuntimeError(f"{path.name}: track count mismatch")
    if len(points) != expected["point_count"]:
        raise RuntimeError(f"{path.name}: point count mismatch")
    if len(times) != expected["timed_point_count"]:
        raise RuntimeError(f"{path.name}: timestamp count mismatch")
    for track in tracks:
        track_times = [node.text for node in track.findall(f".//{{{NS}}}time")]
        if len(set(track_times)) != len(track_times):
            raise RuntimeError(f"{path.name}: duplicate timestamps in one track")
    for point in points:
        lat = float(point.attrib["lat"])
        lon = float(point.attrib["lon"])
        if not (
            math.isfinite(lat)
            and math.isfinite(lon)
            and -90 <= lat <= 90
            and -180 <= lon <= 180
        ):
            raise RuntimeError(f"{path.name}: invalid coordinate")
    return {
        "file": path.name,
        "sha256": sha256(path),
        "track_count": len(tracks),
        "point_count": len(points),
        "timed_point_count": len(times),
        "unique_timestamp_count": len({node.text for node in times}),
    }


def _write_readme(destination: Path, report: dict):
    import_dir = destination / "import_ready"
    files = "\n".join(
        f"{index:02d}. {item['file']}"
        for index, item in enumerate(report["import_files"], start=1)
    )
    text = f"""安卓版一生足迹兼容 GPX（可选输出）

按顺序逐个导入：
{files}

导入包：{import_dir}

可匹配足迹：{report['matched_track_count']}
可匹配点：{report['matched_point_count']}
未匹配足迹：{report['unmatched_track_count']}
未匹配点：{report['unmatched_point_count']}

时间说明
* 每条轨迹的第一点使用百度行程记录中可核对的真实起点。
* 原始 CSV 没有逐点采样时间，speed_raw 的单位也未验证。后续点只使用
  每秒递增的兼容序号，并避开已占用的时间，避免安卓版一生足迹把
  同时间的点合并；这些时间不是原始采样时间或速度推算结果。
* 只有行程起点是真实时间。不同轨迹若有相同真实起点，会保留相同起点。
  一个轨迹文件或单轨迹样本已验证可显示；App 是否会对不同轨迹的相同
  起点做全局合并仍需实机确认，不能为了规避该未知行为改动真实起点。
* 未匹配轨迹放在 review_required，未填写时间，不要导入 App。
* 若轨迹跨过当地午夜，是兼容序号分配后的结果，可从 manifest 汇总查看。

隐私
* 本目录只应保留在本机，不要提交到 Git。
* GPX 和 manifest 默认不写原始 UUID、地点指纹或地址。
* 原始 CSV 只读；生成前后会核对 SHA-256。
"""
    (destination / "README_中文.txt").write_text(text, encoding="utf-8-sig")
    (destination / "导入顺序.txt").write_text(
        "按文件名顺序导入 import_ready 中的 GPX。\n" + files + "\n",
        encoding="utf-8-sig",
    )


def export(
    *,
    raw_dir: Path,
    state_db: Path,
    output_dir: Path,
    prefix: str,
    timezone_offset_hours: float,
    time_mode: str | None,
    max_points: int = DEFAULT_BATCH_POINTS,
    pilot_track: str | None = None,
) -> dict:
    if time_mode != "matched-start-order":
        raise ValueError(
            "StepLife output requires explicit --time-mode matched-start-order "
            "because the source has no per-point timestamps and speed_raw has "
            "no verified unit"
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", prefix):
        raise ValueError("prefix may contain only letters, numbers, underscores and hyphens")
    if max_points < 2:
        raise ValueError("max_points must be at least 2")

    selected, variants = selected_raw_files(raw_dir)
    if not selected:
        raise ValueError("No UUID .csv/.csv.gz files found in raw_dir")
    mappings = load_trip_mappings(state_db, timezone_offset_hours)
    matched = sorted(
        (key for key in selected if key in mappings),
        key=lambda key: (mappings[key]["trip_local"], key),
    )
    unmatched = sorted(key for key in selected if key not in mappings)
    if not matched:
        raise ValueError("No tracks have a verifiable trip-record timestamp")

    if pilot_track:
        pilot_key = pilot_track.lower()
        if pilot_key not in matched:
            raise ValueError("pilot_track must name a matched UUID")
    else:
        pilot_key = next(
            (
                key
                for key in matched
                if int(convert.parse_track(selected[key])["point_count"]) >= 10
            ),
            matched[0],
        )

    before = {str(path): sha256(path) for paths in variants.values() for path in paths}
    local_tz = timezone(timedelta(hours=timezone_offset_hours))
    protected_starts = {
        int(mappings[key]["trip_utc"].timestamp()) for key in matched
    }
    reserved: set[int] = set()
    duplicate_verified_start_count = len(matched) - len(protected_starts)
    allocation_stats = {
        "compatibility_span_seconds": 0,
        "assigned_span_seconds": 0,
        "max_forward_drift_seconds": 0,
        "crossing_midnight_tracks": 0,
    }

    root = output_dir / "steplife"
    destination = root / prefix
    if destination.exists():
        destination = root / "revisions" / (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            + "-"
            + uuid.uuid4().hex[:8]
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{prefix}.building-{uuid.uuid4().hex}"
    staging.mkdir()
    import_dir = staging / "import_ready"
    review_dir = staging / "review_required"
    import_dir.mkdir()
    review_dir.mkdir()
    outputs = []

    def parse_key(key: str) -> tuple[dict, list[dict]]:
        track = parse_uuid_track(selected[key], variants[key])
        return track, track["points"]

    try:
        track, points = parse_key(pilot_key)
        label = "StepLife pilot"
        times, stats = allocate_times(
            pilot_key, points, mappings[pilot_key], reserved, protected_starts, local_tz
        )
        for name, value in stats.items():
            if name == "crosses_local_midnight":
                allocation_stats["crossing_midnight_tracks"] += int(value)
            else:
                allocation_stats[name] = max(allocation_stats[name], value)
        writer = GpxWriter(
            import_dir / "steplife_00_pilot.gpx",
            "StepLife compatibility pilot",
            "One complete source track; only the first timestamp comes from the trip record.",
            "real_trip_start_plus_compatibility_order",
        )
        writer.add_track(label, points, times)
        expected = {
            "track_count": 1,
            "point_count": len(points),
            "timed_point_count": len(points),
        }
        writer.close()
        outputs.append(validate_gpx(writer.path, expected))

        remaining = [key for key in matched if key != pilot_key]
        batch_number = 1
        writer = None
        expected = None
        for ordinal, key in enumerate(remaining, start=2):
            _, points = parse_key(key)
            if writer is not None and writer.point_count + len(points) > max_points:
                writer.close()
                outputs.append(validate_gpx(writer.path, expected))
                writer = None
            if writer is None:
                writer = GpxWriter(
                    import_dir / f"steplife_{batch_number:02d}.gpx",
                    f"StepLife compatibility batch {batch_number:02d}",
                    "Optional Android StepLife output; later timestamps are compatibility-only.",
                    "real_trip_start_plus_compatibility_order",
                )
                expected = {
                    "track_count": 0,
                    "point_count": 0,
                    "timed_point_count": 0,
                }
                batch_number += 1
            times, stats = allocate_times(
                key, points, mappings[key], reserved, protected_starts, local_tz
            )
            for name, value in stats.items():
                if name == "crosses_local_midnight":
                    allocation_stats["crossing_midnight_tracks"] += int(value)
                else:
                    allocation_stats[name] = max(allocation_stats[name], value)
            writer.add_track(f"StepLife track {ordinal:04d}", points, times)
            expected["track_count"] += 1
            expected["point_count"] += len(points)
            expected["timed_point_count"] += len(points)
        if writer is not None:
            writer.close()
            outputs.append(validate_gpx(writer.path, expected))

        review_path = review_dir / "review_no_verified_start.gpx"
        writer = GpxWriter(
            review_path,
            "StepLife review tracks",
            "No verified trip-record start was found; timestamps are intentionally absent.",
            "no_matched_timestamp_time_intentionally_omitted",
        )
        unmatched_points = 0
        for ordinal, key in enumerate(unmatched, start=1):
            _, points = parse_key(key)
            writer.add_track(f"Review track {ordinal:04d}", points, None)
            unmatched_points += len(points)
        writer.close()
        review_output = validate_gpx(
            review_path,
            {
                "track_count": len(unmatched),
                "point_count": unmatched_points,
                "timed_point_count": 0,
            },
        )
        outputs.append(review_output)
    except Exception:
        if "writer" in locals() and writer is not None and not writer.handle.closed:
            writer.handle.close()
        raise

    after = {str(path): sha256(path) for paths in variants.values() for path in paths}
    if before != after:
        raise RuntimeError("Source CSV changed during StepLife export")
    matched_points = sum(item["point_count"] for item in outputs if item["timed_point_count"])
    expected_unique_times = matched_points - duplicate_verified_start_count
    if len(reserved) != expected_unique_times:
        raise RuntimeError("StepLife timestamp allocation is inconsistent")

    import_files = [item for item in outputs if item["timed_point_count"]]
    report = {
        "status": (
            "generated; optional StepLife compatibility GPX requires an "
            "import check in the target app"
        ),
        "source_file_count": len(before),
        "source_track_count": len(selected),
        "matched_track_count": len(matched),
        "matched_point_count": matched_points,
        "unique_timestamp_count": len(reserved),
        "duplicate_verified_start_count": duplicate_verified_start_count,
        "unmatched_track_count": len(unmatched),
        "unmatched_point_count": unmatched_points,
        "import_file_count": len(import_files),
        "review_file_count": 1,
        "import_files": import_files,
        "review_file": review_output,
        "time_policy": {
            "mode": time_mode,
            "matched_start": "verified Baidu trip-record start",
            "later_points": (
                "monotonic compatibility offsets assigned one second apart; "
                "collisions after the verified start are skipped"
            ),
            "not_claimed": (
                "later point timestamps are neither observed sample times nor "
                "distance/speed estimates"
            ),
            "duplicate_starts": (
                "equal verified trip starts are preserved across separate "
                "tracks; timestamps remain unique within each track"
            ),
            "allocation_stats": allocation_stats,
        },
        "privacy": {
            "contains_source_uuids": False,
            "contains_location_fingerprints": False,
            "contains_source_paths": False,
            "source_files_unchanged": True,
        },
        "delivery_directory": str(destination.resolve()),
    }
    (staging / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_readme(staging, report)
    staging.replace(destination)
    report["delivery_directory"] = str(destination.resolve())
    (destination / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report
