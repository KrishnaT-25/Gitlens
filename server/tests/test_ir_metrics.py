import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest
from eval.metrics import metrics_at_k, summarize


def test_metrics_hand_checked_for_relevant_chunks_at_ranks_two_and_three():
    ranked = ["noise", "relevant-a", "relevant-b"]
    at_one = metrics_at_k(ranked, {"relevant-a", "relevant-b"}, 1)
    assert at_one == {"hit_rate": 0.0, "recall": 0.0, "precision": 0.0, "mrr": 0.0, "ndcg": 0.0}

    at_three = metrics_at_k(ranked, {"relevant-a", "relevant-b"}, 3)
    assert at_three["hit_rate"] == 1.0
    assert at_three["recall"] == 1.0
    assert at_three["precision"] == pytest.approx(2 / 3)
    assert at_three["mrr"] == pytest.approx(1 / 2)
    assert at_three["ndcg"] == pytest.approx((1 / 2 + 1 / 1.5849625007) / (1 + 1 / 1.5849625007))


def test_metrics_for_single_hit_at_rank_two():
    result = metrics_at_k(["noise", "target"], {"target"}, 2)
    assert result == pytest.approx({"hit_rate": 1.0, "recall": 1.0, "precision": 0.5,
                                    "mrr": 0.5, "ndcg": 1 / 1.5849625007})


def test_summary_macro_averages_questions_and_rejects_empty_input():
    first = {"metrics": {"1": metrics_at_k(["a"], {"a"}, 1)}, "mrr": 1.0}
    second = {"metrics": {"1": metrics_at_k(["x"], {"a"}, 1)}, "mrr": 0.0}
    assert summarize([first, second], cutoffs=(1,)) == pytest.approx({
        "hit_rate@1": 0.5, "recall@1": 0.5, "precision@1": 0.5, "ndcg@1": 0.5, "mrr": 0.5,
    })
    with pytest.raises(ValueError):
        summarize([])
