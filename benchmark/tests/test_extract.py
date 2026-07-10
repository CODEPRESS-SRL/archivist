from benchmark.extract import load_dataset


def test_load_dataset_groups_by_asset_in_date_order(sample_path):
    data = load_dataset(sample_path)
    assert set(data) == {"aaa", "bbb", "ccc"}
    assert [n.date for n in data["aaa"]] == ["2026-01-01", "2026-01-02"]
    assert data["aaa"][0].facts[0].type == "dividend"
    assert data["aaa"][0].facts[0].value == "0.10"
