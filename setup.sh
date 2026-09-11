#!/bin/bash
# Hummingbot API Setup - Creates .env with sensible defaults (Mac/Linux/WSL2)
# - On Linux (apt-based): installs build deps (gcc, build-essential)
# - Ensures Docker + Docker Compose are available (auto-installs on Linux via get.docker.com)
# - Idempotent: safe to run multiple times, skips already-completed steps
# - Verbose output: shows all installation progress directly
# - Fixed: Removed apt-get upgrade, uses /dev/tty for prompts

set -euo pipefail

echo "Hummingbot API Setup"
echo ""

# --------------------------
# State Tracking Variables
# --------------------------
APT_CACHE_UPDATED=false
DOCKER_ALREADY_PRESENT=false
COMPOSE_ALREADY_PRESENT=false

has_cmd() { command -v "$1" >/dev/null 2>&1; }

prompt_tty() {
  local message="$1"
  local default_value="${2:-}"
  local value=""
  local fd
  if [[ -t 0 ]]; then
    read -r -p "$message" value
  elif { exec {fd}<>/dev/tty; } 2>/dev/null; then
    printf '%s' "$message" >&${fd}
    read -r value <&${fd}
    exec {fd}>&-
  elif IFS= read -r value; then
    :
  else
    value=""
  fi
  echo "${value:-$default_value}"
}

prompt_yes_no() {
  local message="$1"
  local default_value="${2:-n}"
  local value
  value="$(prompt_tty "$message" "$default_value")"
  [[ "$value" =~ ^[Yy]$ ]]
}

prompt_required_tty() {
  local message="$1"
  local value=""
  while true; do
    value="$(prompt_tty "$message" "")"
    if [[ -n "$value" ]]; then
      echo "$value"
      return 0
    fi
    echo "[WARN] This value cannot be empty"
  done
}

# Same as prompt_tty, but with echo off. These values end up in .env and guard
# an API that can place orders and read balances -- typing them in cleartext
# leaves them in the terminal, in scrollback, and in any recorded install log.
prompt_secret_tty() {
  local message="$1"
  local value=""
  local fd
  if [[ -t 0 ]]; then
    read -rs -p "$message" value
    echo "" >&2
  elif { exec {fd}<>/dev/tty; } 2>/dev/null; then
    printf '%s' "$message" >&${fd}
    read -rs value <&${fd}
    printf '\n' >&${fd}
    exec {fd}>&-
  elif IFS= read -r value; then
    :
  else
    value=""
  fi
  echo "$value"
}

# Masked, non-empty, typed twice -- a mistyped password that is never echoed
# is otherwise only discovered when the API refuses to authenticate.
prompt_required_secret_tty() {
  local message="$1"
  local value="" confirm=""
  while true; do
    value="$(prompt_secret_tty "$message")"
    if [[ -z "$value" ]]; then
      echo "[WARN] This value cannot be empty" >&2
      continue
    fi
    if env_value_unsafe "$value"; then
      echo "[WARN] Not supported here: spaces and  \` \$ \" ' \\ ; & | < > ( ) ~" >&2
      echo "[WARN] .env is read by three different parsers, and the Makefile sources it -- these would execute rather than parse." >&2
      continue
    fi
    confirm="$(prompt_secret_tty "Confirm: ")"
    if [[ "$value" != "$confirm" ]]; then
      echo "[WARN] Values did not match, try again" >&2
      continue
    fi
    echo "$value"
    return 0
  done
}

# Characters that are unsafe in a .env value.
#
# The Makefile's run/deploy/emqx-auth targets read .env with `. ./.env`, so a
# value is not merely parsed there -- it is EXECUTED. A password containing
# $(...) or a `;` runs as the deploying user. Spaces were already rejected
# below because ".env is read by three different parsers that disagree on
# quoting"; this is the same argument, for the characters where the
# disagreement is arbitrary code rather than a truncated value.
#
# Rejected: whitespace and  ` $ " ' \ ; & | < > ( ) ~
# Still allowed:  A-Z a-z 0-9 ! @ # % ^ * - _ = + [ ] { } : , . ? /
#
# Validated HERE because setup.sh is the only writer of .env -- one author
# means one place to enforce this, for typed and caller-supplied values alike.
env_value_unsafe() {
  case "$1" in
    *[[:space:]]*|*'`'*|*'$'*|*'"'*|*"'"*|*'\'*|*';'*|*'&'*|*'|'*|*'<'*|*'>'*|*'('*|*')'*|*'~'*) return 0 ;;
    *) return 1 ;;
  esac
}

resolve_script_dir() {
  local src="${BASH_SOURCE[0]}"
  while [ -h "$src" ]; do
    local dir
    dir="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
    src="$(readlink "$src")"
    [[ "$src" != /* ]] && src="$dir/$src"
  done
  cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd
}

SCRIPT_DIR="$(resolve_script_dir)"

# --------------------------
# OS / Environment Detection
# --------------------------
OS="$(uname -s || true)"
ARCH="$(uname -m || true)"

is_linux() { [[ "${OS}" == "Linux" ]]; }
is_macos() { [[ "${OS}" == "Darwin" ]]; }

docker_ok() { has_cmd docker; }

docker_compose_ok() {
  if has_cmd docker && docker compose version >/dev/null 2>&1; then
    return 0
  fi
  if has_cmd docker-compose && docker-compose version >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

need_sudo_or_die() {
  if ! has_cmd sudo; then
    echo "ERROR: 'sudo' is required for dependency installation on this system."
    echo "Please install sudo (or run as root) and re-run this script."
    exit 1
  fi
}

# --------------------------
# APT Cache Management (Linux)
# --------------------------
safe_apt_update() {
  # Only run apt-get update once per script execution
  if [ "$APT_CACHE_UPDATED" = false ]; then
    echo "[INFO] Updating apt cache..."
    sudo env DEBIAN_FRONTEND=noninteractive apt-get update
    APT_CACHE_UPDATED=true
  fi
}

# --------------------------
# Package Check Utilities
# --------------------------
is_package_installed() {
  # Check if a Debian package is installed
  # Usage: is_package_installed package-name
  dpkg -l "$1" 2>/dev/null | grep -q "^ii"
}

# --------------------------
# Linux Dependencies
# --------------------------
install_linux_build_deps() {
  if has_cmd apt-get; then
    # Check if build dependencies are already installed
    if is_package_installed build-essential && has_cmd gcc; then
      echo "[OK] Build dependencies (gcc, build-essential) already installed. Skipping."
      return 0
    fi
    
    need_sudo_or_die
    echo "[INFO] Installing build dependencies (gcc, build-essential)..."

    safe_apt_update
    
    # REMOVED: apt-get upgrade -y 
    # This was causing failures due to system-wide package upgrades
    # apt-get install will get the latest available versions anyway
    
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y gcc build-essential

    echo "[OK] Build dependencies installed."
  else
    echo "[WARN] Detected Linux, but 'apt-get' is not available. Skipping build dependency install."
  fi
}

ensure_curl_on_linux() {
  if has_cmd curl; then
    echo "[OK] curl is already installed."
    return 0
  fi

  if has_cmd apt-get; then
    need_sudo_or_die
    echo "[INFO] Installing curl (required for Docker install script)..."
    safe_apt_update
    sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y curl ca-certificates
    echo "[OK] curl installed."
    return 0
  fi

  echo "[WARN] curl is not installed and apt-get is unavailable. Please install curl and re-run."
  return 1
}

# --------------------------
# Docker Install / Validation
# --------------------------
# Who is running this, for the docker-group messages below.
#
# $USER is set by login shells, and is NOT set by several ways this script is
# legitimately run as root: `docker exec`, cloud-init, a systemd unit, some CI
# runners. Under `set -euo pipefail` a bare "$USER" is then a fatal unbound
# variable, and the install died before its first prompt -- on the root path,
# where the group question is moot anyway. `id -un` always answers.
current_user() {
  printf '%s' "${USER:-$(id -un)}"
}

check_user_in_docker_group() {
  # Check if current user is already in docker group
  if [[ "${EUID}" -eq 0 ]]; then
    # Running as root, no need for docker group
    return 0
  fi
  
  if has_cmd getent && getent group docker >/dev/null 2>&1; then
    if id -nG "$(current_user)" 2>/dev/null | grep -qw docker; then
      return 0
    fi
  fi
  
  return 1
}

add_user_to_docker_group() {
  # Only add user to docker group if not already a member
  if check_user_in_docker_group; then
    echo "[OK] User '$(current_user)' is already in the 'docker' group."
    return 0
  fi
  
  if has_cmd getent && getent group docker >/dev/null 2>&1; then
    if [[ "${EUID}" -ne 0 ]]; then
      echo "[INFO] Adding current user to 'docker' group (may require re-login)..."
      sudo usermod -aG docker "$(current_user)" >/dev/null 2>&1 || true
      echo "[OK] User added to docker group. You may need to log out and back in for this to take effect."
    fi
  fi
}

install_docker_linux() {
  need_sudo_or_die
  ensure_curl_on_linux

  echo "[INFO] Docker not found. Installing Docker using get.docker.com script..."
  curl -fsSL https://get.docker.com -o get-docker.sh
  sudo sh get-docker.sh
  rm -f get-docker.sh

  if has_cmd systemctl; then
    if systemctl is-system-running >/dev/null 2>&1; then
      echo "[INFO] Enabling and starting Docker service..."
      sudo systemctl enable docker 2>/dev/null || true
      sudo systemctl start docker 2>/dev/null || true
    fi
  fi

  add_user_to_docker_group
}

ensure_docker_and_compose() {
  if is_linux; then
    # Check Docker installation
    if docker_ok; then
      echo "[OK] Docker already installed: $(docker --version 2>/dev/null || echo 'version unknown')"
      DOCKER_ALREADY_PRESENT=true
      
      # Even if Docker is installed, ensure user is in docker group
      add_user_to_docker_group
    else
      # Check if Docker binary exists but isn't in PATH
      if [ -x "/usr/bin/docker" ] || [ -x "/usr/local/bin/docker" ]; then
        echo "[INFO] Docker found but not in current PATH. Adding to PATH..."
        export PATH="/usr/bin:/usr/local/bin:$PATH"
        
        if docker_ok; then
          echo "[OK] Docker is now accessible: $(docker --version 2>/dev/null || echo 'version unknown')"
          DOCKER_ALREADY_PRESENT=true
          add_user_to_docker_group
        else
          install_docker_linux
        fi
      else
        install_docker_linux
      fi
    fi

    # Verify Docker is actually working
    if ! docker_ok; then
      echo "ERROR: Docker installation did not succeed or 'docker' is still not on PATH."
      echo "       Try opening a new shell and re-running, or verify Docker installation."
      exit 1
    fi

    # Check Docker Compose installation
    if docker_compose_ok; then
      echo "[OK] Docker Compose already available"
      COMPOSE_ALREADY_PRESENT=true
      
      # Show which version we detected
      if docker compose version >/dev/null 2>&1; then
        echo "[OK] Using Docker Compose plugin: $(docker compose version 2>/dev/null || echo 'version unknown')"
      else
        echo "[OK] Using standalone docker-compose: $(docker-compose version 2>/dev/null || echo 'version unknown')"
      fi
    else
      # Try to install docker-compose-plugin
      if has_cmd apt-get; then
        # Check if plugin package is already installed but not working
        if is_package_installed docker-compose-plugin; then
          echo "[WARN] docker-compose-plugin package is installed but not functioning properly."
          echo "[INFO] Attempting to reinstall docker-compose-plugin..."
          need_sudo_or_die
          safe_apt_update
          sudo env DEBIAN_FRONTEND=noninteractive apt-get install --reinstall -y docker-compose-plugin || true
        else
          need_sudo_or_die
          echo "[INFO] Docker Compose not found. Attempting to install docker-compose-plugin..."
          safe_apt_update
          sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y docker-compose-plugin || true
        fi
      fi
    fi

    # Final verification of Docker Compose
    if ! docker_compose_ok; then
      echo "ERROR: Docker Compose is not available."
      echo "       Expected either 'docker compose' (v2) or 'docker-compose' (v1)."
      echo "       On Ubuntu/Debian, try: sudo apt-get install -y docker-compose-plugin"
      exit 1
    fi
    
  elif is_macos; then
    if ! docker_ok || ! docker_compose_ok; then
      echo "ERROR: Docker and/or Docker Compose not found on macOS."
      echo "       Install Docker Desktop for Mac (Apple Silicon or Intel) and re-run this script."
      echo "       After installation, ensure 'docker' works in this terminal (you may need a new shell)."
      exit 1
    fi
    
    echo "[OK] Docker detected: $(docker --version 2>/dev/null || echo 'version unknown')"
    if docker compose version >/dev/null 2>&1; then
      echo "[OK] Docker Compose detected: $(docker compose version 2>/dev/null || echo 'version unknown')"
    else
      echo "[OK] Docker Compose detected: $(docker-compose version 2>/dev/null || echo 'version unknown')"
    fi
    
  else
    echo "[WARN] Unsupported/unknown OS '${OS}'. Proceeding without installing OS-level dependencies."
    if ! docker_ok || ! docker_compose_ok; then
      echo "ERROR: Docker and/or Docker Compose not found."
      exit 1
    fi
    
    echo "[OK] Docker detected: $(docker --version 2>/dev/null || echo 'version unknown')"
    if docker compose version >/dev/null 2>&1; then
      echo "[OK] Docker Compose detected: $(docker compose version 2>/dev/null || echo 'version unknown')"
    else
      echo "[OK] Docker Compose detected: $(docker-compose version 2>/dev/null || echo 'version unknown')"
    fi
  fi
}

# --------------------------
# Pull Hummingbot Docker Image
# --------------------------
pull_hummingbot_image() {
  echo "[INFO] Pulling latest Hummingbot image (hummingbot/hummingbot:latest)..."
  if docker pull hummingbot/hummingbot:latest; then
    echo "[OK] Hummingbot image pulled successfully."
  else
    echo "[WARN] Could not pull hummingbot/hummingbot:latest (network issue?). You may need to run 'docker pull hummingbot/hummingbot:latest' manually."
  fi
}

# --------------------------
# Pre-flight (deps + docker)
# --------------------------
echo "[INFO] OS=${OS} ARCH=${ARCH}"

# HBAPI_SKIP_DEPS=1 skips the apt/Docker installation pass only -- for a caller
# that has already established both (Condor's installer checks `docker info`
# before it ever gets here). Without it, delegation means every Condor install
# re-runs an apt-get build-dep pass and a Docker install probe that cannot
# change anything, adding minutes to a run that already satisfied them.
#
# Deliberately NOT skipped: the Hummingbot image pull below. Dependency checks
# are the caller's to vouch for; a missing bot image is a functional gap no
# caller currently covers.
if [ "${HBAPI_SKIP_DEPS:-0}" = "1" ]; then
  echo "[INFO] Skipping dependency install (HBAPI_SKIP_DEPS=1) — caller vouches for docker + build deps."
  DOCKER_ALREADY_PRESENT=true
  COMPOSE_ALREADY_PRESENT=true
else

if is_linux; then
  install_linux_build_deps
fi

ensure_docker_and_compose

fi

# Show summary of what was done
echo ""
if [ "$DOCKER_ALREADY_PRESENT" = true ] && [ "$COMPOSE_ALREADY_PRESENT" = true ]; then
  echo "[OK] All dependencies were already installed. No changes made."
elif [ "$DOCKER_ALREADY_PRESENT" = true ]; then
  echo "[OK] Docker was already installed. Docker Compose has been set up."
elif [ "$COMPOSE_ALREADY_PRESENT" = true ]; then
  echo "[OK] Docker has been installed. Docker Compose was already available."
else
  echo "[OK] Docker and Docker Compose have been installed."
fi

echo ""

# Always pull latest Hummingbot image (first install and upgrade)
pull_hummingbot_image

echo ""

# --------------------------
# Existing .env creation flow
# --------------------------
if [ -f ".env" ]; then
  echo ".env file already exists. Skipping setup."
  echo ""
  echo "  To change credentials or Tailscale settings: edit .env, then 'make deploy'."
  echo "  To start over from scratch:                  make reset && make setup"
  echo "  To check the install:                        make doctor"
  echo ""

  # Ensure sentinel file exists
  if [ ! -f ".setup-complete" ]; then
    touch .setup-complete
  fi

  exit 0
fi

# --------------------------
# Non-interactive mode
# --------------------------
# Lets another installer (Condor's setup-environment.sh, CI, a provisioning
# script) drive this setup without a human. It exists because the alternative
# -- a caller hand-writing .env itself -- is how the schema drifted before:
# a second author shipped BROKER_PASSWORD=password and omitted
# BROKER_DASHBOARD_PASSWORD entirely, leaving the broker with credentials that
# did not match its own bootstrap file.
#
# Only the values a human would TYPE are accepted here. Everything this script
# GENERATES -- both broker passwords above all -- stays generated in both
# modes, so a caller cannot supply a weak one. That is the whole point: .env
# has exactly one author regardless of who started the run.
#
# The prompts below read from /dev/tty, so they cannot be fed by a pipe; this
# is the supported way to answer them programmatically.
HBAPI_NONINTERACTIVE="${HBAPI_NONINTERACTIVE:-0}"

# Captured before the defaults below reset them -- these three are the only
# Tailscale settings a caller may supply, and the reset would otherwise
# silently discard them.
_env_ts_enabled="${TAILSCALE_ENABLED:-}"
_env_ts_key="${TAILSCALE_AUTH_KEY:-}"
_env_ts_host="${TAILSCALE_HOSTNAME:-}"
# Optional caller tuning. Emitted only when supplied, so a standalone install's
# .env is unchanged and this repo's own defaults keep applying.
_env_bt_concurrent="${HBAPI_BACKTESTING_MAX_CONCURRENT:-}"
_env_bt_timeout="${HBAPI_BACKTESTING_TIMEOUT_SECONDS:-}"

# Clear screen before prompting user (only if running interactively)
if [ "$HBAPI_NONINTERACTIVE" != "1" ] && [[ -t 0 ]] && [[ -c /dev/tty ]]; then
  # `clear` exits non-zero when TERM is unset (a tty without a termcap entry --
  # CI runners, `script` wrappers, some IDE terminals). Under `set -e` that
  # aborted the whole setup before a single prompt, so fall back to the escape
  # sequence rather than trusting the exit status.
  clear 2>/dev/null || printf "\033c"
fi

TAILSCALE_ENABLED=false
TAILSCALE_AUTH_KEY=""
TAILSCALE_HOSTNAME="hummingbot-api"

if [ "$HBAPI_NONINTERACTIVE" = "1" ]; then
  echo "Hummingbot API Setup (non-interactive)"
  echo ""
  # Fail loudly rather than writing a half-configured .env: these three guard
  # an API that can place orders and read balances, and there is no safe
  # default for any of them.
  : "${HBAPI_USERNAME:?HBAPI_NONINTERACTIVE=1 requires HBAPI_USERNAME}"
  : "${HBAPI_PASSWORD:?HBAPI_NONINTERACTIVE=1 requires HBAPI_PASSWORD}"
  : "${HBAPI_CONFIG_PASSWORD:?HBAPI_NONINTERACTIVE=1 requires HBAPI_CONFIG_PASSWORD}"
  USERNAME="$HBAPI_USERNAME"
  PASSWORD="$HBAPI_PASSWORD"
  CONFIG_PASSWORD="$HBAPI_CONFIG_PASSWORD"
  for _v in USERNAME PASSWORD CONFIG_PASSWORD; do
    eval "_val=\$$_v"
    if env_value_unsafe "$_val"; then
      echo "[ERROR] $_v contains a character that is unsafe in .env: spaces or  \` \$ \" ' \\ ; & | < > ( ) ~" >&2
      echo "[ERROR] The Makefile sources .env, so these would execute rather than parse." >&2
      exit 1
    fi
  done

  # TAILSCALE_ENABLED is the caller-facing switch and keeps its historical
  # name. TAILSCALE_MODE is accepted as a forward-looking alias so callers can
  # already say what they mean (none|host|sidecar); only "none" differs from
  # enabled=true today, the host/sidecar split lands with the ownership work.
  case "${TAILSCALE_MODE:-}" in
    none)           TAILSCALE_ENABLED=false ;;
    host|sidecar)   TAILSCALE_ENABLED=true ;;
    "")             case "$_env_ts_enabled" in
                      [Tt]rue|[Yy]es|1) TAILSCALE_ENABLED=true ;;
                      *)                TAILSCALE_ENABLED=false ;;
                    esac ;;
    *) echo "[ERROR] TAILSCALE_MODE must be none, host or sidecar (got: $TAILSCALE_MODE)" >&2; exit 1 ;;
  esac

  if [ "$TAILSCALE_ENABLED" = true ]; then
    TAILSCALE_AUTH_KEY="$_env_ts_key"
    TAILSCALE_HOSTNAME="${_env_ts_host:-hummingbot-api}"
    # An auth key registers a NEW node, so only the sidecar needs one. In host
    # mode we serve on a node this machine already has, and demanding a
    # credential for that would fail installs that are entirely correct --
    # which is exactly the co-located Condor case.
    if [ "${TAILSCALE_MODE:-}" != "host" ]; then
      if [ -z "$TAILSCALE_AUTH_KEY" ]; then
        echo "[ERROR] Tailscale requested but TAILSCALE_AUTH_KEY is empty" >&2
        echo "        (not needed when TAILSCALE_MODE=host — this host would reuse its own node)" >&2
        exit 1
      fi
      if [[ ! "$TAILSCALE_AUTH_KEY" =~ ^tskey-auth- ]]; then
        echo "[ERROR] TAILSCALE_AUTH_KEY must start with 'tskey-auth-'" >&2
        exit 1
      fi
    fi
  fi
  echo "[INFO] Credentials taken from the environment; broker passwords generated locally."
else

echo "Hummingbot API Setup"
echo ""
echo "Set API credentials (use a strong username, password, and config password):"
echo ""

USERNAME="$(prompt_required_tty "API username: ")"
PASSWORD="$(prompt_required_secret_tty "API password: ")"
CONFIG_PASSWORD="$(prompt_required_secret_tty "Config password: ")"

# --------------------------
# Tailscale Configuration
# --------------------------

if prompt_yes_no "Use Tailscale for secure private networking? [y/N]: " "n"; then
  TAILSCALE_ENABLED=true
  # Ask what the answer depends on before asking for an auth key. A machine
  # that already runs tailscaled needs no key -- it is on the tailnet, and
  # joining a second time from the same host would only contend for the same
  # device. Prompting anyway would demand a credential to do something we are
  # about to decide not to do.
  # shellcheck source=tailnet-state.sh
  . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/tailnet-state.sh"
  if [ "$(tailnet_state)" != none ]; then
    TAILSCALE_MODE=host
    echo ""
    echo "[OK] This machine is already on a tailnet — no auth key needed."
    echo "     Port 8000 will be served on the node it already has."
    echo "     Want the API to have its own tailnet identity instead?"
    echo "     Set TAILSCALE_MODE=sidecar and TAILSCALE_AUTH_KEY in .env, then 'make deploy'."
  else
    TAILSCALE_MODE=sidecar
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  How to get a Tailscale auth key:"
    echo "    1. Create a free account at https://tailscale.com"
    echo "    2. Go to: https://tailscale.com/admin/settings/keys"
    echo "    3. Click 'Generate auth key'"
    echo "    4. Check 'Reusable' for multiple server deployments"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    while true; do
      TAILSCALE_AUTH_KEY="$(prompt_tty "Tailscale auth key (tskey-auth-...): " "")"
      if [[ -z "$TAILSCALE_AUTH_KEY" ]]; then
        echo "[WARN] Auth key cannot be empty"
        continue
      fi
      if [[ ! "$TAILSCALE_AUTH_KEY" =~ ^tskey-auth- ]]; then
        echo "[WARN] Auth key must start with 'tskey-auth-'"
        continue
      fi
      break
    done
    # Hostname defaults to "hummingbot-api" — override via TAILSCALE_HOSTNAME in .env if needed
  fi
fi

fi  # end interactive / non-interactive split

# --------------------------
# Tailnet ownership
# --------------------------
# Recorded in .env as a preference; `make deploy` re-resolves it against the
# live host on every run, so this is a starting point, not a guarantee.
_setup_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=tailnet-state.sh
. "$_setup_here/tailnet-state.sh"

if [ "$TAILSCALE_ENABLED" != true ]; then
  TAILSCALE_MODE=none
elif [ -n "${TAILSCALE_MODE:-}" ]; then
  : # caller was explicit (validated earlier); respect it
else
  case "$(tailnet_state)" in
    native)
      TAILSCALE_MODE=host
      echo ""
      echo "[INFO] This machine is already on a tailnet."
      echo "[INFO] Port 8000 will be served on the node it already has, rather than"
      echo "[INFO] joining a second time — two daemons cannot share one tailnet device."
      echo "[INFO] Want a separate identity anyway? Set TAILSCALE_MODE=sidecar in .env."
      ;;
    sidecar)
      TAILSCALE_MODE=sidecar
      echo "[INFO] Reusing the existing hummingbot-tailscale sidecar."
      ;;
    *)
      TAILSCALE_MODE=sidecar
      ;;
  esac
fi

# Broker credentials are never typed by the user — the API and the bots are the only clients —
# so generate strong ones instead of shipping well-known defaults. Rotating BROKER_PASSWORD
# later requires `make emqx-auth-reset`, since EMQX only imports the bootstrap file for users
# it does not have.
#
# Two separate passwords, deliberately: BROKER_PASSWORD is the MQTT client credential handed
# to every bot instance's conf_client.yml, while BROKER_DASHBOARD_PASSWORD only unlocks the
# EMQX web dashboard (full broker admin — rules, connectors, the lot). Sharing one password
# between them would mean a single leaked bot config grants dashboard admin, not just the
# scoped MQTT access emqx/acl.conf intends.
#
# EMQX rejects single-character-class passwords for the dashboard, so compose the value from
# letters and digits explicitly rather than trusting a random alphanumeric draw to contain both
# — applied to both passwords here for one code path instead of two.
# `head -c N` exits as soon as it has its N bytes, so `tr` gets SIGPIPE on its next write —
# under `set -euo pipefail` that would otherwise abort setup before .env is written.
gen_password() {
    printf '%s%s' \
        "$(LC_ALL=C tr -dc 'A-Za-z' < /dev/urandom | head -c 24 || true)" \
        "$(LC_ALL=C tr -dc '0-9' < /dev/urandom | head -c 8 || true)"
}
BROKER_PASSWORD="$(gen_password)"
BROKER_DASHBOARD_PASSWORD="$(gen_password)"

cat > .env << EOF
# Hummingbot API Configuration
USERNAME=$USERNAME
PASSWORD=$PASSWORD
CONFIG_PASSWORD=$CONFIG_PASSWORD
DEBUG_MODE=false

# MQTT Broker
BROKER_HOST=localhost
BROKER_PORT=1883
BROKER_USERNAME=admin
BROKER_PASSWORD=$BROKER_PASSWORD
BROKER_DASHBOARD_PASSWORD=$BROKER_DASHBOARD_PASSWORD

# Database (auto-configured by docker-compose)
DATABASE_URL=postgresql+asyncpg://hbot:hummingbot-api@localhost:5432/hummingbot_api

# Published-port bind addresses. Both default to 127.0.0.1 in docker-compose.yml.
# Widen API_BIND only if something off-box must reach the API, and prefer a specific
# interface over 0.0.0.0 — e.g. API_BIND=<your-tailscale-ip> with the Tailscale overlay.
# API_BIND=127.0.0.1
# DB_BIND=127.0.0.1

# Gateway (optional). Reuses CONFIG_PASSWORD rather than a hardcoded default:
# this passphrase unlocks Gateway's DEX wallet keys, and "admin" is the first
# thing anyone who reaches the port will try.
GATEWAY_URL=http://localhost:15888
GATEWAY_PASSPHRASE=$CONFIG_PASSWORD

# Aomi Pipeline (optional; needed by the onchain_executor). AOMI_TOKEN is a bearer with
# pipeline:execute (plus custody:delegate to commit); leave it empty to keep the executor
# disabled, or point AOMI_TOKEN_FILE at a file holding the token instead.
AOMI_URL=https://chat.aomi.dev
AOMI_TOKEN=
# Optional operator-owned JSON policy; grants are enforced before durable execution admission.
AOMI_LENDING_POLICY_FILE=

# Paths
BOTS_PATH=$(pwd)

# Tailscale
# TAILSCALE_ENABLED is the switch and keeps its historical meaning.
# TAILSCALE_MODE says HOW: host = share this machine's existing tailnet node,
# sidecar = give the API a node of its own. Deploy re-resolves this against
# the live host every time, so an .env that was right in June cannot put a
# second kernel-mode daemon on a box that grew one in July.
#
# NOTE: this heredoc is unquoted so plain variables expand. Command
# substitution in these comments would EXECUTE while .env is written -- a
# backtick here once ran a make target mid-setup and seeded the broker with
# the wrong password. Keep substitution out of the comments.
TAILSCALE_ENABLED=$TAILSCALE_ENABLED
TAILSCALE_MODE=$TAILSCALE_MODE
TAILSCALE_AUTH_KEY=$TAILSCALE_AUTH_KEY
TAILSCALE_HOSTNAME=$TAILSCALE_HOSTNAME

# ---------------------------------------------------------------------------
# Optional settings. Every line below is commented out and shows the default
# that config.py already applies -- config.py stays the source of truth, so
# uncomment a line only to override it.
# ---------------------------------------------------------------------------

# Performance snapshots
# How often a live executor's performance is snapshotted, in seconds.
#PERFORMANCE_EXECUTOR_SNAPSHOT_INTERVAL=60
# Delete performance snapshots (executor AND controller) older than this many
# days. 0 keeps everything forever -- raise it to cap database growth.
#PERFORMANCE_RETENTION_DAYS=0

# Backtesting
# How many backtests may run at once; further submissions queue. Runs are
# isolated in separate processes, so this can be raised up to the cores you are
# willing to give them.
#BACKTESTING_MAX_CONCURRENT=1
# Wall-clock budget for one backtest; the worker is killed and the task fails past it.
#BACKTESTING_TIMEOUT_SECONDS=1800.0
# How many finished backtests to retain before the oldest are reaped.
#BACKTESTING_MAX_RESULTS=100
# Directory holding archived backtest payloads (inside the bots volume, so it
# survives redeploys).
#BACKTESTING_RESULTS_PATH=bots/data/backtests
# Directory holding downloaded candle history shared by backtest workers, so a
# sweep of configs over one market downloads that market once.
#BACKTESTING_CANDLES_CACHE_PATH=bots/data/backtests/candles
# How many downloaded candle ranges to keep; the least recently used are dropped
# past it. 0 disables the cache and makes every run download its own history.
#BACKTESTING_CANDLES_CACHE_ENTRIES=32
# How long a downloaded candle range may be reused. A window ending near now is
# fetched with its last candle still forming, so an entry is refetched once it is
# older than this.
#BACKTESTING_CANDLES_CACHE_TTL_SECONDS=3600.0

# Market data feeds
# How often to run feed cleanup, in seconds.
#MARKET_DATA_CLEANUP_INTERVAL=300
# How long to keep unused feeds alive, in seconds.
#MARKET_DATA_FEED_TIMEOUT=600
# How long to wait for a candle feed to become ready, in seconds.
#MARKET_DATA_CANDLES_READY_TIMEOUT=30
# WebSocket heartbeat interval, in seconds.
#MARKET_DATA_WS_HEARTBEAT_INTERVAL=30
# Allowed range for a market-data WebSocket subscription update interval, in seconds.
#MARKET_DATA_WS_MIN_UPDATE_INTERVAL=0.25
#MARKET_DATA_WS_MAX_UPDATE_INTERVAL=60.0
# Allowed range, and the applied default, for a /ws/executors subscription
# update interval, in seconds. The floor is stricter than the market-data one
# because executor push loops hit the database instead of reading in-memory
# candles and order books.
#MARKET_DATA_WS_EXECUTOR_MIN_UPDATE_INTERVAL=0.5
#MARKET_DATA_WS_EXECUTOR_MAX_UPDATE_INTERVAL=60.0
#MARKET_DATA_WS_EXECUTOR_DEFAULT_UPDATE_INTERVAL=2.0
# How often to refresh tickers from connected exchanges, in seconds.
#MARKET_DATA_TICKER_UPDATE_INTERVAL=30
# Max age of cached tickers before an on-demand request refetches them, in seconds.
#MARKET_DATA_TICKER_MAX_AGE=60
# How long a ticker-only connector stays in the background refresh cycle after
# its last request, in seconds.
#MARKET_DATA_TICKER_SUBSCRIPTION_TTL=600
# How often to refresh Hyperliquid HIP-3 (builder-deployed perp dex) tickers, in
# seconds. Set to 0 to exclude HIP-3 markets.
#MARKET_DATA_HYPERLIQUID_HIP3_INTERVAL=120

# CORS (SEC-019). A wildcard origin must never be combined with credentials, so
# trusted origins are restricted to localhost by default.
# Explicit list of trusted origins, as JSON, e.g. ["https://dashboard.example.com"]
#CORS_ALLOW_ORIGINS=[]
# Regex matching trusted origins; an empty string disables regex matching.
#CORS_ALLOW_ORIGIN_REGEX=https?://(localhost|127\.0\.0\.1)(:\d+)?
# Allow credentialed (cookies/auth) cross-origin requests.
#CORS_ALLOW_CREDENTIALS=true
# HTTP methods and headers allowed for cross-origin requests.
#CORS_ALLOW_METHODS=["*"]
#CORS_ALLOW_HEADERS=["*"]

# AWS (only needed for S3 archiving)
#AWS_API_KEY=
#AWS_SECRET_KEY=
#AWS_S3_DEFAULT_BUCKET_NAME=
EOF

# Appended rather than templated: these are the caller's tuning, not this
# repo's defaults, and an install that did not ask for them should not carry
# them at all.
if [ -n "$_env_bt_concurrent" ] || [ -n "$_env_bt_timeout" ]; then
  {
    echo ""
    echo "# Backtesting (set by the installer that drove this setup)"
    [ -n "$_env_bt_concurrent" ] && echo "BACKTESTING_MAX_CONCURRENT=$_env_bt_concurrent"
    [ -n "$_env_bt_timeout" ] && echo "BACKTESTING_TIMEOUT_SECONDS=$_env_bt_timeout"
  } >> .env
fi

# Holds the API password, the config/Gateway passphrase and (when set) a
# Tailscale auth key. The usual 644 umask makes all of that readable by every
# other account on the box.
chmod 600 .env 2>/dev/null || true

touch .setup-complete

echo ""
echo ".env created successfully!"
echo ""
echo "Next steps:"
echo ""
echo "Option 1: Start all services with Docker (recommended)"
echo "  make deploy"
echo ""
echo "Option 2: Run API locally (dev mode)"
echo "  make install   # Creates the conda environment - Note: Please install the latest Anaconda version manually"
echo "  make run       # Run API (installs and connects Tailscale automatically if TAILSCALE_ENABLED=true)"
echo ""
echo "Then check it:"
echo "  make doctor    # Verifies dependencies, .env, containers, port exposure and API access"
if [ "$TAILSCALE_ENABLED" = true ]; then
  echo ""
  echo "Tailscale:"
  # Say what THIS mode will do. In host mode no sidecar starts and no node by
  # that name ever registers, so the sidecar summary described a deployment
  # that was not about to happen.
  if [ "$TAILSCALE_MODE" = host ]; then
    echo "  Mode:           host — port 8000 is served on the tailnet node this machine already has"
    _ts_name="$(tailnet_node_name 2>/dev/null || true)"
    if [ -n "$_ts_name" ]; then
      echo "  Condor URL:     http://$_ts_name:8000"
    else
      echo "  Condor URL:     run 'make doctor' after 'make deploy' — it prints this node's name"
    fi
  else
    echo "  Mode:           sidecar — the API gets a tailnet node of its own"
    echo "  Docker deploy:  Tailscale sidecar starts automatically with 'make deploy'"
    echo "  Source run:     Tailscale installs and connects automatically with 'make run'"
    # Deliberately NOT printed as a settled URL. The node does not exist yet,
    # and Tailscale suffixes a hostname that is already taken -- ask for
    # "hummingbot-api" on a tailnet that has one and you get
    # "hummingbot-api-1". Printing the requested name as the URL is how an
    # install ends up pointed at somebody else's machine.
    echo "  Condor URL:     http://$TAILSCALE_HOSTNAME:8000 — unless that name is already taken on"
    echo "                  your tailnet, in which case this node registers as"
    echo "                  $TAILSCALE_HOSTNAME-1, -2, ... 'make doctor' prints the real one."
  fi
  echo "  Status:         make tailscale-status"
fi
echo ""
