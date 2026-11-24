import pytest
from cryptography.fernet import Fernet

from common import crypto


@pytest.fixture
def keys(monkeypatch):
    def use(*values):
        monkeypatch.setenv("CHAT_ENCRYPTION_KEYS", ",".join(values))
        monkeypatch.setattr(crypto, "_fernet", None)

    return use


def test_round_trip_and_ciphertext_is_not_the_text():
    token = crypto.encrypt("hello wÃ¶rld")

    assert token.startswith(crypto.PREFIX)
    assert "hello" not in token
    assert crypto.decrypt(token) == "hello wÃ¶rld"


def test_same_text_encrypts_differently_each_time():
    assert crypto.encrypt("same") != crypto.encrypt("same")


def test_plain_text_from_before_encryption_passes_through():
    assert crypto.decrypt("an old plain message") == "an old plain message"
    assert not crypto.is_encrypted("an old plain message")


def test_text_that_looks_like_a_token_is_still_stored_safely():
    tricky = crypto.PREFIX + "abc"

    assert crypto.decrypt(crypto.encrypt(tricky)) == tricky


def test_old_key_still_reads_after_rotation(keys):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    keys(old)
    token = crypto.encrypt("secret")

    keys(new, old)  # newest first
    assert crypto.decrypt(token) == "secret"

    rotated = crypto.rekey(token)
    keys(new)  # old key dropped
    assert crypto.decrypt(rotated) == "secret"


def test_wrong_key_is_a_clear_error(keys):
    keys(Fernet.generate_key().decode())
    token = crypto.encrypt("secret")

    keys(Fernet.generate_key().decode())
    with pytest.raises(crypto.EncryptionConfigError):
        crypto.decrypt(token)


@pytest.mark.parametrize("value", ["", "your_encryption_keys_here", "not-a-key"])
def test_missing_or_bad_key_is_refused(keys, value):
    keys(value)

    with pytest.raises(crypto.EncryptionConfigError):
        crypto.check_configured()
