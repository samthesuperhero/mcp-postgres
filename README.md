# mcp-postgres

Repository: <https://github.com/samthesuperhero/mcp-postgres>

An **MCP (Model Context Protocol) server** that gives Claude and other AI agents a safe,
privilege-aware interface to manage a **PostgreSQL** database on a RHEL-based host.

It runs as a `systemd` service under a dedicated `mcp-postgres` user, connects to PostgreSQL
locally (`127.0.0.1:5432`, role `mcp`), and **only enables what it is actually allowed to do** —
checking both its OS rights (sudo/wheel) and the DB privileges of role `mcp`, and reporting
those capabilities back to the agent. With sudo it may edit exactly two files —
`postgresql.conf` and `pg_hba.conf` — and reload PostgreSQL.

Role `mcp` is a cluster-global PostgreSQL role, so the agent can work with **any database in
the cluster** it can connect to — not just one. Tools act on a session-wide *current* database;
the agent switches it with `use_database` (and lists candidates with `list_databases`). Every
result names the `database` it came from.

> Design details are in **[ARCHITECTURE.md](./ARCHITECTURE.md)**. This README is the deployment
> runbook.

---

## What the agent can do

Tools are **capability-gated** — the agent sees only what the current privileges allow:

- **Always:** pick the target database (`use_database`), inspect databases/schemas/tables, run
  read-only `SELECT` queries, and read the live capability report.
- **If role `mcp` can write:** run DML/DDL.
- **If role `mcp` is admin:** manage roles and databases.
- **If `mcp-postgres` has sudo:** read/edit `postgresql.conf` & `pg_hba.conf` and reload
  PostgreSQL.

Privileges are re-checked before every action, so the agent is told immediately if its rights
change while running.

The server is **self-describing**: on connect it returns MCP `instructions` (an overview, the
tier model, and the result envelope), every tool carries safety annotations, and two resources
are published — `docs://mcp-postgres/guide` (full capability guide) and `capabilities://current`
(the live report). An agent can learn what it may do without any prior knowledge — start by
calling `get_capabilities`.

---

## Requirements

- RHEL-based Linux (RHEL / Rocky / Alma 8 or 9), `systemd`
- Python 3.11+
- PostgreSQL reachable at `127.0.0.1:5432`
- A PostgreSQL role `mcp` with a password (its privileges are up to you — the service adapts)

---

## Deployment

Everything is installed by a single Python script, **`install.py`**, run once by a privileged
user (root or a sudo-capable admin). It uses only the system `python3` — no packages to install
first — and is idempotent (safe to re-run to upgrade or repair).

### 1. Get the code (as the privileged user)
```bash
git clone https://github.com/samthesuperhero/mcp-postgres.git
```

### 2. Run the installer
```bash
sudo mcp-postgres/install \
  --bind 127.0.0.1 --port 41780 \
  --start --run-selftest
# add --grant-wheel to allow editing postgresql.conf / pg_hba.conf (else config tools stay off)
```
`mcp-postgres/install` is a thin launcher committed alongside the code (so it works right
after cloning); it is exactly equivalent to `sudo python3 mcp-postgres/install.py …`. To pass
secrets via the environment, keep them across `sudo` with `-E`, e.g.
`MCP_PG_TOKEN=… sudo -E mcp-postgres/install --force`.
You'll be **prompted for the `mcp` DB password** (or set `MCP_PG_DB_PASSWORD`); the bearer token
is generated automatically (or set `MCP_PG_TOKEN`). The installer then:

- creates the system user `mcp-postgres` (`--grant-wheel` adds it to `wheel`);
- builds the venv at `/opt/mcp-postgres/venv` and installs the `mcp_postgres` package;
- installs the `privhelper`, the systemd unit, and a **scoped** `/etc/sudoers.d/mcp-postgres`
  (allowing `mcp-postgres` to run only the `privhelper` via `sudo`);
- writes config + secrets under `/etc/mcp-postgres/` with tight permissions;
- with `--start`, enables and starts the service; with `--run-selftest`, runs the prod checks.

The printed output includes the generated bearer token — save it for step 4.

Re-running the installer is safe: it **won't overwrite an existing `config.toml`, DB password,
or token**. To change them, pass `--force` (`sudo mcp-postgres/install --force` regenerates
config from the flags and rotates the token) or supply a new value via `MCP_PG_DB_PASSWORD` /
`MCP_PG_TOKEN`.

### 3. Create the PostgreSQL role
The service connects as role `mcp`. Create it with whatever privileges you intend (read-only,
read-write, or admin — the service adapts):
```bash
sudo -u postgres psql -c "CREATE ROLE mcp LOGIN PASSWORD 'CHANGE_ME';"
```
Or let the installer do it by adding `--create-db-role` in step 2 (requires the running user to
have `postgres` superuser access).

### 4. Connect an agent
Hand the agent the endpoint URL and the bearer token — see
**[Connecting an agent](#connecting-an-agent)** below for the exact values, where to read the
token, and a ready-to-run Claude Code example.

> Re-run the self-tests any time: `sudo -u mcp-postgres /opt/mcp-postgres/venv/bin/mcp-postgres-selftest`
> — verifies the service is active, the MCP handshake and DB connectivity work, the reported
> capabilities match reality, and the config-file allowlist and read-only guard hold.

---

## Configuring the service

Everything the service reads lives under **`/etc/mcp-postgres/`** on the host (created by the
installer). After any change, apply it with `sudo systemctl restart mcp-postgres`.

### `config.toml` — non-secret settings
Mode `0640`, owner `root:mcp-postgres`. Secrets are **not** stored here.
```toml
[server]
bind = "127.0.0.1"   # keep on localhost — nginx is the public front
port = 41780
path = "/mcp"

[database]
host = "127.0.0.1"
port = 5432
user = "mcp"         # the PostgreSQL role the service connects as
dbname = "postgres"

[logging]
level = "INFO"

[oauth]                 # optional OAuth 2.1 layer (off by default) — see "Connecting
enabled = false         # the claude.ai web connector" below. The static token keeps
public_url = ""         # working alongside it. public_url = your public HTTPS base.
access_token_ttl = 3600
refresh_token_ttl = 2592000
state_dir = "/var/lib/mcp-postgres"
```
Edit it directly, or re-run the installer with `--force` to regenerate it from the
`--bind` / `--port` / `--db-user` / `--enable-oauth` / `--public-url` / … flags.

**Schema changes across versions.** `sudo mcp-postgres/update` **migrates `config.toml` in
place** to the running version's schema: it writes a timestamped `config.toml.<ts>.bak`, adds
any newly-introduced settings as *commented* lines (so the effective config is unchanged — the
service keeps using their defaults until you uncomment), and comments out settings that have been
retired. Your values, comments, and layout are preserved; only new/retired keys are annotated.
Skip it with `sudo mcp-postgres/update --no-config-migrate`, or run it on demand with
`sudo /opt/mcp-postgres/venv/bin/mcp-postgres-migrate-config`. Unrecognized keys (e.g. typos) are
left untouched and merely reported by the self-test and in the service log.

### Secrets — two files, mode `0600`, owner `mcp-postgres`
| File | Contents |
|------|----------|
| `/etc/mcp-postgres/secret` | password for the PostgreSQL role `mcp` (never stored in `config.toml`) |
| `/etc/mcp-postgres/token`  | the MCP bearer token agents must present |

To change a secret, edit the file (then restart) or re-run the installer:
`--force` rotates the token, while `MCP_PG_DB_PASSWORD=… sudo -E mcp-postgres/install --force`
sets a specific DB password (`MCP_PG_TOKEN=…` sets a specific token). The env vars
`MCP_PG_DB_PASSWORD` / `MCP_PG_TOKEN` also override the files at runtime, and `MCP_PG_CONFIG_DIR`
relocates the whole directory (used by tests).

### Reading the bearer token
The installer prints it once, when it generates it (step 2). To retrieve it later:
```bash
sudo cat /etc/mcp-postgres/token
```

---

## Connecting an agent

An agent needs exactly two credentials:

| Credential | Value |
|------------|-------|
| **Endpoint URL** | on-host: `http://127.0.0.1:41780/mcp` · remote (via nginx): `https://<host>/mcp` |
| **Bearer token** | the contents of `/etc/mcp-postgres/token` — `sudo cat` it (see above) |

Transport is MCP **Streamable HTTP**; authentication is the header `Authorization: Bearer <token>`.
The app stays bound to `127.0.0.1:41780`, so **remote** agents connect through the nginx reverse
proxy (which terminates TLS) — never the app port directly.

**Claude Code:**
```bash
claude mcp add --transport http postgres http://127.0.0.1:41780/mcp \
  --header "Authorization: Bearer <token>"
# remote: swap the URL for your nginx endpoint, e.g. https://<host>/mcp
```

**Any other MCP client:** point it at the endpoint URL over the Streamable HTTP transport and set
the `Authorization: Bearer <token>` header. Once connected the agent can discover the rest itself —
the server returns `instructions` on connect and publishes a capability guide; a good first call is
`get_capabilities`.

### The claude.ai web connector (OAuth)

The claude.ai **web** app authenticates connectors only via **OAuth** — its Advanced settings take
an OAuth *Client ID / Secret*, with no field for a static bearer header — so the default static-token
setup can't attach there. Turn on the built-in **OAuth 2.1** layer to connect it. The static token
keeps working for Claude Code / Desktop (dual auth).

**1. Enable OAuth** — set the public HTTPS base your nginx serves and restart:
```toml
# /etc/mcp-postgres/config.toml
[oauth]
enabled = true
public_url = "https://db.example.com"   # advertised to clients — NOT the 127.0.0.1 bind
```
```bash
sudo systemctl restart mcp-postgres
# or at install time:  sudo mcp-postgres/install --force --enable-oauth --public-url https://db.example.com
```

**2. Point nginx at the new paths.** OAuth adds root-level routes alongside `/mcp`; the reverse proxy
must forward all of them to the same `127.0.0.1:41780` upstream:
```nginx
location ~ ^/(mcp|authorize|token|register|revoke|login|\.well-known/) {
    proxy_pass http://127.0.0.1:41780;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;          # keep the long-lived Streamable HTTP responses flowing
}
```

**3. Add the connector in claude.ai** — use `https://db.example.com/mcp` as the URL and **leave the
Client ID and Secret blank** (the server supports dynamic client registration, so claude.ai registers
itself). When you connect, a **login page** asks for a passphrase: enter the server's **bearer token**
(`sudo cat /etc/mcp-postgres/token`). That approves the connection and the web chat gains the same
capability-gated tools.

> Verify the OAuth surface any time with the self-test (it runs a full registration → token → MCP
> handshake against the local endpoint when `[oauth]` is enabled), or by hand:
> `curl -s https://db.example.com/.well-known/oauth-protected-resource/mcp`.

**Troubleshooting — `421 Misdirected Request` / `Invalid Host header`:** the server allowlists the
host from `public_url` for DNS-rebinding protection. If authenticated `/mcp` calls 421, make sure
`public_url`'s host exactly matches the hostname nginx serves and that nginx forwards the real Host
(`proxy_set_header Host $host;`), then restart the service.

---

## Operations

```bash
sudo systemctl restart mcp-postgres      # apply config changes
journalctl -u mcp-postgres -f            # follow logs
sudo mcp-postgres/admin --status         # report OS/DB admin rights (no change)
sudo mcp-postgres/admin                  # toggle OS wheel + DB superuser in lockstep
```
`mcp-postgres/admin` is the committed launcher for `admin.py` (equivalent to
`sudo python3 mcp-postgres/admin.py …`).

### Updating

To ship new code to a deployed host, run one command from the repo directory:
```bash
sudo mcp-postgres/update            # git pull + reinstall + restart + self-test
```
It pulls the latest code (as the checkout owner), refreshes the privhelper/unit/sudoers,
**force-reinstalls** the package into the venv (a plain re-install would be skipped since the
version is unchanged), restarts the service, and runs the self-test — failing loudly if the
service comes back unhealthy. Config and secrets under `/etc/mcp-postgres/` are left untouched.
Flags: `--no-pull` (deploy local/uncommitted changes) and `--no-selftest`.

Config-file backups (`.bak`, timestamped) are written next to `postgresql.conf` / `pg_hba.conf`
before any edit. Some `postgresql.conf` settings need a **PostgreSQL restart** (not just a
reload) to take effect — the service flags these to the agent but never restarts PostgreSQL on
its own.

---

## License

TBD.
