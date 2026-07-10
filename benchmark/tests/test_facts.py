from benchmark.facts import extract_facts


def test_extracts_dividend_with_value_and_unit():
    facts = extract_facts("Acme declares quarterly dividend", "Board approved a dividend of $0.24 per share.")
    assert any(f.type == "dividend" and f.value == "0.24" and f.unit == "USD" for f in facts)


def test_extracts_rating_downgrade():
    facts = extract_facts("Analyst cuts Acme", "Morgan downgraded the stock to underweight.")
    assert any(f.type == "rating" and f.value == "downgrade" for f in facts)


def test_extracts_percentage_verbatim():
    # value must be a token that appears literally in the text (probe = re.escape(value))
    facts = extract_facts("Acme shares slide", "The stock fell 4.2% after the report.")
    pct = next(f for f in facts if f.type == "pct")
    assert pct.value == "4.2%" and pct.unit == "%"
    assert "4.2%" in ("Acme shares slide. The stock fell 4.2% after the report.")


def test_extracts_dollar_amount_verbatim():
    facts = extract_facts("SK Hynix Seeks Funds", "SK Hynix seeks $29 billion with a new US listing.")
    assert any(f.type == "usd" and f.value == "$29 billion" for f in facts)


def test_no_facts_returns_empty():
    assert extract_facts("Weather is nice", "Nothing financial here.") == ()
