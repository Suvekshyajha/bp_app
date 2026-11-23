"""Make a self-signed certificate for local development / testing.

    python make_dev_certs.py

Writes certs/server.crt and certs/server.key (valid 1 year, for
"localhost" and 127.0.0.1). Then set:

  chat service (retriever) .env:   CHAT_TLS_CERT=certs/server.crt
                                   CHAT_TLS_KEY=certs/server.key
  orchestrator .env:               CHAT_TLS_CA=certs/server.crt

Never commit certs/*.key. For a real deployment use a certificate from your
own CA or from your hosting provider instead.
"""

import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

OUT = Path("certs")
OUT.mkdir(exist_ok=True)

key = ec.generate_private_key(ec.SECP256R1())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
now = datetime.datetime.now(datetime.timezone.utc)

cert = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(now - datetime.timedelta(minutes=1))
    .not_valid_after(now + datetime.timedelta(days=365))
    .add_extension(
        x509.SubjectAlternativeName([
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]),
        critical=False,
    )
    .sign(key, hashes.SHA256())
)

(OUT / "server.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
(OUT / "server.key").write_bytes(key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
))

print("Wrote certs/server.crt and certs/server.key")