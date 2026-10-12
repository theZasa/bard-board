"""Client certificate for the bot. Mumble identifies registered users by certificate."""

from __future__ import annotations

import datetime as dt
import logging
import os
from pathlib import Path

log = logging.getLogger("certs")


def ensure_certificate(cert_file: Path, key_file: Path, common_name: str) -> None:
    if cert_file.exists() and key_file.exists():
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    log.info("Erzeuge neues Client-Zertifikat für %s", common_name)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365 * 20))
        .sign(key, hashes.SHA256())
    )
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
        )
    )
    os.chmod(key_file, 0o600)
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
