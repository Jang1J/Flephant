#!/usr/bin/env python
"""Validate local artifact path claims in Elephant Lab reports.

This is a read-only "liar detector" for generated evidence. It checks whether
JSON reports that claim local report/model/zip paths actually point to existing
files. Current deployment surfaces are hard failures; historical retained
reports are warnings unless ``--fail-on-any-missing`` is used.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
_KST = ZoneInfo("Asia/Seoul")

_LOCAL_PATH_RE = re.compile(r"^(?:/Users/|artifacts/|new/|dist/)")
_PATH_KEY_RE = re.compile(
    r"(?:^|_)(?:path|paths|file|files|zip|artifact|metadata|model)(?:$|_)",
    re.IGNORECASE,
)
_DOC_PATH_RE = re.compile(
    r"(?:/Users/[^/\s]+/(?:Desktop/)?(?:Full_Part/)?Elephant_Lab/)?"
    r"(?:artifacts|new|dist)/[A-Za-z0-9_./\\-]+"
)

_IGNORE_TRAIL_PARTS = {
    "deleted",
    "removed",
    "moved",
    "quarantined",
    "ignored",
    "ignored_newer_non_deployable_reports",
}
_IGNORE_KEYS = {"source_url", "url"}


def _repo_relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _is_pass_like(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    status = str(obj.get("status", "")).upper()
    verdict = str(obj.get("verdict", "")).lower()
    return status == "PASS" or verdict == "pass" or obj.get("deployable") is True


def _is_ignored_trail(trail: list[str]) -> bool:
    lowered = {part.lower() for part in trail}
    return bool(lowered & _IGNORE_TRAIL_PARTS)


def _looks_like_local_path(value: str) -> bool:
    return bool(_LOCAL_PATH_RE.match(str(value)))


def _resolve_path(value: str, *, root: Path = ROOT) -> Path:
    raw = str(value)
    if raw.startswith("/Users/"):
        return Path(raw)
    return root / raw


def _path_claim_key(key: str) -> bool:
    if key in _IGNORE_KEYS:
        return False
    return bool(_PATH_KEY_RE.search(key))


def _scan_json_payload(
    obj: Any,
    *,
    root: Path,
    report_path: Path,
    trail: list[str] | None = None,
    pass_context: bool = False,
) -> tuple[int, list[dict[str, Any]]]:
    """Return ``(checked_count, missing_claims)`` for a JSON payload."""
    trail = trail or []
    checked = 0
    issues: list[dict[str, Any]] = []
    active = pass_context or _is_pass_like(obj)

    if _is_ignored_trail(trail):
        return 0, []

    if isinstance(obj, dict):
        for key, value in obj.items():
            child_trail = [*trail, str(key)]
            if isinstance(value, (dict, list)):
                n, found = _scan_json_payload(
                    value,
                    root=root,
                    report_path=report_path,
                    trail=child_trail,
                    pass_context=active,
                )
                checked += n
                issues.extend(found)
                continue
            if isinstance(value, str) and _path_claim_key(str(key)) and _looks_like_local_path(value):
                checked += 1
                if active and not _resolve_path(value, root=root).exists():
                    issues.append({
                        "report": _repo_relative(report_path),
                        "key": ".".join(child_trail),
                        "value": value,
                    })
        return checked, issues

    if isinstance(obj, list):
        parent_key = trail[-1] if trail else ""
        for idx, value in enumerate(obj):
            child_trail = [*trail, str(idx)]
            if isinstance(value, (dict, list)):
                n, found = _scan_json_payload(
                    value,
                    root=root,
                    report_path=report_path,
                    trail=child_trail,
                    pass_context=active,
                )
                checked += n
                issues.extend(found)
                continue
            if isinstance(value, str) and _path_claim_key(parent_key) and _looks_like_local_path(value):
                checked += 1
                if active and not _resolve_path(value, root=root).exists():
                    issues.append({
                        "report": _repo_relative(report_path),
                        "key": ".".join(child_trail),
                        "value": value,
                    })
    return checked, issues


def scan_json_report(path: Path, *, root: Path = ROOT) -> tuple[int, list[dict[str, Any]]]:
    return _scan_json_payload(_read_json(path), root=root, report_path=path)


def _latest(pattern: str, *, root: Path = ROOT) -> Path | None:
    files = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def current_surface_reports(bundle_id: str, *, root: Path = ROOT) -> list[Path]:
    patterns = [
        f"artifacts/reports/service_policy_replay/service_policy_replay_{bundle_id}_*.json",
        f"artifacts/reports/backtest/backtest_{bundle_id}_*.json",
        f"artifacts/reports/deploy/deploy_{bundle_id}_*.json",
        f"artifacts/reports/service_readiness/service_readiness_{bundle_id}_*.json",
        "artifacts/reports/prelive_gate/prelive_gate_*.json",
        "artifacts/reports/paper_auto_trading/paper_auto_service_rehearsal_*.json",
        "artifacts/reports/paper_trading/paper_trading_balance_reconciliation_*.json",
        "artifacts/reports/paper_trading/paper_trading_submit_probe_order_*.json",
    ]
    out: list[Path] = []
    for pattern in patterns:
        found = _latest(pattern, root=root)
        if found is not None:
            out.append(found)
    return out


def scan_report_set(paths: list[Path], *, root: Path = ROOT) -> dict[str, Any]:
    checked = 0
    missing: list[dict[str, Any]] = []
    for path in paths:
        try:
            n, found = scan_json_report(path, root=root)
        except Exception as e:
            missing.append({
                "report": _repo_relative(path),
                "key": "<json_parse>",
                "value": f"{type(e).__name__}: {e}",
            })
            continue
        checked += n
        missing.extend(found)
    return {
        "reports_scanned": len(paths),
        "path_claims_checked": checked,
        "missing_claim_count": len(missing),
        "missing_claims": missing,
    }


def scan_notebooks(paths: list[Path], *, root: Path = ROOT) -> dict[str, Any]:
    checked = 0
    missing: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists():
            missing.append({
                "notebook": _repo_relative(path),
                "path": _repo_relative(path),
                "reason": "notebook_missing",
            })
            continue
        data = _read_json(path)
        text = "\n".join("".join(cell.get("source", [])) for cell in data.get("cells", []))
        for match in _DOC_PATH_RE.finditer(text):
            raw = match.group(0).rstrip("'\"),]`.")
            if not raw.endswith((".json", ".pkl", ".ipynb", ".zip")):
                continue
            checked += 1
            if not _resolve_path(raw, root=root).exists():
                missing.append({
                    "notebook": _repo_relative(path),
                    "path": raw,
                    "reason": "notebook_claim_missing",
                })
    return {
        "notebooks_scanned": len(paths),
        "path_claims_checked": checked,
        "missing_claim_count": len(missing),
        "missing_claims": missing,
    }


def scan_validation_zip(zip_path: Path, expected_entries: list[str]) -> dict[str, Any]:
    if not zip_path.exists():
        return {
            "zip_exists": False,
            "entry_count": 0,
            "missing_claim_count": 1,
            "missing_claims": [{"path": _repo_relative(zip_path), "reason": "zip_missing"}],
        }
    import zipfile

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    missing = [
        {"path": entry, "reason": "zip_entry_missing"}
        for entry in expected_entries
        if entry not in names
    ]
    return {
        "zip_exists": True,
        "entry_count": len(names),
        "missing_claim_count": len(missing),
        "missing_claims": missing,
    }


def _top_reports(missing_claims: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return Counter(str(item.get("report", "<unknown>")) for item in missing_claims).most_common(20)


def build_report(
    *,
    bundle_id: str,
    scope: str,
    include_notebooks: bool,
    include_validation_zip: bool,
) -> dict[str, Any]:
    if scope == "current":
        report_paths = current_surface_reports(bundle_id)
    else:
        report_paths = sorted((ROOT / "artifacts" / "reports").glob("**/*.json"))

    report_scan = scan_report_set(report_paths)
    notebook_scan = {
        "notebooks_scanned": 0,
        "path_claims_checked": 0,
        "missing_claim_count": 0,
        "missing_claims": [],
    }
    if include_notebooks:
        notebook_scan = scan_notebooks(sorted(
            (ROOT / "new/docs/meetings").glob("*.ipynb")
        ))

    zip_scan = {
        "zip_exists": None,
        "entry_count": 0,
        "missing_claim_count": 0,
        "missing_claims": [],
    }
    if include_validation_zip:
        zip_scan = scan_validation_zip(
            ROOT / f"dist/elephant_model_performance_validation_{bundle_id}_20260522.zip",
            [
                "new/docs/meetings/20260522_model_performance_validation.ipynb",
                f"artifacts/bundles/{bundle_id}/lgbm/latest_model.pkl",
                f"artifacts/bundles/{bundle_id}/lgbm/latest_model_metadata.json",
                f"artifacts/reports/backtest/backtest_{bundle_id}_20260521_215920.json",
            ],
        )

    missing_count = (
        report_scan["missing_claim_count"]
        + notebook_scan["missing_claim_count"]
        + zip_scan["missing_claim_count"]
    )
    status = "PASS" if missing_count == 0 else ("FAIL" if scope == "current" else "WARN")
    return {
        "schema_version": "1.0.0",
        "status": status,
        "generated_at": datetime.now(_KST).isoformat(),
        "scope": scope,
        "bundle_id": bundle_id,
        "reports": report_scan,
        "notebooks": notebook_scan,
        "validation_zip": zip_scan,
        "missing_claim_count": missing_count,
        "top_missing_report_sources": _top_reports(report_scan["missing_claims"]),
    }


def _write_report(report: dict[str, Any], *, report_dir: Path) -> dict[str, Any]:
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(_KST).strftime("%Y%m%d_%H%M%S")
    bundle_id = str(report.get("bundle_id", "unknown")).replace("/", "_")
    path = report_dir / f"artifact_claims_{bundle_id}_{ts}.json"
    with path.open("w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    report["report_path"] = str(path)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-id", default="BUNDLE-20260521-POSTCLOSE")
    parser.add_argument("--scope", choices=("current", "all"), default="current")
    parser.add_argument("--include-notebooks", action="store_true")
    parser.add_argument("--include-validation-zip", action="store_true")
    parser.add_argument("--fail-on-any-missing", action="store_true")
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=ROOT / "artifacts" / "reports" / "artifact_claims",
    )
    args = parser.parse_args(argv)

    report = build_report(
        bundle_id=str(args.bundle_id),
        scope=str(args.scope),
        include_notebooks=bool(args.include_notebooks),
        include_validation_zip=bool(args.include_validation_zip),
    )
    if args.write_report:
        report = _write_report(report, report_dir=args.report_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["status"] == "FAIL":
        return 1
    if args.fail_on_any_missing and report["missing_claim_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
