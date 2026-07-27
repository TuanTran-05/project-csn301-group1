#!/usr/bin/env bash
#
# Prepare the AI Server node to run the backend.
#
#   cd backend && ./scripts/setup_ai_server.sh
#
# Idempotent: safe to re-run after pulling new code. Never prints a secret.
# It stops before seeding, because seeding needs passwords only you should choose.

set -euo pipefail

cd "$(dirname "$0")/.."
BACKEND_DIR="$(pwd)"
VENV="${BACKEND_DIR}/.venv"
PY="${VENV}/bin/python"

say() { printf '\n== %s\n' "$1"; }
fail() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

# -- 1. interpreter -------------------------------------------------------
say "Checking the Python interpreter"

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    printf 'Found: %s\n' "$(python3 --version 2>&1 || echo 'no python3')"
    fail "Python 3.11+ is required. The backend uses 'X | None' annotations that
       are evaluated at runtime, so 3.10 fails at import.
       On Ubuntu 22.04 (ships 3.10):  sudo apt install python3.11 python3.11-venv
       Or use an Ubuntu 24.04 / Debian 12 node instead."
fi
printf '   using %s (%s)\n' "$PYTHON_BIN" "$("$PYTHON_BIN" --version 2>&1)"

# -- 2. virtualenv --------------------------------------------------------
say "Creating the virtualenv"
if [ ! -x "$PY" ]; then
    "$PYTHON_BIN" -m venv "$VENV" \
        || fail "venv creation failed. Install the venv package:
       sudo apt install ${PYTHON_BIN}-venv"
fi
"$PY" -m pip install --quiet --upgrade pip

say "Installing dependencies"
"$PY" -m pip install --quiet -e ".[dev]"

# -- 3. configuration -----------------------------------------------------
say "Preparing .env"
if [ -f .env ]; then
    echo "   .env already exists, leaving it untouched"
else
    cp .env.example .env
    # Generate the three secrets so no placeholder value ever reaches production.
    CRED_KEY="$("$PY" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
    SECRET="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    JWT="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
    "$PY" - "$CRED_KEY" "$SECRET" "$JWT" <<'PYEOF'
import sys
from pathlib import Path

cred, secret, jwt = sys.argv[1:4]
env = Path(".env")
lines = []
for line in env.read_text().splitlines():
    if line.startswith("CREDENTIAL_ENCRYPTION_KEY="):
        line = f"CREDENTIAL_ENCRYPTION_KEY={cred}"
    elif line.startswith("SECRET_KEY="):
        line = f"SECRET_KEY={secret}"
    elif line.startswith("JWT_SECRET_KEY="):
        line = f"JWT_SECRET_KEY={jwt}"
    lines.append(line)
env.write_text("\n".join(lines) + "\n")
PYEOF
    chmod 600 .env
    echo "   created .env with freshly generated keys (mode 600)"
fi

# -- 4. database ----------------------------------------------------------
say "Applying database migrations"
FLASK_APP=wsgi.py "${VENV}/bin/flask" db upgrade

# -- 5. self-check --------------------------------------------------------
say "Running the test suite"
if "$PY" -m pytest -q; then
    echo "   test suite passed"
else
    fail "The test suite failed. Fix that before pointing this at real devices."
fi

# -- 6. what is still missing --------------------------------------------
say "Setup complete"
MISSING=()
grep -q '^AI_API_KEY=.\+' .env || MISSING+=("AI_API_KEY        (Gemini key from https://aistudio.google.com/apikey)")
grep -q '^LAB_SSH_USERNAME=.\+' .env || MISSING+=("LAB_SSH_USERNAME  (the Cisco user, e.g. ai-automation)")
grep -q '^LAB_SSH_PASSWORD=.\+' .env || MISSING+=("LAB_SSH_PASSWORD  (that user's password)")
grep -q '^SEED_ADMIN_PASSWORD=.\+' .env || MISSING+=("SEED_ADMIN_PASSWORD (your backend login password)")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo
    echo "Still to fill in ${BACKEND_DIR}/.env:"
    for item in "${MISSING[@]}"; do
        echo "  - ${item}"
    done
fi

cat <<EOF

Then:
  ${PY} scripts/seed_lab.py        # 9 devices + admin + encrypted credentials
  ${PY} scripts/smoke_test_lab.py  # real SSH check, exits 1 if any device fails
  FLASK_APP=wsgi.py ${VENV}/bin/flask run --host 0.0.0.0 --port 5000
EOF
