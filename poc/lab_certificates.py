"""Generate a dedicated two-machine TLS test identity. Requires cryptography.

Generated keys are unencrypted lab material. Store them in an access-controlled
directory, copy only host.* to A and guest.* to B plus ca.pem to both. Never
commit keys. This is not the production machine-identity provisioning service.
"""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def generate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    names = ('ca.pem', 'host.pem', 'host.key', 'guest.pem', 'guest.key')
    if any((directory / name).exists() for name in names):
        raise ValueError('Refusing to overwrite existing test identities')
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Portway isolated lab CA')])

    def builder(subject, public_key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer)
                .public_key(public_key).serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5))
                .not_valid_after(now + timedelta(days=7)))

    ca = (builder(issuer, ca_key.public_key())
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .sign(ca_key, hashes.SHA256()))
    (directory / 'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    for role, purpose in (('host', ExtendedKeyUsageOID.SERVER_AUTH),
                          ('guest', ExtendedKeyUsageOID.CLIENT_AUTH)):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'portway-' + role)])
        certificate = (builder(subject, key.public_key())
                       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                       .add_extension(x509.SubjectAlternativeName([x509.DNSName('portway-' + role)]), False)
                       .add_extension(x509.ExtendedKeyUsage([purpose]), False)
                       .sign(ca_key, hashes.SHA256()))
        (directory / (role + '.pem')).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        (directory / (role + '.key')).write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    # CA private key is intentionally not persisted.


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='native/certificates')
    generate(parser.parse_args().output)
