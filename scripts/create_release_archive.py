"""Create a source-only release zip without secrets or runtime artifacts.

The archive is built from ``git ls-files`` plus an explicit denylist. This keeps
``.env``, ``.git`` and generated ``artifacts`` out even if a local workspace is
dirty or contains large runtime outputs.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


_REPO_ROOT = Path(__file__).resolve().parents[1]
_KST = ZoneInfo("Asia/Seoul")
_DENY_PREFIXES = (
    ".git/",
    "artifacts/",
    "new/artifacts/",
    "__MACOSX/",
)
_DENY_NAMES = {".env", ".DS_Store"}


def _git_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git ls-files failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_allowed(path: str) -> bool:
    if path in _DENY_NAMES:
        return False
    if any(path.startswith(prefix) for prefix in _DENY_PREFIXES):
        return False
    if "/.env" in path or path.endswith("/.env"):
        return False
    return True


def create_archive(output: Path) -> dict[str, object]:
    output.parent.mkdir(parents=True, exist_ok=True)
    tracked = _git_files()
    allowed = [path for path in tracked if _is_allowed(path)]
    included = [path for path in allowed if (_REPO_ROOT / path).is_file()]
    missing = sorted(set(allowed) - set(included))
    denied = sorted(set(tracked) - set(allowed))

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path in included:
            zf.write(_REPO_ROOT / rel_path, arcname=rel_path)

    return {
        "output": str(output),
        "included_count": len(included),
        "missing_tracked_paths": missing,
        "denied_tracked_paths": denied,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    default_name = f"elephant_lab_release_{datetime.now(_KST):%Y%m%d_%H%M%S}.zip"
    parser.add_argument(
        "--output",
        type=Path,
        default=_REPO_ROOT / "dist" / default_name,
        help="Output zip path. Default: dist/elephant_lab_release_<timestamp>.zip",
    )
    args = parser.parse_args()

    report = create_archive(args.output)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
