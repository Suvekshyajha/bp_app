"""Watch an answer arrive token by token, with the time each token arrived.

Run from backend/retriever, with the retriever service already running:

    python watch_stream.py "Write 300 words about the sea."

It uses the same settings as the orchestrator's client. Set them in your
shell first (this script never reads a .env file):

    CHAT_SERVICE_TOKEN       if the service needs a token
    CHAT_TLS_CA              FILE PATH of the certificate that signed the service's
                             certificate (turns TLS on)
    CHAT_TLS_CLIENT_CERT     FILE PATHS, only for mutual TLS (set both or neither)
    CHAT_TLS_CLIENT_KEY
    CHAT_SERVICE_ADDRESS     default 127.0.0.1:<CHAT_SERVICE_PORT>
    WATCH_USER, WATCH_CHAT   optional

Those TLS values are paths to files, not the certificate text itself.
"""

import asyncio
import os
import sys
import time
import uuid

import grpc

import chat_pb2
import chat_pb2_grpc

PORT = os.getenv("CHAT_SERVICE_PORT", "50051")
ADDRESS = os.getenv("CHAT_SERVICE_ADDRESS", f"127.0.0.1:{PORT}")
TLS_CA = os.getenv("CHAT_TLS_CA")
TLS_CLIENT_CERT = os.getenv("CHAT_TLS_CLIENT_CERT")
TLS_CLIENT_KEY = os.getenv("CHAT_TLS_CLIENT_KEY")
USER = os.getenv("WATCH_USER", "watch-stream-user")
CHAT = os.getenv("WATCH_CHAT", f"watch-{uuid.uuid4().hex[:8]}")
SERVICE_TOKEN = os.getenv("CHAT_SERVICE_TOKEN")


def read(path: str) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def open_channel():
    """TLS (and mutual TLS) when CHAT_TLS_CA is set, like the orchestrator does."""

    if bool(TLS_CLIENT_CERT) != bool(TLS_CLIENT_KEY):
        raise SystemExit("Set both CHAT_TLS_CLIENT_CERT and CHAT_TLS_CLIENT_KEY, or neither.")

    if not TLS_CA:
        return grpc.aio.insecure_channel(ADDRESS)

    mutual = TLS_CLIENT_CERT and TLS_CLIENT_KEY
    credentials = grpc.ssl_channel_credentials(
        root_certificates=read(TLS_CA),
        private_key=read(TLS_CLIENT_KEY) if mutual else None,
        certificate_chain=read(TLS_CLIENT_CERT) if mutual else None,
    )

    return grpc.aio.secure_channel(ADDRESS, credentials)


async def main(query: str) -> None:
    metadata = (("authorization", f"Bearer {SERVICE_TOKEN}"),) if SERVICE_TOKEN else None
    request = chat_pb2.ChatRequest(user_id=USER, conversation_id=CHAT, query=query)

    started = time.monotonic()
    times = []  # when each token arrived, in ms since the request was sent

    async with open_channel() as channel:
        chat = chat_pb2_grpc.ChatServiceStub(channel)

        async for message in chat.Chat(request, metadata=metadata):
            now = (time.monotonic() - started) * 1000
            kind = message.WhichOneof("event")

            if kind == "context":
                print(f"[{now:7.0f} ms] context arrived\n")
            elif kind == "token":
                times.append(now)
                # The time goes first, so you can see the gaps between tokens
                print(f"[{now:7.0f} ms] {message.token.text!r}", flush=True)
            elif kind == "done":
                print(f"\n[{now:7.0f} ms] done")

    if not times:
        print("No tokens received.")
        return

    spread = times[-1] - times[0]
    print(
        f"\n{len(times)} tokens | first at {times[0]:.0f} ms | last at {times[-1]:.0f} ms "
        f"| spread {spread:.0f} ms"
    )
    print("Streaming: the spread is long (hundreds of ms or more).")
    print("Buffered:  the spread is tiny (a few ms) and every token comes at the very end.")


if __name__ == "__main__":
    try:
        asyncio.run(main(" ".join(sys.argv[1:]) or "Write 300 words about the sea."))
    except grpc.aio.AioRpcError as error:
        print(f"\nThe service answered {error.code().name}: {error.details()}")

        if error.code() == grpc.StatusCode.UNAUTHENTICATED:
            if SERVICE_TOKEN:
                print(f"CHAT_SERVICE_TOKEN is set here ({len(SERVICE_TOKEN)} characters) but the service refused it.")
                print("Check for quotes or spaces around the value, and that it is the SAME value the service runs with.")
            else:
                print("CHAT_SERVICE_TOKEN is not set in this terminal. Set it first, then run the script again.")

        raise SystemExit(1)