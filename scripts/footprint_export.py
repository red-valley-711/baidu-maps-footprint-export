"""Portable entrypoint for the session-proven Baidu UUID CSV workflow.

No network API, login credentials, root, or privileged app-data access is used.
Export/plan/test are offline; collect and pull-cache require an authorized device.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import shutil
import tempfile
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import baidu_adb_collector as core
import parse_baidu_hex_tracks as convert
import steplife_gpx

SCRIPT_DIR = Path(__file__).resolve().parent
UUID_CSV = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\.csv(?:\.gz)?")
FORMATS = ("geojson", "gpx", "kml", "points-csv", "summary-csv")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def month(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value):
        raise argparse.ArgumentTypeError("Use YYYY-MM, years 2000–2099")
    return int(value[:4]), int(value[5:])


def month_range(start: tuple[int, int], end: tuple[int, int]):
    if start < end:
        raise ValueError("--start must be at or after --end")
    year, number = start
    while (year, number) >= end:
        yield year, number
        year, number = (year - 1, 12) if number == 1 else (year, number - 1)


def configure(args) -> dict:
    config_path = Path(args.config).resolve() if args.config else None
    config = read_json(config_path) if config_path else {}
    allowed = {"workspace", "raw_dir", "state_dir", "output_dir", "adb", "device",
               "cache_dir", "formats", "prefix", "timezone_offset_hours", "profile"}
    if unknown := set(config) - allowed:
        raise ValueError(f"Unknown config keys: {sorted(unknown)}")
    base = config_path.parent if config_path else Path.cwd()

    def location(name, default, relative_to):
        value = getattr(args, name, None) or config.get(name, default)
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (relative_to / path).resolve()

    root = location("workspace", ".", base)
    config.update(workspace=root, raw_dir=location("raw_dir", "raw", root),
                  state_dir=location("state_dir", "state", root),
                  output_dir=location("output_dir", "qgis", root))
    for name, default in (("adb", "adb"), ("device", "emulator-5554"),
                          ("cache_dir", ""), ("formats", "geojson,gpx,kml"),
                          ("prefix", "baidu_tracks"), ("timezone_offset_hours", 0)):
        value = getattr(args, name, None)
        config[name] = value if value is not None else config.get(name, default)
    # Explicit executable paths in config are relative to the config file.
    if ("/" in config["adb"] or "\\" in config["adb"]) and not Path(config["adb"]).is_absolute():
        config["adb"] = str((base / config["adb"]).resolve())
    profile_path = getattr(args, "profile", None) or config.get("profile")
    if profile_path:
        path = Path(profile_path)
        config["profile"] = path if path.is_absolute() else base / path
    else:
        config["profile"] = SCRIPT_DIR.parent / "references/ldplayer-720x1280.json"
    if not re.fullmatch(r"[A-Za-z0-9_-]+", config["prefix"]):
        raise ValueError("prefix may contain only letters, numbers, underscores and hyphens")
    config["formats"] = list(dict.fromkeys(config["formats"].split(",")))
    if not config["formats"] or set(config["formats"]) - set(FORMATS):
        raise ValueError(f"formats must be a comma-separated subset of {FORMATS}")
    if not -12 <= float(config["timezone_offset_hours"]) <= 14:
        raise ValueError("timezone offset must be between -12 and 14")
    # All three trees must be disjoint, including nesting/aliases. In particular,
    # raw_dir == state_dir/pulls must never make a download its own source file.
    for first, second in (("raw_dir", "state_dir"), ("raw_dir", "output_dir"), ("state_dir", "output_dir")):
        a, b = config[first], config[second]
        if a == b or a in b.parents or b in a.parents:
            raise ValueError(f"{first} and {second} must be separate, non-nested directories")
    core.ROOT = root
    core.ADB = config["adb"]
    core.CACHE_DIR = config["cache_dir"]
    core.RAW_DIR = config["raw_dir"]
    core.STATE_DIR = config["state_dir"]
    core.PULL_DIR = core.STATE_DIR / "pulls"
    core.DB_PATH = core.STATE_DIR / "collector.sqlite3"
    core.LOG_PATH = core.STATE_DIR / "events.jsonl"
    core.STATUS_PATH = core.STATE_DIR / "status.json"
    return config


def raw_files(folder: Path) -> list[Path]:
    return sorted(path for path in folder.iterdir() if path.is_file() and UUID_CSV.fullmatch(path.name)) if folder.exists() else []


def track_id(path: Path) -> str:
    return path.name.removesuffix(".gz").removesuffix(".csv").lower()


def export(config: dict) -> dict:
    baseline_path = config["state_dir"] / "baseline_snapshot.json"
    baseline = read_json(baseline_path) if baseline_path.exists() else None
    baseline_ids = baseline.get("track_ids") if isinstance(baseline, dict) else None
    if baseline_ids is not None and (not isinstance(baseline_ids, list) or not all(
        isinstance(value, str) and UUID_CSV.fullmatch(value + ".csv") for value in baseline_ids
    )):
        raise ValueError("baseline.track_ids must be a list of UUID strings; outputs were not changed")
    if isinstance(baseline, dict) and "file_count" in baseline and (
        not isinstance(baseline["file_count"], int) or isinstance(baseline["file_count"], bool) or baseline["file_count"] < 0
    ):
        raise ValueError("baseline.file_count must be a nonnegative integer; outputs were not changed")
    files = raw_files(config["raw_dir"])
    if not files:
        raise ValueError("No UUID .csv/.csv.gz files found in raw_dir")
    selected = {}
    variants = {}
    for path in files:
        uuid = track_id(path)
        variants.setdefault(uuid, []).append(path)
        old = selected.get(uuid)
        if old and path.suffix == old.suffix:
            raise ValueError(f"Ambiguous UUID with different filename case: {path.name}")
        if not old or old.name.endswith(".gz"):
            selected[uuid] = path
    tracks = []
    for uuid, path in sorted(selected.items()):
        parsed = convert.parse_track(path)
        parsed["track_id"] = uuid
        # Equivalent CSV and gzip copies must contain the same actual rows.
        for other in variants[uuid]:
            if other != path:
                if convert.parse_track(other)["points"] != parsed["points"]:
                    raise ValueError(f"Conflicting CSV/gzip copies for UUID {uuid}")
        tracks.append(parsed)
    out = config["output_dir"]
    out.mkdir(parents=True, exist_ok=True)
    prefix = config["prefix"]
    suffixes = {"geojson": ".geojson", "gpx": ".gpx", "kml": ".kml",
                "points-csv": "_points.csv", "summary-csv": "_summary.csv"}
    writers = {"geojson": convert.write_geojson, "gpx": convert.write_gpx,
               "kml": convert.write_kml, "points-csv": convert.write_points_csv,
               "summary-csv": convert.write_summary_csv}
    convert.SOURCE_DEVICE_PATH = config["cache_dir"] or "not specified (local CSV export)"
    convert.LOCAL_TZ = timezone(timedelta(hours=float(config["timezone_offset_hours"])))
    # parse_track used this timezone while deriving mtime strings.
    for item in tracks:
        item["cache_mtime_local"] = datetime.fromisoformat(item["cache_mtime_utc"].replace("Z", "+00:00")).astimezone(convert.LOCAL_TZ).isoformat(timespec="seconds")
    names = [prefix + suffixes[fmt] for fmt in config["formats"]]
    if any((out / name).exists() for name in names + [f"{prefix}_manifest.json"]):
        out = out / "revisions" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid_module.uuid4().hex[:8])
        out.mkdir(parents=True, exist_ok=False)
    for fmt, name in zip(config["formats"], names):
        destination = out / name
        descriptor, temp_name = tempfile.mkstemp(prefix=name + ".", suffix=".tmp", dir=out)
        os.close(descriptor)
        temporary = Path(temp_name)
        writers[fmt](tracks, temporary)
        # Exclusive creation also protects against a concurrent export. If a copy
        # fails, its new partial file and the complete temporary remain recoverable.
        with temporary.open("rb") as source, destination.open("xb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        temporary.unlink()
    convert.SELECTED_OUTPUTS = names
    convert.SOURCE_FILE_COUNT = len(files)
    manifest_path = out / f"{prefix}_manifest.json"
    descriptor, temp_name = tempfile.mkstemp(prefix=prefix + "_manifest.", suffix=".tmp", dir=out)
    os.close(descriptor)
    temporary = Path(temp_name)
    convert.write_manifest(tracks, list(selected.values()), temporary)
    manifest = read_json(temporary)
    manifest["added_unique_tracks"] = len(set(selected) - set(baseline_ids)) if baseline_ids is not None else None
    manifest["added_files"] = len(files) - baseline["file_count"] if baseline and "file_count" in baseline else None
    manifest["outputs"] = [str((out / name).resolve()) for name in names]
    manifest["sha256"] = {name: convert.sha256(out / name) for name in names}
    manifest["collection_scope_note"] = "CSV export completeness is not proof of cloud/account completeness."
    manifest["access_method"] = "Read-only conversion of local CSV/gzip files; their acquisition method is not verified by this command."
    manifest["collection_months"] = None
    manifest["unresolved_records"] = None
    db_path = config["state_dir"] / "collector.sqlite3"
    if db_path.exists():
        connection = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
        try:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "collection_months" in tables:
                manifest["collection_months"] = [dict(year=y, month=m, status=s) for y, m, s in connection.execute("SELECT year,month,status FROM collection_months ORDER BY year DESC,month DESC")]
            if "records" in tables:
                manifest["unresolved_records"] = connection.execute("SELECT COUNT(*) FROM records WHERE status IN ('server_only_or_slow','retry_pending')").fetchone()[0]
        finally:
            connection.close()
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with temporary.open("rb") as source, manifest_path.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    temporary.unlink()
    return manifest


def steplife_export(
    config: dict,
    *,
    time_mode: str,
    max_points: int = steplife_gpx.DEFAULT_BATCH_POINTS,
    pilot_track: str | None = None,
) -> dict:
    return steplife_gpx.export(
        raw_dir=config["raw_dir"],
        state_db=config["state_dir"] / "collector.sqlite3",
        output_dir=config["output_dir"],
        prefix=config["prefix"],
        timezone_offset_hours=float(config["timezone_offset_hours"]),
        time_mode=time_mode,
        max_points=max_points,
        pilot_track=pilot_track,
    )


def next_month(connection, start, end, excluded=(), only=None):
    allowed = list(month_range(start, end))
    excluded = set(excluded)
    allowed = [key for key in allowed if key not in excluded and (only is None or key in only)]
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_months'").fetchone()
    states = {(y, m): status for y, m, status in connection.execute("SELECT year, month, status FROM collection_months")} if exists else {}
    return next((key for key in allowed if states.get(key) != "complete"), None)


def queue_retries(connection, start, end, excluded=()):
    allowed = set(month_range(start, end)) - set(excluded)
    queued = set()
    for fp, year, base, status in connection.execute(
        "SELECT fingerprint, year_label, legacy_fingerprint, status FROM records WHERE status IN ('server_only_or_slow', 'retry_pending')"
    ).fetchall():
        match = re.match(r"(\d{1,2})月", base or "")
        if not year or not match:
            continue
        key = (int(year), int(match.group(1)))
        if key not in allowed:
            continue
        connection.execute("UPDATE records SET status='retry_pending' WHERE fingerprint=?", (fp,))
        connection.execute("INSERT INTO collection_months(year, month, status) VALUES(?, ?, 'pending') ON CONFLICT(year,month) DO UPDATE SET status='pending'", key)
        queued.add(key)
    connection.commit()
    return queued


def make_baseline(config):
    path = config["state_dir"] / "baseline_snapshot.json"
    if path.exists():
        return
    files = raw_files(config["raw_dir"])
    path.write_text(json.dumps({"created_at_utc": core.now_utc(), "file_count": len(files),
                               "track_ids": sorted({track_id(p) for p in files})}, indent=2), encoding="utf-8")


def preflight(config):
    if os.name != "nt":
        raise RuntimeError("Live collection profile requires Windows OCR; offline export works cross-platform")
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", config["cache_dir"]) or ".." in config["cache_dir"].split("/"):
        raise ValueError("Provide the verified absolute Android cache_dir; shell metacharacters are rejected")
    device = config["device"]
    if core.run_adb(device, "get-state").decode().strip() != "device":
        raise RuntimeError("Device is not connected/authorized")
    output = core.shell(device, "wm", "size")
    sizes = re.findall(r"(\d+)x(\d+)", output)
    profile = read_json(config["profile"])
    if not sizes or list(map(int, sizes[-1])) != profile["screen_size"]:
        raise RuntimeError("Screen resolution differs from profile. Recalibrate visually; do not change device settings automatically")
    if profile["screen_size"] != [720, 1280]:
        raise RuntimeError("Only the observed 720x1280 card/list geometry has been verified")
    core.cache_listing(device)  # Readability is required, never escalated with su/run-as.
    return profile


def configure_months(config, profile):
    import baidu_adb_month_collector as months
    months.STATE_DIR = core.STATE_DIR
    months.DB_PATH = core.DB_PATH
    for name in ("YEAR_WHEEL_X", "MONTH_WHEEL_X", "WHEEL_Y", "WHEEL_STEP", "YEAR_CROP", "MONTH_CROP"):
        setattr(months, name, profile[name.lower()])
    return months


def collect(config, args):
    profile = preflight(config)
    months = configure_months(config, profile)
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    core.LIST_SETTLE_SECONDS = args.settle_seconds
    core.rebuild_qgis = lambda: export(config)
    with core.collector_lock():
        connection = core.open_db(args.legacy_year)
        months.create_month_table(connection)
        make_baseline(config)
        only = queue_retries(connection, args.start, args.end, args.exclude_month) if args.retry_unresolved else None
        if args.audit_months:
            for key in month_range(args.start, args.end):
                if key not in args.exclude_month:
                    connection.execute("UPDATE collection_months SET status='pending' WHERE year=? AND month=?", key)
            connection.commit()
        session = core.now_utc()
        session_state = {"actions": 0, "last_result": None}
        # Retry-only mode never opens known-successful or unrelated cards.
        if args.retry_unresolved:
            load = core.load_processed
            def load_retry_processed(db):
                result = load(db)
                result["retry_targets"] = {r[0] for r in db.execute("SELECT stable_fingerprint FROM records WHERE status='retry_pending'")}
                return result
            is_processed = core.card_is_processed
            core.load_processed = load_retry_processed
            core.card_is_processed = lambda card, done: card["stable_fingerprint"] not in done["retry_targets"] or is_processed(card, done)
        month_count = 0
        options = SimpleNamespace(file_timeout=args.file_timeout, ui_timeout=args.ui_timeout,
                                  poll_seconds=args.poll_seconds, stagnant_scrolls=args.stagnant_scrolls,
                                  qgis_every=args.qgis_every, no_qgis=args.no_qgis, max_records=args.max_records)
        reason = "date_range_complete"
        target = None
        try:
            while target := next_month(connection, args.start, args.end, args.exclude_month, only):
                months.begin_month(connection, *target)
                _, _, completed = months.run_month(config["device"], connection, *target, options, session, session_state)
                if not completed:
                    reason = "max_records_reached"
                    break
                month_count += 1
                if args.max_months and month_count >= args.max_months:
                    reason = "max_months_reached"
                    break
            if not args.no_qgis and raw_files(config["raw_dir"]):
                export(config)
            core.write_status(connection, config["device"], session, None, "stopped", reason)
            core.append_event({"event": "month_collector_stopped", "reason": reason,
                               "session_actions": session_state["actions"], "at_utc": core.now_utc()})
        except BaseException as exc:
            if target:
                months.fail_month(connection, *target, exc)
            state = "needs_user" if isinstance(exc, core.AccountActionRequired) else "record_error"
            core.write_status(connection, config["device"], session, {"month": target}, state, str(exc))
            raise
        finally:
            # Interrupted targeted retries retain metadata and become eligible for a later explicit retry.
            connection.execute("UPDATE records SET status='server_only_or_slow' WHERE status='retry_pending'")
            connection.commit()
            connection.close()
    print(json.dumps({"reason": reason, "actions": session_state["actions"], "state_dir": str(core.STATE_DIR)}, ensure_ascii=False))


def plan(config, args):
    path = config["state_dir"] / "collector.sqlite3"
    if not path.exists():
        allowed = [key for key in month_range(args.start, args.end) if key not in args.exclude_month]
        print(json.dumps({"database_exists": False, "next_month": next(iter(allowed), None), "months": len(allowed)}, ensure_ascii=False))
        return
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns = {r[1] for r in connection.execute("PRAGMA table_info(records)")}
        fields = ["sequence" if "sequence" in columns else "rowid", "status", "year_label" if "year_label" in columns else "NULL", "stable_fingerprint" if "stable_fingerprint" in columns else "fingerprint"]
        result = {"database_exists": True,
                  "records": dict(connection.execute("SELECT status, COUNT(*) FROM records GROUP BY status")) if "records" in tables else {},
                  "months": dict(connection.execute("SELECT status, COUNT(*) FROM collection_months GROUP BY status")) if "collection_months" in tables else {},
                  "legacy_schema": "stable_fingerprint" not in columns or "collection_months" not in tables,
                  "next_month": next_month(connection, args.start, args.end, args.exclude_month),
                  "last_nodes": connection.execute("SELECT " + ",".join(fields) + " FROM records ORDER BY " + fields[0] + " DESC LIMIT 5").fetchall() if "records" in tables else []}
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        connection.close()


def pull_cache(config):
    preflight(config)
    core.STATE_DIR.mkdir(parents=True, exist_ok=True)
    count = 0
    with core.collector_lock():
        make_baseline(config)
        first = {r["name"]: r for r in core.cache_listing(config["device"])}
        import time
        time.sleep(1)
        second = core.cache_listing(config["device"])
        for info in second:
            if not UUID_CSV.fullmatch(info["name"]):
                continue
            if not info["size"] or first.get(info["name"]) != info:
                raise RuntimeError("Cache is still changing; wait for the App and rerun pull-cache")
            _, status, _ = core.pull_new_file(config["device"], info)
            count += status == "pulled"
    print(json.dumps({"new_files": count, "raw_dir": str(core.RAW_DIR)}))


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("export", "steplife", "plan", "collect", "pull-cache"):
        p = sub.add_parser(name)
        p.add_argument("--config", help="JSON configuration (relative paths resolve from this file)")
        for option in ("workspace", "raw-dir", "state-dir", "output-dir", "adb", "device", "cache-dir", "prefix", "profile"):
            p.add_argument("--" + option)
        p.add_argument("--formats", help="geojson,gpx,kml,points-csv,summary-csv")
        p.add_argument("--timezone-offset-hours", type=float)
        if name == "steplife":
            p.add_argument(
                "--time-mode",
                required=True,
                choices=("matched-start-order",),
                help=(
                    "Required explicit opt-in: preserve the matched trip start "
                    "and assign later points monotonic compatibility offsets"
                ),
            )
            p.add_argument("--max-points", type=int, default=steplife_gpx.DEFAULT_BATCH_POINTS)
            p.add_argument("--pilot-track", help="Optional matched UUID to isolate as the pilot")
        if name in ("collect", "plan"):
            p.add_argument("--start", type=month, required=True, help="Newest month, inclusive")
            p.add_argument("--end", type=month, required=True, help="Oldest month, inclusive")
            p.add_argument("--exclude-month", type=month, action="append", default=[])
        if name == "collect":
            p.add_argument("--legacy-year", help="Explicit year for a backed-up legacy unversioned DB")
            p.add_argument("--file-timeout", type=float, default=20)
            p.add_argument("--ui-timeout", type=float, default=180)
            p.add_argument("--poll-seconds", type=float, default=0.4)
            p.add_argument("--settle-seconds", type=float, default=30)
            p.add_argument("--stagnant-scrolls", type=int, default=3)
            p.add_argument("--qgis-every", type=int, default=500)
            p.add_argument("--max-records", type=int, default=0, help="5 for the initial benchmark; 0 is unlimited")
            p.add_argument("--max-months", type=int, default=0)
            p.add_argument("--no-qgis", action="store_true")
            p.add_argument("--retry-unresolved", action="store_true", help="One bounded retry pass, only unresolved records within requested dates")
            p.add_argument("--audit-months", action="store_true", help="Revisit completed months while skipping registered cards")
    args = parser.parse_args()
    if args.command in ("collect", "plan"):
        list(month_range(args.start, args.end))
    if args.command == "collect":
        for name in ("file_timeout", "ui_timeout", "poll_seconds", "settle_seconds", "stagnant_scrolls"):
            if getattr(args, name) <= 0:
                parser.error(f"{name} must be positive")
        for name in ("qgis_every", "max_records", "max_months"):
            if getattr(args, name) < 0:
                parser.error(f"{name} must be nonnegative")
        if args.retry_unresolved and args.audit_months:
            parser.error("Choose retry-unresolved or audit-months separately")
        if args.legacy_year and not re.fullmatch(r"20\d{2}", args.legacy_year):
            parser.error("legacy-year must be a four-digit year between 2000 and 2099")
    if args.command == "steplife" and args.max_points < 2:
        parser.error("max-points must be at least 2")
    config = configure(args)
    if args.command == "export":
        print(json.dumps(export(config), ensure_ascii=False, indent=2))
    elif args.command == "steplife":
        print(
            json.dumps(
                steplife_export(
                    config,
                    time_mode=args.time_mode,
                    max_points=args.max_points,
                    pilot_track=args.pilot_track,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.command == "plan":
        plan(config, args)
    elif args.command == "pull-cache":
        pull_cache(config)
    else:
        collect(config, args)


if __name__ == "__main__":
    # Keep JSON output readable by callers regardless of the Windows console codepage.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        cli()
    except core.AccountActionRequired as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
