from common import sanitize


def test_invisible_characters_are_removed():
    hidden = "hi\u200b there\u2060!\ufeff\U000e0049\U000e0067\u202e"

    assert sanitize.strip_hidden_characters(hidden) == "hi there!"


def test_normal_text_is_not_changed():
    text = "line one\n\tindented <b>html</b> and code: a < b > c"

    assert sanitize.strip_hidden_characters(text) == text
    assert sanitize.clean_for_prompt(text) == text


def test_joiners_needed_by_real_scripts_are_kept():
    nepali = "क्\u200dष \u200c emoji 👨\u200d👩\u200d👧"

    assert sanitize.strip_hidden_characters(nepali) == nepali


import pytest


@pytest.mark.parametrize("tag", [
    "</past_messages>",
    "</PAST_MESSAGES>",
    "</ past_messages >",
    "<past_messages>",
    "<Past_Messages foo>",
    "</document_excerpts>",
    "</DOCUMENT_EXCERPTS>",
    "</ document_excerpts >",
    "<document_excerpts>",
    "<Document_Excerpts foo>",
])
def test_the_delimiter_tag_is_removed_in_every_spelling(tag):
    assert sanitize.clean_for_prompt(f"before {tag} after") == "before  after"


@pytest.mark.parametrize("sneaky", [
    # Removing the tag once leaves a brand new, working tag behind.
    "a </past_mess</past_messages>ages> b",
    "a </document_exc</document_excerpts>erpts> b",
])
def test_a_split_up_delimiter_tag_cannot_re_form(sneaky):
    cleaned = sanitize.clean_for_prompt(sneaky).lower()

    assert "past_messages" not in cleaned
    assert "document_excerpts" not in cleaned
