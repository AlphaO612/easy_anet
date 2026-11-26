"""Import an operator-provided pair on its VPS and verify a published TLS listener."""
import argparse
try:
    import fcntl
except ImportError:
    fcntl = None
import ipaddress
import os
from pathlib import Path
try:
    import pwd
except ImportError:
    pwd = None
import re
import socket
import ssl
import sys
import tempfile


def dns_name(value):
    try:
        ipaddress.ip_address(value)
        return False
    except ValueError:
        return (len(value) <= 253 and "." in value and
                all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                    for part in value.split(".")))


def import_pair(owner, installation, domain, cert, key):
    if not re.fullmatch("[a-f0-9]{10}", installation) or not dns_name(domain):
        raise ValueError("Некорректные идентификатор установки или домен.")
    user = pwd.getpwnam(owner)
    if user.pw_uid == 0:
        raise ValueError("Для rootless Docker нужен непривилегированный владелец.")
    if os.getuid() not in (0, user.pw_uid):
        raise ValueError("Требуются права владельца или root.")
    paths = [Path(cert), Path(key)]
    if any(not path.is_absolute() or not path.is_file() for path in paths):
        raise ValueError("Оба PEM-файла должны существовать на входном VPS.")
    pair = [path.read_bytes() for path in paths]
    if os.geteuid() == 0:
        # The operator supplies the two source paths. All destination opens and
        # renames happen only after dropping root and supplementary groups.
        os.setgroups([])
        os.setgid(user.pw_gid)
        os.setuid(user.pw_uid)
    os.umask(0o077)
    root = Path(user.pw_dir) / ".local/state/anet-wizard" / installation
    directory = root / "tls" / domain
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=directory) as tmp:
        staged = [Path(tmp) / "fullchain.pem", Path(tmp) / "privkey.pem"]
        for path, value in zip(staged, pair):
            path.write_bytes(value)
        # Empty callback rejects encrypted keys without an OpenSSL TTY prompt.
        ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain(*staged, password=lambda: "")
        with (root / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for path in staged:
                os.replace(path, directory / path.name)


def verify_listener(port, domain, certificate, *, public_trust):
    """Check SNI, peer identity, validity and the expected leaf on the VPN port."""
    pem = certificate.read_text()
    end = "-----END CERTIFICATE-----"
    leaf = ssl.PEM_cert_to_DER_cert(pem.split(end, 1)[0] + end)
    context = ssl.create_default_context()
    if not public_trust:
        # An operator may distribute a private CA to clients. Trust the supplied
        # local chain for this check and still verify its name, dates and pin.
        context.load_verify_locations(cadata=pem)
        context.verify_flags |= getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname=domain) as tls:
            if tls.getpeercert(binary_form=True) != leaf:
                raise ValueError("На порту VLESS предъявлен другой сертификат.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("owner", "installation", "domain", "cert", "key"):
        parser.add_argument("--" + key, required=True)
    try:
        import_pair(**vars(parser.parse_args()))
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f"Не удалось импортировать сертификат: {error}")
