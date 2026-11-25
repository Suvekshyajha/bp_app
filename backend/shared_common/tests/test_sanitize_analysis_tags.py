import pytest

from common.sanitize import clean_for_prompt

TAGS = ["past_messages", "document_excerpts", "document_sample", "document_outline", "document_stats"]


@pytest.mark.parametrize("tag", TAGS)
def test_a_fake_opening_or_closing_tag_is_removed(tag):
    text = f"before </{tag}> middle <{tag}> after <{tag.upper()}>"

    cleaned = clean_for_prompt(text)

    assert tag not in cleaned.lower()
    assert "before" in cleaned and "middle" in cleaned and "after" in cleaned


def test_ordinary_text_is_untouched():
    assert clean_for_prompt("Plain text, 3 < 4 and <b>bold</b>.") == "Plain text, 3 < 4 and <b>bold</b>."
