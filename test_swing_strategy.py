import unittest

import numpy as np
import pandas as pd

from swing_strategy import (
    _backtest,
    _indicators,
    _market_states,
    _promoter_score,
    _technical_score,
    _trade_levels,
)


class SwingStrategyTests(unittest.TestCase):
    def test_promoter_score_is_bounded_and_rewards_repeated_disclosures(self):
        self.assertEqual(_promoter_score(0, 0), 0)
        self.assertEqual(_promoter_score(5_000_000, 1), 10)
        self.assertEqual(_promoter_score(100_000_000, 3), 20)

    def test_trade_levels_have_two_r_and_three_r_targets(self):
        row = pd.Series({"High": 250.0, "ATR14": 5.0, "LOW10": 240.0})
        levels = _trade_levels(row)
        risk = levels["entry"] - levels["stop"]
        self.assertAlmostEqual(levels["target1"] - levels["entry"], 2 * risk)
        self.assertAlmostEqual(levels["target2"] - levels["entry"], 3 * risk)
        self.assertGreater(levels["max_entry"], levels["entry"])
        self.assertLess(levels["stop"], levels["entry"])

    def test_indicators_and_market_regime_are_calculated(self):
        index = pd.date_range("2022-01-01", periods=300, freq="B")
        close = pd.Series(np.linspace(100, 220, len(index)), index=index)
        frame = pd.DataFrame({
            "Open": close * 0.999,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": 100_000,
        }, index=index)
        enriched = _indicators(frame)
        self.assertIn("ATR14", enriched.columns)
        self.assertIn("RSI14", enriched.columns)
        market = _market_states(frame, enriched.index)
        self.assertEqual(market.iloc[-1], "BULL")

    def test_backtest_returns_required_metrics_without_lookahead_crash(self):
        index = pd.date_range("2020-01-01", periods=500, freq="B")
        trend = np.linspace(100, 240, len(index))
        wave = np.sin(np.arange(len(index)) / 4) * 2
        close = trend + wave
        frame = pd.DataFrame({
            "Open": close * 0.998,
            "High": close * 1.012,
            "Low": close * 0.988,
            "Close": close,
            "Volume": np.where(np.arange(len(index)) % 8 == 0, 180_000, 100_000),
        }, index=index)
        enriched = _indicators(frame)
        market = pd.Series("BULL", index=index)
        result = _backtest(enriched, market)
        for key in ("trades", "win_rate", "profit_factor", "expectancy_r",
                    "max_drawdown_pct", "total_return_pct", "sample_ok"):
            self.assertIn(key, result)
        self.assertGreaterEqual(result["trades"], 0)


if __name__ == "__main__":
    unittest.main()
