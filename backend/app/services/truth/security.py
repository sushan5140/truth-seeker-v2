import ipaddress
import socket
from urllib.parse import urljoin, urlsplit, urlunsplit


class UnsafeUrlError(ValueError):
    pass


BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "0.0.0.0"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan")


def _is_blocked_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def validate_remote_url(value: str, *, resolve_dns: bool = True) -> str:
    candidate = value.strip()
    if not candidate:
        raise UnsafeUrlError("URL is empty")
    parsed = urlsplit(candidate)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeUrlError("Only HTTP and HTTPS URLs are allowed")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("URLs containing credentials are not allowed")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname or hostname in BLOCKED_HOSTS or hostname.endswith(BLOCKED_SUFFIXES):
        raise UnsafeUrlError("Local or invalid host is not allowed")
    if parsed.port and parsed.port not in {80, 443}:
        raise UnsafeUrlError("Non-standard network ports are not allowed")

    try:
        literal_address = ipaddress.ip_address(hostname)
    except ValueError:
        literal_address = None
    if literal_address is not None:
        if _is_blocked_ip(hostname):
            raise UnsafeUrlError("Private or local IP addresses are not allowed")
    else:
        if resolve_dns:
            try:
                addresses = {item[4][0] for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
            except socket.gaierror as exc:
                raise UnsafeUrlError("Host could not be resolved") from exc
            if not addresses or any(_is_blocked_ip(address) for address in addresses):
                raise UnsafeUrlError("Host resolves to a private or unsafe address")

    path = parsed.path or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def validate_redirect(base_url: str, location: str) -> str:
    return validate_remote_url(urljoin(base_url, location), resolve_dns=True)
