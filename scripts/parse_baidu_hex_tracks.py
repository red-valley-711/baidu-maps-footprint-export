from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape


LOCAL_TZ = timezone.utc
SOURCE_DEVICE_PATH = "not specified (local CSV export)"
SELECTED_OUTPUTS = []
SOURCE_FILE_COUNT = 0

# Baidu Mercator bounds and inverse polynomial coefficients.
MC_BANDS = (
    12890594.86,
    8362377.87,
    5591021.0,
    3481989.83,
    1678043.12,
    0.0,
)
MC_TO_LL = (
    (
        1.410526172116255e-8,
        8.98305509648872e-6,
        -1.9939833816331,
        200.9824383106796,
        -187.2403703815547,
        91.6087516669843,
        -23.38765649603339,
        2.57121317296198,
        -0.03801003308653,
        17337981.2,
    ),
    (
        -7.435856389565537e-9,
        8.983055097726239e-6,
        -0.78625201886289,
        96.32687599759846,
        -1.85204757529826,
        -59.36935905485877,
        47.40033549296737,
        -16.50741931063887,
        2.28786674699375,
        10260144.86,
    ),
    (
        -3.030883460898826e-8,
        8.98305509983578e-6,
        0.30071316287616,
        59.74293618442277,
        7.357984074871,
        -25.38371002664745,
        13.45380521110908,
        -3.29883767235584,
        0.32710905363475,
        6856817.37,
    ),
    (
        -1.981981304930552e-8,
        8.983055099779535e-6,
        0.03278182852591,
        40.31678527705744,
        0.65659298677277,
        -4.44255534477492,
        0.85341911805263,
        0.12923347998204,
        -0.04625736007561,
        4482777.06,
    ),
    (
        3.09191371068437e-9,
        8.983055096812155e-6,
        0.00006995724062,
        23.10934304144901,
        -0.00023663490511,
        -0.6321817810242,
        -0.00663494467273,
        0.03430082397953,
        -0.00466043876332,
        2555164.4,
    ),
    (
        2.890871144776878e-9,
        8.983055095805407e-6,
        -3.068298e-8,
        7.47137025468032,
        -0.00000353937994,
        -0.02145144861037,
        -0.00001234426596,
        0.00010322952773,
        -0.00000323890364,
        826088.5,
    ),
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def bd09mc_to_bd09ll(x: float, y: float) -> tuple[float, float]:
    band = MC_BANDS[-1]
    coeff = MC_TO_LL[-1]
    for candidate_band, candidate_coeff in zip(MC_BANDS, MC_TO_LL):
        if abs(y) >= candidate_band:
            band = candidate_band
            coeff = candidate_coeff
            break
    lon = coeff[0] + coeff[1] * abs(x)
    ratio = abs(y) / coeff[9]
    lat = 0.0
    for exponent, value in enumerate(coeff[2:9]):
        lat += value * ratio**exponent
    return (
        lon if x >= 0 else -lon,
        lat if y >= 0 else -lat,
    )


def bd09_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    x = lon - 0.0065
    y = lat - 0.006
    x_pi = math.pi * 3000.0 / 180.0
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * x_pi)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * x_pi)
    return z * math.cos(theta), z * math.sin(theta)


def in_china(lat: float, lon: float) -> bool:
    return 72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271


def transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (
        20.0 * math.sin(6.0 * x * math.pi)
        + 20.0 * math.sin(2.0 * x * math.pi)
    ) * 2.0 / 3.0
    ret += (
        20.0 * math.sin(y * math.pi)
        + 40.0 * math.sin(y / 3.0 * math.pi)
    ) * 2.0 / 3.0
    ret += (
        160.0 * math.sin(y / 12.0 * math.pi)
        + 320.0 * math.sin(y * math.pi / 30.0)
    ) * 2.0 / 3.0
    return ret


def transform_lon(x: float, y: float) -> float:
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += (
        20.0 * math.sin(6.0 * x * math.pi)
        + 20.0 * math.sin(2.0 * x * math.pi)
    ) * 2.0 / 3.0
    ret += (
        20.0 * math.sin(x * math.pi)
        + 40.0 * math.sin(x / 3.0 * math.pi)
    ) * 2.0 / 3.0
    ret += (
        150.0 * math.sin(x / 12.0 * math.pi)
        + 300.0 * math.sin(x / 30.0 * math.pi)
    ) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    if not in_china(lat, lon):
        return lon, lat
    dlat = transform_lat(lon - 105.0, lat - 35.0)
    dlon = transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = 1.0 - 0.00669342162296594323 * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    dlat = dlat * 180.0 / (
        6335552.717000426 / (magic * sqrt_magic) * math.pi
    )
    dlon = dlon * 180.0 / (
        6378245.0 / sqrt_magic * math.cos(rad_lat) * math.pi
    )
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    if not in_china(lat, lon):
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(8):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        dlon, dlat = lon - glon, lat - glat
        wlon += dlon
        wlat += dlat
        if abs(dlon) < 1e-9 and abs(dlat) < 1e-9:
            break
    return wlon, wlat


def bd09mc_to_wgs84(x: float, y: float) -> tuple[float, float, float, float]:
    lon_bd09ll, lat_bd09ll = bd09mc_to_bd09ll(x, y)
    lon_gcj02, lat_gcj02 = bd09_to_gcj02(lon_bd09ll, lat_bd09ll)
    lon_wgs84, lat_wgs84 = gcj02_to_wgs84(lon_gcj02, lat_gcj02)
    return lon_gcj02, lat_gcj02, lon_wgs84, lat_wgs84


def haversine_m(p1: dict, p2: dict) -> float:
    radius = 6371008.8
    lat1 = math.radians(p1["lat_wgs84"])
    lat2 = math.radians(p2["lat_wgs84"])
    dlat = lat2 - lat1
    dlon = math.radians(p2["lon_wgs84"] - p1["lon_wgs84"])
    a = (
        math.sin(dlat / 2.0) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2
    )
    return 2.0 * radius * math.asin(math.sqrt(a))


def open_text(path: Path):
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def parse_track(path: Path) -> dict:
    track_id = path.name.removesuffix(".gz").removesuffix(".csv")
    points = []
    column_counts = set()

    with open_text(path) as fh:
        for line_no, row in enumerate(csv.reader(fh), start=1):
            if not row or not any(cell.strip() for cell in row):
                continue
            column_counts.add(len(row))
            if len(row) not in (5, 7):
                raise ValueError(f"{path.name}:{line_no}: expected 5 or 7 columns")
            try:
                x, y = float(row[0]), float(row[1])
                speed = float(row[2])
                bearing = float(row[3])
                accuracy = float(row[4])
                extra_1 = float(row[5]) if len(row) > 5 else None
                extra_2 = float(row[6]) if len(row) > 6 else None
            except ValueError as exc:
                raise ValueError(
                    f"{path.name}:{line_no}: non-numeric value"
                ) from exc

            if not all(math.isfinite(v) for v in (x, y, speed, bearing, accuracy, extra_1, extra_2) if v is not None):
                raise ValueError(f"{path.name}:{line_no}: nonfinite number")
            lon_gcj02, lat_gcj02, lon_wgs84, lat_wgs84 = bd09mc_to_wgs84(
                x, y
            )
            if not in_china(lat_wgs84, lon_wgs84):
                raise ValueError(
                    f"{path.name}:{line_no}: converted point outside China"
                )
            points.append(
                {
                    "point_index": len(points),
                    "x_bd09mc": x,
                    "y_bd09mc": y,
                    "lon_bd09ll": bd09mc_to_bd09ll(x, y)[0],
                    "lat_bd09ll": bd09mc_to_bd09ll(x, y)[1],
                    "lon_gcj02": lon_gcj02,
                    "lat_gcj02": lat_gcj02,
                    "lon_wgs84": lon_wgs84,
                    "lat_wgs84": lat_wgs84,
                    "speed_raw": speed,
                    "bearing_raw": bearing,
                    "accuracy_raw": accuracy,
                    "extra_1": extra_1,
                    "extra_2": extra_2,
                }
            )

    if len(points) < 2:
        raise ValueError(f"{path.name}: fewer than two coordinate rows")

    lons = [p["lon_wgs84"] for p in points]
    lats = [p["lat_wgs84"] for p in points]
    speeds = [p["speed_raw"] for p in points]
    mtime_utc = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    distance_m = sum(
        haversine_m(a, b) for a, b in zip(points, points[1:])
    )
    return {
        "track_id": track_id,
        "source_file": path.name,
        "source_bytes": path.stat().st_size,
        "sha256": sha256(path),
        "column_counts": sorted(column_counts),
        "points": points,
        "point_count": len(points),
        "distance_m": distance_m,
        "bounds_wgs84": {
            "west": min(lons),
            "south": min(lats),
            "east": max(lons),
            "north": max(lats),
        },
        "speed_raw_min": min(speeds),
        "speed_raw_mean": sum(speeds) / len(speeds),
        "speed_raw_max": max(speeds),
        "cache_mtime_utc": mtime_utc.isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "cache_mtime_local": mtime_utc.astimezone(LOCAL_TZ).isoformat(
            timespec="seconds"
        ),
    }


def track_properties(track: dict) -> dict:
    return {
        "track_id": track["track_id"],
        "source_file": track["source_file"],
        "point_count": track["point_count"],
        "column_counts": ",".join(str(n) for n in track["column_counts"]),
        "distance_m": round(track["distance_m"], 2),
        "bounds_wgs84": track["bounds_wgs84"],
        "speed_raw_min": track["speed_raw_min"],
        "speed_raw_mean": round(track["speed_raw_mean"], 6),
        "speed_raw_max": track["speed_raw_max"],
        "cache_mtime_utc": track["cache_mtime_utc"],
        "cache_mtime_local": track["cache_mtime_local"],
        "raw_coordinate_system": "BD-09MC",
        "gis_coordinate_system": "WGS84",
        "has_timestamps": False,
        "mtime_note": "File modification time only; no per-point timestamps in CSV.",
    }


def write_geojson(tracks: list[dict], path: Path) -> None:
    metadata = {
        "source": SOURCE_DEVICE_PATH,
        "track_count": len(tracks),
        "point_count": sum(t["point_count"] for t in tracks),
        "raw_coordinate_system": "BD-09MC",
        "output_coordinate_system": "WGS84",
        "timestamp_note": "CSV files contain no per-point timestamps.",
    }
    with path.open("w", encoding="utf-8") as fh:
        fh.write('{"type":"FeatureCollection","name":"Baidu hex cache tracks",')
        fh.write(f'"metadata":{json.dumps(metadata, ensure_ascii=False)},"features":[')
        for index, track in enumerate(tracks):
            feature = {
                "type": "Feature",
                "id": track["track_id"],
                "properties": track_properties(track),
                "geometry": {
                    "type": "LineString",
                    "coordinates": [
                        [
                            round(point["lon_wgs84"], 8),
                            round(point["lat_wgs84"], 8),
                        ]
                        for point in track["points"]
                    ],
                },
            }
            if index:
                fh.write(",")
            fh.write(
                json.dumps(feature, ensure_ascii=False, separators=(",", ":"))
            )
        fh.write("]}")


def write_points_csv(tracks: list[dict], path: Path) -> None:
    fields = [
        "track_id",
        "point_index",
        "source_file",
        "x_bd09mc",
        "y_bd09mc",
        "lon_bd09ll",
        "lat_bd09ll",
        "lon_gcj02",
        "lat_gcj02",
        "lon_wgs84",
        "lat_wgs84",
        "speed_raw",
        "bearing_raw",
        "accuracy_raw",
        "extra_1",
        "extra_2",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for track in tracks:
            for point in track["points"]:
                writer.writerow(
                    {
                        "track_id": track["track_id"],
                        "source_file": track["source_file"],
                        **point,
                    }
                )


def write_summary_csv(tracks: list[dict], path: Path) -> None:
    fields = [
        "track_id",
        "source_file",
        "point_count",
        "column_counts",
        "distance_m",
        "west",
        "south",
        "east",
        "north",
        "speed_raw_min",
        "speed_raw_mean",
        "speed_raw_max",
        "cache_mtime_utc",
        "cache_mtime_local",
        "sha256",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for track in tracks:
            bounds = track["bounds_wgs84"]
            writer.writerow(
                {
                    "track_id": track["track_id"],
                    "source_file": track["source_file"],
                    "point_count": track["point_count"],
                    "column_counts": ";".join(
                        str(n) for n in track["column_counts"]
                    ),
                    "distance_m": round(track["distance_m"], 2),
                    "west": bounds["west"],
                    "south": bounds["south"],
                    "east": bounds["east"],
                    "north": bounds["north"],
                    "speed_raw_min": track["speed_raw_min"],
                    "speed_raw_mean": track["speed_raw_mean"],
                    "speed_raw_max": track["speed_raw_max"],
                    "cache_mtime_utc": track["cache_mtime_utc"],
                    "cache_mtime_local": track["cache_mtime_local"],
                    "sha256": track["sha256"],
                }
            )


def write_gpx(tracks: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write(
            '<gpx version="1.1" creator="Codex Baidu hex CSV parser" '
            'xmlns="http://www.topografix.com/GPX/1/1" '
            'xmlns:baidu="https://example.invalid/baidu-hex-cache">\n'
        )
        fh.write("  <metadata><name>Baidu hex cache tracks</name></metadata>\n")
        for track in tracks:
            fh.write("  <trk>\n")
            fh.write(f"    <name>{escape(track['track_id'])}</name>\n")
            fh.write(
                "    <desc>"
                + escape(
                    f"{track['source_file']}; {track['point_count']} points; "
                    f"cache mtime {track['cache_mtime_local']}; "
                    "BD09MC converted to WGS84; CSV has no point timestamps"
                )
                + "</desc>\n"
            )
            fh.write("    <trkseg>\n")
            for point in track["points"]:
                fh.write(
                    f'      <trkpt lat="{point["lat_wgs84"]:.8f}" '
                    f'lon="{point["lon_wgs84"]:.8f}">\n'
                )
                fh.write("        <extensions>\n")
                for tag, key in [
                    ("speedRaw", "speed_raw"),
                    ("bearingRaw", "bearing_raw"),
                    ("accuracyRaw", "accuracy_raw"),
                    ("bd09mcX", "x_bd09mc"),
                    ("bd09mcY", "y_bd09mc"),
                    ("extra1", "extra_1"),
                    ("extra2", "extra_2"),
                ]:
                    value = point[key]
                    if value is not None:
                        fh.write(
                            f'          <baidu:{tag}>{value}</baidu:{tag}>\n'
                        )
                fh.write("        </extensions>\n")
                fh.write("      </trkpt>\n")
            fh.write("    </trkseg>\n  </trk>\n")
        fh.write("</gpx>\n")


def write_kml(tracks: list[dict], path: Path) -> None:
    colors = (
        "ff2a2aff",
        "ff00b050",
        "ffff7f00",
        "ff00c8ff",
        "ffff00ff",
        "ff3c78d8",
        "ffffbf00",
        "ff9c27b0",
    )
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        fh.write('<kml xmlns="http://www.opengis.net/kml/2.2">\n')
        fh.write("  <Document>\n")
        fh.write("    <name>Baidu hex cache tracks</name>\n")
        for index, track in enumerate(tracks):
            color = colors[index % len(colors)]
            style_id = f"line-{index % len(colors)}"
            fh.write("    <Placemark>\n")
            fh.write(f"      <name>{escape(track['track_id'])}</name>\n")
            fh.write(
                "      <description>"
                + escape(
                    f"{track['source_file']}; {track['point_count']} points; "
                    f"cache mtime {track['cache_mtime_local']}; "
                    "BD09MC converted to WGS84; no point timestamps"
                )
                + "</description>\n"
            )
            fh.write(f"      <styleUrl>#{style_id}</styleUrl>\n")
            fh.write("      <LineString><tessellate>1</tessellate>")
            fh.write("<altitudeMode>clampToGround</altitudeMode><coordinates>")
            fh.write(
                " ".join(
                    f"{point['lon_wgs84']:.8f},{point['lat_wgs84']:.8f},0"
                    for point in track["points"]
                )
            )
            fh.write("</coordinates></LineString>\n")
            fh.write("    </Placemark>\n")
        for index in range(len(colors)):
            fh.write(
                "    <Style id=\"line-"
                + str(index)
                + "\"><LineStyle><color>"
                + colors[index]
                + "</color><width>2</width></LineStyle></Style>\n"
            )
        fh.write("  </Document>\n</kml>\n")


def write_manifest(
    tracks: list[dict],
    raw_files: list[Path],
    manifest_path: Path,
) -> None:
    total_points = sum(track["point_count"] for track in tracks)
    total_distance = sum(track["distance_m"] for track in tracks)
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "source_device_path": SOURCE_DEVICE_PATH,
        "access_method": (
            "Read-only ADB pulls of CSV files generated by the Baidu Maps "
            "app while historical footprint details are opened; no root and "
            "no system changes."
        ),
        "source_files": SOURCE_FILE_COUNT,
        "selected_unique_files": len(raw_files),
        "track_count": len(tracks),
        "point_count": total_points,
        "total_distance_m": round(total_distance, 2),
        "raw_coordinate_system": "BD-09MC",
        "output_coordinate_system": "WGS84",
        "coordinate_chain": "BD-09MC -> BD-09LL -> GCJ-02 -> WGS84",
        "timestamps": (
            "The CSV rows have no per-point timestamp. "
            "cache_mtime is the source file modification time and is not "
            "assumed to be the exact trip start or end."
        ),
        "format": {
            "columns_5": "x_bd09mc,y_bd09mc,speed_raw,bearing_raw,accuracy_raw",
            "columns_7": (
                "x_bd09mc,y_bd09mc,speed_raw,bearing_raw,accuracy_raw,"
                "extra_1,extra_2"
            ),
            "speed_unit_note": (
                "speed_raw is preserved verbatim; the unit is not independently verified."
            ),
        },
        "outputs": SELECTED_OUTPUTS,
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

