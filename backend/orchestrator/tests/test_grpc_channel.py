"""make_channel against a real local gRPC server: TLS, token and mutual TLS."""

import asyncio
import datetime
import ipaddress

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import chat_pb2
import chat_pb2_grpc
import retriever_pb2
import retriever_pb2_grpc
from grpc_channel import make_channel

TOKEN = "s3cret-token"


# ---------- throw-away certificates ----------

def make_cert(tmp_path, name):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    return cert_path, key_path


# ---------- a tiny server that checks the token like the real one ----------

class CheckToken(grpc.aio.ServerInterceptor):
    def __init__(self):
        async def deny(request, context):
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "bad token")

        self._deny = grpc.unary_unary_rpc_method_handler(deny)

    async def intercept_service(self, continuation, details):
        given = dict(details.invocation_metadata or ()).get("authorization")

        if given != f"Bearer {TOKEN}":
            return self._deny

        return await continuation(details)


class History(chat_pb2_grpc.HistoryServiceServicer):
    async def ListConversations(self, request, context):
        return chat_pb2.ListConversationsResponse(
            conversations=[chat_pb2.Conversation(id="c1", title="hello")]
        )


class Chat(chat_pb2_grpc.ChatServiceServicer):
    async def Chat(self, request, context):
        yield chat_pb2.ChatEvent(token=chat_pb2.Token(text="hi"))


class Retriever(retriever_pb2_grpc.RetrieverServiceServicer):
    async def StoreMessage(self, request_iterator, context):
        async for request in request_iterator:
            yield retriever_pb2.StoreMessageResponse(
                message_id="m1", request_id=request.request_id
            )


async def start_server(cert, key, client_ca=None):
    server = grpc.aio.server(interceptors=[CheckToken()])
    chat_pb2_grpc.add_HistoryServiceServicer_to_server(History(), server)
    chat_pb2_grpc.add_ChatServiceServicer_to_server(Chat(), server)
    retriever_pb2_grpc.add_RetrieverServiceServicer_to_server(Retriever(), server)

    credentials = grpc.ssl_server_credentials(
        [(key.read_bytes(), cert.read_bytes())],
        root_certificates=client_ca.read_bytes() if client_ca else None,
        require_client_auth=bool(client_ca),
    )
    port = server.add_secure_port("127.0.0.1:0", credentials)
    await server.start()
    return server, f"localhost:{port}"


def configure(monkeypatch, ca=None, token=TOKEN, client=None):
    for name in ("CHAT_TLS_CA", "CHAT_SERVICE_TOKEN",
                 "CHAT_TLS_CLIENT_CERT", "CHAT_TLS_CLIENT_KEY"):
        monkeypatch.delenv(name, raising=False)

    if ca:
        monkeypatch.setenv("CHAT_TLS_CA", str(ca))
    if token:
        monkeypatch.setenv("CHAT_SERVICE_TOKEN", token)
    if client:
        monkeypatch.setenv("CHAT_TLS_CLIENT_CERT", str(client[0]))
        monkeypatch.setenv("CHAT_TLS_CLIENT_KEY", str(client[1]))


async def list_conversations(address):
    async with make_channel(address) as channel:
        stub = chat_pb2_grpc.HistoryServiceStub(channel)
        return await stub.ListConversations(
            chat_pb2.ListConversationsRequest(user_id="u1"), timeout=5
        )


async def code_of(coroutine):
    with pytest.raises(grpc.aio.AioRpcError) as error:
        await coroutine
    return error.value.code()


# ---------- tests ----------

def test_tls_and_token_work_for_every_kind_of_call(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")
    configure(monkeypatch, ca=cert)

    async def scenario():
        server, address = await start_server(cert, key)
        try:
            reply = await list_conversations(address)          # unary

            async with make_channel(address) as channel:
                events = [e async for e in chat_pb2_grpc.ChatServiceStub(channel).Chat(
                    chat_pb2.ChatRequest(user_id="u1"), timeout=5)]   # server stream

                store = retriever_pb2_grpc.RetrieverServiceStub(channel).StoreMessage

                async def requests():
                    yield retriever_pb2.StoreMessageRequest(request_id="r1")

                replies = [r async for r in store(requests(), timeout=5)]   # two-way stream

            return reply, events, replies
        finally:
            await server.stop(None)

    reply, events, replies = asyncio.run(scenario())

    assert reply.conversations[0].title == "hello"
    assert events[0].token.text == "hi"
    assert replies[0].request_id == "r1"


def test_wrong_or_missing_token_is_refused(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")

    async def scenario():
        server, address = await start_server(cert, key)
        try:
            configure(monkeypatch, ca=cert, token="wrong")
            wrong = await code_of(list_conversations(address))

            configure(monkeypatch, ca=cert, token=None)
            missing = await code_of(list_conversations(address))

            return wrong, missing
        finally:
            await server.stop(None)

    assert asyncio.run(scenario()) == (
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNAUTHENTICATED,
    )


def test_plain_text_client_cannot_talk_to_a_tls_server(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")
    configure(monkeypatch, ca=None)  # no TLS on the client

    async def scenario():
        server, address = await start_server(cert, key)
        try:
            return await code_of(list_conversations(address))
        finally:
            await server.stop(None)

    assert asyncio.run(scenario()) == grpc.StatusCode.UNAVAILABLE


def test_a_server_with_an_untrusted_certificate_is_refused(tmp_path, monkeypatch):
    real_cert, real_key = make_cert(tmp_path, "server")
    other_cert, _ = make_cert(tmp_path, "impostor")
    configure(monkeypatch, ca=other_cert)  # we trust a different certificate

    async def scenario():
        server, address = await start_server(real_cert, real_key)
        try:
            return await code_of(list_conversations(address))
        finally:
            await server.stop(None)

    assert asyncio.run(scenario()) == grpc.StatusCode.UNAVAILABLE


def test_mutual_tls(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")
    client_cert, client_key = make_cert(tmp_path, "client")

    async def scenario():
        server, address = await start_server(cert, key, client_ca=client_cert)
        try:
            configure(monkeypatch, ca=cert, client=(client_cert, client_key))
            ok = await list_conversations(address)

            configure(monkeypatch, ca=cert)  # no client certificate
            refused = await code_of(list_conversations(address))

            return ok, refused
        finally:
            await server.stop(None)

    ok, refused = asyncio.run(scenario())

    assert ok.conversations[0].id == "c1"
    assert refused == grpc.StatusCode.UNAVAILABLE


def test_half_a_client_certificate_is_a_config_error(tmp_path, monkeypatch):
    configure(monkeypatch, client=(tmp_path / "a", tmp_path / "b"))
    monkeypatch.delenv("CHAT_TLS_CLIENT_KEY")

    async def scenario():
        make_channel("localhost:1")

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())