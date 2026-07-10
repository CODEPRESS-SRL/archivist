from benchmark.report import summarize


def test_summary_lists_each_adapter_final_footprint():
    rows = [
        {"adapter": "archivist", "day": 120, "footprint_tokens": 1200, "recall": 0.9,
         "survival_recall": 0.85, "avg_retrieve_tokens": 300, "write_tokens_cumulative": 50000},
        {"adapter": "claude-mem", "day": 120, "footprint_tokens": 48000, "recall": 0.9,
         "survival_recall": 0.8, "avg_retrieve_tokens": 300, "write_tokens_cumulative": 52000},
    ]
    out = summarize(rows)
    assert "archivist" in out and "claude-mem" in out
    assert "1200" in out and "48000" in out
