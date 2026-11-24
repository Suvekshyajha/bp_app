"""The chat service must not start without a service token.

The service trusts the user_id it is given, so the token is the only thing
that keeps other programs on the network from reading or deleting anyone's
chats. A missing token therefore stops the service; it does not just warn.

For local development only, CHAT_ALLOW_NO_AUTH=true allows starting without
a token, and only while the service listens on this machine.
"""

import logging

logger = logging.getLogger(__name__)

MIN_TOKEN_LENGTH = 16

# Values copied from an .env.example that nobody changed
PLACEHOLDERS = {"change_me", "changeme", "your_token_here", "your_service_token_here"}

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

HOW_TO_MAKE_ONE = (
    'Make one with: python -c "import secrets; print(secrets.token_urlsafe(32))" '
    "and put the same value in the orchestrator's .env."
)


def require_service_token(
    token: str | None,
    allow_no_auth: bool,
    bind_host: str,
) -> str | None:
    """Return the token to enforce, or None if running without one is
    explicitly allowed. Raises RuntimeError if the service must not start."""

    if token:
        if token.lower() in PLACEHOLDERS:
            raise RuntimeError(
                "CHAT_SERVICE_TOKEN is still the example value. " + HOW_TO_MAKE_ONE
            )

        if len(token) < MIN_TOKEN_LENGTH:
            logger.warning(
                "CHAT_SERVICE_TOKEN is short (%d characters). Use at least %d.",
                len(token),
                MIN_TOKEN_LENGTH,
            )

        return token

    if not allow_no_auth:
        raise RuntimeError(
            "CHAT_SERVICE_TOKEN is not set, so the service will not start: "
            "without it any caller that can reach the port could read or "
            "delete any user's chats. " + HOW_TO_MAKE_ONE + " "
            "(For local development only: CHAT_ALLOW_NO_AUTH=true.)"
        )

    if bind_host not in LOCAL_HOSTS:
        raise RuntimeError(
            "CHAT_ALLOW_NO_AUTH=true is only allowed when CHAT_BIND_HOST is "
            "this machine (127.0.0.1). Set CHAT_SERVICE_TOKEN instead."
        )

    logger.warning(
        "Running WITHOUT a service token (CHAT_ALLOW_NO_AUTH=true). "
        "Development only: any program on this machine can read every chat."
    )

    return None