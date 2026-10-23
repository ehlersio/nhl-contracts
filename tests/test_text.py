from datetime import date

from nhl_contracts.normalize.text import (
    clean,
    money_value,
    name_key,
    season_from_date,
    shift_season,
    split_sentences,
    term_years,
)


def test_money_value_units():
    assert money_value("11.85", "million") == 11_850_000
    assert money_value("925,000", None) == 925_000
    assert money_value("1.125", "M") == 1_125_000
    assert money_value("5", None) is None  # "$5 a ticket" is not a contract figure


def test_term_years_words_and_digits():
    assert term_years("a three-year, $11.85 million contract") == 3
    assert term_years("8-year, $75.04 million contract") == 8
    assert term_years("an eight\\-year contract") is None  # escapes are removed by clean(), not here
    assert term_years(clean("an eight\\-year contract")) == 8
    assert term_years("multi-year contract") is None


def test_clean_strips_markup_and_joins_soft_breaks():
    raw = ('<forge-entity title="Marat Khusnutdinov" slug="marat-khusnutdinov-8482177" code="player">'
           "Marat Khusnutdinov</forge-entity> signed a **three-year** contract with the "
           "[Bruins](https://www.nhl.com/bruins).\n\nIt begins next season.")
    assert clean(raw) == "Marat Khusnutdinov signed a three-year contract with the Bruins.\nIt begins next season."
    glued = clean("announced that the\n\nclub has signed forward A B to a deal.")
    assert glued == "announced that the club has signed forward A B to a deal."


def test_split_sentences_keeps_initials_and_money():
    s = "The club signed forwards Lleyton Roed and J.R. Avon to one-year deals ($850,000 AAV). He is 24."
    parts = split_sentences(s)
    assert parts[0].startswith("The club signed forwards Lleyton Roed and J.R. Avon")
    assert len(parts) == 2
    assert split_sentences("A six-year, $82.5 million contract. Next.")[0] == "A six-year, $82.5 million contract."


def test_seasons():
    assert season_from_date(date(2026, 10, 21)) == "2026-27"
    assert season_from_date(date(2026, 3, 1)) == "2025-26"
    assert shift_season("2026-27", 1) == "2027-28"
    assert shift_season("2029-30", -2) == "2027-28"


def test_name_key_folds_accents():
    assert name_key("Juuso Välimäki") == name_key("Juuso Valimaki") == "juuso valimaki"
