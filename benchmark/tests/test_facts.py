from benchmark.facts import extract_facts


def test_extracts_dividend_with_value_and_unit():
    facts = extract_facts("Acme declares quarterly dividend", "Board approved a dividend of $0.24 per share.")
    assert any(f.type == "dividend" and f.value == "0.24" and f.unit == "USD" for f in facts)


def test_extracts_rating_downgrade():
    facts = extract_facts("Analyst cuts Acme", "Morgan downgraded the stock to underweight.")
    assert any(f.type == "rating" and f.value == "downgrade" for f in facts)


def test_extracts_price_move_percent():
    facts = extract_facts("Acme shares slide", "The stock fell 4.2% after the report.")
    assert any(f.type == "price_move" and f.value == "-4.2" and f.unit == "%" for f in facts)


def test_no_facts_returns_empty():
    assert extract_facts("Weather is nice", "Nothing financial here.") == ()
