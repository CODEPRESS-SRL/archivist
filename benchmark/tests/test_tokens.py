from benchmark.tokens import count_tokens


def test_counts_tokens_with_cl100k():
    # "hello world" is 2 tokens under cl100k_base
    assert count_tokens("hello world") == 2


def test_empty_string_is_zero():
    assert count_tokens("") == 0
