import unittest

from weather99.paper_report import evaluation_lines


class PaperReportTests(unittest.TestCase):
    def setUp(self):
        self.raw = {
            "buckets": [
                {"token_id": "cold", "bucket_label": "9°C", "bid": 0.001, "ask": 0.003},
                {"token_id": "hot", "bucket_label": "13°C", "bid": 0.61, "ask": None},
            ]
        }
        self.candidate = {
            "buckets": ["cold"],
            "blended_probability": 0.276,
            "reasons": [
                "cold:ask_out_of_range",
                "blended_probability_below_threshold",
                "market_mode_not_in_combo",
            ],
        }
        self.row = {
            "target_date": "2026-11-19",
            "layer": "24h",
            "status": "rejected",
            "reasons": ["no_eligible_combo"],
            "best_candidate": self.candidate,
        }
        self.signal = {"market_mode": "hot", "candidates": [self.candidate]}
        self.config = {
            "entry": {"probability_threshold": {"24h": 0.55}},
            "research_assumptions": {"ask_min": 0.01, "ask_max": 0.99},
        }

    def report(self):
        return "\n".join(evaluation_lines(self.row, (self.raw, self.signal, self.config)))

    def test_rejected_candidate_is_scoped_and_quantified(self):
        text = self.report()
        self.assertIn("整体结论：没有组合同时满足入场条件", text)
        self.assertIn("被拒候选示例", text)
        self.assertIn("27.6%，入场门槛 55.0%（未达标）", text)
        self.assertIn("9°C：卖价 $0.003，允许范围 $0.01～$0.99", text)
        self.assertIn("热门档 13°C", text)

    def test_missing_quote_names_the_affected_bucket_and_side(self):
        self.candidate.update(buckets=["hot"], reasons=["hot:one_sided_book"])
        self.assertIn("13°C：缺少卖价", self.report())
        self.assertNotIn("缺少买价", self.report())

    def test_selected_candidate_takes_priority_over_rejected_example(self):
        self.row.update(
            selected={"buckets": ["hot"], "blended_probability": 0.9, "reasons": []},
            reasons=[],
            fill={"status": "paper_filled"},
        )
        text = self.report()
        self.assertIn("已模拟买入", text)
        self.assertIn("通过条件的组合：13°C", text)
        self.assertNotIn("卖价 $0.003", text)

    def test_missing_historical_config_does_not_invent_threshold(self):
        self.config.clear()
        self.assertIn("该次门槛记录不可用", self.report())
        self.assertNotIn("55.0%", self.report())
