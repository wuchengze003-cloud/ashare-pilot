import pytest

from ashare_research.qlib_benchmark import alpha158_next_open_label


def test_alpha158_label_matches_next_open_execution():
    assert alpha158_next_open_label(5, 10) == "Ref($close,-5)/Ref($open,-1)-1-0.00100000"


def test_alpha158_label_rejects_invalid_horizon():
    with pytest.raises(ValueError, match="positive"):
        alpha158_next_open_label(0)
