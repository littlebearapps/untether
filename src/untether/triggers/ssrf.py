"""SSRF protection for outbound HTTP requests in triggers.

Validates URLs and resolved IP addresses against blocked private/reserved
ranges before allowing outbound requests.  Used by webhook ``http_forward``
action, external payload URL fetching, and cron data-fetch triggers.

See https://github.com/littlebearapps/untether/issues/276
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Sequence
from urllib.parse import urlparse

from ..logging import get_logger

logger = get_logger(__name__)

# Private and reserved IP ranges that must be blocked by default.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    # IPv4
    ipaddress.IPv4Network("127.0.0.0/8"),  # Loopback
    ipaddress.IPv4Network("10.0.0.0/8"),  # RFC 1918
    ipaddress.IPv4Network("172.16.0.0/12"),  # RFC 1918
    ipaddress.IPv4Network("192.168.0.0/16"),  # RFC 1918
    ipaddress.IPv4Network("169.254.0.0/16"),  # Link-local
    ipaddress.IPv4Network("0.0.0.0/8"),  # "This" network
    ipaddress.IPv4Network("100.64.0.0/10"),  # Shared address (CGN)
    ipaddress.IPv4Network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.IPv4Network("192.0.2.0/24"),  # Documentation (TEST-NET-1)
    ipaddress.IPv4Network("198.51.100.0/24"),  # Documentation (TEST-NET-2)
    ipaddress.IPv4Network("203.0.113.0/24"),  # Documentation (TEST-NET-3)
    ipaddress.IPv4Network("224.0.0.0/4"),  # Multicast
    ipaddress.IPv4Network("240.0.0.0/4"),  # Reserved
    ipaddress.IPv4Network("255.255.255.255/32"),  # Broadcast
    # IPv6
    ipaddress.IPv6Network("::1/128"),  # Loopback
    ipaddress.IPv6Network("::/128"),  # Unspecified
    ipaddress.IPv6Network("fc00::/7"),  # Unique local
    ipaddress.IPv6Network("fe80::/10"),  # Link-local
    ipaddress.IPv6Network("ff00::/8"),  # Multicast
    # IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1)
    ipaddress.IPv6Network("::ffff:127.0.0.0/104"),
    ipaddress.IPv6Network("::ffff:10.0.0.0/104"),
    ipaddress.IPv6Network("::ffff:172.16.0.0/108"),
    ipaddress.IPv6Network("::ffff:192.168.0.0/112"),
    ipaddress.IPv6Network("::ffff:169.254.0.0/112"),
)

# Schemes allowed for outbound requests.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Default and maximum timeout for outbound fetches (seconds).
DEFAULT_TIMEOUT: int = 15
MAX_TIMEOUT: int = 60

# Default and maximum response size (bytes).
DEFAULT_MAX_BYTES: int = 10 * 1024 * 1024  # 10 MB
MAX_MAX_BYTES: int = 100 * 1024 * 1024  # 100 MB

# Maximum number of redirects to follow.
MAX_REDIRECTS: int = 2


class SSRFError(Exception):
    """Raised when an outbound request is blocked by SSRF protection."""


class SSRFBlockedError(SSRFError):
    """#679: the host is (or resolves only to) a blocked private/reserved
    address. Carries the parsed ``hostname`` and the blocked ``addresses`` so
    callers can build actionable guidance without parsing the message.
    ``str(exc)`` is unchanged from the plain :class:`SSRFError` it replaces."""

    def __init__(
        self, message: str, *, hostname: str, addresses: tuple[str, ...]
    ) -> None:
        super().__init__(message)
        self.hostname = hostname
        self.addresses = addresses


class SSRFResolutionError(SSRFError):
    """#679: DNS resolution for ``hostname`` failed or returned nothing."""

    def __init__(self, message: str, *, hostname: str) -> None:
        super().__init__(message)
        self.hostname = hostname


def redact_url_userinfo(url: str) -> str:
    """#679: replace ``user[:pass]@`` in *url*'s netloc with ``***@``.

    Returns the input unchanged when it has no userinfo or can't be parsed —
    this is a log-hygiene helper and must never raise.
    """
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
    except ValueError:
        return url
    if "@" not in netloc:
        return url
    host_part = netloc.rsplit("@", 1)[1]
    try:
        return parsed._replace(netloc=f"***@{host_part}").geturl()
    except ValueError:
        return url


def strip_url_userinfo(url: str) -> str:
    """#679: drop ``user[:pass]@`` from *url*'s netloc entirely. The SSRF
    verdict depends only on the scheme and host, so validating the stripped
    copy is equivalent and keeps credentials out of every ``ssrf.*`` log."""
    try:
        parsed = urlparse(url)
        netloc = parsed.netloc
    except ValueError:
        return url
    if "@" not in netloc:
        return url
    try:
        return parsed._replace(netloc=netloc.rsplit("@", 1)[1]).geturl()
    except ValueError:
        return url


# #679: private ranges an operator may reasonably run a self-hosted service in
# (RFC 1918, CGN — which covers Tailscale — and IPv6 ULA, plus mapped forms).
# A suggestion for these is the bare address (least privilege). Link-local
# (incl. 169.254.169.254 cloud metadata), unspecified, multicast, reserved,
# broadcast and documentation ranges are deliberately absent: we never nudge
# an operator towards allowlisting them.
_SUGGESTABLE_PRIVATE: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
    ipaddress.IPv4Network("100.64.0.0/10"),
    ipaddress.IPv6Network("fc00::/7"),
    ipaddress.IPv6Network("::ffff:10.0.0.0/104"),
    ipaddress.IPv6Network("::ffff:172.16.0.0/108"),
    ipaddress.IPv6Network("::ffff:192.168.0.0/112"),
)

_MAX_SUGGESTIONS = 3


def suggest_allowlist(addresses: Sequence[str]) -> tuple[str, ...]:
    """#679: the narrowest safe allowlist entries that would unblock *addresses*.

    - IPv4 loopback → ``"127.0.0.0/8"`` (matches the documented example); when
      present it alone is enough, because a hostname passes if ANY resolved
      address is allowed.
    - ``::1`` → ``"::1"``; IPv4-mapped loopback → ``"::ffff:127.0.0.0/104"``.
    - RFC 1918 / CGN (tailnet) / ULA and their mapped forms → the bare address.
    - Anything else (link-local, cloud metadata, multicast, reserved, …) →
      nothing.

    De-duplicated, sorted, capped at 3.
    """
    parsed: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for raw in addresses:
        try:
            parsed.append(ipaddress.ip_address(raw))
        except ValueError:
            continue
    if any(isinstance(a, ipaddress.IPv4Address) and a.is_loopback for a in parsed):
        return ("127.0.0.0/8",)
    out: set[str] = set()
    for addr in parsed:
        if isinstance(addr, ipaddress.IPv6Address):
            # Mapped first: on Python 3.13+ ``is_loopback`` is also true for
            # ``::ffff:127.x`` and "::1" would not unblock it.
            mapped = addr.ipv4_mapped
            if mapped is not None and mapped.is_loopback:
                out.add("::ffff:127.0.0.0/104")
                continue
            if addr.is_loopback:
                out.add("::1")
                continue
        if any(addr in net for net in _SUGGESTABLE_PRIVATE):
            out.add(str(addr))
    return tuple(sorted(out))[:_MAX_SUGGESTIONS]


def parse_networks(
    entries: Sequence[str],
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse CIDR / bare-IP strings into networks for use as an SSRF allowlist.

    Bare IPs (``"10.0.0.5"``) become host networks (``/32`` or ``/128``).
    Raises :class:`ValueError` on any unparseable entry — callers validating
    config should surface this as a config error.
    """
    return [ipaddress.ip_network(entry, strict=False) for entry in entries]


def _is_blocked_ip(
    addr: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    extra_blocked: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
    allowlist: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
) -> bool:
    """Check whether *addr* falls in a blocked range.

    The *allowlist* is checked first — if the address matches an allowlist
    entry it is permitted even if it also matches a blocked range.  This lets
    admins explicitly opt in to hitting local services.
    """
    for net in allowlist:
        if addr in net:
            return False
    return any(addr in net for net in (*BLOCKED_NETWORKS, *extra_blocked))


def validate_url(
    url: str,
    *,
    allowlist: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
) -> str:
    """Validate a URL for outbound fetching.

    Checks scheme and, if the host is an IP literal, checks it against
    blocked ranges immediately.  Hostname-based URLs pass this check and
    are validated at DNS resolution time via :func:`resolve_and_validate`.

    Returns the normalised URL string on success.

    Raises :class:`SSRFError` on validation failure.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise SSRFError(f"Invalid URL: {exc}") from exc

    if parsed.scheme not in ALLOWED_SCHEMES:
        logger.warning(
            "ssrf.scheme_blocked", url=redact_url_userinfo(url), scheme=parsed.scheme
        )
        raise SSRFError(
            f"Scheme {parsed.scheme!r} not allowed; "
            f"permitted: {', '.join(sorted(ALLOWED_SCHEMES))}"
        )

    if not parsed.hostname:
        logger.warning("ssrf.no_hostname", url=redact_url_userinfo(url))
        raise SSRFError("URL has no hostname")

    # If the host is an IP literal, check it immediately.
    try:
        addr = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        # It's a hostname — will be checked at resolution time.
        pass
    else:
        if _is_blocked_ip(addr, allowlist=allowlist):
            logger.warning("ssrf.ip_blocked", hostname=parsed.hostname)
            raise SSRFBlockedError(
                f"Blocked: {parsed.hostname} resolves to private/reserved range",
                hostname=parsed.hostname,
                addresses=(parsed.hostname,),
            )

    return url


def resolve_and_validate(
    hostname: str,
    *,
    port: int = 443,
    allowlist: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
) -> list[tuple[str, int]]:
    """Resolve *hostname* via DNS and validate all addresses.

    Returns a list of ``(ip_string, port)`` tuples for addresses that pass
    validation.

    Raises :class:`SSRFError` if **all** resolved addresses are blocked or
    if DNS resolution fails entirely.

    This function performs blocking DNS resolution and should be called
    from a worker thread (e.g. via ``anyio.to_thread.run_sync``).
    """
    try:
        results = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SSRFResolutionError(
            f"DNS resolution failed for {hostname!r}: {exc}", hostname=hostname
        ) from exc

    if not results:
        raise SSRFResolutionError(f"No DNS results for {hostname!r}", hostname=hostname)

    allowed: list[tuple[str, int]] = []
    blocked: list[str] = []

    for _family, _type, _proto, _canonname, sockaddr in results:
        ip_str = sockaddr[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _is_blocked_ip(addr, allowlist=allowlist):
            blocked.append(ip_str)
            logger.warning(
                "ssrf.dns_blocked",
                hostname=hostname,
                ip=ip_str,
                reason="private/reserved range",
            )
        else:
            allowed.append((ip_str, port))

    if not allowed:
        blocked_str = ", ".join(blocked)
        raise SSRFBlockedError(
            f"All resolved addresses for {hostname!r} are blocked: {blocked_str}",
            hostname=hostname,
            addresses=tuple(blocked),
        )

    return allowed


async def validate_url_with_dns(
    url: str,
    *,
    allowlist: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = (),
) -> str:
    """Validate URL scheme, host, and DNS resolution (async).

    Combines :func:`validate_url` (scheme + IP literal check) with
    :func:`resolve_and_validate` (DNS resolution + IP check) for
    hostname-based URLs.

    Returns the validated URL string.
    Raises :class:`SSRFError` on any validation failure.
    """
    import anyio

    validated_url = validate_url(url, allowlist=allowlist)
    parsed = urlparse(validated_url)
    hostname = parsed.hostname
    assert hostname is not None  # validate_url already checked

    # If the host is already an IP literal, validate_url handled it.
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        # Hostname — resolve and check all addresses.
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        await anyio.to_thread.run_sync(
            lambda: resolve_and_validate(hostname, port=port, allowlist=allowlist)
        )

    logger.info("ssrf.validated", url=redact_url_userinfo(validated_url))
    return validated_url


def clamp_timeout(timeout: int | float | None) -> float:
    """Clamp a user-supplied timeout to the allowed range."""
    if timeout is None:
        return float(DEFAULT_TIMEOUT)
    return float(max(1, min(timeout, MAX_TIMEOUT)))


def clamp_max_bytes(max_bytes: int | None) -> int:
    """Clamp a user-supplied max-bytes to the allowed range."""
    if max_bytes is None:
        return DEFAULT_MAX_BYTES
    return max(1024, min(max_bytes, MAX_MAX_BYTES))
