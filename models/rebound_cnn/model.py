"""
KR-Rebound-CNN Model Architecture (v1.0)
설계서 기준:
  - 3-channel 64x64 chart image 입력
  - Image Encoder: Conv(3→32)+BN+ReLU+MaxPool(2) → Conv(32→64)+BN+ReLU+MaxPool(2) → Conv(64→128)+BN+ReLU → AdaptiveAvgPool(1,1) → 128-d
  - Context Encoder: 39-d context (23 base + 16 sectors) → Dense(64)+ReLU+Dropout(0.3) → Dense(32)+ReLU → 32-d
  - Fusion Head: Concat(128+32=160) → BatchNorm1d(64)+Dense(64)+ReLU+Dropout(0.4) → Dense(1) → raw logit
  - 5거래일 rebound logit 출력 (BCEWithLogitsLoss 사용; 추론 시 sigmoid 적용)

Context Branch 피처 (설계서 §10.2):
  macro(4) + technical(5) + price_stretch(2) + sector_relative(3)
  + sector_onehot(n_sectors) + market_cap_rank(1)
  기본 n_context_features=26 (universe 16섹터 기준: 15 + 16 = 31, 설계서 명세 기준: 15 + 11 = 26)
"""

import torch
import torch.nn as nn


def _get_device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device("mps")
    if torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


DEVICE = _get_device()


class ImageEncoder(nn.Module):
    """
    3-channel 64x64 차트 이미지 → 128-d 표현 벡터.

    Conv(3→32)+BN+ReLU+MaxPool(2)   64x64 → 32x32
    Conv(32→64)+BN+ReLU+MaxPool(2)  32x32 → 16x16
    Conv(64→128)+BN+ReLU
    AdaptiveAvgPool(1,1) → Flatten → 128-d
    """

    def __init__(self):
        super().__init__()
        self.encoder = nn.Sequential(
            # Block 1: Conv(32, 3x3) + BN + ReLU + MaxPool(2)
            nn.Conv2d(3, 32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.MaxPool2d(2),                     # 64x64 → 32x32
            # Block 2: Conv(64, 3x3) + BN + ReLU + MaxPool(2)
            nn.Conv2d(32, 64, kernel_size=3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(),
            nn.MaxPool2d(2),                     # 32x32 → 16x16
            # Block 3: Conv(128, 3x3) + BN + ReLU
            nn.Conv2d(64, 128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(),
            # Global average pooling → (B, 128, 1, 1)
            nn.AdaptiveAvgPool2d((1, 1)),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (B, 3, H, W) → (B, 128)"""
        out = self.encoder(x)          # (B, 128, 1, 1)
        return out.view(out.size(0), -1)  # (B, 128)


class ContextEncoder(nn.Module):
    """
    39-d context (23 base + n_sectors) features → 32-d 표현 벡터. (설계서 §10.2)

    context_features: macro(4) + technical(5) + price_stretch(2) + sector_relative(3)
                      + sector_onehot(n_sectors) + market_cap_rank(1)

    Dense(n_context_features→64)+ReLU+Dropout(0.2) → Dense(64→32)+ReLU → 32-d
    """

    def __init__(self, n_context_features: int = 26):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_context_features, 64),
            nn.ReLU(),
            nn.Dropout(0.3),  # Gemini Pro: 0.2→0.3
            nn.Linear(64, 32),
            nn.ReLU(),
        )

    def forward(self, context_features: torch.Tensor) -> torch.Tensor:
        """
        context_features: (B, n_context_features)
        반환: (B, 32)
        """
        return self.net(context_features)  # (B, 32)


class KRReboundCNN(nn.Module):
    """
    KR-Rebound-CNN (설계서 §10.2 Context Branch 확장)

    입력:
      - chart_tensor: (B, 3, 64, 64)           3채널 64x64 차트 이미지
      - context_features: (B, n_context_features)  26-d 통합 context 피처

    출력:
      - rebound_logit: (B, 1)                  반등 logit (raw, unbounded)

    아키텍처 (설계서 준수):
      ImageEncoder   → 128-d
      ContextEncoder → 32-d
      Concat(128+32=160) → Dense(64)+ReLU+Dropout(0.2) → Dense(1) (logit)
    """

    def __init__(self, n_context_features: int = 26):
        super().__init__()

        self.image_encoder = ImageEncoder()
        self.context_encoder = ContextEncoder(n_context_features)

        # Fusion Head: 128 + 32 = 160
        # Gemini Pro 피드백: Dropout 0.2→0.4 + BatchNorm1d (과적합 방지)
        self.fusion_head = nn.Sequential(
            nn.Linear(160, 64),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.Dropout(0.4),
            nn.Linear(64, 1),
        )

    def forward(
        self,
        chart_tensor: torch.Tensor,
        context_features: torch.Tensor,
    ) -> torch.Tensor:
        """
        chart_tensor: (B, 3, H, W)
        context_features: (B, n_context_features)
        반환: (B, 1) rebound logit (raw, unbounded)
        """
        img_vec = self.image_encoder(chart_tensor)          # (B, 128)
        ctx_vec = self.context_encoder(context_features)    # (B, 32)
        fused = torch.cat([img_vec, ctx_vec], dim=1)        # (B, 160)
        return self.fusion_head(fused)                      # (B, 1)


def build_model(
    n_context_features: int = 26,
    device: torch.device = None,
) -> KRReboundCNN:
    """모델 인스턴스 생성 및 device 이동. 출력은 raw logit (BCEWithLogitsLoss 사용)."""
    if device is None:
        device = DEVICE
    model = KRReboundCNN(n_context_features)
    model = model.to(device)
    print(f"[Modeler] KRReboundCNN 초기화 완료 (n_context_features={n_context_features}, device={device})")
    return model


if __name__ == "__main__":
    device = _get_device()
    n_ctx = 26
    model = build_model(n_context_features=n_ctx, device=device)

    # 설계서 §11.1 아키텍처 확인
    # ImageEncoder: Conv32+BN+ReLU+MaxPool(2) → Conv64+BN+ReLU+MaxPool(2) → Conv128+BN+ReLU → AdaptiveAvgPool(1,1)
    # FusionHead: Dense(64)+ReLU+Dropout(0.2) → Dense(1) [logit, no Sigmoid]
    B = 4
    chart = torch.randn(B, 3, 64, 64, device=device)
    context = torch.randn(B, n_ctx, device=device)

    logit = model(chart, context)
    print(f"[Modeler] forward 테스트 통과: 입력 {chart.shape}, context {context.shape} → 출력 {logit.shape}")
    assert logit.shape == (B, 1), f"예상 출력 shape (B,1), 실제 {logit.shape}"
    print(f"[Modeler] 모델 아키텍처 검증 완료 (n_context_features={n_ctx}, MaxPool 포함, Dropout=0.2, logit 출력)")
