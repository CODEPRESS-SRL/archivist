from pathlib import Path

import pytest

SAMPLE = Path(__file__).parent.parent / "data" / "sample" / "dataset.jsonl"


@pytest.fixture
def sample_path() -> str:
    return str(SAMPLE)
