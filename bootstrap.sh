# Sourced by run.sh and test.sh — not meant to be run directly.
#
# Creates .venv if it is missing, installs requirements.txt, and leaves
# $VENV_BIN pointing at the venv's executable directory.

# $PYTHON wins if it is set. Otherwise probe the candidates rather than
# trusting the PATH: on Windows, `python3` is often a Microsoft Store stub that
# resolves fine but refuses to run.
if [ -z "${PYTHON:-}" ]; then
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c '' >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [ -z "${PYTHON:-}" ]; then
  echo "No working Python interpreter found. Set \$PYTHON to one." >&2
  exit 1
fi

# The venv's executable directory is Scripts/ on Windows, bin/ elsewhere.
venv_bin() {
  if [ -d .venv/Scripts ]; then echo .venv/Scripts; else echo .venv/bin; fi
}

# A .venv built by a different platform's Python — say, checked out on Windows
# and then run under WSL — is present but unusable. Rebuild it instead of
# failing with an error that points nowhere.
if [ -d .venv ] && ! "$(venv_bin)/python" -c '' >/dev/null 2>&1; then
  echo "The existing .venv does not run here; rebuilding it." >&2
  rm -rf .venv
fi

if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi

VENV_BIN="$(venv_bin)"

if ! "$VENV_BIN/python" -m pip --version >/dev/null 2>&1; then
  echo "The virtualenv has no pip. On Debian/Ubuntu: sudo apt install python3-venv" >&2
  exit 1
fi

"$VENV_BIN/python" -m pip install --quiet --disable-pip-version-check -r requirements.txt
