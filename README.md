# mcp-postgres

An **MCP (Model Context Protocol) server** that gives Claude and other AI agents a safe,
privilege-aware interface to manage a **PostgreSQL** database on a RHEL-based host.

It runs as a `systemd` service under a dedicated `mcp-postgres` user, connects to PostgreSQL
locally (`127.0.0.1:5432`, role `mcp`), and **only enables what it is actually allowed to do** —
checking both its OS rights (sudo/wheel) and the DB privileges of role `mcp`, and reporting
those capabilities back to the agent. With sudo it may edit exactly two files —
`postgresql.conf` and `pg_hba.conf` — and reload PostgreSQL.

> Design details are in **[ARCHITECTURE.md](./ARCHITECTURE.md)**. This README is the deployment
> runbook.

---

## What the agent can do

Tools are **capability-gated** — the agent sees only what the current privileges allow:

- **Always:** inspect databases/schemas/tables, run read-only `SELECT` queries, and read the
  live capability report.
- **If role `mcp` can write:** run DML/DDL.
- **If role `mcp` is admin:** manage roles and databases.
- **If `mcp-postgres` has sudo:** read/edit `postgresql.conf` & `pg_hba.conf` and reload
  PostgreSQL.

Privileges are re-checked before every action, so the agent is told immediately if its rights
change while running.

---

## Requirements

- RHEL-based Linux (RHEL / Rocky / Alma 8 or 9), `systemd`
- Python 3.11+
- PostgreSQL reachable at `127.0.0.1:5432`
- A PostgreSQL role `mcp` with a password (its privileges are up to you — the service adapts)

---

## Deployment

### 1. Create the OS user
```bash
sudo useradd --system --home-dir /opt/mcp-postgres --shell /sbin/nologin mcp-postgres
# Optional: grant config-editing capability (postgresql.conf / pg_hba.conf).
# Either full wheel, OR just the scoped sudoers rule the installer drops in (recommended).
# sudo usermod -aG wheel mcp-postgres
```

### 2. Create the PostgreSQL role
```bash
sudo -u postgres psql -c "CREATE ROLE mcp LOGIN PASSWORD 'CHANGE_ME';"
# Grant whatever privileges you intend (read-only, read-write, or admin) — out of scope here.
```

### 3. Install
```bash
sudo ./install.sh
```
The installer creates the venv at `/opt/mcp-postgres/venv`, installs the `mcp_postgres`
package, and lays down: the `privhelper`, the systemd unit, a **scoped** `/etc/sudoers.d/mcp-postgres`
(allowing `mcp-postgres` to run only the `privhelper` via `sudo`), and template config under
`/etc/mcp-postgres/`.

### 4. Configure
Edit `/etc/mcp-postgres/config.toml` (bind address/port, DB name, log level), then set secrets:
```bash
# DB password for role "mcp"
sudo install -m 0600 -o mcp-postgres -g mcp-postgres /dev/stdin /etc/mcp-postgres/secret <<< 'CHANGE_ME'
# Bearer token agents must present
sudo install -m 0600 -o mcp-postgres -g mcp-postgres /dev/stdin /etc/mcp-postgres/token <<< "$(openssl rand -hex 32)"
```

### 5. Start
```bash
sudo systemctl enable --now mcp-postgres
sudo systemctl status mcp-postgres
```

### 6. Verify (prod self-tests)
```bash
sudo -u mcp-postgres /opt/mcp-postgres/venv/bin/mcp-postgres-selftest
```
Checks the service is active, the MCP handshake works, DB connectivity, that the reported
capabilities match reality, and that the config-file allowlist and read-only guard hold.

### 7. Connect an agent
The server speaks MCP over **Streamable HTTP** at `http://<host>:8080/mcp` (default). Example
for Claude Code:
```bash
claude mcp add --transport http postgres http://127.0.0.1:8080/mcp \
  --header "Authorization: Bearer <token-from-step-4>"
```
For remote access, front it with a TLS-terminating reverse proxy (e.g. nginx) — keep the app
bound to `127.0.0.1`.

---

## Operations

```bash
sudo systemctl restart mcp-postgres      # apply config changes
journalctl -u mcp-postgres -f            # follow logs
```

Config-file backups (`.bak`, timestamped) are written next to `postgresql.conf` / `pg_hba.conf`
before any edit. Some `postgresql.conf` settings need a **PostgreSQL restart** (not just a
reload) to take effect — the service flags these to the agent but never restarts PostgreSQL on
its own.

---

## License

TBD.
