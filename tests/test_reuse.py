"""Offline regression tests. Never invokes ADB or touches a user's export."""
from __future__ import annotations

import argparse
import gzip
import importlib
import json
import math
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import footprint_export as app
import baidu_adb_collector as core
import audit_public_tree as public_tree

A = "00000000-0000-4000-8000-000000000001"
B = "00000000-0000-4000-8000-000000000002"
C = "00000000-0000-4000-8000-000000000003"
ROWS5 = "12958000,4826000,1.5,90,5\n12958010,4826010,1.7,92,5\n"
ROWS7 = "12958020,4826020,2,91,6,0,1\n12958030,4826030,3,93,6,0,1\n"
ROWS5C = "12958040,4826040,2.2,94,6\n12958050,4826050,2.4,96,6\n"


def ui(bottom=1253, year="2022", full_address=True):
    extra = '<node text="目的地"/>' if full_address else ''
    return ET.fromstring(f'''<hierarchy><node bounds="[0,0][720,1280]" text="我的足迹记录">
      <node text="{year}年" bounds="[0,200][720,230]"/>
      <node package="com.baidu.BaiduMap" clickable="true" long-clickable="true" bounds="[5,1000][715,{bottom}]">
        <node text="01月15日 08:30"/><node text="1.2公里"/><node text="起点"/>{extra}
      </node></node></hierarchy>''')


def config(root, formats="geojson,gpx,kml"):
    args = argparse.Namespace(config=None, workspace=str(root), raw_dir="input",
                              state_dir="state", output_dir="result", formats=formats,
                              prefix="alternate", timezone_offset_hours=-5)
    return app.configure(args)


def db():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE collection_months(year INTEGER,month INTEGER,status TEXT,PRIMARY KEY(year,month))")
    return connection


class ReuseTests(unittest.TestCase):
    def test_another_directory_prefix_formats_and_timezone(self):
        with tempfile.TemporaryDirectory(prefix="footprint-reuse-") as tmp:
            root = Path(tmp)
            conf = config(root)
            conf["raw_dir"].mkdir()
            first = conf["raw_dir"] / (A + ".csv")
            first.write_text(ROWS5, encoding="utf-8")
            (conf["raw_dir"] / (B + ".csv.gz")).write_bytes(gzip.compress(ROWS7.encode()))
            (conf["raw_dir"] / (A + ".csv.gz")).write_bytes(gzip.compress(ROWS5.encode()))
            (conf["raw_dir"] / "unrelated.csv").write_text("not a track")
            before = {p.name: core.sha256(p) for p in conf["raw_dir"].iterdir()}
            conf["state_dir"].mkdir()
            app.make_baseline(conf)
            # A later file is newly added relative to a deliberately different baseline.
            baseline = conf["state_dir"] / "baseline_snapshot.json"
            baseline.write_text(json.dumps({"track_ids": [A]}), encoding="utf-8")
            manifest = app.export(conf)
            self.assertEqual(manifest["source_files"], 3)
            self.assertEqual(manifest["track_count"], 2)
            self.assertEqual(manifest["point_count"], 4)
            self.assertEqual(manifest["added_unique_tracks"], 1)
            geo = json.loads((conf["output_dir"] / "alternate.geojson").read_text(encoding="utf-8"))
            gpx = ET.parse(conf["output_dir"] / "alternate.gpx")
            kml = ET.parse(conf["output_dir"] / "alternate.kml")
            self.assertEqual(len(geo["features"]), 2)
            self.assertEqual(len(gpx.findall(".//{*}trk")), 2)
            self.assertEqual(len(kml.findall(".//{*}Placemark")), 2)
            self.assertEqual(len(gpx.findall(".//{*}trkpt")), 4)
            self.assertFalse(gpx.findall(".//{*}time"))
            for feature in geo["features"]:
                self.assertTrue(feature["properties"]["cache_mtime_local"].endswith("-05:00"))
                self.assertTrue(all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in feature["geometry"]["coordinates"]))
            self.assertEqual(before, {p.name: core.sha256(p) for p in conf["raw_dir"].iterdir()})
            # CLI from an unrelated cwd and a JSON config whose workspace is relative.
            cfg_path = root / "second.json"
            cfg_path.write_text(json.dumps({"workspace": ".", "raw_dir": "input", "output_dir": "another-output", "state_dir": "another-state"}), encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPTS / "footprint_export.py"), "export", "--config", str(cfg_path), "--formats", "kml,summary-csv", "--prefix", "second", "--timezone-offset-hours", "0"], cwd=root.parent, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            result2 = json.loads(result.stdout)
            self.assertEqual(result2["track_count"], 2)
            self.assertEqual({p.name for p in (root / "another-output").iterdir()}, {"second.kml", "second_summary.csv", "second_manifest.json"})

    def test_conflicting_csv_gzip_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = config(Path(tmp))
            conf["raw_dir"].mkdir()
            (conf["raw_dir"] / (A + ".csv")).write_text(ROWS5)
            (conf["raw_dir"] / (A + ".csv.gz")).write_bytes(gzip.compress(ROWS7.encode()))
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                app.export(conf)

    def test_optional_steplife_output_is_private_and_non_destructive(self):
        with tempfile.TemporaryDirectory(prefix="footprint-steplife-") as tmp:
            root = Path(tmp)
            conf = config(root, formats="geojson")
            conf["raw_dir"].mkdir()
            conf["state_dir"].mkdir()
            for key, rows in ((A, ROWS5), (B, ROWS7), (C, ROWS5C)):
                (conf["raw_dir"] / f"{key}.csv").write_text(rows, encoding="utf-8")
            db_path = conf["state_dir"] / "collector.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.execute(
                "CREATE TABLE records(new_file TEXT, fingerprint TEXT)"
            )
            connection.executemany(
                "INSERT INTO records VALUES (?, ?)",
                [
                    (A + ".csv", "2024|01月02日 08:30|synthetic-place"),
                    (B + ".csv", "2024|01月02日 08:30|synthetic-place"),
                ],
            )
            connection.commit()
            connection.close()
            source_before = {
                path.name: core.sha256(path)
                for path in conf["raw_dir"].iterdir()
            }

            report = app.steplife_export(
                conf,
                time_mode="matched-start-order",
                max_points=2,
                pilot_track=A,
            )
            destination = conf["output_dir"] / report["delivery_directory"]
            self.assertEqual(report["delivery_directory"], "steplife/alternate")
            imports = sorted((destination / "import_ready").glob("*.gpx"))
            review = destination / "review_required" / "review_no_verified_start.gpx"
            self.assertEqual([path.name for path in imports], [
                "steplife_00_pilot.gpx",
                "steplife_01.gpx",
            ])
            import_times = []
            for path in imports:
                parsed = ET.parse(path)
                for track in parsed.findall(".//{*}trk"):
                    track_times = [
                        node.text for node in track.findall(".//{*}time")
                    ]
                    self.assertEqual(len(track_times), len(set(track_times)))
                    import_times.extend(track_times)
            review_xml = ET.parse(review)
            self.assertEqual(len(import_times), 4)
            self.assertEqual(
                import_times,
                [
                    "2024-01-02T13:30:00Z",
                    "2024-01-02T13:30:01Z",
                    "2024-01-02T13:30:00Z",
                    "2024-01-02T13:30:02Z",
                ],
            )
            self.assertEqual(report["duplicate_verified_start_count"], 1)
            self.assertEqual(report["unique_timestamp_count"], 3)
            self.assertEqual(
                len(review_xml.findall(".//{*}trkpt")),
                2,
            )
            self.assertFalse(review_xml.findall(".//{*}time"))
            manifest_text = (destination / "manifest.json").read_text(
                encoding="utf-8"
            )
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["delivery_directory"], "steplife/alternate")
            self.assertFalse(manifest["privacy"]["contains_absolute_local_paths"])
            public_text = "\n".join(
                path.read_text(encoding="utf-8-sig", errors="replace")
                for path in destination.rglob("*")
                if path.is_file()
            )
            for private_value in (A, B, C, "synthetic-place", str(root)):
                self.assertNotIn(private_value, public_text)
            self.assertIn("导入包：import_ready/", public_text)
            self.assertEqual(source_before, {
                path.name: core.sha256(path)
                for path in conf["raw_dir"].iterdir()
            })

            saved_pilot = core.sha256(imports[0])
            second = app.steplife_export(
                conf,
                time_mode="matched-start-order",
                max_points=2,
                pilot_track=A,
            )
            self.assertNotEqual(report["delivery_directory"], second["delivery_directory"])
            self.assertTrue(second["delivery_directory"].startswith("steplife/revisions/"))
            second_destination = conf["output_dir"] / second["delivery_directory"]
            second_text = "\n".join(
                path.read_text(encoding="utf-8-sig", errors="replace")
                for path in second_destination.rglob("*")
                if path.is_file()
            )
            self.assertNotIn(str(root), second_text)
            self.assertEqual(saved_pilot, core.sha256(imports[0]))

    def test_steplife_requires_explicit_time_mode(self):
        with tempfile.TemporaryDirectory(prefix="footprint-steplife-") as tmp:
            conf = config(Path(tmp), formats="geojson")
            with self.assertRaisesRegex(ValueError, "explicit"):
                app.steplife_export(
                    conf,
                    time_mode="trip-start-only",
                    max_points=2,
                )

    def test_public_tree_audit_rejects_local_assets(self):
        with tempfile.TemporaryDirectory(prefix="footprint-audit-") as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("synthetic public text", encoding="utf-8")
            (root / "config.example.json").write_text("{}", encoding="utf-8")
            self.assertEqual(public_tree.audit(root), [])
            (root / "synthetic.csv").write_text("1,2,3", encoding="utf-8")
            (root / "config.local.json").write_text("{}", encoding="utf-8")
            (root / ".env").write_text("placeholder=value", encoding="utf-8")
            (root / "private.md").write_text(
                "-----BEGIN " + "PRIVATE KEY-----",
                encoding="utf-8",
            )
            issues = public_tree.audit(root)
            self.assertEqual(len(issues), 4)

    def test_bad_rows_and_nonfinite_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / (A + ".csv")
            for rows in ("1,2,3\n", "nan,4826000,1,2,3\n", "12958000,4826000,1,2,3\n", "1,2,3,4,5,6\n"):
                path.write_text(rows)
                with self.assertRaises(ValueError):
                    app.convert.parse_track(path)

    def test_bottom_card_and_year_dedup(self):
        card = core.record_cards(ui())[0]
        self.assertEqual(card["bounds"][3], 1253)
        self.assertFalse(core.record_cards(ui(bottom=1280)))
        shortened = core.record_cards(ui(full_address=False))[0]
        previous_year = core.record_cards(ui(year="2021"))[0]
        self.assertEqual(card["stable_fingerprint"], shortened["stable_fingerprint"])
        self.assertNotEqual(card["stable_fingerprint"], previous_year["stable_fingerprint"])
        self.assertTrue(card["stable_fingerprint"].startswith("2022|"))

    def test_range_resume_does_not_escape_or_repeat(self):
        connection = db()
        connection.executemany("INSERT INTO collection_months VALUES(?,?,?)", [(2024, 9, "complete"), (2024, 8, "running"), (2026, 10, "running")])
        self.assertEqual(app.next_month(connection, (2024, 9), (2023, 12)), (2024, 8))
        self.assertEqual(app.next_month(connection, (2024, 9), (2023, 12), [(2024, 8)]), (2024, 7))
        self.assertEqual(list(app.month_range((2024, 1), (2023, 12))), [(2024, 1), (2023, 12)])
        with self.assertRaises(ValueError):
            list(app.month_range((2023, 1), (2024, 1)))
        connection.close()

    def test_retry_preserves_rows_sequences_and_range(self):
        connection = db()
        connection.execute("CREATE TABLE records(fingerprint TEXT,year_label TEXT,legacy_fingerprint TEXT,status TEXT,sequence INTEGER)")
        rows = [("a", "2024", "08月01日 08:00|1公里", "server_only_or_slow", 9),
                ("b", "2024", "08月02日 08:00|1公里", "pulled", 10),
                ("c", "2026", "10月01日 08:00|1公里", "server_only_or_slow", 50)]
        connection.executemany("INSERT INTO records VALUES(?,?,?,?,?)", rows)
        queued = app.queue_retries(connection, (2024, 9), (2024, 1))
        self.assertEqual(queued, {(2024, 8)})
        after = connection.execute("SELECT fingerprint,status,sequence FROM records ORDER BY sequence").fetchall()
        self.assertEqual(after, [("a", "retry_pending", 9), ("b", "pulled", 10), ("c", "server_only_or_slow", 50)])
        self.assertEqual(app.next_month(connection, (2024, 9), (2024, 1), only=queued), (2024, 8))
        connection.close()

    def test_unstable_cache_never_pulled(self):
        listing = iter([[{"name": A + ".csv", "size": 1}], [{"name": A + ".csv", "size": 2}]])
        with patch.object(core.time, "monotonic", side_effect=[0, 0, 1, 3]), patch.object(core.time, "sleep"), patch.object(core, "cache_listing", side_effect=lambda _: next(listing)):
            self.assertIsNone(core.wait_for_new_file("offline", set(), 2, 0.1, 2))

    def test_account_check_cannot_enter_recovery(self):
        xml = '<hierarchy><node text="请完成验证"/></hierarchy>'.encode()
        with patch.object(core, "run_adb", side_effect=[b"OK", xml]):
            with self.assertRaises(core.AccountActionRequired):
                core.dump_ui("offline")

    def test_month_run_stops_benchmark_without_marking_complete(self):
        try:
            months = importlib.import_module("baidu_adb_month_collector")
        except ModuleNotFoundError as exc:
            self.skipTest(f"Optional live-collection dependency unavailable: {exc}")
        root = ui()
        card = core.record_cards(root, "2022")[0]
        options = argparse.Namespace(ui_timeout=1, file_timeout=1, poll_seconds=0.1,
                                     stagnant_scrolls=3, no_qgis=True, qgis_every=0, max_records=1)
        state = {"actions": 0}
        with patch.object(months, "apply_month_filter", return_value=root), \
             patch.object(core, "load_processed", return_value={"exact": set(), "stable": set(), "by_year_and_base": set(), "legacy_bases": set()}), \
             patch.object(core, "write_status"), \
             patch.object(core, "process_card", return_value={"status": "pulled"}) as process, \
             patch.object(core, "keyevent"), \
             patch.object(months, "normalize_to_footprint_list", return_value=root), \
             patch.object(months, "wait_for_filtered_month", return_value=root), \
             patch.object(months, "finish_month") as finish:
            had, count, completed = months.run_month("offline", None, 2022, 1, options, "test", state)
            self.assertTrue(had)
            self.assertEqual(count, 1)
            self.assertFalse(completed)
            finish.assert_not_called()
            self.assertEqual(process.call_args.args[2]["stable_fingerprint"], card["stable_fingerprint"])

    def test_coordinate_formula_regression(self):
        # A fixed synthetic projected point in the tested region; round trip of the
        # GCJ inverse is also checked independently of the output writers.
        lon_gcj, lat_gcj, lon, lat = app.convert.bd09mc_to_wgs84(12958000, 4826000)
        forward_lon, forward_lat = app.convert.wgs84_to_gcj02(lon, lat)
        self.assertLess(abs(forward_lon - lon_gcj), 1e-8)
        self.assertLess(abs(forward_lat - lat_gcj), 1e-8)
        self.assertTrue(115 < lon < 118 and 39 < lat < 41)

    def test_bad_baseline_stops_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = config(Path(tmp), "geojson,kml")
            conf["raw_dir"].mkdir()
            (conf["raw_dir"] / (A + ".csv")).write_text(ROWS5)
            conf["state_dir"].mkdir()
            (conf["state_dir"] / "baseline_snapshot.json").write_text('{"track_ids":7}')
            with self.assertRaisesRegex(ValueError, "baseline.track_ids"):
                app.export(conf)
            self.assertFalse(conf["output_dir"].exists())

    def test_old_schema_plan_and_rejected_migration_are_readonly(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = config(Path(tmp))
            conf["state_dir"].mkdir()
            db_path = conf["state_dir"] / "collector.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute("CREATE TABLE records(fingerprint TEXT,sequence INTEGER,status TEXT)")
                connection.execute("INSERT INTO records VALUES('01月15日 08:30|1.2公里',7,'pulled')")
            connection.close()
            before = core.sha256(db_path)
            with self.assertRaisesRegex(ValueError, "Legacy records have no year"):
                core.open_db()
            self.assertEqual(core.sha256(db_path), before)
            result = subprocess.run([sys.executable, str(SCRIPTS / "footprint_export.py"), "plan", "--workspace", str(tmp), "--state-dir", "state", "--start", "2023-02", "--end", "2022-12"], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)["legacy_schema"])
            self.assertEqual(core.sha256(db_path), before)

    def test_nested_directories_are_rejected_before_any_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for raw, state, output in (("state/pulls", "state", "gis"), ("raw", "raw/state", "gis"), ("raw", "state", "raw/gis"), ("raw", "gis/state", "gis")):
                args = argparse.Namespace(config=None, workspace=str(root), raw_dir=raw, state_dir=state, output_dir=output)
                with self.assertRaisesRegex(ValueError, "non-nested"):
                    app.configure(args)
            self.assertEqual(list(root.iterdir()), [])

    def test_reexport_preserves_existing_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = config(Path(tmp), "geojson,kml")
            conf["raw_dir"].mkdir()
            (conf["raw_dir"] / (A + ".csv")).write_text(ROWS5)
            first = app.export(conf)
            saved = {p: core.sha256(Path(p)) for p in first["outputs"]}
            saved_manifest = core.sha256(conf["output_dir"] / "alternate_manifest.json")
            second = app.export(conf)
            self.assertNotEqual(first["outputs"], second["outputs"])
            self.assertTrue(all("revisions" in Path(p).parts for p in second["outputs"]))
            self.assertEqual(saved, {p: core.sha256(Path(p)) for p in first["outputs"]})
            self.assertEqual(saved_manifest, core.sha256(conf["output_dir"] / "alternate_manifest.json"))

    def test_pull_cannot_overwrite_original_or_alias_its_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = config(Path(tmp))
            conf["raw_dir"].mkdir()
            original = conf["raw_dir"] / (A + ".csv")
            original.write_bytes(b"original")
            def fake_pull(device, *args, **kwargs):
                Path(args[-1]).write_bytes(b"different")
                return b""
            info = {"name": A + ".csv", "size": 9}
            with patch.object(core, "run_adb", side_effect=fake_pull):
                with self.assertRaisesRegex(RuntimeError, "Refusing to overwrite"):
                    core.pull_new_file("offline", info)
            self.assertEqual(original.read_bytes(), b"original")
            with patch.object(core, "PULL_DIR", core.RAW_DIR), patch.object(core, "run_adb") as adb:
                with self.assertRaisesRegex(ValueError, "must not overlap"):
                    core.pull_new_file("offline", info)
                adb.assert_not_called()
            self.assertEqual(original.read_bytes(), b"original")

    def test_dedup_retains_full_database_backup_and_archived_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = config(Path(tmp))
            conf["state_dir"].mkdir()
            connection = core.open_db()
            for sequence, base, status in ((1, "01月15日 08:30|1.2公里|起点", "pulled"), (2, "01月15日 08:30|1.2公里|起点|终点", "server_only_or_slow")):
                connection.execute("INSERT INTO records(fingerprint,legacy_fingerprint,year_label,stable_fingerprint,sequence,status) VALUES(?,?,?,?,?,?)", ("2022|"+base, base, "2022", "2022|01月15日 08:30|1.2公里", sequence, status))
            connection.commit()
            connection.close()
            connection = core.open_db()
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 1)
            snapshots = [json.loads(r[0]) for r in connection.execute("SELECT record_json FROM record_history")]
            self.assertEqual({r["sequence"] for r in snapshots}, {1, 2})
            connection.close()
            backup_path, = list((conf["state_dir"] / "backups").glob("*.sqlite3"))
            backup = sqlite3.connect(backup_path)
            try:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup.execute("SELECT COUNT(*) FROM records").fetchone()[0], 2)
            finally:
                backup.close()


if __name__ == "__main__":
    unittest.main()
