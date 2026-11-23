"""chat_client against a real local gRPC server: TLS, token and mutual TLS."""

import asyncio
import importlib.util
import datetime
import ipaddress
from pathlib import Path

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import chat_pb2
import chat_pb2_grpc
# Other test files may put a fake "chat_client" into sys.modules. Load the
# real file under a private name so this test always checks the real code.
_spec = importlib.util.spec_from_file_location(
    "chat_client_under_test",
    Path(__file__).resolve().parent.parent / "chat_client.py",
)
chat_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat_client)

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


    async def DeleteConversation(self, request, context):
        return chat_pb2.DeleteConversationResponse()


class Chat(chat_pb2_grpc.ChatServiceServicer):
    async def Chat(self, request, context):
        yield chat_pb2.ChatEvent(token=chat_pb2.Token(text="hi"))


async def start_server(cert, key, client_ca=None):
    server = grpc.aio.server(interceptors=[CheckToken()])
    chat_pb2_grpc.add_HistoryServiceServicer_to_server(History(), server)
    chat_pb2_grpc.add_ChatServiceServicer_to_server(Chat(), server)

    credentials = grpc.ssl_server_credentials(
        [(key.read_bytes(), cert.read_bytes())],
        root_certificates=client_ca.read_bytes() if client_ca else None,
        require_client_auth=bool(client_ca),
    )
    port = server.add_secure_port("127.0.0.1:0", credentials)
    await server.start()
    return server, f"localhost:{port}"


def configure(monkeypatch, address, ca=None, token=TOKEN, client=None):
    """Point chat_client at the test server, the way the .env file would."""

    monkeypatch.setattr(chat_client, "CHAT_SERVICE_ADDRESS", address)
    monkeypatch.setattr(chat_client, "CHAT_TLS_CA", str(ca) if ca else None)
    monkeypatch.setattr(chat_client, "CHAT_SERVICE_TOKEN", token)
    monkeypatch.setattr(chat_client, "CHAT_TLS_CLIENT_CERT", str(client[0]) if client else None)
    monkeypatch.setattr(chat_client, "CHAT_TLS_CLIENT_KEY", str(client[1]) if client else None)


async def use_client(call):
    """Run one chat_client call with a fresh channel, then close it."""

    try:
        return await call()
    finally:
        await chat_client.close()


async def history():
    return await chat_client.list_conversations("u1", 10, None)


async def error_code(call):
    with pytest.raises(chat_client.ChatServiceError) as error:
        await use_client(call)
    return error.value.code


# ---------- tests ----------

def test_tls_and_token_work_for_every_call(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")

    async def scenario():
        server, address = await start_server(cert, key)
        configure(monkeypatch, address, ca=cert)
        try:
            async def everything():
                items = (await chat_client.list_conversations("u1", 10, None))["items"]
                events = [e async for e in chat_client.chat("u1", "c1", "hi")]
                await chat_client.delete_conversation("u1", "c1")
                return items, events

            return await use_client(everything)
        finally:
            await server.stop(None)

    items, events = asyncio.run(scenario())

    assert items[0]["title"] == "hello"
    assert events == [("token", "hi")]


def test_wrong_or_missing_token_is_refused(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")

    async def scenario():
        server, address = await start_server(cert, key)
        try:
            configure(monkeypatch, address, ca=cert, token="wrong")
            wrong = await error_code(history)

            configure(monkeypatch, address, ca=cert, token=None)
            missing = await error_code(history)

            return wrong, missing
        finally:
            await server.stop(None)

    assert asyncio.run(scenario()) == (
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNAUTHENTICATED,
    )


def test_plain_text_client_cannot_talk_to_a_tls_server(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")

    async def scenario():
        server, address = await start_server(cert, key)
        configure(monkeypatch, address, ca=None)
        try:
            return await error_code(history)
        finally:
            await server.stop(None)

    assert asyncio.run(scenario()) == grpc.StatusCode.UNAVAILABLE


def test_a_server_with_an_untrusted_certificate_is_refused(tmp_path, monkeypatch):
    real_cert, real_key = make_cert(tmp_path, "server")
    other_cert, _ = make_cert(tmp_path, "impostor")

    async def scenario():
        server, address = await start_server(real_cert, real_key)
        configure(monkeypatch, address, ca=other_cert)  # we trust someone else
        try:
            return await error_code(history)
        finally:
            await server.stop(None)

    assert asyncio.run(scenario()) == grpc.StatusCode.UNAVAILABLE


def test_mutual_tls(tmp_path, monkeypatch):
    cert, key = make_cert(tmp_path, "server")
    client_cert, client_key = make_cert(tmp_path, "client")

    async def scenario():
        server, address = await start_server(cert, key, client_ca=client_cert)
        try:
            configure(monkeypatch, address, ca=cert, client=(client_cert, client_key))
            ok = await use_client(history)

            configure(monkeypatch, address, ca=cert)  # no client certificate
            refused = await error_code(history)

            return ok, refused
        finally:
            await server.stop(None)

    ok, refused = asyncio.run(scenario())

    assert ok["items"][0]["id"] == "c1"
    assert refused == grpc.StatusCode.UNAVAILABLE


def test_half_a_client_certificate_is_a_config_error(tmp_path, monkeypatch):
    cert, _ = make_cert(tmp_path, "server")
    configure(monkeypatch, "localhost:1", ca=cert)
    monkeypatch.setattr(chat_client, "CHAT_TLS_CLIENT_CERT", str(cert))  # key missing

    async def scenario():
        chat_client._stubs()

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())