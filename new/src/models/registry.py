"""S1-0 Batch B ModelRegistry. C17 ModelRegistryContract 구현.

저장 구조 (risk_config.yaml model_registry 참조):
  artifacts/lgbm/
    baseline.pkl              # S1-0 최초 baseline
    baseline_metadata.json
    v2.pkl                    # S3-6 야간 재학습 v2
    v2_metadata.json
    v{n}.pkl ...
    latest_model.pkl          # 현재 active (symlink 또는 copy)
    registry.json             # 인덱스 + active_version

API:
  save(model, metadata, is_latest=True) -> Path
  load_latest() -> (model, metadata)
  load_version(version) -> (model, metadata)
  list_versions() -> list[metadata]
  rollback(version) -> None

불변 원칙 준수:
  - 저장 경로 / 파일명 규약 → risk_config.yaml model_registry 섹션
  - 필수 메타데이터 필드 → risk_config.yaml model_registry.metadata_required_fields
  - direct_pkl_overwrite 금지 (save() 경유 필수, C17 forbidden)
"""
from __future__ import annotations

import json
import os
import pickle
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.config_loader import load as config_load
from src.utils.logger import get_logger

logger = get_logger("registry")


# ====================================================================== #
# Errors
# ====================================================================== #


class VersionNotFoundError(KeyError):
    """레지스트리에 지정 버전 없음."""


class PklLoadFailedError(RuntimeError):
    """pkl 파일 로드 실패."""


class RegistryCorruptedError(RuntimeError):
    """registry.json 파싱 실패 또는 무결성 오류."""


# ====================================================================== #
# ModelMetadata
# ====================================================================== #


@dataclass
class ModelMetadata:
    """C17 registry_schema.versions 한 항목과 1:1."""

    version: str
    bundle_id: str | None
    model_path: str
    metadata_path: str
    created_at: str
    train_start: str
    train_end: str
    feature_cols: list[str]
    label_horizon_bars: int
    target_col: str
    label_generation_version: str
    label_session_scope: str
    metrics: dict[str, float]
    commit_hash: str
    data_version: str
    status: str = "active"            # candidate | active | archived | rollback

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ====================================================================== #
# Registry
# ====================================================================== #


_REPO_ROOT = Path(__file__).resolve().parents[3]
_ARTIFACTS_ROOT = _REPO_ROOT / "artifacts"
_DEPLOY_ACTIVATION_TOKEN = object()


def _path_for_metadata(path: Path) -> str:
    """Store repo-local artifact paths portably while keeping tmp paths absolute."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(_REPO_ROOT))
    except ValueError:
        return str(resolved)


class ModelRegistry:
    """LightGBM 모델 버전 관리. C17 준수."""

    def __init__(self, artifacts_dir: Path | None = None) -> None:
        cfg = config_load("risk_config.yaml", "model_registry")
        self._schema_version: str = str(cfg["schema_version"])
        self._dir_name: str = str(cfg["artifacts_dir"])
        self._registry_file: str = str(cfg["registry_file"])
        self._latest_name: str = str(cfg["latest_symlink"])
        self._versioning_scheme: str = str(cfg["versioning_scheme"])
        self._max_versions_keep: int = int(cfg["max_versions_keep"])
        self._required_fields: list[str] = list(cfg["metadata_required_fields"])

        if artifacts_dir is not None:
            self.base_dir: Path = Path(artifacts_dir)
        else:
            override_dir = os.getenv("ELEPHANT_LGBM_REGISTRY_DIR", "").strip()
            if override_dir:
                override_path = Path(override_dir)
                self.base_dir = (
                    override_path if override_path.is_absolute()
                    else _REPO_ROOT / override_path
                )
            else:
                # risk_config.yaml artifacts_dir: "artifacts/lgbm" → repo root 기준
                self.base_dir = _REPO_ROOT / self._dir_name

        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ================================================================== #
    # Public API
    # ================================================================== #

    def save(
        self,
        model: Any,
        metadata: dict[str, Any],
        is_latest: bool = True,
    ) -> Path:
        """pkl + metadata JSON 저장 후 registry.json + latest 갱신 (atomic).

        자동 주입 필드: commit_hash (git rev-parse), created_at (ISO8601), status.
        나머지 필수 필드는 호출자 책임.

        Raises:
          KeyError: required_fields 누락 (자동 주입 후에도 누락 시)
          OSError: 파일 저장 실패
        """
        # 1. 자동 주입 가능한 필드 먼저 채움
        enriched = dict(metadata)
        if is_latest and enriched.get("bundle_id"):
            raise ValueError(
                "bundle_id가 있는 모델은 C12/C14 deploy gate 전 active/latest로 저장할 수 없다. "
                "candidate 저장은 is_latest=False를 사용하라."
            )
        enriched.setdefault("created_at", _now_iso())
        enriched.setdefault("commit_hash", _git_commit_hash())
        if is_latest:
            enriched.setdefault("status", "active")
        else:
            # Candidate 저장은 deploy gate 전 단계다. active status는 gate 내부에서만 허용한다.
            enriched["status"] = "candidate"

        # 2. required fields 검증 (자동 주입 후)
        self._assert_metadata_fields(enriched)

        version = str(enriched["version"])
        pkl_path = self.base_dir / f"{version}.pkl"
        meta_path = self.base_dir / f"{version}_metadata.json"

        # 3. pkl 저장
        with pkl_path.open("wb") as fh:
            pickle.dump(model, fh)

        # 4. metadata 경로 확정
        enriched["model_path"] = _path_for_metadata(pkl_path)
        enriched["metadata_path"] = _path_for_metadata(meta_path)

        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(enriched, fh, ensure_ascii=False, indent=2)

        logger.info(
            "[registry] 저장: version=%s, pkl=%s, metadata=%s",
            version, pkl_path.name, meta_path.name,
        )

        # 5. registry.json 갱신
        self._update_registry_index(enriched, is_latest=is_latest)

        # 6. latest symlink
        if is_latest:
            self._update_latest_pointer(pkl_path)

        # 7. 오래된 버전 정리 (archived)
        self._enforce_max_versions()

        return pkl_path

    def load_latest(self) -> tuple[Any, dict[str, Any]]:
        """현재 active 버전 로드. registry.active_version 참조."""
        registry = self._read_registry_index()
        active = registry.get("active_version")
        if not active:
            raise VersionNotFoundError("active_version 미설정 (빈 registry)")
        return self.load_version(active)

    def load_version(self, version: str) -> tuple[Any, dict[str, Any]]:
        """지정 버전 로드."""
        pkl_path = self.base_dir / f"{version}.pkl"
        meta_path = self.base_dir / f"{version}_metadata.json"

        if not pkl_path.exists():
            raise VersionNotFoundError(f"version={version} pkl 없음: {pkl_path}")
        if not meta_path.exists():
            raise VersionNotFoundError(f"version={version} metadata 없음")

        try:
            with pkl_path.open("rb") as fh:
                model = pickle.load(fh)
        except Exception as e:
            raise PklLoadFailedError(
                f"pickle load 실패 version={version}: {e}"
            ) from e

        try:
            with meta_path.open("r", encoding="utf-8") as fh:
                metadata = json.load(fh)
        except json.JSONDecodeError as e:
            raise RegistryCorruptedError(
                f"metadata JSON 파싱 실패 version={version}: {e}"
            ) from e
        except (IOError, OSError, PermissionError) as e:
            raise RegistryCorruptedError(
                f"metadata 읽기 실패 version={version} ({type(e).__name__}): {e}"
            ) from e

        return model, metadata

    def list_versions(self) -> list[dict[str, Any]]:
        """등록된 버전 metadata list (newest first)."""
        registry = self._read_registry_index()
        versions = registry.get("versions", [])
        # created_at 내림차순
        return sorted(
            versions,
            key=lambda m: m.get("created_at", ""),
            reverse=True,
        )

    def compare_versions(
        self,
        version_a: str,
        version_b: str,
        metric_keys: list[str] | None = None,
    ) -> dict[str, float]:
        """두 버전의 metrics 차이를 반환.

        반환 dict: {metric_key: metrics_b[key] - metrics_a[key]}
        기본 metric_keys = ['ic', 'icir', 'rank_ic', 'arr', 'ir', 'mdd', 'sr']

        L3 self_evolution_gain_sharpe 계산 선행 목적.
        예: compare_versions('baseline', 'v2')['sr'] = v2.sr - baseline.sr.

        Args:
            version_a: 비교 기준 버전 (이전 버전).
            version_b: 비교 대상 버전 (이후 버전).
            metric_keys: 비교할 metric 키 목록. None이면 기본 7종 사용.

        Returns:
            dict[str, float]: {metric_key: metrics_b - metrics_a}
            누락 키는 건너뜀 (양쪽 모두 존재하는 키만 포함).

        Raises:
            VersionNotFoundError: version_a 또는 version_b 미등록 시.
        """
        default_keys = ["ic", "icir", "rank_ic", "arr", "ir", "mdd", "sr"]
        keys = list(metric_keys) if metric_keys is not None else default_keys

        _, meta_a = self.load_version(version_a)
        _, meta_b = self.load_version(version_b)

        metrics_a: dict[str, float] = meta_a.get("metrics", {})
        metrics_b: dict[str, float] = meta_b.get("metrics", {})

        result: dict[str, float] = {}
        for k in keys:
            if k in metrics_a and k in metrics_b:
                result[k] = float(metrics_b[k]) - float(metrics_a[k])
            else:
                logger.debug(
                    "[registry] compare_versions: metric_key='%s' 누락 (a=%s, b=%s). 건너뜀.",
                    k, version_a, version_b,
                )
        return result

    def rollback(self, version: str) -> None:
        """latest_model.pkl을 지정 버전으로 되돌림. active_version 업데이트.

        주의 (C17 forbidden: mode_b_auto_rollback_without_approval):
            이 메서드는 approval 검증 없이 rollback을 수행한다.
            **C14 ModeBDeployer 경유 필수** - deploy gate에서 operator 승인 확인 후 호출.
            Mode A(장중) 경로에서의 직접 호출은 금지.
            수동 rollback은 operator가 CLI로 명시 실행.
        """
        pkl_path = self.base_dir / f"{version}.pkl"
        if not pkl_path.exists():
            raise VersionNotFoundError(
                f"rollback 대상 version={version} pkl 없음"
            )
        self._update_latest_pointer(pkl_path)

        # registry active_version 업데이트
        registry = self._read_registry_index()
        registry["active_version"] = version
        for entry in registry.get("versions", []):
            if entry.get("version") == version:
                entry["status"] = "active"
            elif entry.get("status") == "active":
                entry["status"] = "rollback"
        self._write_registry_index(registry)
        logger.info("[registry] rollback 완료: active=%s", version)

    def activate_deployed_candidate(
        self,
        version: str,
        *,
        deploy_token: object | None = None,
    ) -> dict[str, Any]:
        """C14 deploy gate 통과 후 candidate version을 active로 승격한다.

        일반 ``save(..., is_latest=True)`` 는 ``bundle_id``가 있는 후보를 막는다.
        이 메서드는 ModeBDeployer가 C12/C14 검증을 끝낸 뒤에만 호출하는 승격 경로다.
        """
        if deploy_token is not _DEPLOY_ACTIVATION_TOKEN:
            raise PermissionError(
                "activate_deployed_candidate는 ModeBDeployer 내부 deploy token 경유로만 호출 가능"
            )
        version = str(version).strip()
        if not version:
            raise VersionNotFoundError("activate 대상 version이 비어 있음")

        pkl_path = self.base_dir / f"{version}.pkl"
        meta_path = self.base_dir / f"{version}_metadata.json"
        if not pkl_path.exists():
            raise VersionNotFoundError(f"activate 대상 version={version} pkl 없음")
        if not meta_path.exists():
            raise VersionNotFoundError(f"activate 대상 version={version} metadata 없음")

        try:
            with meta_path.open("r", encoding="utf-8") as fh:
                metadata = json.load(fh)
        except json.JSONDecodeError as e:
            raise RegistryCorruptedError(
                f"metadata JSON 파싱 실패 version={version}: {e}"
            ) from e

        metadata["status"] = "active"
        metadata["model_path"] = _path_for_metadata(pkl_path)
        metadata["metadata_path"] = _path_for_metadata(meta_path)
        self._assert_metadata_fields(metadata)

        self._update_latest_pointer(pkl_path)

        registry = self._read_registry_index()
        versions: list[dict[str, Any]] = list(registry.get("versions", []))
        found = False
        for entry in versions:
            if entry.get("version") == version:
                entry.update(metadata)
                entry["status"] = "active"
                found = True
            elif entry.get("status") == "active":
                entry["status"] = "rollback"
        if not found:
            versions.append(metadata)

        registry["schema_version"] = self._schema_version
        registry["active_version"] = version
        registry["versions"] = versions
        self._write_registry_index(registry)

        with meta_path.open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, ensure_ascii=False, indent=2)

        logger.info("[registry] deploy 승격 완료: active=%s", version)
        return metadata

    def restore_active_version(
        self,
        version: str | None,
        *,
        clear_latest_pointer: bool = True,
    ) -> None:
        """C14 rollback 시 registry active_version을 이전 상태로 복원한다."""
        version_norm = str(version).strip() if version is not None else None
        version_norm = version_norm or None

        registry = self._read_registry_index()
        versions: list[dict[str, Any]] = list(registry.get("versions", []))

        if version_norm is None:
            registry["active_version"] = None
            for entry in versions:
                if entry.get("status") == "active":
                    entry["status"] = "candidate" if entry.get("bundle_id") else "rollback"
            if clear_latest_pointer:
                latest_path = self.base_dir / self._latest_name
                if latest_path.exists() or latest_path.is_symlink():
                    latest_path.unlink()
            registry["versions"] = versions
            self._write_registry_index(registry)
            logger.info("[registry] active_version 비움")
            return

        pkl_path = self.base_dir / f"{version_norm}.pkl"
        if not pkl_path.exists():
            raise VersionNotFoundError(
                f"restore 대상 version={version_norm} pkl 없음"
            )

        found = False
        for entry in versions:
            if entry.get("version") == version_norm:
                entry["status"] = "active"
                found = True
            elif entry.get("status") == "active":
                entry["status"] = "rollback"
        if not found:
            raise VersionNotFoundError(
                f"restore 대상 version={version_norm} registry 항목 없음"
            )

        self._update_latest_pointer(pkl_path)
        registry["schema_version"] = self._schema_version
        registry["active_version"] = version_norm
        registry["versions"] = versions
        self._write_registry_index(registry)
        logger.info("[registry] active_version 복원: %s", version_norm)

    # ================================================================== #
    # registry.json 관리
    # ================================================================== #

    def _read_registry_index(self) -> dict[str, Any]:
        path = self.base_dir / self._registry_file
        if not path.exists():
            return {
                "schema_version": self._schema_version,
                "active_version": None,
                "versions": [],
            }
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise RegistryCorruptedError(
                    f"registry.json이 dict 아님: {type(data)}"
                )
            return data
        except json.JSONDecodeError as e:
            raise RegistryCorruptedError(
                f"registry.json 파싱 실패: {e}"
            ) from e
        except (IOError, OSError, PermissionError) as e:
            # NFS/권한/I/O 장애 → RegistryCorruptedError로 wrap (호출자 예상 가능)
            raise RegistryCorruptedError(
                f"registry.json 읽기 실패 ({type(e).__name__}): {e}"
            ) from e

    def _write_registry_index(self, data: dict[str, Any]) -> None:
        path = self.base_dir / self._registry_file
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    def _update_registry_index(
        self,
        metadata: dict[str, Any],
        is_latest: bool,
    ) -> None:
        registry = self._read_registry_index()
        versions: list[dict[str, Any]] = list(registry.get("versions", []))

        # 기존 동일 버전 제거
        version = metadata["version"]
        versions = [v for v in versions if v.get("version") != version]

        # is_latest 시 기존 active → rollback 태그
        if is_latest:
            for v in versions:
                if v.get("status") == "active":
                    v["status"] = "rollback"

        versions.append(metadata)
        registry["schema_version"] = self._schema_version
        registry["versions"] = versions
        if is_latest:
            registry["active_version"] = version
        self._write_registry_index(registry)

    # ================================================================== #
    # Latest symlink 관리
    # ================================================================== #

    def _update_latest_pointer(self, pkl_path: Path) -> None:
        """latest_model.pkl → pkl_path atomic 교체. symlink 우선, 실패 시 copy fallback.

        Atomic 규약 (Sprint 3 야간 재학습 동시성 대비):
            1. symlink: `.tmp` suffix로 새 symlink 생성 후 `os.replace` atomic rename.
            2. copy: `.tmp` 임시 파일로 copy 후 `os.replace` atomic rename.
            중간 kill되어도 stale latest_model.pkl이 남지 않는다.
        """
        latest_path = self.base_dir / self._latest_name
        tmp_path = self.base_dir / (self._latest_name + ".tmp")

        # 기존 tmp 흔적 제거 (전 프로세스 crash 잔재)
        if tmp_path.exists() or tmp_path.is_symlink():
            try:
                tmp_path.unlink()
            except OSError:
                pass

        try:
            # relative symlink (pkl_path.name)
            os.symlink(pkl_path.name, tmp_path)
            os.replace(tmp_path, latest_path)  # atomic rename
            logger.info(
                "[registry] symlink atomic 갱신: %s → %s",
                latest_path, pkl_path.name,
            )
        except OSError as e:
            logger.warning(
                "[registry] symlink 실패 (%s). copy fallback (atomic rename).", e
            )
            try:
                if tmp_path.exists() or tmp_path.is_symlink():
                    tmp_path.unlink()
            except OSError:
                pass
            shutil.copy2(pkl_path, tmp_path)
            os.replace(tmp_path, latest_path)

    # ================================================================== #
    # 메타데이터 검증
    # ================================================================== #

    def _assert_metadata_fields(self, metadata: dict[str, Any]) -> None:
        missing = [f for f in self._required_fields if f not in metadata]
        if missing:
            raise KeyError(
                f"metadata 필수 필드 누락: {missing}. "
                f"risk_config.yaml model_registry.metadata_required_fields 참조."
            )

    # ================================================================== #
    # retention (max_versions_keep)
    # ================================================================== #

    def _enforce_max_versions(self) -> None:
        """저장된 버전 수 > max_versions_keep 이면 오래된 버전 archived 마킹.

        실제 파일 삭제는 하지 않음 (사용자 승인 필요). status만 업데이트.
        """
        registry = self._read_registry_index()
        versions: list[dict[str, Any]] = list(registry.get("versions", []))
        if len(versions) <= self._max_versions_keep:
            return
        # created_at 오름차순 → 오래된 것부터 archived
        sorted_v = sorted(versions, key=lambda m: m.get("created_at", ""))
        to_archive = sorted_v[:-self._max_versions_keep]
        changed = False
        for v in to_archive:
            if v.get("status") != "archived" and v.get("version") != registry.get("active_version"):
                v["status"] = "archived"
                changed = True
        if changed:
            self._write_registry_index(registry)
            logger.info(
                "[registry] %d 개 오래된 버전 → archived 마킹",
                len(to_archive),
            )


# ====================================================================== #
# Helpers
# ====================================================================== #


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _git_commit_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.debug("[registry] git commit hash 조회 실패: %s", e)
    return "unknown"
