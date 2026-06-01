"""Tests for analysis/factor_mining.py — IC/IR 因子挖掘"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from analysis.factor_mining import FactorMiner, FactorResult, TRAIN_DATA_PATH


class TestDataPathValidation:
    """铁律 #2 数据隔离测试"""

    def test_train_path_accepted(self):
        miner = FactorMiner(train_path=str(TRAIN_DATA_PATH))
        assert miner._train_path.resolve() == TRAIN_DATA_PATH.resolve()

    def test_validate_path_rejected(self):
        with pytest.raises(PermissionError, match="铁律 #2"):
            FactorMiner(train_path=str(Path("validation/datasets/validate/")))

    def test_oos_path_rejected(self):
        with pytest.raises(PermissionError, match="铁律 #2"):
            FactorMiner(train_path=str(Path("validation/datasets/oos/")))

    def test_arbitrary_path_rejected(self):
        with pytest.raises(PermissionError, match="铁律 #2"):
            FactorMiner(train_path="/tmp/random_data")


class TestICComputation:

    def test_perfect_positive_ic(self):
        miner = FactorMiner()
        factor = pd.Series([float(i) for i in range(40)])
        returns = pd.Series([float(i) * 0.01 for i in range(40)])
        ic = miner.compute_ic(factor, returns)
        assert ic is not None
        assert ic > 0.9

    def test_perfect_negative_ic(self):
        miner = FactorMiner()
        factor = pd.Series([float(40 - i) for i in range(40)])
        returns = pd.Series([float(i) * 0.01 for i in range(40)])
        ic = miner.compute_ic(factor, returns)
        assert ic is not None
        assert ic < -0.9

    def test_insufficient_samples(self):
        miner = FactorMiner()
        ic = miner.compute_ic(pd.Series([1.0, 2.0]), pd.Series([0.01, 0.02]))
        assert ic is None

    def test_nan_handling(self):
        miner = FactorMiner()
        n = 50
        factor = pd.Series(np.where(np.arange(n) % 5 == 1, np.nan, np.arange(n, dtype=float)))
        returns = pd.Series(np.where(np.arange(n) % 7 == 3, np.nan, np.arange(n, dtype=float) * 0.01))
        ic = miner.compute_ic(factor, returns)
        assert ic is not None


class TestForwardReturns:

    def test_forward_1_period(self):
        ret = FactorMiner.compute_forward_returns(pd.Series([100.0, 102.0, 101.0, 105.0]), 1)
        assert ret.iloc[0] == pytest.approx(0.02)
        assert pd.isna(ret.iloc[-1])

    def test_forward_2_period(self):
        ret = FactorMiner.compute_forward_returns(pd.Series([100.0, 100.0, 110.0, 120.0]), 2)
        assert ret.iloc[0] == pytest.approx(0.10)
        assert pd.isna(ret.iloc[-2])
        assert pd.isna(ret.iloc[-1])


class TestRunIntegration:

    @pytest.fixture
    def sample_df(self):
        rng = np.random.default_rng(42)
        n = 200
        close = 100.0 + np.cumsum(rng.normal(0, 1, n))
        df = pd.DataFrame({"close": close})
        df["RSI_14"] = 50.0 + rng.normal(0, 10, n)
        df["SMA_20"] = pd.Series(close).rolling(5).mean().bfill()
        return df

    def test_run_returns_results(self, sample_df):
        results = FactorMiner().run(sample_df)
        assert len(results) > 0
        assert all(isinstance(r, FactorResult) for r in results)

    def test_sorted_by_ir(self, sample_df):
        irs = [r.ir for r in FactorMiner().run(sample_df)]
        assert irs == sorted(irs, reverse=True)

    def test_empty_dataframe(self):
        assert FactorMiner().run(pd.DataFrame({"close": []})) == []

    def test_missing_price(self):
        assert FactorMiner().run(pd.DataFrame({"RSI_14": [1, 2, 3]})) == []

    def test_get_top_factors(self, sample_df):
        miner = FactorMiner()
        miner.run(sample_df)
        assert len(miner.get_top_factors(n=2)) <= 2

    def test_category_summary(self, sample_df):
        miner = FactorMiner()
        miner.run(sample_df)
        assert isinstance(miner.get_category_summary(), dict)

    def test_nan_factor_values(self, sample_df):
        sample_df.loc[10:20, "RSI_14"] = np.nan
        assert len(FactorMiner().run(sample_df)) > 0


