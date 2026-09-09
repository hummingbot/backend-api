# Hummingbot API

A REST API for managing Hummingbot trading bots across multiple exchanges, with AI assistant integration via MCP.

> **Why we recommend Tailscale for production**
>
> Hummingbot API controls real trading: orders, balances, bots, and stored exchange keys. That has always required strong passwords and careful configuration—but **the risk surface has grown**. Tools like **MCP**, **Condor agents**, and other AI assistants make powerful API actions easier to trigger, while cloud VPSes are constantly scanned for open ports like **8000**.
>
> **Tailscale is one safeguard you can add**: it puts the API on a private encrypted network so only your devices can reach it, without publishing port 8000 to the internet. It does **not** replace proper security—use strong API and config passwords, keep exchange keys protected, and avoid exposing sensitive services publicly. Tailscale also works when the API and clients run on the **same machine**. But if your only client is co-located too (e.g. Condor deploying this API for itself), it already reaches you over `localhost` regardless — this API's own tailnet node is only needed for *other* devices to reach it directly.

## Quick Start

**Recommended (Docker):** install from an empty directory with the [Hummingbot Deploy](https://github.com/hummingbot/deploy) helper script. **Docker** must be installed and running first.

### Before you install (production)

For VPS or remote deployments, prepare Tailscale first:

1. Create a free account at [tailscale.com](https://tailscale.com)
2. Generate a **reusable** auth key at [Settings → Keys](https://login.tailscale.com/admin/settings/keys) (starts with `tskey-auth-`)
3. Enable **[MagicDNS](https://login.tailscale.com/admin/dns)** in the Tailscale admin console

Full walkthrough: [hummingbot.org Tailscale guide](https://hummingbot.org/hummingbot-api/tailscale/) · [Securing Condor and Hummingbot API with Tailscale](https://hummingbot.org/blog/posts/securing-condor-and-hummingbot-api-with-tailscale/)

### Install

```bash
curl -fsSL https://raw.githubusercontent.com/hummingbot/deploy/main/setup.sh | bash -s -- --hummingbot-api
```

The script clones **`hummingbot-api`**, runs **`make setup`** (creates **`.env`**), pulls Compose images, and runs **`make deploy`**, which starts the **API**, **PostgreSQL**, and **EMQX** containers.

The setup script prompts for:

- **Credentials** — API username/password (HTTP Basic Auth) and config password (encrypts bot credentials)
- **Tailscale** — answer **`y`** when asked *Use Tailscale for secure private networking?* and paste your auth key (default hostname: **`hummingbot-api`**)

If the script finishes but services did not start, run:

```bash
cd hummingbot-api
make setup
make deploy
```

### Access the API

| Where you connect from | URL |
|------------------------|-----|
| Same machine as the API | `http://localhost:8000` |
| Another device on your tailnet (Condor, MCP, browser) | `http://hummingbot-api:8000` |

Use your API username and password for all requests. **Do not open port 8000 on your public firewall** when Tailscale is enabled.

| Command | Description |
|---------|-------------|
| `make setup` | Create `.env` file with configuration |
| `make deploy` | Start all services (API, PostgreSQL, EMQX) |
| `make stop` | Stop all services |
| `make run` | Run API locally in dev mode |
| `make install` | Install conda environment for development |
| `make build` | Build Docker image |
| `make tailscale-status` | Show Tailscale connection + serve (proxy) status |
| `make doctor` | Verify dependencies, `.env`, containers, port exposure and API access |

## Services

After hummingbot-api is running, these services are available:

| Service | Local URL | Tailnet URL (when Tailscale enabled) | Description |
|---------|-----------|--------------------------------------|-------------|
| **API** | http://localhost:8000 | http://hummingbot-api:8000 | REST API |
| **Swagger UI** | http://localhost:8000/docs | http://hummingbot-api:8000/docs | Interactive API documentation |
| **PostgreSQL** | localhost:5432 | — | Database |
| **EMQX** | 127.0.0.1:1883 | — | MQTT broker (auth required, loopback-only) |
| **EMQX Dashboard** | http://127.0.0.1:18083 | — | Broker admin (`admin` / `BROKER_DASHBOARD_PASSWORD`) |

> **Broker access.** The broker requires a username and password and is published on the loopback
> interface only — bot containers run with `network_mode: host` and reach it at `127.0.0.1:1883`,
> while the API reaches it in-network as `emqx:1883`. Credentials come from `BROKER_USERNAME` /
> `BROKER_PASSWORD` in `.env`; `make deploy` seeds them into the broker via `make emqx-auth`.
> To change them afterwards, edit `.env` and run `make emqx-auth-reset` — EMQX only imports the
> bootstrap file for accounts it does not already have.
>
> The dashboard login is a **separate** credential, `BROKER_DASHBOARD_PASSWORD` — deliberately
> not the same value as `BROKER_PASSWORD`, since that one is written into every bot instance's
> `conf_client.yml` and the dashboard grants full broker admin (rules, connectors), not just the
> scoped MQTT access `emqx/acl.conf` gives `BROKER_PASSWORD`. Like the MQTT bootstrap account,
> EMQX only applies it on first init, so changing it in `.env` also needs `make emqx-auth-reset`
> (it wipes the same `emqx-data` volume, rotating both passwords together).
>
> The broker also denies every topic outside `hbot/#` and `hummingbot-api/response/#`
> (`emqx/acl.conf`), so a leaked broker credential cannot be used to read the whole bus or to
> drive the rule engine. Run **`make emqx-audit`** to print the broker's listeners, auth,
> authorization and any rules, actions, connectors or bridges — a rule nobody added can make
> the broker issue authenticated HTTP requests into internal services, and it survives
> restarts. Use `make emqx-audit EMQX_CONTAINER=<name>` to check another deployment.

> **Ports.** The API (`8000`) and Postgres (`5432`) also bind to `127.0.0.1` by default. Set
> `API_BIND` in `.env` if something off-box must reach the API — prefer a specific interface
> over `0.0.0.0`; with the Tailscale overlay, `API_BIND=<tailscale-ip>` keeps MagicDNS working
> without publishing the API to the internet.

PostgreSQL and EMQX are bound to `127.0.0.1` only — they're never reachable
from another machine, on the tailnet or otherwise. Bot containers still reach
them fine (they run with `network_mode: host`, so `127.0.0.1` is their host's
loopback too).

## Connect AI Assistant (MCP)

> **Production:** use `http://hummingbot-api:8000` (MagicDNS) instead of `localhost` when MCP runs on a different device than the API. Both must be on the same Tailscale account.

### Claude Code (CLI)

```bash
claude mcp add --transport stdio hummingbot -- \
  docker run --rm -i \
  -e HUMMINGBOT_API_URL=http://hummingbot-api:8000 \
  -v hummingbot_mcp:/root/.hummingbot_mcp \
  hummingbot/hummingbot-mcp:latest
```

For local-only dev on the same machine, use `http://host.docker.internal:8000` instead.

Then use natural language:
- "Show my portfolio balances"
- "Set up my Binance account"
- "Create a market making strategy for ETH-USDT"

### Claude Desktop

Add to your config file:
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "hummingbot": {
      "command": "docker",
      "args": ["run", "--rm", "-i", "-e", "HUMMINGBOT_API_URL=http://hummingbot-api:8000", "-v", "hummingbot_mcp:/root/.hummingbot_mcp", "hummingbot/hummingbot-mcp:latest"]
    }
  }
}
```

Restart Claude Desktop after adding.

## Gateway (DEX Trading)

Gateway enables decentralized exchange trading. Start it via MCP:

> "Start Gateway"

Or via API at http://localhost:8000/docs using the Gateway endpoints (`POST /gateway/start` with an empty body).

Gateway always runs **secured**: it serves TLS + mutual-cert (mTLS) authentication and the required
certificates are auto-generated on first start under `bots/gateway-files/certs/` — no passphrase
prompt and no manual cert step. The single secret protecting the Gateway (TLS + wallet encryption)
is your `CONFIG_PASSWORD`. Once running, Gateway is available at https://localhost:15888.

> There is no development/insecure mode: a Gateway holding wallet keys must never be served over
> plain HTTP, so the API only ever runs it with mTLS.

## Configuration

The `.env` file contains all configuration. Key settings:

```bash
USERNAME=admin              # API username
PASSWORD=admin              # API password
CONFIG_PASSWORD=admin       # Encrypts bot credentials
DEBUG_MODE=false            # Verbose logging and reload
DATABASE_URL=...            # PostgreSQL connection
GATEWAY_URL=...             # Gateway URL (for DEX)

# Performance snapshots and backtests
PERFORMANCE_EXECUTOR_SNAPSHOT_INTERVAL=60   # Seconds between live executor snapshots
PERFORMANCE_RETENTION_DAYS=0                # Delete snapshots older than N days; 0 keeps them forever
BACKTESTING_MAX_CONCURRENT=1                # Backtests allowed to run at once (one core each)

# Tailscale (recommended for production)
TAILSCALE_ENABLED=true
TAILSCALE_AUTH_KEY=tskey-auth-...
TAILSCALE_HOSTNAME=hummingbot-api   # MagicDNS hostname on your tailnet

# Published-port bind addresses (both default to 127.0.0.1 in docker-compose.yml).
# Widen API_BIND only if something off-box must reach the API directly — with the
# Tailscale overlay, API_BIND=<tailscale-ip> keeps MagicDNS working without ever
# publishing the API to the internet. See docker-compose.yml / docker-compose.tailscale.yml
# API_BIND=127.0.0.1
# DB_BIND=127.0.0.1
```

These are the settings most deployments touch, not the full list: `config.py` is the authoritative
list of every setting, its default and what it does. The `.env` that `setup.sh` generates also carries
the optional `PERFORMANCE_`, `BACKTESTING_`, `MARKET_DATA_`, `CORS_` and `AWS_` groups as commented-out
lines showing their defaults, so you can see and override them without leaving the file.

Edit `.env` and restart with `make deploy` to apply changes.

## Secure Connection via Tailscale

[Tailscale](https://tailscale.com) creates a private WireGuard network (tailnet) that makes the API accessible only to devices on your tailnet — no open ports, no firewall rules needed.

Use this when running on a VPS or cloud server and want to access the API privately from another machine (e.g. Condor or MCP tools).

### Prerequisites: Get a Tailscale auth key

1. Create a free account at [tailscale.com](https://tailscale.com)
2. Go to **Settings → Keys**: [tailscale.com/admin/settings/keys](https://tailscale.com/admin/settings/keys)
3. Click **Generate auth key** — check **Reusable** for multiple deployments
4. Copy the key (starts with `tskey-auth-`)
5. Enable **[MagicDNS](https://login.tailscale.com/admin/dns)** in the Tailscale admin console

### Setup

Run `make setup` and answer `y` when prompted:

> Use Tailscale for secure private networking? [y/N]

This adds the following to `.env`:

```bash
TAILSCALE_ENABLED=true
TAILSCALE_AUTH_KEY=tskey-auth-...
TAILSCALE_HOSTNAME=hummingbot-api   # MagicDNS hostname on your tailnet
```

### Deploy

```bash
make deploy
```

When `TAILSCALE_ENABLED=true`, this automatically runs:

```bash
docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d
```

A Tailscale sidecar container joins your tailnet using `network_mode: host`. `hummingbot-api`'s port 8000 is bound to loopback only by default (`API_BIND` in `.env` — see [Configuration](#configuration)), so it does not sit on any public interface. The sidecar's `tailscale serve` config (`tailscale-serve.json`, applied automatically via `TS_SERVE_CONFIG` when the sidecar starts — no `tailscale serve` command needed) is what makes it reachable again: it forwards the tailnet IP's `:8000` to `127.0.0.1:8000`, so the API ends up reachable at `http://hummingbot-api:8000` from any device on the same tailnet, and *only* from the tailnet.

### Connecting MCP tools via Tailscale

Once on the same tailnet, use the MagicDNS hostname instead of `localhost`:

```bash
claude mcp add --transport stdio hummingbot -- \
  docker run --rm -i \
  -e HUMMINGBOT_API_URL=http://hummingbot-api:8000 \
  -v hummingbot_mcp:/root/.hummingbot_mcp \
  hummingbot/hummingbot-mcp:latest
```

### Dev mode

When `TAILSCALE_ENABLED=true`, `make run` will automatically install Tailscale if needed, connect to your tailnet, and bind uvicorn to `127.0.0.1` only (Tailscale handles external access).

### Check status

```bash
make tailscale-status
```

This shows both tailnet peer status and `tailscale serve status` — confirm
port 8000 shows up as forwarded, not just that the node joined the tailnet.

From another device on your tailnet:

```bash
curl -u YOUR_USERNAME:YOUR_PASSWORD http://hummingbot-api:8000/
```

## API Features

- **Portfolio**: Balances, positions, P&L across all exchanges
- **Trading**: Place orders, manage positions, track history
- **Bots**: Deploy, monitor, and control trading bots
- **Market Data**: Prices, orderbooks, candles, funding rates
- **Strategies**: Create and manage trading strategies

Full API documentation at http://localhost:8000/docs

## Development

```bash
make install              # Create conda environment
conda activate hummingbot-api
make run                  # Run with hot-reload
```

## Troubleshooting

**Start here:**
```bash
make doctor
```
Checks dependencies, `.env` (including credentials still left at well-known
defaults), the `hummingbot-api` / `hummingbot-broker` / `hummingbot-postgres`
containers, which ports are on a public interface, Tailscale's tailnet *and*
serve status, whether the API actually answers an authenticated request, and
whether it is connected to the MQTT broker. Read-only, and it names the fix for
whatever it finds.

**Bots deploy but report nothing — no controllers, no logs, no performance?**

The API is up and REST works, but it is not connected to the broker: bot status
arrives over MQTT, so a bot with no broker is a bot with nothing to say. Confirm
it, then re-seed:
```bash
make doctor                 # "Broker connection" says whether the API is connected
docker compose logs emqx | grep -i "auth-bootstrap"
make emqx-auth-reset        # rewrites the bootstrap file and re-seeds the broker
```
A `Permission denied` on `/opt/emqx/etc/auth-bootstrap.csv` means the broker
could not read the file it seeds the account from, so no account was ever
created and the API's correct credentials came back `Not authorized`. The broker
still comes up healthy either way, which is why this shows up as missing bots
rather than as a broker error.

**API won't start?**
```bash
docker compose logs hummingbot-api
```

**Database issues?**
```bash
docker compose down -v    # Reset all data
make deploy               # Fresh start
```

**Check service status:**
```bash
docker ps | grep hummingbot
```

**Tailscale not connecting?**
```bash
make tailscale-status     # Check tailnet peers
```
Confirm the node appears in `tailscale status` and that MagicDNS is enabled in your Tailscale admin console.

## Support

- **Docs**: https://hummingbot.org/hummingbot-api/
- **Tailscale guide**: https://hummingbot.org/hummingbot-api/tailscale/
- **API Docs**: http://localhost:8000/docs
- **Issues**: https://github.com/hummingbot/hummingbot-api/issues
