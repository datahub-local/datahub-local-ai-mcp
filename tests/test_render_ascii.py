"""ASCII folding, which has to keep the letter and not just the bytes.

Rendered output is ASCII because a reply carrying invalid UTF-8 has its result
dropped entirely while the run still reports success. Dropping every non-ASCII
character satisfies that and destroys the word: `PANALES` became `PAALES` and
`ATUN` became `ATN` in Slack, against a warehouse holding both correctly. A
product whose name loses a letter reads as a different product and matches
nothing when someone searches for the real one.
"""

from __future__ import annotations

from mcp_runner.render import ascii_only


def test_output_is_always_ascii():
    assert ascii_only("PAÑALES 日本語 Ø").isascii()


def test_accented_letters_keep_their_base_letter():
    # The four that reached Slack mangled, from real receipt lines.
    assert ascii_only("PAÑALES TALLA 5 PACK") == "PANALES TALLA 5 PACK"
    assert ascii_only("ATÚN CLARO GIRAS PK6") == "ATUN CLARO GIRAS PK6"
    assert ascii_only("CÁP. CLASSIC") == "CAP. CLASSIC"
    assert ascii_only("PLÁTANO") == "PLATANO"


def test_a_folded_name_keeps_its_length():
    # The failure was silent because a shortened name still looks like a name.
    for word in ("PAÑALES", "ATÚN", "SANDÍA", "LIMÓN"):
        assert len(ascii_only(word)) == len(word)


def test_currency_is_named_rather_than_dropped():
    # Deleting it left `TOTAL ()`, which reads as a missing figure.
    assert ascii_only("TOTAL (€)") == "TOTAL (EUR)"


def test_known_punctuation_still_folds():
    assert ascii_only("a · b") == "a - b"
    assert ascii_only("a → b") == "a -> b"


def test_unmappable_characters_are_still_dropped():
    assert ascii_only("日本語") == ""
