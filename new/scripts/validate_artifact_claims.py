"""Validate local artifact path claims in docs, notebooks, and release zips.

This script is read-only:
- does not read .env
- does not call external APIs
- does not mutate registries
"""
from __future__ import annotations

import argparse
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[2]

ARTIFACT_RE = re.compile(
    r"(?:/Users/[^\s'\"`)]+/Elephant_Lab/)?"
    r"(artifacts/[A-Za-z0-9_./@+=:,~%\\-]+)"
)


@dataclass(frozen=True)
class Claim:
    source: str
    artifact_path: str
    exists: bool


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate local artifact path claims against the workspace",
    )
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument(
        "--scope",
        choices=["current", "all"],
        default="current",
        help="current scans current bundle-facing docs; all scans broader docs.",
    )
    parser.add_argument("--include-notebooks", action="store_true")
    parser.add_argument("--include-validation-zip", action="store_true")
    parser.add_argument(
        "--package-root",
        default="",
        help="Validate claims relative to an extracted review package root.",
    )
    parser.add_argument("--write-report", action="store_true")
    return parser.parse_args(argv)


def _candidate_sources(
    scope: str,
    include_notebooks: bool,
    *,
    root: Path = REPO_ROOT,
) -> list[Path]:
    roots = [
        root / "new" / "experiments" / "paper_policy_lab",
        root / "new" / "experiments" / "lgbm_global_search",
        root / "new" / "docs" / "meetings",
    ]
    suffixes = {".md", ".json", ".yaml", ".yml"}
    if include_notebooks:
        suffixes.add(".ipynb")
    if scope == "all":
        roots.extend([root / "new" / "docs", root / "artifacts"])

    seen: set[Path] = set()
    sources: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and path.suffix in suffixes and path not in seen:
                seen.add(path)
                sources.append(path)
    return sorted(sources)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return f"__READ_ERROR__ {type(e).__name__}: {e}"


def _relative_to(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _claims_from_text(path: Path, text: str, *, root: Path = REPO_ROOT) -> list[Claim]:
    claims: list[Claim] = []
    for match in ARTIFACT_RE.finditer(text):
        rel = match.group(1).rstrip(".,;:]}")
        artifact = root / rel
        claims.append(
            Claim(
                source=_relative_to(path, root),
                artifact_path=rel,
                exists=artifact.exists(),
            )
        )
    return claims


def _dedupe_claims(claims: Iterable[Claim]) -> list[Claim]:
    deduped: dict[tuple[str, str], Claim] = {}
    for claim in claims:
        deduped[(claim.source, claim.artifact_path)] = claim
    return sorted(deduped.values(), key=lambda c: (c.source, c.artifact_path))


def _zip_candidates(bundle_id: str, *, root: Path = REPO_ROOT) -> list[Path]:
    patterns = [
        f"*{bundle_id}*.zip",
        "*validation*.zip",
        "*release*.zip",
    ]
    paths: list[Path] = []
    for pattern in patterns:
        for root_name in ("artifacts", "dist"):
            scan_root = root / root_name
            if scan_root.exists():
                paths.extend(scan_root.rglob(pattern))
    return sorted(set(path for path in paths if path.is_file()))


def _validate_zips(bundle_id: str, *, root: Path = REPO_ROOT) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path in _zip_candidates(bundle_id, root=root):
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
            checks.append(
                {
                    "path": _relative_to(path, root),
                    "status": "PASS",
                    "entry_count": len(names),
                    "contains_bundle_id": any(bundle_id in name for name in names),
                    "forbidden_env_entries": [
                        name for name in names if Path(name).name == ".env"
                    ],
                }
            )
        except Exception as e:
            checks.append(
                {
                    "path": _relative_to(path, root),
                    "status": "FAIL",
                    "error_type": type(e).__name__,
                    "error": str(e),
                }
            )
    return checks


def build_report(
    *,
    bundle_id: str,
    scope: str = "current",
    include_notebooks: bool = False,
    include_validation_zip: bool = False,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(package_root).resolve() if package_root else REPO_ROOT
    sources = _candidate_sources(scope, include_notebooks, root=root)
    claims: list[Claim] = []
    for source in sources:
        claims.extend(_claims_from_text(source, _read_text(source), root=root))
    deduped = _dedupe_claims(claims)
    missing = [claim for claim in deduped if not claim.exists]
    zip_checks = (
        _validate_zips(bundle_id, root=root) if include_validation_zip else []
    )
    zip_failures = [
        check
        for check in zip_checks
        if check.get("status") != "PASS" or check.get("forbidden_env_entries")
    ]
    blockers: list[str] = []
    warnings: list[dict[str, Any]] = []
    if missing:
        blockers.append("missing_artifact_claims")
    if zip_failures:
        blockers.append("validation_zip_failures")
    if not sources:
        warnings.append({
            "reason": "no_sources_scanned",
            "source_count": 0,
            "claim_count": 0,
        })
    if sources and not deduped:
        warnings.append({
            "reason": "no_artifact_claims_found",
            "source_count": len(sources),
            "claim_count": 0,
        })
    status = "BLOCKED" if blockers else "WARN" if warnings else "PASS"
    return {
        "schema_version": "1.0.0",
        "status": status,
        "action": "validate_artifact_claims",
        "generated_at": datetime.now(KST).isoformat(),
        "bundle_id": bundle_id,
        "validation_root": str(root),
        "validation_scope": "package_root" if package_root else "full_dev_workspace",
        "reproducible_from_this_package": bool(package_root),
        "scope": scope,
        "include_notebooks": include_notebooks,
        "include_validation_zip": include_validation_zip,
        "source_count": len(sources),
        "claim_count": len(deduped),
        "low_signal": bool(warnings),
        "missing_count": len(missing),
        "missing_claims": [claim.__dict__ for claim in missing],
        "zip_checks": zip_checks,
        "blockers": blockers,
        "warnings": warnings,
        "safety": {
            "external_api_called": False,
            "env_read": False,
            "registry_mutated": False,
            "live_trading_allowed": False,
        },
    }


def _write_report(report: dict[str, Any], *, root: Path = REPO_ROOT) -> Path:
    out_dir = root / "artifacts" / "reports" / "artifact_claims"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(KST).strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"artifact_claims_{report['bundle_id']}_{ts}.json"
    report["report_path"] = str(path)
    report["report_path_relative"] = str(path.relative_to(root))
    with path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(
        bundle_id=str(args.bundle_id),
        scope=str(args.scope),
        include_notebooks=bool(args.include_notebooks),
        include_validation_zip=bool(args.include_validation_zip),
        package_root=str(args.package_root) if args.package_root else None,
    )
    if args.write_report:
        report_root = (
            Path(str(args.package_root)).resolve() if args.package_root else REPO_ROOT
        )
        _write_report(report, root=report_root)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
