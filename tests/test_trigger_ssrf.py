"""Tests for SSRF protection utility."""

from __future__ import annotations

import ipaddress
import socket
from unittest.mock import patch

import pytest

from untether.triggers.ssrf import (
    BLOCKED_NETWORKS,
    SSRFError,
    _is_blocked_ip,
    clamp_max_bytes,
    clamp_timeout,
    resolve_and_validate,
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
