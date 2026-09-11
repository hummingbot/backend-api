.PHONY: setup run deploy stop install uninstall build install-pre-commit tailscale-status doctor reset emqx-auth emqx-auth-reset emqx-audit

SETUP_SENTINEL := .setup-complete

setup: $(SETUP_SENTINEL)

$(SETUP_SENTINEL):
	chmod +x setup.sh
	./setup.sh

# Run locally (dev mode)
# When TAILSCALE_ENABLED=true: installs Tailscale if needed, connects, configures tailscale serve,
# then binds uvicorn to 127.0.0.1 only (tailscale serve exposes port 8000 on the tailnet)
run: emqx-auth
	docker compose up emqx postgres -d
	@set -a; [ -f .env ] && . ./.env; set +a; \
	if [ "$${TAILSCALE_ENABLED:-false}" = "true" ]; then \
		echo "[INFO] Tailscale mode: setting up Tailscale for source install..."; \
		if ! command -v tailscale >/dev/null 2>&1; then \
			echo "[INFO] Installing Tailscale..."; \
			curl -fsSL https://tailscale.com/install.sh | sh; \
		fi; \
		if ! tailscale status >/dev/null 2>&1; then \
			echo "[INFO] Connecting to Tailscale network..."; \
			sudo tailscale up --authkey="$${TAILSCALE_AUTH_KEY}" --hostname="$${TAILSCALE_HOSTNAME:-hummingbot-api}" --accept-dns=true; \
		fi; \
		tailscale serve status 2>/dev/null | grep -q ":8000" || \
			sudo tailscale serve --bg --tcp=8000 tcp://127.0.0.1:8000; \
		echo "[INFO] Binding uvicorn to 127.0.0.1 (tailscale serve exposes port 8000 on tailnet)"; \
		conda run --no-capture-output -n hummingbot-api uvicorn main:app --reload --host 127.0.0.1 --port 8000; \
	else \
		conda run --no-capture-output -n hummingbot-api uvicorn main:app --reload; \
	fi

# Deploy with Docker
#
# Tailnet ownership is resolved HERE, not only at setup time. A setup-time
# decision is bypassed by every normal day-2 command: `make reset` deletes
# .env while a sidecar may still be running, and this target is routinely run
# on its own against an .env written weeks earlier. Whatever setup.sh recorded
# is a preference; what the host actually looks like right now is the fact.
#
#   none     plain compose, no overlay
#   host     this machine is already on the tailnet -- expose 8000 via
#            `tailscale serve` on the existing node instead of joining twice
#   sidecar  the API gets its own tailnet node, in userspace mode when a
#            native daemon is already present
deploy: $(SETUP_SENTINEL) emqx-auth
	@set -a; [ -f .env ] && . ./.env; set +a; \
	. ./tailnet-state.sh; \
	mode="$$(tailnet_mode)" || exit 1; \
	case "$$mode" in \
	  none) \
	    docker compose up -d ;; \
	  host) \
	    echo "[INFO] Tailscale: reusing this host's existing node (mode=host)."; \
	    docker compose up -d; \
	    if command -v tailscale >/dev/null 2>&1; then \
	      tailscale serve status 2>/dev/null | grep -q ":8000" || \
	        sudo tailscale serve --bg --tcp=8000 tcp://127.0.0.1:8000 || \
	        echo "[WARN] Could not configure serve for :8000 — run it yourself: sudo tailscale serve --bg --tcp=8000 tcp://127.0.0.1:8000"; \
	    else \
	      echo "[WARN] mode=host but no tailscale CLI on PATH — port 8000 is loopback-only."; \
	    fi ;; \
	  sidecar) \
	    if tailnet_needs_userspace; then \
	      echo "[INFO] A tailscaled already owns this host's tailnet device."; \
	      echo "[INFO] Starting the sidecar in userspace mode so both can coexist."; \
	      export TS_USERSPACE=true; \
	    fi; \
	    docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d ;; \
	esac

# Verify dependencies, .env, containers, port exposure and API access.
# Read-only; exits non-zero when a check actually fails.
doctor:
	@chmod +x doctor.sh && ./doctor.sh

EMQX_AUTH_FILE := .emqx/auth-bootstrap.csv

# Generate the EMQX built-in-database bootstrap file from the broker credentials in .env.
# EMQX ships with anonymous MQTT enabled; this seeds the one account the API and the bots
# use so the broker can reject everything else. The file holds a plaintext password, so it
# is gitignored.
#
# The secret is kept off other host users by the MODE OF THE DIRECTORY (0700), not of the
# file (0644). The file is bind-mounted into the broker, which runs as its own `emqx` user
# (uid 1000): on Linux a bind mount carries the host uid through unmapped, so a 0600 file
# owned by the deploying user is unreadable inside the container. EMQX then logs a
# `Permission denied` for the bootstrap file, skips the import, comes up healthy with NO
# accounts, and rejects the API's correct credentials as `Not authorized` (issue #224).
# Directory mode is enough because Docker resolves the bind-mount path as root once, at
# mount time — the container never traverses .emqx/ to reach the file.
#
# is_superuser is deliberately false: EMQX superusers bypass authorization entirely, which
# would make emqx/acl.conf dead config.
#
# NOTE: EMQX imports the bootstrap file only for users that do not already exist. Changing
# BROKER_PASSWORD in .env therefore has no effect on a broker whose emqx-data volume already
# has the account — run `make emqx-auth-reset` to drop the volume and re-seed.
emqx-auth:
	@set -a; [ -f .env ] && . ./.env; set +a; \
	user="$${BROKER_USERNAME:-admin}"; pass="$${BROKER_PASSWORD:-password}"; \
	bad=0; \
	case "$$user$$pass" in *,*) bad=1 ;; esac; \
	[ "$$(printf '%s' "$$user$$pass" | wc -l)" -eq 0 ] || bad=1; \
	if [ "$$bad" -eq 1 ]; then \
		echo "[ERROR] BROKER_USERNAME/BROKER_PASSWORD cannot contain a comma or newline" \
			"— it would corrupt the CSV bootstrap file's field structure." >&2; \
		exit 1; \
	fi; \
	mkdir -p $(dir $(EMQX_AUTH_FILE)); \
	chmod 700 $(dir $(EMQX_AUTH_FILE)); \
	printf 'user_id,password,is_superuser\n%s,%s,false\n' "$$user" "$$pass" > $(EMQX_AUTH_FILE); \
	chmod 644 $(EMQX_AUTH_FILE); \
	echo "[INFO] Wrote $(EMQX_AUTH_FILE) for broker user $$user"

# Broker container to audit. Override to check another deployment:
#   make emqx-audit EMQX_CONTAINER=<name>
EMQX_CONTAINER ?= hummingbot-broker

# Audit the broker's security posture and check for persistence.
#
# EMQX's rule engine can issue authenticated HTTP requests to internal services, so a rule
# or connector nobody added is a backdoor, not a curiosity — and it survives restarts in
# cluster.hocon. Installing one requires a dashboard session, which is why the well-known
# admin/public default mattered. This prints everything needed to answer "is anything here
# that we did not put here", in one command.
emqx-audit:
	@echo "── listeners ────────────────────────────────────────────────"
	@docker exec $(EMQX_CONTAINER) /opt/emqx/bin/emqx ctl listeners 2>/dev/null \
		| grep -E "^[a-z]|listen_on|current_conn" || echo "  broker not running"
	@echo "── published ports (host side) ──────────────────────────────"
	@docker port $(EMQX_CONTAINER) 2>/dev/null || true
	@echo "── authentication (empty list == anonymous allowed) ─────────"
	@docker exec $(EMQX_CONTAINER) /opt/emqx/bin/emqx ctl conf show authentication 2>/dev/null || true
	@echo "── authorization (want no_match = deny) ─────────────────────"
	@docker exec $(EMQX_CONTAINER) /opt/emqx/bin/emqx ctl conf show authorization 2>/dev/null || true
	@echo "── ACL rules in force ───────────────────────────────────────"
	@docker exec $(EMQX_CONTAINER) sh -c 'grep -E "^\{" /opt/emqx/etc/acl.conf' 2>/dev/null || true
	@echo "── PERSISTENCE: rules / actions / connectors / bridges ──────"
	@echo "   A rule here can make the broker issue authenticated HTTP requests"
	@echo "   into internal services. Anything you did not add is a backdoor."
	@docker exec $(EMQX_CONTAINER) sh -c \
		"awk '/^(actions|connectors|bridges|rule_engine) \\{/{p=1} p{print} /^\\}/{if(p){p=0;print \"\"}}' \
		 /opt/emqx/data/configs/cluster.hocon 2>/dev/null \
		 | grep -vE 'created_at|last_modified|metadata'" \
		2>/dev/null | sed 's/^/   /' || true
	@docker exec $(EMQX_CONTAINER) sh -c \
		"grep -qE '^(actions|connectors|bridges) \\{|rules \\{' /opt/emqx/data/configs/cluster.hocon 2>/dev/null" \
		&& echo "   ^^ REVIEW THE ABOVE — stock deployments have none of these." \
		|| echo "   none — no rules, actions, connectors or bridges configured."

# Compose derives the project name from COMPOSE_PROJECT_NAME if set, else the lowercased
# directory name -- $(notdir $(CURDIR)) alone gets this wrong for any directory with an
# uppercase letter, silently making the volume filter below match nothing.
COMPOSE_PROJECT := $(shell echo "$${COMPOSE_PROJECT_NAME:-$(notdir $(CURDIR))}" | tr '[:upper:]' '[:lower:]')

# Rotate the broker credentials: wipe the EMQX state volume so the bootstrap file is
# re-imported with the current .env values, and the dashboard's default password (a
# separate credential, BROKER_DASHBOARD_PASSWORD) is re-applied too -- both live in the
# same mnesia data dir. Retained messages and broker state are lost; bots and the API
# reconnect on their own. The volume is matched by compose labels rather than by name,
# since other compose projects on the same host also have emqx volumes.
emqx-auth-reset: emqx-auth
	docker compose rm -sf emqx
	@docker volume ls -q \
		--filter "label=com.docker.compose.project=$(COMPOSE_PROJECT)" \
		--filter "label=com.docker.compose.volume=emqx-data" \
		| xargs -r docker volume rm
	docker compose up -d emqx

TAILSCALE_CONTAINER := hummingbot-tailscale

# Show Tailscale connection status (Docker sidecar or local install)
tailscale-status:
	@if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx '$(TAILSCALE_CONTAINER)'; then \
		echo "[INFO] Tailscale sidecar (Docker)"; \
		docker exec $(TAILSCALE_CONTAINER) tailscale status; \
		echo ""; \
		echo "[INFO] Serve status (confirms port 8000 is proxied, not just tailnet-joined):"; \
		docker exec $(TAILSCALE_CONTAINER) tailscale serve status; \
	elif command -v tailscale >/dev/null 2>&1; then \
		echo "[INFO] Tailscale (local)"; \
		tailscale status; \
		echo ""; \
		echo "[INFO] Serve status:"; \
		tailscale serve status; \
	else \
		echo "Tailscale is not available."; \
		echo "  Docker deploy: ensure TAILSCALE_ENABLED=true and run 'make deploy'"; \
		echo "  Source run:    use 'make run' with Tailscale enabled (installs locally)"; \
		exit 1; \
	fi

# Stop all services
stop:
	docker compose down

# Install conda environment
install:
	@if ! command -v conda >/dev/null 2>&1; then \
		echo "Error: Conda is not found in PATH. Please install Conda or add it to your PATH."; \
		exit 1; \
	fi
	@if conda env list | grep -q '^hummingbot-api '; then \
		echo "Environment already exists."; \
	else \
		conda env create -f environment.yml; \
	fi
	$(MAKE) install-pre-commit
	$(MAKE) setup

uninstall:
	conda env remove -n hummingbot-api -y
	rm -f $(SETUP_SENTINEL)

install-pre-commit:
	conda run -n hummingbot-api pip install pre-commit
	conda run -n hummingbot-api pre-commit install

# Build Docker image
build:
	docker build -t hummingbot/hummingbot-api:latest .

# Reset to near-origin state:
#   - stops Docker containers (with volume wipe) and/or source uvicorn if running
#   - removes .env and .setup-complete from the project root
#   - removes all credential folders under bots/credentials/ except master_account
#   - removes all .yml files under bots/credentials/master_account/
reset:
	@echo "[INFO] Checking for running hummingbot-api services..."
	@# Both compose files, always. The Tailscale sidecar and its state volume are
	@# declared ONLY in the overlay, so a base-file-only `down -v` leaves the
	@# container running as an orphan -- still holding the host's tailscale0
	@# device -- while .env is deleted, so the next `make setup` has no record
	@# that it exists. --remove-orphans covers a sidecar left by an older layout.
	@# Unconditional when Docker is available, rather than gated on finding a
	@# named container. `make run` (source) starts only emqx and postgres, so a
	@# name check for hummingbot-api/the sidecar skipped the teardown entirely
	@# while reset still deleted .env and the credential state -- leaving the
	@# broker's volume, and the accounts seeded from the OLD BROKER_PASSWORD,
	@# to be reused by a setup that generates a new one. That is the same
	@# .env/broker desync `make emqx-auth-reset` exists to repair.
	@# `down` on a project with nothing running is a no-op, so there is no
	@# case worth guarding against.
	@# Reset is all-or-nothing. If the volumes cannot be dropped, deleting .env
	@# and the credential state anyway is worse than not resetting at all: the
	@# next setup generates a new BROKER_PASSWORD while the retained EMQX volume
	@# still holds the account seeded from the old one, so the broker comes up
	@# healthy and rejects the API's correct credentials. Recovering needs
	@# `make emqx-auth-reset` -- which also needs the Docker that was missing.
	@if ! docker info >/dev/null 2>&1; then \
		if [ "$(ALLOW_PARTIAL_RESET)" = "1" ]; then \
			echo "[WARN] Docker unavailable; removing local files only (ALLOW_PARTIAL_RESET=1)."; \
			echo "[WARN] Container volumes are UNTOUCHED. Run 'make emqx-auth-reset' once Docker is back,"; \
			echo "[WARN] or the broker will keep rejecting the credentials the next setup generates."; \
		else \
			echo "[ERROR] Docker is not available, so container volumes cannot be wiped." >&2; \
			echo "[ERROR] Refusing to reset: removing .env while the broker keeps its old" >&2; \
			echo "[ERROR] accounts leaves an install that authenticates against nothing." >&2; \
			echo "[ERROR] Start Docker (or fix its permissions) and re-run 'make reset'." >&2; \
			echo "[ERROR] To remove local files anyway: make reset ALLOW_PARTIAL_RESET=1" >&2; \
			exit 1; \
		fi; \
	else \
		echo "[INFO] Stopping containers and wiping volumes..."; \
		docker compose -f docker-compose.yml -f docker-compose.tailscale.yml down -v --remove-orphans; \
	fi
	@# Verify the teardown actually took, rather than trusting that it ran.
	@# Compose derives the project name from COMPOSE_PROJECT_NAME, else the
	@# directory name -- so a stack deployed under a different name, or before
	@# this directory was renamed, is untouched by the `down` above, which then
	@# succeeds as a no-op. Deleting .env after that leaves the broker holding
	@# accounts seeded from a password the next setup will never generate, and
	@# it surfaces as bots that cannot authenticate against a healthy broker.
	@#
	@# Any surviving emqx-data volume is treated as ours to be safe about:
	@# aborting costs one environment variable, proceeding costs a silent
	@# authentication failure that needs `make emqx-auth-reset` to unpick.
	@# The query's own failure must not read as "no leftovers": that is the
	@# permissive answer, and it would delete .env on a teardown nobody could
	@# verify. An unanswerable check is treated exactly like a positive one.
	@if leftover="$$(docker volume ls -q --filter 'label=com.docker.compose.volume=emqx-data' 2>/dev/null)"; then \
		:; \
	else \
		leftover='<could not query docker volumes>'; \
	fi; \
	if [ -n "$$leftover" ] && [ "$(ALLOW_PARTIAL_RESET)" != "1" ]; then \
		echo "[ERROR] A broker data volume survived the teardown:" >&2; \
		for v in $$leftover; do echo "           $$v" >&2; done; \
		echo "[ERROR] It belongs to a compose project under a different name — this stack was" >&2; \
		echo "[ERROR] deployed with another COMPOSE_PROJECT_NAME, or this directory has been" >&2; \
		echo "[ERROR] renamed since. Removing .env now would leave that broker holding accounts" >&2; \
		echo "[ERROR] the next setup cannot reproduce." >&2; \
		echo "[ERROR] Remove it first:  docker volume rm <name above>" >&2; \
		echo "[ERROR] Or reset local files anyway:  make reset ALLOW_PARTIAL_RESET=1" >&2; \
		exit 1; \
	fi
	@if pgrep -f "uvicorn main[:]app" >/dev/null 2>&1; then \
		echo "[INFO] Source uvicorn process found — stopping..."; \
		pkill -f "uvicorn main[:]app" || true; \
	fi
	@echo "[INFO] Removing .env and .setup-complete..."
	rm -f .env $(SETUP_SENTINEL)
	@echo "[INFO] Clearing credentials..."
	@find bots/credentials -mindepth 1 -maxdepth 1 -type d ! -name master_account -exec rm -rf {} +
	@find bots/credentials/master_account -name "*.yml" -delete
	@rm -f bots/credentials/master_account/.password_verification
	@echo "[INFO] Reset complete."
