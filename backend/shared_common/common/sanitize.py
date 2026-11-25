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

A third thing is only DETECTED, never removed: phrases that read like an
attempt to give the model new orders ("ignore all previous instructions").
A regex cannot be a real defence (a paraphrase gets past it, and deleting the
words would damage honest documents, for example a security paper that quotes
them). So a match is logged for the operator, the text is left as it is, and
the real protection stays where it is: the cleaning above and the rule in the
system prompt that documents are data, never instructions.
"""

import logging
import re

logger = logging.getLogger(__name__)


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


# Phrases typical of prompt injection. Case does not matter and line breaks or
# extra spaces between the words do not hide a match. A heuristic: it is tuned
# to catch the common wordings and to stay quiet on ordinary text.
INJECTION_PATTERNS = re.compile(
    r"""
    # "ignore all previous instructions", "disregard the system prompt", ...
    (?:ignore|disregard|forget|override|bypass)\s+
        (?:(?:all|any|the|your|my|these|those|of)\s+)*
        (?:previous|prior|above|earlier|preceding|system|original|safety)\s+
        (?:instructions?|prompts?|rules?|guidelines?|directions?|context|messages?)
  | # "ignore everything above", "forget all instructions"
    (?:ignore|disregard|forget)\s+(?:everything|anything)\s+(?:above|before|so\s+far|you\s+were\s+told)
  | (?:ignore|disregard|forget)\s+(?:all|any)\s+(?:instructions?|prompts?|rules?|guidelines?)
  | # "new instructions:", "updated system prompt:"
    (?:new|updated|real|actual)\s+(?:system\s+)?(?:instructions?|prompt|rules?)\s*:
  | # "you are now a ...", "from now on you ..."
    you\s+are\s+now\s+(?:a|an|in|the)\b
  | from\s+now\s+on,?\s+you\s+(?:are|will|must|should)\b
  | # "act as an unrestricted ...", "pretend to be a jailbroken ..."
    (?:act|behave|respond|pretend)\s+(?:as|like|to\s+be)\s+(?:if\s+you\s+(?:are|were)\s+)?(?:a|an|the)?\s*
        (?:different|unrestricted|unfiltered|uncensored|jailbroken|dan\b|developer|admin)
  | # "reveal your system prompt"
    (?:reveal|show|print|repeat|output|leak|disclose)\s+(?:me\s+)?(?:your|the)\s+
        (?:system\s+|hidden\s+|initial\s+|secret\s+)?(?:prompt|instructions?|rules?|configuration)
  | # "developer mode", "jailbreak mode"
    (?:developer|debug|admin(?:istrator)?|god|jailbreak|sudo)\s+mode\b
  | # a fake chat role line that starts an order: "System: you must ..."
    (?:^|\n)[ \t]*(?:system|assistant|developer)[ \t]*:[ \t]*
        (?:you\b|your\b|ignore|disregard|from\s+now|always|never|do\s+not|don'?t)
  | # fake chat-template markers
    <\s*/?\s*(?:system|im_start|im_end|instructions?)\s*>
  | \[\s*/?\s*(?:INST|SYS|SYSTEM)\s*\]
  | # hiding things from the user
    \b(?:do\s+not|don'?t)\s+(?:tell|inform|mention\s+(?:this\s+)?to)\s+the\s+user\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# How much of a match is written to the log (the text may be someone's
# document, so only a short piece of it is kept)
LOGGED_MATCH_CHARS = 60


def find_injection(text: str) -> str | None:
    """The first phrase in `text` that looks like a prompt injection, or None."""

    match = INJECTION_PATTERNS.search(text)

    return " ".join(match.group(0).split()) if match else None


def looks_like_injection(text: str) -> bool:
    """True when `text` contains a phrase typical of prompt injection."""

    return find_injection(text) is not None


def strip_hidden_characters(text: str) -> str:
    """Remove invisible characters that can carry hidden instructions."""

    return HIDDEN_CHARACTERS.sub("", text)


def clean_for_prompt(text: str) -> str:
    """Text read from storage, made safe to place inside a prompt block.

    The cleaning is repeated until the text stops changing. One pass is not
    enough: removing "</past_messages>" from
    "</past_mess</past_messages>ages>" leaves a new working tag behind.
    Every pass only removes characters, so the loop always ends.

    Text that reads like an injection attempt is logged and returned
    unchanged (see the top of this file for why it is not removed). It is
    checked AFTER the cleaning, so hidden characters or a broken-up tag cannot
    be used to split a phrase and get past the check.
    """

    previous = None

    while previous != text:
        previous = text
        text = DELIMITER_TAG.sub("", strip_hidden_characters(text))

    suspicious = find_injection(text)

    if suspicious:
        logger.warning(
            "Possible prompt injection in stored text (left in place): %r",
            suspicious[:LOGGED_MATCH_CHARS],
        )

    return text