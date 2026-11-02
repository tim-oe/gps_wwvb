"""Write requirements.txt from the main group of poetry.lock, with hashes.

Poetry 2 does not ship ``poetry export``. The Pi installer uses
``pip install --require-hashes -r requirements.txt``.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    lock = tomllib.loads((ROOT / "poetry.lock").read_text(encoding="utf-8"))
    lines = [
        "# Generated from poetry.lock by scripts/export_requirements.py.",
        "# Regenerate with: make requirements",
        "",
    ]
    packages = [pkg for pkg in lock["package"] if "main" in pkg.get("groups", [])]
    for package in sorted(packages, key=lambda item: item["name"].lower()):
        marker = package.get("markers")
        requirement = f"{package['name']}=={package['version']}"
        if marker:
            requirement += f" ; {marker}"
        hashes = [item["hash"] for item in package.get("files", [])]
        if not hashes:
            raise SystemExit(f"{package['name']} has no hashes in poetry.lock")
        if len(hashes) == 1:
            lines.append(f"{requirement} \\\n    --hash={hashes[0]}")
        else:
            parts = [requirement + " \\"]
            for index, digest in enumerate(hashes):
                suffix = " \\" if index < len(hashes) - 1 else ""
                parts.append(f"    --hash={digest}{suffix}")
            lines.append("\n".join(parts))
        lines.append("")
    (ROOT / "requirements.txt").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
