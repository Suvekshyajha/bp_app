"""Cleaning of untrusted text before it goes into a prompt.

Used by the retriever (chat messages and document excerpts) and by the
ingestion service (document text). Two things are removed:

1. Characters a person cannot see but a model can read. Attackers hide
   instructions in them ("ASCII smuggling"): Unicode tag characters, zero
   width space / word joiner / BOM, bidi overrides and control characters.
   NOT removed: U+200C and U+200D (zero-width non-joiner / joiner), which
   Nepali, Hindi, Persian and emoji sequences need to display correctly.

2. Our own delimiter tags, in any spelling, so stored text cannot close a
   <past_messages>, <document_excerpts> or <available_documents> block and
   write outside it.
"""

import re


HIDDEN_CHARACTERS = re.compile(
    "["
    "\u200b"                # zero-width space
    "\u2060"                # word joiner
    "\ufeff"                # byte order mark
    "\u202a-\u202e"         # bidi embeddings and overrides
    "\u2066-\u2069"         # bidi isolates
    "\U000e0000-\U000e007f"  # Unicode tag characters
    "\U000e0100-\U000e01ef"  # variation selectors supplement
    "\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f"  # control characters (not \t \n \r)
    "]"
)

DELIMITER_TAG = re.compile(
    r"<\s*/?\s*(?:past_messages|document_excerpts|available_documents|document_sample|document_outline|document_stats)\b[^>]*>?",
    re.IGNORECASE,
)


def strip_hidden_characters(text: str) -> str:
    """Remove invisible characters that can carry hidden instructions."""

    return HIDDEN_CHARACTERS.sub("", text)


def clean_for_prompt(text: str) -> str:
    """Text read from storage, made safe to place inside a prompt block.

    The cleaning is repeated until the text stops changing. One pass is not
    enough: removing "</past_messages>" from
    "</past_mess</past_messages>ages>" leaves a new working tag behind.
    Every pass only removes characters, so the loop always ends.
    """

    previous = None

    while previous != text:
        previous = text
        text = DELIMITER_TAG.sub("", strip_hidden_characters(text))

    return text
