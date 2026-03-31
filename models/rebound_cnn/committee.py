"""
KR-Rebound-Committee v2.0 (GPT Pro #8)
- Stage B1: LightGBM tree core on context features (tabular core)
- Stage B2: CNN branch (existing KR-Rebound-CNN)
- Stage C: score fusion + agreement-based uncertainty

committee.enabled=false 기본값: 기존 동작 무변경.
v2.0: LightGBM tree core (한국 시장 data-constrained context에서 tree 우위)
      SGDClassifier fallback 유지 (LightGBM 미설치 시)
"""

import pickle
import numpy as np
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = Path(__file__).resolve().parent


def train_tree_core(X_train, y_train, X_val, y_val, config: dict) -> tuple:
    """Tree core (LightGBM) 학습. LightGBM 미설치 시 SGDClassifier fallback.

    GPT Pro #8: 한국 시장 data-constrained context에서 tree-based가 안정적.
    반환: (model, metrics_dict)
    """
    from sklearn.metrics import roc_auc_score, brier_score_loss

    # LightGBM 우선 시도
    try:
        import lightgbm as lgb
        use_tree = True
    except ImportError:
        use_tree = False

    if use_tree:
        print(f"[Committee] LightGBM tree core 학습 시작: train={len(X_train)}, val={len(X_val)}")

        pos_count = int(y_train.sum())
        neg_count = len(y_train) - pos_count
        scale_pos = neg_count / pos_count if pos_count > 0 else 1.0

        model = lgb.LGBMClassifier(
            n_estimators=100,
            num_leaves=15,
            learning_rate=0.05,
            min_child_samples=max(5, len(X_train) // 50),
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,
            reg_lambda=0.1,
            scale_pos_weight=scale_pos,
            verbose=-1,
            n_jobs=1,  # MPS + LightGBM OMP 충돌 방지
        )
        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)] if len(X_val) > 0 else None,
            callbacks=[lgb.early_stopping(stopping_rounds=15, verbose=False)] if len(X_val) > 0 else None,
        )
        model_type = "LightGBM"
    else:
        # SGDClassifier fallback (LightGBM 미설치 시)
        from sklearn.linear_model import SGDClassifier
        tc_cfg = config.get("tree_core", {})
        alpha = tc_cfg.get("alpha", 0.01)
        l1_ratio = tc_cfg.get("l1_ratio", 0.5)
        print(f"[Committee] SGDClassifier fallback: alpha={alpha}, l1_ratio={l1_ratio}")
        model = SGDClassifier(
            loss="log_loss", penalty="elasticnet",
            alpha=alpha, l1_ratio=l1_ratio, max_iter=1000, random_state=42,
        )
        model.fit(X_train, y_train)
        model_type = "SGDClassifier"

    train_prob = model.predict_proba(X_train)[:, 1]
    train_auc = roc_auc_score(y_train, train_prob) if len(np.unique(y_train)) >= 2 else 0.5

    if len(X_val) > 0 and len(y_val) > 0:
        val_prob = model.predict_proba(X_val)[:, 1]
        val_auc = roc_auc_score(y_val, val_prob) if len(np.unique(y_val)) >= 2 else 0.5
        val_brier = float(brier_score_loss(y_val, val_prob))
    else:
        val_auc = train_auc
        val_brier = float("nan")

    metrics = {
        "train_auc": round(float(train_auc), 4),
        "val_auc": round(float(val_auc), 4),
        "val_brier": round(val_brier, 4) if not np.isnan(val_brier) else None,
        "n_features": int(X_train.shape[1]),
        "model_type": model_type,
    }
    print(f"[Committee] {model_type} 완료: train_auc={metrics['train_auc']}, val_auc={metrics['val_auc']}")
    return model, metrics


def fuse_scores(
    p_tab: float,
    p_cnn: float,
    tab_weight: float = 0.70,
    cnn_weight: float = 0.30,
    agreement_threshold: float = 0.55,
) -> tuple:
    """Committee score fusion.

    p_tab: LightGBM tree core calibrated probability
    p_cnn: CNN confirmatory branch probability
    반환: (p_final, agreement, uncertainty_score)

    CNN은 confirmatory branch: disagreement 시 hold 보수화.
    """
    agreement = 1.0 - abs(p_tab - p_cnn)
    p_final = tab_weight * p_tab + cnn_weight * p_cnn

    # disagreement 크고 buy threshold 미달이면 보수적으로 억제
    if agreement < agreement_threshold and p_final < 0.70:
        p_final = min(p_final, 0.54)

    uncertainty_score = 1.0 - agreement
    return float(p_final), float(agreement), float(uncertainty_score)


def save_tree_core(model, path: Path = None):
    """Tree core 모델을 pickle로 저장."""
    if path is None:
        path = MODEL_DIR / "tree_core.pkl"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(model, f)
    print(f"[Committee] Tree core 저장: {path}")


def load_tree_core(path: Path = None):
    """Tree core 모델 로드. 파일 없으면 None 반환."""
    if path is None:
        path = MODEL_DIR / "tree_core.pkl"
    path = Path(path)
    if not path.exists():
        print(f"[Committee] tree_core.pkl 없음: {path}")
        return None
    with open(path, "rb") as f:
        model = pickle.load(f)
    print(f"[Committee] Tree core 로드: {path}")
    return model


def extract_context_arrays(dataset) -> tuple:
    """ReboundDataset.samples에서 numpy 배열 추출.

    반환: (X: np.ndarray shape (N, n_features), y: np.ndarray shape (N,))
    dataset.samples의 각 샘플에 context_features, label 키가 있어야 한다.
    """
    X_list = []
    y_list = []
    for s in dataset.samples:
        ctx = s["context_features"]
        if hasattr(ctx, "numpy"):
            ctx = ctx.numpy()
        else:
            ctx = np.array(ctx, dtype=np.float32)
        X_list.append(ctx)
        label = s["label"]
        if hasattr(label, "item"):
            label = label.item()
        y_list.append(float(label))
    return np.array(X_list, dtype=np.float32), np.array(y_list, dtype=np.float32)
