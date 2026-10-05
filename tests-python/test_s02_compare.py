import unittest
import pandas as pd
from qt_research.s02_compare import target_rows, aligned_dates, observed_jq_fill_price

class CompareTests(unittest.TestCase):
    def test_observed_spread_half_and_tick_rounding(self):
        self.assertEqual(observed_jq_fill_price(1.212,'buy'),1.212)
        self.assertEqual(observed_jq_fill_price(101.097,'buy'),101.102)
        self.assertEqual(observed_jq_fill_price(101.128,'sell'),101.123)
        with self.assertRaises(ValueError):observed_jq_fill_price(10,'unknown')

    def test_symbol_and_html_normalization(self):
        rows=target_rows(["2024-01-02 HUMAN|轮动共同目标|买入目标=[&apos;513100.XSHG&apos;]"])
        self.assertEqual(rows, {"2024-01-02": ["513100.SH"]})

    def test_duplicate_and_malformed_target_rejected(self):
        row="2024-01-02 HUMAN|轮动共同目标|买入目标=[]"
        with self.assertRaises(ValueError):target_rows([row,row])
        with self.assertRaises(ValueError):target_rows(["2024-01-02 HUMAN|轮动共同目标|invalid"])

    def test_date_overlap_insufficient(self):
        aligned_dates(pd.Series(["a","b"]),pd.Series(["a","b"]))
        for right in (["a"],["a","a"],["b","a"]):
            with self.assertRaises(ValueError):aligned_dates(pd.Series(["a","b"]),pd.Series(right))
