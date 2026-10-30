"""Fail when personal export artifacts or obvious credentials enter this tree."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "__pycache__", "build", "dist"}
DATA_SUFFIXES = {
    ".csv",
    ".db",
    ".geojson",
    ".gpx",
    ".har",
    ".kml",
    ".log",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".xls",
    ".xlsx",
    ".sqlite",
    ".sqlite3",
    ".jsonl",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tgz",
    ".gz",
}
PRIVATE_NAMES = {
    "config.local.json",
    "local-session.md",
    ".env",
}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
)


def audit(root: Path) -> list[str]:
    issues: list[str] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if not path.is_file():
            continue
        if path.name == "config.example.json":
            continue
        lower_name = path.name.lower()
        if (
            lower_name in PRIVATE_NAMES
            or lower_name.startswith(".env.")
            or lower_name.endswith(".local.json")
            or lower_name.endswith((".pem", ".key"))
            or lower_name.startswith(("credentials", "secrets"))
            and lower_name.endswith(".json")
        ):
            issues.append(f"private configuration: {relative}")
            continue
        if (
            path.suffix.lower() in DATA_SUFFIXES
            or lower_name.endswith((".csv.gz", ".gpx.gz", ".kml.gz", ".har.gz"))
        ):
            issues.append(f"personal-data candidate: {relative}")
            continue
        if path == Path(__file__).resolve():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            issues.append(f"credential-like text: {relative}")
    return issues


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    issues = audit(Path(args.root).resolve())
    if issues:
        print("\n".join(issues))
        raise SystemExit(1)
    print("public-tree audit passed")


if __name__ == "__main__":
    main()
