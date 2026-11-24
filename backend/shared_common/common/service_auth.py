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
    name: str = "CHAT_SERVICE_TOKEN",
) -> str | None:
    """Return the token to enforce, or None if running without one is
    explicitly allowed. Raises RuntimeError if the service must not start.

    `name` is the environment variable the token comes from. It only changes
    the wording of the errors: "INGEST_SERVICE_TOKEN" also gives
    INGEST_ALLOW_NO_AUTH and INGEST_BIND_HOST in the text.
    """

    prefix = name.removesuffix("_SERVICE_TOKEN")

    if token:
        if token.lower() in PLACEHOLDERS:
            raise RuntimeError(
                f"{name} is still the example value. " + HOW_TO_MAKE_ONE
            )

        if len(token) < MIN_TOKEN_LENGTH:
            logger.warning(
                "%s is short (%d characters). Use at least %d.",
                name,
                len(token),
                MIN_TOKEN_LENGTH,
            )

        return token

    if not allow_no_auth:
        raise RuntimeError(
            f"{name} is not set, so the service will not start: "
            "without it any caller that can reach the port could read or "
            "delete any user's data. " + HOW_TO_MAKE_ONE + " "
            f"(For local development only: {prefix}_ALLOW_NO_AUTH=true.)"
        )

    if bind_host not in LOCAL_HOSTS:
        raise RuntimeError(
            f"{prefix}_ALLOW_NO_AUTH=true is only allowed when {prefix}_BIND_HOST is "
            f"this machine (127.0.0.1). Set {name} instead."
        )

    logger.warning(
        f"Running WITHOUT a service token ({prefix}_ALLOW_NO_AUTH=true). "
        "Development only: any program on this machine can read all data."
    )

    return None