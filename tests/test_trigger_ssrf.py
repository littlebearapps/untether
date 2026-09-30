"""Tests for SSRF protection utility."""

from __future__ import annotations

import ipaddress
import socket
from unittest.mock import patch

import pytest
from structlog.testing import capture_logs

from untether.triggers.ssrf import (
    BLOCKED_NETWORKS,
    SSRFBlockedError,
    SSRFError,
    SSRFResolutionError,
    _is_blocked_ip,
    clamp_max_bytes,
    clamp_timeout,
    parse_networks,
    redact_url_userinfo,
    resolve_and_validate,
    suggest_allowlist,
    validate_url,
    validate_url_with_dns,
)

# ---------------------------------------------------------------------------
# _is_blocked_ip
# ---------------------------------------------------------------------------


class TestIsBlockedIP:
    """Direct IP address blocking checks."""

    @pytest.mark.parametrize(
        "ip",
        [
            "127.0.0.1",
            "127.0.0.2",
            "127.255.255.255",
            "10.0.0.1",
            "10.255.255.255",
            "172.16.0.1",
            "172.31.255.255",
            "192.168.0.1",
            "192.168.255.255",
            "169.254.1.1",
            "0.0.0.0",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
        ],
    )
    def test_blocked_ipv4(self, ip: str) -> None:
        addr = ipaddress.ip_address(ip)
        assert _is_blocked_ip(addr) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "::1",
            "::",
            "fc00::1",
            "fdff::1",
            "fe80::1",
            "ff02::1",
        ],
    )
    def test_blocked_ipv6(self, ip: str) -> None:
        addr = ipaddress.ip_address(ip)
        assert _is_blocked_ip(addr) is True

    @pytest.mark.parametrize(
        "ip",
        [
            "8.8.8.8",
            "1.1.1.1",
            "93.184.216.34",
            "203.0.114.1",
            "2607:f8b0:4004:800::200e",
        ],
    )
    def test_allowed_public_ips(self, ip: str) -> None:
        addr = ipaddress.ip_address(ip)
        assert _is_blocked_ip(addr) is False

    def test_ipv4_mapped_ipv6_loopback_blocked(self) -> None:
        addr = ipaddress.ip_address("::ffff:127.0.0.1")
        assert _is_blocked_ip(addr) is True

    def test_ipv4_mapped_ipv6_private_blocked(self) -> None:
        addr = ipaddress.ip_address("::ffff:10.0.0.1")
        assert _is_blocked_ip(addr) is True

    def test_ipv4_mapped_ipv6_public_allowed(self) -> None:
        addr = ipaddress.ip_address("::ffff:8.8.8.8")
        assert _is_blocked_ip(addr) is False

    def test_allowlist_overrides_block(self) -> None:
        addr = ipaddress.ip_address("10.0.0.5")
        allowlist = [ipaddress.IPv4Network("10.0.0.0/24")]
        assert _is_blocked_ip(addr, allowlist=allowlist) is False

    def test_allowlist_does_not_affect_other_ranges(self) -> None:
        addr = ipaddress.ip_address("192.168.1.1")
        allowlist = [ipaddress.IPv4Network("10.0.0.0/24")]
        assert _is_blocked_ip(addr, allowlist=allowlist) is True

    def test_extra_blocked_ranges(self) -> None:
        addr = ipaddress.ip_address("8.8.8.8")
        extra = [ipaddress.IPv4Network("8.8.8.0/24")]
        assert _is_blocked_ip(addr, extra_blocked=extra) is True

    def test_cgn_range_blocked(self) -> None:
        """100.64.0.0/10 (Carrier-Grade NAT) should be blocked."""
        addr = ipaddress.ip_address("100.64.0.1")
        assert _is_blocked_ip(addr) is True


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------


class TestValidateURL:
    """URL scheme and host validation."""

    def test_valid_https_url(self) -> None:
        result = validate_url("https://api.github.com/repos")
        assert result == "https://api.github.com/repos"

    def test_valid_http_url(self) -> None:
        result = validate_url("http://example.com/webhook")
        assert result == "http://example.com/webhook"

    def test_ftp_scheme_blocked(self) -> None:
        with pytest.raises(SSRFError, match=r"Scheme.*not allowed"):
            validate_url("ftp://files.example.com/data")

    def test_file_scheme_blocked(self) -> None:
        with pytest.raises(SSRFError, match=r"Scheme.*not allowed"):
            validate_url("file:///etc/passwd")

    def test_javascript_scheme_blocked(self) -> None:
        with pytest.raises(SSRFError, match=r"Scheme.*not allowed"):
            validate_url("javascript:alert(1)")

    def test_no_hostname_blocked(self) -> None:
        with pytest.raises(SSRFError, match="no hostname"):
            validate_url("https://")

    def test_ip_literal_loopback_blocked(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            validate_url("http://127.0.0.1:8080/api")

    def test_ip_literal_private_blocked(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            validate_url("http://10.0.0.5/internal")

    def test_ip_literal_link_local_blocked(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            validate_url("http://169.254.169.254/latest/meta-data/")

    def test_ip_literal_public_allowed(self) -> None:
        result = validate_url("https://93.184.216.34/page")
        assert "93.184.216.34" in result

    def test_hostname_passes_without_dns_check(self) -> None:
        """Hostnames are not resolved by validate_url — that's for resolve_and_validate."""
        result = validate_url("https://internal.corp.example.com/api")
        assert result == "https://internal.corp.example.com/api"

    def test_ipv6_loopback_blocked(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            validate_url("http://[::1]:8080/api")

    def test_allowlist_permits_blocked_ip(self) -> None:
        allowlist = [ipaddress.IPv4Network("127.0.0.0/8")]
        result = validate_url("http://127.0.0.1:9876/health", allowlist=allowlist)
        assert "127.0.0.1" in result


# ---------------------------------------------------------------------------
# resolve_and_validate
# ---------------------------------------------------------------------------


class TestResolveAndValidate:
    """DNS resolution + IP validation."""

    def test_public_ip_passes(self) -> None:
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
        ]
        with patch("socket.getaddrinfo", return_value=fake_results):
            result = resolve_and_validate("example.com", port=443)
        assert result == [("93.184.216.34", 443)]

    def test_private_ip_blocked(self) -> None:
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("192.168.1.1", 443),
            ),
        ]
        with (
            patch("socket.getaddrinfo", return_value=fake_results),
            pytest.raises(SSRFError, match=r"All resolved addresses.*blocked"),
        ):
            resolve_and_validate("evil.example.com", port=443)

    def test_mixed_results_filters_blocked(self) -> None:
        """When DNS returns both public and private IPs, only public ones pass."""
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.1", 443),
            ),
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
        ]
        with patch("socket.getaddrinfo", return_value=fake_results):
            result = resolve_and_validate("dual.example.com", port=443)
        assert result == [("93.184.216.34", 443)]

    def test_dns_failure_raises(self) -> None:
        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror("NXDOMAIN")),
            pytest.raises(SSRFError, match="DNS resolution failed"),
        ):
            resolve_and_validate("nonexistent.invalid", port=443)

    def test_empty_dns_results_raises(self) -> None:
        with (
            patch("socket.getaddrinfo", return_value=[]),
            pytest.raises(SSRFError, match="No DNS results"),
        ):
            resolve_and_validate("empty.example.com", port=443)

    def test_allowlist_permits_private(self) -> None:
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.5", 443),
            ),
        ]
        allowlist = [ipaddress.IPv4Network("10.0.0.0/24")]
        with patch("socket.getaddrinfo", return_value=fake_results):
            result = resolve_and_validate(
                "internal.corp", port=443, allowlist=allowlist
            )
        assert result == [("10.0.0.5", 443)]

    def test_loopback_blocked_even_as_hostname(self) -> None:
        """DNS rebinding: hostname resolves to 127.0.0.1."""
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 80),
            ),
        ]
        with (
            patch("socket.getaddrinfo", return_value=fake_results),
            pytest.raises(SSRFError, match=r"All resolved addresses.*blocked"),
        ):
            resolve_and_validate("rebind.evil.com", port=80)

    def test_metadata_ip_blocked(self) -> None:
        """AWS/GCP metadata endpoint (169.254.169.254) blocked."""
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("169.254.169.254", 80),
            ),
        ]
        with (
            patch("socket.getaddrinfo", return_value=fake_results),
            pytest.raises(SSRFError, match=r"All resolved addresses.*blocked"),
        ):
            resolve_and_validate("metadata.internal", port=80)


# ---------------------------------------------------------------------------
# validate_url_with_dns (async)
# ---------------------------------------------------------------------------


class TestValidateURLWithDNS:
    """Async URL + DNS validation."""

    @pytest.mark.anyio
    async def test_public_hostname_passes(self) -> None:
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("93.184.216.34", 443),
            ),
        ]
        with patch("socket.getaddrinfo", return_value=fake_results):
            result = await validate_url_with_dns("https://example.com/api")
        assert result == "https://example.com/api"

    @pytest.mark.anyio
    async def test_private_hostname_blocked(self) -> None:
        fake_results = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.1", 443),
            ),
        ]
        with (
            patch("socket.getaddrinfo", return_value=fake_results),
            pytest.raises(SSRFError, match=r"All resolved addresses.*blocked"),
        ):
            await validate_url_with_dns("https://internal.corp.com/api")

    @pytest.mark.anyio
    async def test_ip_literal_skips_dns(self) -> None:
        """IP literal URLs don't need DNS resolution."""
        result = await validate_url_with_dns("https://93.184.216.34/api")
        assert "93.184.216.34" in result

    @pytest.mark.anyio
    async def test_ip_literal_blocked_without_dns(self) -> None:
        with pytest.raises(SSRFError, match="private/reserved"):
            await validate_url_with_dns("http://127.0.0.1/api")

    @pytest.mark.anyio
    async def test_bad_scheme_blocked(self) -> None:
        with pytest.raises(SSRFError, match="Scheme"):
            await validate_url_with_dns("ftp://example.com/file")


# ---------------------------------------------------------------------------
# clamp_timeout / clamp_max_bytes
# ---------------------------------------------------------------------------


class TestClampTimeout:
    def test_default(self) -> None:
        assert clamp_timeout(None) == 15.0

    def test_within_range(self) -> None:
        assert clamp_timeout(30) == 30.0

    def test_below_minimum(self) -> None:
        assert clamp_timeout(0) == 1.0
        assert clamp_timeout(-5) == 1.0

    def test_above_maximum(self) -> None:
        assert clamp_timeout(120) == 60.0

    def test_float_passthrough(self) -> None:
        assert clamp_timeout(7.5) == 7.5


class TestClampMaxBytes:
    def test_default(self) -> None:
        assert clamp_max_bytes(None) == 10 * 1024 * 1024

    def test_within_range(self) -> None:
        assert clamp_max_bytes(5_000_000) == 5_000_000

    def test_below_minimum(self) -> None:
        assert clamp_max_bytes(100) == 1024

    def test_above_maximum(self) -> None:
        assert clamp_max_bytes(200_000_000) == 100 * 1024 * 1024


# ---------------------------------------------------------------------------
# BLOCKED_NETWORKS completeness
# ---------------------------------------------------------------------------


class TestBlockedNetworks:
    """Verify the blocked networks tuple covers key ranges."""

    def test_loopback_covered(self) -> None:
        assert any(ipaddress.ip_address("127.0.0.1") in net for net in BLOCKED_NETWORKS)

    def test_rfc1918_all_three_covered(self) -> None:
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.0.1"):
            assert any(ipaddress.ip_address(ip) in net for net in BLOCKED_NETWORKS), (
                f"{ip} not covered"
            )

    def test_link_local_covered(self) -> None:
        assert any(
            ipaddress.ip_address("169.254.1.1") in net for net in BLOCKED_NETWORKS
        )

    def test_ipv6_loopback_covered(self) -> None:
        assert any(ipaddress.ip_address("::1") in net for net in BLOCKED_NETWORKS)

    def test_ipv6_ula_covered(self) -> None:
        assert any(ipaddress.ip_address("fc00::1") in net for net in BLOCKED_NETWORKS)

    def test_public_ip_not_covered(self) -> None:
        assert not any(
            ipaddress.ip_address("8.8.8.8") in net for net in BLOCKED_NETWORKS
        )


# ---------------------------------------------------------------------------
# #679: structured errors, allowlist suggestions, userinfo redaction
# ---------------------------------------------------------------------------


def _gai(*ips: str) -> list[tuple]:
    """Build fake ``getaddrinfo`` results for *ips*."""
    out: list[tuple] = []
    for ip in ips:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 443, 0, 0) if family == socket.AF_INET6 else (ip, 443)
        out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return out


class TestStructuredErrors:
    def test_679_ip_literal_raises_blocked_error_with_fields(self) -> None:
        with pytest.raises(SSRFBlockedError) as info:
            validate_url("http://127.0.0.1:8000/v1")
        exc = info.value
        assert isinstance(exc, SSRFError)
        assert exc.hostname == "127.0.0.1"
        assert exc.addresses == ("127.0.0.1",)
        assert str(exc) == "Blocked: 127.0.0.1 resolves to private/reserved range"

    def test_679_dns_all_blocked_raises_blocked_error_with_all_addresses(
        self,
    ) -> None:
        with (
            patch("socket.getaddrinfo", return_value=_gai("127.0.0.1", "::1")),
            pytest.raises(SSRFBlockedError) as info,
        ):
            resolve_and_validate("localhost", port=8000)
        assert info.value.hostname == "localhost"
        assert info.value.addresses == ("127.0.0.1", "::1")
        assert str(info.value).startswith("All resolved addresses for 'localhost'")

    def test_679_dns_partial_block_still_passes(self) -> None:
        with patch(
            "socket.getaddrinfo", return_value=_gai("127.0.0.1", "93.184.216.34")
        ):
            result = resolve_and_validate("mixed.example.com", port=443)
        assert result == [("93.184.216.34", 443)]

    def test_679_dns_failure_raises_resolution_error(self) -> None:
        with (
            patch("socket.getaddrinfo", side_effect=socket.gaierror("nope")),
            pytest.raises(SSRFResolutionError) as info,
        ):
            resolve_and_validate("nx.invalid")
        assert info.value.hostname == "nx.invalid"
        assert isinstance(info.value, SSRFError)
        with (
            patch("socket.getaddrinfo", return_value=[]),
            pytest.raises(SSRFResolutionError),
        ):
            resolve_and_validate("empty.invalid")

    def test_679_scheme_error_is_plain_ssrf_error(self) -> None:
        with pytest.raises(SSRFError) as info:
            validate_url("ftp://example.com/x")
        assert type(info.value) is SSRFError

    @pytest.mark.anyio
    async def test_679_ssrf_logs_redact_userinfo(self) -> None:
        with (
            capture_logs() as logs,
            patch("socket.getaddrinfo", return_value=_gai("93.184.216.34")),
        ):
            await validate_url_with_dns("http://user:s3cret@example.com/v1")
            with pytest.raises(SSRFError):
                validate_url("ftp://user:s3cret@x/")
        assert logs
        for entry in logs:
            for value in entry.values():
                assert "s3cret" not in str(value)
        validated = [e for e in logs if e["event"] == "ssrf.validated"]
        assert validated[0]["url"] == "http://***@example.com/v1"

    def test_679_redact_url_userinfo(self) -> None:
        assert redact_url_userinfo("https://api.groq.com/v1") == (
            "https://api.groq.com/v1"
        )
        assert redact_url_userinfo("http://user@host:8000/v1") == (
            "http://***@host:8000/v1"
        )
        assert redact_url_userinfo("http://user:pass@host/v1") == ("http://***@host/v1")
        # Unparseable input is returned unchanged, never raises.
        assert redact_url_userinfo("http://[::1") == "http://[::1"


class TestSuggestAllowlist:
    def test_679_suggest_loopback_v4(self) -> None:
        assert suggest_allowlist(["127.0.0.1"]) == ("127.0.0.0/8",)

    def test_679_suggest_loopback_dual_stack(self) -> None:
        assert suggest_allowlist(["127.0.0.1", "::1"]) == ("127.0.0.0/8",)

    def test_679_suggest_loopback_v6_only(self) -> None:
        assert suggest_allowlist(["::1"]) == ("::1",)

    def test_679_suggest_mapped_loopback(self) -> None:
        assert suggest_allowlist(["::ffff:127.0.0.1"]) == ("::ffff:127.0.0.0/104",)

    def test_679_suggest_private_exact_ip(self) -> None:
        assert suggest_allowlist(["10.1.2.3"]) == ("10.1.2.3",)
        assert suggest_allowlist(["100.101.102.103"]) == ("100.101.102.103",)
        assert suggest_allowlist(["fd7a:115c:a1e0::1"]) == ("fd7a:115c:a1e0::1",)

    @pytest.mark.parametrize(
        "addr",
        [
            "169.254.169.254",
            "fe80::1",
            "0.0.0.0",
            "224.0.0.1",
            "192.0.2.1",
            "255.255.255.255",
        ],
    )
    def test_679_suggest_never_metadata_or_link_local(self, addr: str) -> None:
        assert suggest_allowlist([addr]) == ()

    def test_679_suggest_mixed_drops_unsafe(self) -> None:
        assert suggest_allowlist(["10.0.0.5", "169.254.169.254"]) == ("10.0.0.5",)

    def test_679_suggest_capped_and_deduped(self) -> None:
        result = suggest_allowlist(
            ["10.0.0.5", "10.0.0.5", "10.0.0.9", "192.168.1.2", "172.16.0.3"]
        )
        assert len(result) <= 3
        assert len(set(result)) == len(result)
        assert list(result) == sorted(result)

    def test_679_suggest_ignores_garbage(self) -> None:
        assert suggest_allowlist(["localhost", ""]) == ()

    @pytest.mark.parametrize(
        "addr",
        [
            "127.0.0.1",
            "::1",
            "::ffff:127.0.0.1",
            "10.1.2.3",
            "172.16.4.5",
            "192.168.1.20",
            "100.101.102.103",
            "fd7a:115c:a1e0::1",
            "::ffff:10.0.0.5",
        ],
    )
    def test_679_every_suggestion_unblocks_its_address(self, addr: str) -> None:
        ip = ipaddress.ip_address(addr)
        assert _is_blocked_ip(ip)
        suggested = suggest_allowlist([addr])
        assert suggested
        assert not _is_blocked_ip(ip, allowlist=parse_networks(suggested))
