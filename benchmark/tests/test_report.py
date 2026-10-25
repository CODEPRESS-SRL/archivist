from benchmark.report import render_lambda_footprint_chart, summarize


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


def test_lambda_chart_writes_png(tmp_path):
    def _rows(fp):
        return [
            {"adapter": "archivist", "day": d, "footprint_tokens": fp * d,
             "recall": 0.9, "survival_recall": 0.8, "avg_retrieve_tokens": 300,
             "write_tokens_cumulative": 1000 * d}
            for d in (30, 60)
        ] + [
            {"adapter": "rag-wiki", "day": d, "footprint_tokens": 0, "recall": 0.4,
             "survival_recall": 0.4, "avg_retrieve_tokens": 400,
             "write_tokens_cumulative": 0}
            for d in (30, 60)
        ]
    runs = {"0.15": _rows(10), "0.85": _rows(40)}
    out = tmp_path / "by_lambda.png"
    render_lambda_footprint_chart(runs, str(out))
    assert out.exists() and out.stat().st_size > 0
