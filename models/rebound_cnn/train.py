"""
KR-Rebound-CNN Training Pipeline (v1.0)
- AdamW optimizer (weight_decay=0.01)
- Weighted BCE loss (pos_weight = n_neg / n_pos)
- 3-seed ensemble: model_seed42.pt, model_seed123.pt, model_seed456.pt
- walk-forward expanding window 학습
- early stopping (patience=8) + gradient clipping
- confidence calibration (isotonic regression)
- ensemble variance → uncertainty_score

Usage:
    python models/rebound_cnn/train.py
    python models/rebound_cnn/train.py --config models/rebound_cnn/config.yaml
"""

import argparse
import json
import pickle
import random
import sys
from pathlib import Path

import numpy as np
import yaml

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE_DIR))

MODEL_DIR = Path(__file__).resolve().parent


def _set_seed(seed: int = 42):
    random.seed(seed)
    np.random.seed(seed)
    try:
        import torch
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except ImportError:
        pass


def _get_device():
    try:
        import torch
        if torch.backends.mps.is_available():
            return torch.device("mps")
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")
    except ImportError:
        return None


def _compute_pos_weight(dataset) -> float:
    """
    Weighted BCE를 위한 pos_weight 계산.
    pos_weight = n_neg / n_pos (설계서 기준)
    """
    labels = [float(s["label"]) for s in dataset.samples]
    n_pos = sum(labels)
    n_neg = len(labels) - n_pos
    if n_pos == 0:
        return 1.0
    return n_neg / n_pos


def _collect_model_outputs(model, loader, device):
    """모델 추론: (probs, labels) numpy array 반환."""
    import torch
    model.eval()
    all_probs = []
    all_labels = []
    with torch.no_grad():
        for chart, context, label in loader:
            chart = chart.to(device)
            context = context.to(device)
            prob = model(chart, context)  # (B, 1), Sigmoid 출력
            all_probs.extend(prob.squeeze(1).cpu().numpy().tolist())
            all_labels.extend(label.numpy().tolist())
    return np.array(all_probs), np.array(all_labels)


def _compute_auc(labels: np.ndarray, probs: np.ndarray) -> float:
    """AUC-ROC 계산."""
    try:
        from sklearn.metrics import roc_auc_score
        if len(np.unique(labels)) < 2:
            return 0.5
        return float(roc_auc_score(labels, probs))
    except Exception:
        return 0.5


def train_one_fold(
    model,
    train_loader,
    val_loader,
    config: dict,
    device,
    fold_idx: int,
    seed: int = 42,
    pos_weight: float = 1.0,
) -> dict:
    """
    단일 fold 학습.
    - AdamW optimizer
    - Weighted BCE loss (pos_weight)
    - early stopping (patience=8)
    - gradient clipping
    fold 결과 dict 반환.
    """
    import torch
    import torch.nn as nn
    import torch.optim as optim

    _set_seed(seed)

    lr = config["training"]["learning_rate"]
    max_epochs = config["training"]["max_epochs"]
    patience = config["training"]["early_stopping_patience"]
    grad_clip = config["training"]["gradient_clip"]
    weight_decay = config["training"].get("weight_decay", 0.01)

    optimizer = optim.AdamW(model.parameters(), lr=lr, weight_decay=weight_decay)

    # Weighted BCE 수동 구현 (BCEWithLogitsLoss 대신 Sigmoid가 모델에 내장)
    # label=1 샘플에 pos_weight 적용
    def weighted_bce_loss(pred: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
        """pred: (B,1) Sigmoid 출력, target: (B,1) 0/1 label."""
        eps = 1e-7
        pred_clamped = torch.clamp(pred, eps, 1.0 - eps)
        bce = -(target * torch.log(pred_clamped) * pos_weight
                + (1 - target) * torch.log(1 - pred_clamped))
        return bce.mean()

    best_val_loss = float("inf")
    best_epoch = 0
    no_improve = 0
    best_state = None

    fold_log = {
        "fold": fold_idx,
        "seed": seed,
        "pos_weight": round(pos_weight, 4),
        "train_loss_history": [],
        "val_loss_history": [],
        "best_epoch": 0,
        "best_val_loss": None,
        "val_auc": None,
    }

    for epoch in range(1, max_epochs + 1):
        # 학습 단계
        model.train()
        train_losses = []
        for chart, context, label in train_loader:
            chart = chart.to(device)
            context = context.to(device)
            label = label.to(device).unsqueeze(1)

            optimizer.zero_grad()
            prob = model(chart, context)
            loss = weighted_bce_loss(prob, label)

            if torch.isnan(loss) or torch.isinf(loss):
                print(f"[Modeler] Fold {fold_idx} Epoch {epoch}: NaN/Inf loss 감지, 배치 스킵")
                continue

            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
            optimizer.step()
            train_losses.append(loss.item())

        # 검증 단계
        model.eval()
        val_losses = []
        with torch.no_grad():
            for chart, context, label in val_loader:
                chart = chart.to(device)
                context = context.to(device)
                label = label.to(device).unsqueeze(1)
                prob = model(chart, context)
                loss = weighted_bce_loss(prob, label)
                if not (torch.isnan(loss) or torch.isinf(loss)):
                    val_losses.append(loss.item())

        train_loss = float(np.mean(train_losses)) if train_losses else float("nan")
        val_loss = float(np.mean(val_losses)) if val_losses else float("nan")

        fold_log["train_loss_history"].append(round(train_loss, 6))
        fold_log["val_loss_history"].append(round(val_loss, 6))

        print(
            f"[Modeler] Fold {fold_idx} Seed {seed} Epoch {epoch}/{max_epochs} "
            f"train_loss={train_loss:.4f} val_loss={val_loss:.4f}"
        )

        if not np.isnan(val_loss) and val_loss < best_val_loss:
            best_val_loss = val_loss
            best_epoch = epoch
            no_improve = 0
            best_state = {k: v.clone() for k, v in model.state_dict().items()}
        else:
            no_improve += 1
            if no_improve >= patience:
                print(f"[Modeler] Fold {fold_idx} Seed {seed} Early stopping at epoch {epoch}")
                break

    # best state 복원
    if best_state is not None:
        model.load_state_dict(best_state)

    val_probs, val_labels = _collect_model_outputs(model, val_loader, device)
    val_auc = _compute_auc(val_labels, val_probs)

    fold_log["best_epoch"] = best_epoch
    fold_log["best_val_loss"] = round(best_val_loss, 6)
    fold_log["val_auc"] = round(val_auc, 4)

    print(
        f"[Modeler] Fold {fold_idx} Seed {seed} 완료: best_epoch={best_epoch}, "
        f"val_loss={best_val_loss:.4f}, val_auc={val_auc:.4f}"
    )
    return fold_log


def run_isotonic_calibration(model, val_loader, device, min_samples: int = 100) -> dict:
    """
    Isotonic regression calibrator 학습. calibrator 객체와 메트릭 반환.
    하위 호환용으로 유지. 내부적으로 run_calibration_comparison을 호출한다.
    """
    result = run_calibration_comparison(model, val_loader, device, min_samples=min_samples)
    return {
        "calibrator": result["calibrator"],
        "metrics": {
            "n_samples": result["meta"]["raw"].get("n_samples", 0),
            "brier_raw": result["meta"]["raw"]["brier"],
            "brier_calibrated": result["meta"][result["meta"]["winner"]]["brier"],
            "improvement": round(
                result["meta"]["raw"]["brier"]
                - result["meta"][result["meta"]["winner"]]["brier"],
                6,
            ),
            "winner": result["meta"]["winner"],
        },
    }


def _compute_ece(probs: np.ndarray, labels: np.ndarray, n_bins: int = 10) -> float:
    """Expected Calibration Error (ECE) 계산."""
    bin_edges = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for i in range(n_bins):
        mask = (probs >= bin_edges[i]) & (probs < bin_edges[i + 1])
        if mask.sum() == 0:
            continue
        acc = float(labels[mask].mean())
        conf = float(probs[mask].mean())
        ece += mask.sum() / len(probs) * abs(acc - conf)
    return float(ece)


def run_calibration_comparison(
    model, val_loader, device, min_samples: int = 100
) -> dict:
    """Temperature Scaling vs Isotonic Regression 비교.

    두 방법을 모두 학습하고 Brier score 우선으로 winner를 결정.
    winner calibrator를 calibrator.pkl로 저장하고,
    비교 결과를 calibrator_meta.json으로도 저장한다.

    반환: {"calibrator": winner_obj, "meta": comparison_dict}
    """
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss

    probs, labels = _collect_model_outputs(model, val_loader, device)
    n = len(probs)
    print(f"[Modeler] Calibration 비교 시작 — 샘플: {n}개")

    if n < min_samples:
        print(
            f"[Modeler] 경고: calibration 샘플 {n} < min_samples {min_samples}. 그대로 진행."
        )

    # 1. Raw
    brier_raw = float(brier_score_loss(labels, probs))
    ece_raw = _compute_ece(probs, labels)

    # 2. Temperature Scaling (logit 역변환 후 T로 나눠 sigmoid)
    eps = 1e-8
    logits = np.log(
        np.clip(probs, eps, 1 - eps) / (1 - np.clip(probs, eps, 1 - eps))
    )

    best_T = 1.0
    best_brier_temp = brier_raw
    for T in np.arange(0.1, 5.0, 0.1):
        cal_probs_t = 1.0 / (1.0 + np.exp(-logits / T))
        brier_t = float(brier_score_loss(labels, cal_probs_t))
        if brier_t < best_brier_temp:
            best_T = float(T)
            best_brier_temp = brier_t

    best_temp_probs = 1.0 / (1.0 + np.exp(-logits / best_T))
    ece_temp = _compute_ece(best_temp_probs, labels)

    # 3. Isotonic Regression
    iso = IsotonicRegression(out_of_bounds="clip")
    iso.fit(probs, labels)
    cal_probs_iso = iso.predict(probs)
    brier_iso = float(brier_score_loss(labels, cal_probs_iso))
    ece_iso = _compute_ece(cal_probs_iso, labels)

    # 4. Winner 결정 (Brier 우선)
    if best_brier_temp <= brier_iso:
        winner = "temperature"
        calibrator = {"type": "temperature", "T": best_T}
    else:
        winner = "isotonic"
        calibrator = iso

    meta = {
        "winner": winner,
        "raw": {
            "n_samples": n,
            "brier": round(brier_raw, 6),
            "ece": round(ece_raw, 6),
        },
        "temperature": {
            "brier": round(best_brier_temp, 6),
            "ece": round(ece_temp, 6),
            "T": round(best_T, 2),
        },
        "isotonic": {
            "brier": round(brier_iso, 6),
            "ece": round(ece_iso, 6),
        },
    }

    print(
        f"[Modeler] Calibration 비교 완료: "
        f"raw_brier={brier_raw:.4f}, "
        f"temperature_brier={best_brier_temp:.4f}(T={best_T:.1f}), "
        f"isotonic_brier={brier_iso:.4f} → winner={winner}"
    )

    # calibrator_meta.json 저장
    meta_path = MODEL_DIR / "calibrator_meta.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[Modeler] calibrator_meta.json 저장: {meta_path}")

    return {"calibrator": calibrator, "meta": meta}


def train_ensemble_seeds(
    train_ds,
    val_ds,
    train_loader,
    val_loader,
    config: dict,
    device,
    fold_idx: int,
    pos_weight: float,
    model_dir: Path,
) -> dict:
    """
    3-seed ensemble 학습.
    각 seed별 model_seed{seed}.pt 저장.
    ensemble 평균 확률 + variance(uncertainty_score) 계산.
    반환: ensemble 결과 dict
    """
    import torch
    from models.rebound_cnn.model import build_model

    seeds = config["training"].get("ensemble_seeds", [42, 123, 456])
    seed_probs_list = []
    seed_logs = []
    seed_models = []

    for seed in seeds:
        print(f"[Modeler] === Ensemble Seed {seed} 학습 시작 ===")
        n_ctx = train_ds.n_context_features
        model = build_model(n_context_features=n_ctx, device=device)
        fold_result = train_one_fold(
            model, train_loader, val_loader, config, device,
            fold_idx=fold_idx, seed=seed, pos_weight=pos_weight
        )
        seed_logs.append(fold_result)

        # seed별 모델 저장
        model_path = model_dir / f"model_seed{seed}.pt"
        torch.save(model.state_dict(), model_path)
        print(f"[Modeler] Seed {seed} 모델 저장: {model_path}")

        # val 추론 결과 수집
        val_probs, val_labels = _collect_model_outputs(model, val_loader, device)
        seed_probs_list.append(val_probs)
        seed_models.append(model)

    # Ensemble: seed별 확률 평균 + 분산
    probs_matrix = np.stack(seed_probs_list, axis=0)  # (n_seeds, n_samples)
    ensemble_mean = probs_matrix.mean(axis=0)          # (n_samples,)
    ensemble_var = probs_matrix.var(axis=0)            # (n_samples,) → uncertainty_score

    print(
        f"[Modeler] Ensemble 완료: mean_prob={ensemble_mean.mean():.4f}, "
        f"mean_uncertainty={ensemble_var.mean():.6f}"
    )

    # 최선 seed 모델 (val_auc 기준) → model.pt로 복사
    best_seed_idx = int(np.argmax([log["val_auc"] for log in seed_logs]))
    best_seed = seeds[best_seed_idx]
    best_model = seed_models[best_seed_idx]
    torch.save(best_model.state_dict(), model_dir / "model.pt")
    print(f"[Modeler] 최선 seed={best_seed} (val_auc={seed_logs[best_seed_idx]['val_auc']:.4f}) → model.pt 저장")

    return {
        "seeds": seeds,
        "seed_logs": seed_logs,
        "best_seed": best_seed,
        "ensemble_mean_prob": round(float(ensemble_mean.mean()), 6),
        "ensemble_mean_uncertainty": round(float(ensemble_var.mean()), 8),
        "best_model": best_model,
        "val_labels": val_labels,
    }


def build_fallback_split(all_dates: list, ratio: float = 0.8):
    """날짜 부족 시 80/20 fallback split."""
    n = len(all_dates)
    split = int(n * ratio)
    return all_dates[:split], all_dates[split:]


def run_training(config_path: Path):
    """메인 학습 파이프라인."""
    import torch
    import torch.utils.data as td_utils

    from models.rebound_cnn.dataset import ReboundDataset
    from models.rebound_cnn.model import build_model

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    seed = config["training"]["seed"]
    _set_seed(seed)

    device = _get_device()
    print(f"[Modeler] device={device}, seed={seed}")

    dmp_dir = _BASE_DIR / "artifacts" / "daily_market_packet"
    dmp_files = sorted(dmp_dir.glob("DMP-*.json"))
    all_dates = [p.stem.replace("DMP-", "") for p in dmp_files]

    if not all_dates:
        print("[Modeler] DMP 파일이 없습니다. 학습 중단.")
        return

    print(f"[Modeler] 가용 DMP 날짜: {len(all_dates)}일")

    wf = config["training"]["walk_forward"]
    wf_mode = wf.get("mode", "expanding")  # "expanding" 또는 "sliding"
    train_w = wf["train_window"]
    val_w = wf["val_window"]
    step = wf["step_size"]
    min_hist = config["data"]["min_history_days"]
    batch_size = config["training"]["batch_size"]
    lookback = config["data"]["lookback_days"]
    horizon = config["data"]["forecast_horizon"]

    training_log = {
        "config_path": str(config_path),
        "total_dates": len(all_dates),
        "walk_forward_mode": wf_mode,
        "folds": [],
        "calibration": None,
        "ensemble_summary": None,
        "final_model_path": str(MODEL_DIR / "model.pt"),
        "calibrator_path": str(MODEL_DIR / "calibrator.pkl"),
        "context_scaler_path": str(MODEL_DIR / "context_scaler.pkl"),
        "n_context_features": None,  # fold 첫 번째 실행 후 채워짐
    }

    print(f"[Modeler] walk-forward mode={wf_mode}")

    # walk-forward folds 구성
    folds = []
    if len(all_dates) >= min_hist:
        for start in range(0, len(all_dates) - train_w - val_w + 1, step):
            if wf_mode == "expanding":
                # expanding: 시작점 고정(0), 끝점 = start + train_w (점점 확장)
                train_dates = all_dates[0: start + train_w]
            else:
                # sliding: 기존 동작 (고정 크기 윈도우 슬라이딩)
                train_dates = all_dates[start: start + train_w]
            val_dates = all_dates[start + train_w: start + train_w + val_w]
            if len(train_dates) > lookback + horizon and len(val_dates) > horizon:
                folds.append((train_dates, val_dates))

    # folds 없으면 fallback
    if not folds:
        print(
            f"[Modeler] 경고: 가용 날짜 {len(all_dates)}일 < min_history_days {min_hist} "
            "또는 walk-forward fold 구성 불가. 전체 데이터 80/20 fallback."
        )
        train_dates, val_dates = build_fallback_split(all_dates)
        if len(train_dates) <= lookback + horizon or len(val_dates) <= horizon:
            print("[Modeler] 경고: fallback split도 샘플 부족. 전체를 train+val 겸용으로 사용.")
            train_dates = all_dates
            val_dates = all_dates
        folds = [(train_dates, val_dates)]

    print(f"[Modeler] 학습 fold 수: {len(folds)}")

    best_val_auc_global = -float("inf")
    best_ensemble_result = None
    last_val_loader = None
    last_best_model = None

    for fold_idx, (train_dates, val_dates) in enumerate(folds, 1):
        print(f"[Modeler] === Fold {fold_idx}/{len(folds)} ===")

        train_ds = ReboundDataset(dmp_dir, train_dates, config)
        # val dataset: train_dates + val_dates를 history로 로드하여 lookback 확보,
        # 샘플은 val_dates에서만 생성 (out-of-time validation, PIT-safe)
        val_context_dates = train_dates + val_dates
        val_ds = ReboundDataset(dmp_dir, val_context_dates, config, sample_dates=val_dates)

        if len(train_ds) == 0:
            print(f"[Modeler] Fold {fold_idx} train 샘플 0개 → 스킵")
            continue
        if len(val_ds) == 0:
            print(f"[Modeler] Fold {fold_idx} val 샘플 0개 → train 샘플을 val로 겸용")
            val_ds = train_ds

        # pos_weight 계산 (n_neg / n_pos)
        pos_weight = _compute_pos_weight(train_ds)
        print(f"[Modeler] Fold {fold_idx} pos_weight={pos_weight:.4f}")

        train_loader = td_utils.DataLoader(
            train_ds, batch_size=batch_size, shuffle=True, drop_last=False
        )
        val_loader = td_utils.DataLoader(
            val_ds, batch_size=batch_size, shuffle=False
        )

        # StandardScaler fit (train set 기준) + val에 transform 적용
        # 설계서 §7.2: scaling fit은 train fold에서만
        scaler = train_ds.fit_scaler()
        train_ds.apply_scaler(scaler)
        val_ds.apply_scaler(scaler)

        # scaler 저장 (fold별 최신 train fold scaler를 model_dir에 저장)
        scaler_path = MODEL_DIR / "context_scaler.pkl"
        with open(scaler_path, "wb") as f:
            pickle.dump(scaler, f)
        print(f"[Modeler] Fold {fold_idx} context_scaler.pkl 저장: {scaler_path}")
        if training_log["n_context_features"] is None:
            training_log["n_context_features"] = train_ds.n_context_features
            print(f"[Modeler] n_context_features={train_ds.n_context_features}")

        # 3-seed ensemble 학습
        ensemble_result = train_ensemble_seeds(
            train_ds, val_ds, train_loader, val_loader,
            config, device, fold_idx, pos_weight, MODEL_DIR
        )

        # fold 로그 취합
        fold_summary = {
            "fold": fold_idx,
            "train_days": len(train_dates),
            "val_days": len(val_dates),
            "train_samples": len(train_ds),
            "val_samples": len(val_ds),
            "pos_weight": round(pos_weight, 4),
            "ensemble_seeds": ensemble_result["seeds"],
            "best_seed": ensemble_result["best_seed"],
            "seed_logs": ensemble_result["seed_logs"],
            "ensemble_mean_prob": ensemble_result["ensemble_mean_prob"],
            "ensemble_mean_uncertainty": ensemble_result["ensemble_mean_uncertainty"],
        }
        training_log["folds"].append(fold_summary)

        # 전체 기준 최선 fold 선택 (val_auc 기준)
        best_seed_auc = max(
            log["val_auc"] for log in ensemble_result["seed_logs"]
        )
        if best_seed_auc > best_val_auc_global:
            best_val_auc_global = best_seed_auc
            best_ensemble_result = ensemble_result
            last_val_loader = val_loader
            last_best_model = ensemble_result["best_model"]

    if last_best_model is None:
        print("[Modeler] 학습된 모델이 없습니다. 종료.")
        return

    # Calibration 비교 (Temperature Scaling vs Isotonic Regression) + winner 저장
    cal_result = None
    if last_val_loader is not None:
        try:
            cal_result = run_calibration_comparison(
                last_best_model,
                last_val_loader,
                device,
                min_samples=config["calibration"]["min_samples"],
            )
            cal_path = MODEL_DIR / "calibrator.pkl"
            with open(cal_path, "wb") as f:
                pickle.dump(cal_result["calibrator"], f)
            print(f"[Modeler] Calibrator 저장 (winner={cal_result['meta']['winner']}): {cal_path}")
            training_log["calibration"] = cal_result["meta"]
        except Exception as e:
            print(f"[Modeler] Calibration 비교 실패: {e}")

    # ensemble 요약 저장
    if best_ensemble_result is not None:
        training_log["ensemble_summary"] = {
            "seeds": best_ensemble_result["seeds"],
            "best_seed": best_ensemble_result["best_seed"],
            "mean_prob": best_ensemble_result["ensemble_mean_prob"],
            "mean_uncertainty": best_ensemble_result["ensemble_mean_uncertainty"],
        }

    # split_manifest 저장 (실험 재현성)
    training_log["split_manifest"] = {
        "all_dates": all_dates,
        "folds": [
            {
                "fold_idx": fold_idx,
                "train_dates": train_dates,
                "val_dates": val_dates,
            }
            for fold_idx, (train_dates, val_dates) in enumerate(folds, 1)
        ],
    }

    # Committee v1.1: ElasticNet 학습 (committee.enabled=true 시만 실행)
    if config.get("committee", {}).get("enabled", False):
        try:
            from models.rebound_cnn.committee import (
                extract_context_arrays,
                train_elasticnet,
                save_elasticnet,
            )

            if last_val_loader is not None and best_ensemble_result is not None:
                # 마지막 fold의 train/val dataset을 재구성
                last_fold_train_dates, last_fold_val_dates = folds[-1]
                train_ds_enet = ReboundDataset(dmp_dir, last_fold_train_dates, config)
                val_ds_enet = ReboundDataset(dmp_dir, last_fold_val_dates, config)

                # train fold 기준 scaler 로드 후 적용
                scaler_path_enet = MODEL_DIR / "context_scaler.pkl"
                if scaler_path_enet.exists():
                    with open(scaler_path_enet, "rb") as _f:
                        _scaler = pickle.load(_f)
                    train_ds_enet.apply_scaler(_scaler)
                    val_ds_enet.apply_scaler(_scaler)

                X_train_e, y_train_e = extract_context_arrays(train_ds_enet)
                X_val_e, y_val_e = extract_context_arrays(val_ds_enet)

                enet, enet_log = train_elasticnet(
                    X_train_e, y_train_e, X_val_e, y_val_e, config
                )
                save_elasticnet(enet)
                training_log["elasticnet"] = enet_log
                print(
                    f"[Modeler] Committee v1.1 ElasticNet 학습 완료: "
                    f"val_auc={enet_log['val_auc']}"
                )
            else:
                print("[Modeler] Committee: last_val_loader 없음, ElasticNet 학습 스킵")
        except Exception as e:
            print(f"[Modeler] Committee ElasticNet 학습 실패: {e}")
    else:
        print("[Modeler] Committee v1.1 비활성화 (committee.enabled=false). ElasticNet 학습 스킵.")

    # 학습 로그 저장
    log_path = MODEL_DIR / "training_log.json"
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(training_log, f, ensure_ascii=False, indent=2)
    print(f"[Modeler] 학습 로그 저장: {log_path}")

    print("[Modeler] 학습 파이프라인 완료.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="KR-Rebound-CNN Training")
    parser.add_argument(
        "--config",
        type=str,
        default=str(MODEL_DIR / "config.yaml"),
        help="config.yaml 경로",
    )
    args = parser.parse_args()
    run_training(Path(args.config))
