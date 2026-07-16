from datetime import date

from ashare_research.candidate import bootstrap_superiority_probability


def report(returns):
    equity = 100.0
    curve = []
    for index, value in enumerate(returns):
        equity *= 1 + value
        curve.append({"date": date(2026, 1, 1 + index).isoformat(), "equity": equity})
    return {"portfolio": {"equity_curve": curve}}


def test_block_bootstrap_detects_clear_superiority():
    candidate = report([0.01] * 25)
    champion = report([0.0] * 25)
    assert bootstrap_superiority_probability(candidate, champion, samples=200) > 0.99


def test_block_bootstrap_rejects_insufficient_history():
    assert bootstrap_superiority_probability(report([0.01] * 5), report([0.0] * 5)) == 0
