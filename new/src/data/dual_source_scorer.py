"""C3A DualSourceScoreContract. 뉴스 vs 커뮤니티 divergence 점수화."""
from __future__ import annotations


class DualSourceScorer:
    """C3A DualSourceScoreContract 5피처 생성기.

    5피처 (api_contracts.md C3A + architecture.md §2.3 ④):
      news_score_t: 현재 시점 뉴스 감성 점수 (FinBERT 기반).
      comm_score_t_1: 1 lag 커뮤니티 점수 (spam+sentiment 분류).
      comm_score_t_2: 2 lag 커뮤니티 점수 (peak_lag_days 반영).
      news_comm_divergence: |news_score_t - comm_score_t_1| 기반 불일치 지수.
      community_noise_multiplier: 게시량 z-score 기반 감쇠 계수.
          community_noise_zscore 임계값 초과 시 multiplier < 1로 신호 감쇠.

    divergence 임계값 + decay 파라미터는 risk_config.yaml dual_source 섹션 로드
    (하드코딩 금지). divergence 상승 → uncertainty → PPO position 축소.

    Sprint 4 S4-1 구현 예정 (FinBERT + Spam/Sentiment + Divergence + Decay).
    """

    def score(
        self,
        news_score_t: float,
        comm_score_t_1: float,
        comm_score_t_2: float,
    ) -> dict:
        """5피처 딕셔너리 계산 및 반환.

        Returns:
            {
                "news_score_t": float,
                "comm_score_t_1": float,
                "comm_score_t_2": float,
                "news_comm_divergence": float,
                "community_noise_multiplier": float,
            }
        """
        raise NotImplementedError("Sprint 4 S4-1 구현 예정")
