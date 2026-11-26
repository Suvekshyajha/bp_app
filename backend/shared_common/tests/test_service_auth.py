"""The chat service refuses to start without a service token."""

import logging

import pytest

from common.service_auth import require_service_token

GOOD = "a-long-enough-token-1234"


def test_a_real_token_is_returned():
    assert require_service_token(GOOD, False, "0.0.0.0") == GOOD


@pytest.mark.parametrize("missing", [None, ""])
def test_no_token_stops_the_service(missing):
    with pytest.raises(RuntimeError, match="CHAT_SERVICE_TOKEN is not set"):
        require_service_token(missing, False, "127.0.0.1")


@pytest.mark.parametrize("placeholder", ["change_me", "CHANGE_ME", "changeme"])
def test_the_example_value_is_refused(placeholder):
    with pytest.raises(RuntimeError, match="example value"):
        require_service_token(placeholder, False, "127.0.0.1")


def test_the_example_value_is_refused_even_with_the_opt_out():
    with pytest.raises(RuntimeError):
        require_service_token("change_me", True, "127.0.0.1")


def test_a_short_token_works_but_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert require_service_token("short", False, "127.0.0.1") == "short"

    assert "short" in caplog.text


def test_opt_out_works_on_this_machine_only_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        assert require_service_token(None, True, "127.0.0.1") is None

    assert "WITHOUT a service token" in caplog.text

    with pytest.raises(RuntimeError, match="only allowed"):
        require_service_token(None, True, "0.0.0.0")


def test_opt_out_is_not_needed_when_there_is_a_token():
    assert require_service_token(GOOD, True, "0.0.0.0") == GOOD


def test_a_custom_name_appears_in_every_error_text():
    ingest = {"name": "INGEST_SERVICE_TOKEN"}

    with pytest.raises(RuntimeError, match="INGEST_SERVICE_TOKEN is not set"):
        require_service_token(None, False, "127.0.0.1", **ingest)

    with pytest.raises(RuntimeError, match="INGEST_ALLOW_NO_AUTH=true"):
        require_service_token(None, False, "127.0.0.1", **ingest)

    with pytest.raises(RuntimeError, match="INGEST_BIND_HOST"):
        require_service_token(None, True, "0.0.0.0", **ingest)

    with pytest.raises(RuntimeError, match="INGEST_SERVICE_TOKEN is still the example"):
        require_service_token("change_me", False, "127.0.0.1", **ingest)


def test_the_default_name_is_unchanged_for_the_chat_service():
    with pytest.raises(RuntimeError, match="CHAT_SERVICE_TOKEN is not set"):
        require_service_token(None, False, "127.0.0.1")
