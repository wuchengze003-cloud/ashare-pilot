import numpy as np
import pandas as pd
import pytest

from ashare_research.explain import (
    global_feature_importance,
    local_feature_contributions,
    top_contributions,
)


class Linear:
    coef_ = np.asarray([2.0, -1.0])


def test_linear_importance_and_local_contributions_are_exact():
    frame = pd.DataFrame({"momentum": [3.0], "volatility": [4.0]}, index=["A"])

    importance = global_feature_importance(Linear(), list(frame.columns))
    contributions = local_feature_contributions(Linear(), frame)

    assert importance == pytest.approx({"momentum": 2 / 3, "volatility": 1 / 3})
    assert contributions.loc["A", "momentum"] == 6
    assert contributions.loc["A", "volatility"] == -4
    assert list(top_contributions(contributions, "A")) == ["momentum", "volatility"]
