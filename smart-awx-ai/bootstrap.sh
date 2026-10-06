#!/usr/bin/env bash
#
# Smart AWX + AI Agent - one-shot bootstrap for a fresh Ubuntu machine.
#
#   curl -fsSL <raw-url>/bootstrap.sh -o bootstrap.sh
#   sudo bash bootstrap.sh
#
# It only does what install.py cannot do for itself: make sure git and python3
# exist, fetch this repository, and hand over. Every flag you pass is forwarded
# to install.py, so these all work:
#
#   sudo bash bootstrap.sh --dry-run
#   sudo bash bootstrap.sh --operator-version 2.19.1
#   sudo bash bootstrap.sh --profile TEST --keep-ufw
#
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/BHASHKARKUSHWAHA/Test-awx.git}"
REPO_BRANCH="${REPO_BRANCH:-claude/ubuntu-awx-ai-agent-platform-oxyj69}"
CHECKOUT_DIR="${CHECKOUT_DIR:-/opt/smart-awx-ai-src}"
PROJECT_SUBDIR="smart-awx-ai"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- checks
[ "$(id -u)" -eq 0 ] || die "run this as root: sudo bash bootstrap.sh"

if [ -r /etc/os-release ]; then
    . /etc/os-release
    say "detected ${PRETTY_NAME:-unknown}"
    case "${ID:-}" in
        ubuntu|debian) ;;
        *) warn "this installer targets Ubuntu/Debian; ${ID:-unknown} is untested" ;;
    esac
else
    warn "/etc/os-release is missing; continuing anyway"
fi

[ -d /run/systemd/system ] || warn "systemd was not detected - install.py will refuse unless you pass --force (K3s, Ollama and the agent service all need it)"

# ---------------------------------------------------------- dependencies
need_install=""
for binary in git python3 curl ca-certificates; do
    case "$binary" in
        ca-certificates) [ -d /etc/ssl/certs ] || need_install="$need_install ca-certificates" ;;
        *) command -v "$binary" >/dev/null 2>&1 || need_install="$need_install $binary" ;;
    esac
done

if [ -n "$need_install" ]; then
    say "installing bootstrap packages:$need_install"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # shellcheck disable=SC2086
    apt-get install -y -qq --no-install-recommends $need_install
fi

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' \
    || die "python3 >= 3.8 is required (found $(python3 --version 2>&1))"

# --------------------------------------------------------------- sources
if [ -d "$CHECKOUT_DIR/.git" ]; then
    say "updating existing checkout in $CHECKOUT_DIR"
    git -C "$CHECKOUT_DIR" remote set-url origin "$REPO_URL"
    git -C "$CHECKOUT_DIR" fetch --depth 1 origin "$REPO_BRANCH"
    git -C "$CHECKOUT_DIR" checkout -B "$REPO_BRANCH" "origin/$REPO_BRANCH"
else
    say "cloning $REPO_URL ($REPO_BRANCH) into $CHECKOUT_DIR"
    rm -rf "$CHECKOUT_DIR"
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$CHECKOUT_DIR"
fi

PROJECT_DIR="$CHECKOUT_DIR/$PROJECT_SUBDIR"
[ -f "$PROJECT_DIR/install.py" ] || die "install.py not found in $PROJECT_DIR - wrong branch?"

# --------------------------------------------------------------- install
say "starting the installer: python3 install.py $*"
echo
cd "$PROJECT_DIR"
exec python3 install.py "$@"
