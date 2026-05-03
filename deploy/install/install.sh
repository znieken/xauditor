#!/usr/bin/env bash
#
# xauditor analyst install script.
#
# Usage:
#   curl http://<vm-host>:<INSTALL_HOST_PORT>/install.sh \
#     | bash -s -- http://<vm-host>:<PYPI_HOST_PORT>/simple/
#
# or with the env-var fallback:
#   XAUDITOR_PYPI_URL=http://<vm-host>:<PYPI_HOST_PORT>/simple/ \
#     bash install.sh
#
# Environment variables (all optional except XAUDITOR_PYPI_URL when no
# positional arg is given):
#   XAUDITOR_PYPI_URL       PyPI index URL; positional arg 1 overrides this.
#   XAUDITOR_VENV           Virtualenv location. Default: $HOME/.xauditor-venv
#   XAUDITOR_INSTALL_PORTAL When set to "1", also installs xauditor-portal
#                           into the same venv. Default: unset (skip portal).

set -euo pipefail

PYPI_INDEX="${1:-${XAUDITOR_PYPI_URL:-}}"

if [[ -z "$PYPI_INDEX" ]]; then
    cat >&2 <<'EOF'
usage: install.sh <pypi-index-url>
       XAUDITOR_PYPI_URL=<pypi-index-url> install.sh

Example:
    curl http://<vm>:8082/install.sh \
      | bash -s -- http://<vm>:8081/simple/
EOF
    exit 2
fi

VENV_DIR="${XAUDITOR_VENV:-$HOME/.xauditor-venv}"

if ! command -v python3 >/dev/null 2>&1; then
    echo "error: python3 not found on PATH; xauditor requires Python 3.12+" >&2
    exit 3
fi

echo "Creating virtualenv at $VENV_DIR ..."
python3 -m venv "$VENV_DIR"

# shellcheck disable=SC1091
"$VENV_DIR/bin/pip" install --quiet --upgrade pip

echo "Installing xauditor from $PYPI_INDEX (fallback: https://pypi.org/simple/) ..."
"$VENV_DIR/bin/pip" install --quiet \
    --index-url "$PYPI_INDEX" \
    --extra-index-url https://pypi.org/simple/ \
    xauditor

if [[ "${XAUDITOR_INSTALL_PORTAL:-}" == "1" ]]; then
    echo "Installing xauditor-portal ..."
    "$VENV_DIR/bin/pip" install --quiet \
        --index-url "$PYPI_INDEX" \
        --extra-index-url https://pypi.org/simple/ \
        xauditor-portal
fi

cat <<EOF

xauditor installed at: $VENV_DIR/bin/xauditor

To put it on PATH for this shell:
    export PATH="$VENV_DIR/bin:\$PATH"

Or invoke it directly:
    $VENV_DIR/bin/xauditor --help
EOF
