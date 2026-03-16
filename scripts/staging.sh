#!/usr/bin/env bash
# Staging install helper for Untether.
#
# Manages the staging bot's install source — switches between TestPyPI
# release candidates (for dogfooding) and stable PyPI releases.
#
# Usage:
#   staging.sh install 0.35.0rc1   # Install rc from TestPyPI
#   staging.sh rollback             # Revert to last stable PyPI version
#   staging.sh reset                # Reinstall from real PyPI (post-release)
#   staging.sh status               # Show current install source and version

set -euo pipefail

PACKAGE="untether"

case "${1:-}" in
    install)
        VERSION="${2:?Usage: staging.sh install VERSION (e.g. 0.35.0rc1)}"
        echo "Installing $PACKAGE==$VERSION from TestPyPI..."
        pipx install --force \
            --pip-args="--index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/" \
            "$PACKAGE==$VERSION"
        echo ""
        echo "Installed. Next steps:"
        echo "  systemctl --user restart untether"
        echo "  scripts/healthcheck.sh --version $VERSION"
        ;;
    rollback)
        echo "Rolling back to latest stable PyPI release..."
        pipx install --force \
            --pip-args="--index-url https://pypi.org/simple/" \
            "$PACKAGE"
        INSTALLED=$(untether --version 2>/dev/null || echo "unknown")
        echo "Rolled back to $INSTALLED"
        echo ""
        echo "Next steps:"
        echo "  systemctl --user restart untether"
        ;;
    reset)
        echo "Resetting to real PyPI (post-release)..."
        pipx install --force \
            --pip-args="--index-url https://pypi.org/simple/" \
            "$PACKAGE"
        INSTALLED=$(untether --version 2>/dev/null || echo "unknown")
        echo "Reset to $INSTALLED from PyPI"
        echo ""
        echo "Next steps:"
        echo "  systemctl --user restart untether"
        ;;
    status)
        INSTALLED=$(untether --version 2>/dev/null || echo "unknown")
        if [[ "$INSTALLED" == *rc* ]] || [[ "$INSTALLED" == *dev* ]] || [[ "$INSTALLED" == *a* ]] || [[ "$INSTALLED" == *b* ]]; then
            echo "STAGING: $INSTALLED (pre-release from TestPyPI)"
        else
            echo "STABLE: $INSTALLED (from PyPI)"
        fi
        ;;
    *)
        echo "Usage: staging.sh {install VERSION|rollback|reset|status}"
        echo ""
        echo "Commands:"
        echo "  install VERSION  Install a release candidate from TestPyPI"
        echo "  rollback         Revert to the latest stable PyPI version"
        echo "  reset            Reinstall from real PyPI (after final release)"
        echo "  status           Show current install source (staging vs stable)"
        exit 1
        ;;
esac
