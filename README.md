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

Everything is installed by a single Python script, **`install.py`**, run once by a privileged
user (root or a sudo-capable admin). It uses only the system `python3` — no packages to install
first — and is idempotent (safe to re-run to upgrade or repair).

### 1. Get the code (as the privileged user)
```bash
git clone <repo-url> mcp-postgres
```

### 2. Run the installer
```bash
sudo python3 mcp-postgres/install.py \
  --bind 127.0.0.1 --port 8080 \
  --start --run-selftest
# add --grant-wheel to allow editing postgresql.conf / pg_hba.conf (else config tools stay off)
```
You'll be **prompted for the `mcp` DB password** (or set `MCP_PG_DB_PASSWORD`); the bearer token
is generated automatically (or set `MCP_PG_TOKEN`). The installer then:

- creates the system user `mcp-postgres` (`--grant-wheel` adds it to `wheel`);
- builds the venv at `/opt/mcp-postgres/venv` and installs the `mcp_postgres` package;
- installs the `privhelper`, the systemd unit, and a **scoped** `/etc/sudoers.d/mcp-postgres`
  (allowing `mcp-postgres` to run only the `privhelper` via `sudo`);
- writes config + secrets under `/etc/mcp-postgres/` with tight permissions;
- with `--start`, enables and starts the service; with `--run-selftest`, runs the prod checks.

The printed output includes the generated bearer token — save it for step 4.

### 3. Create the PostgreSQL role
The service connects as role `mcp`. Create it with whatever privileges you intend (read-only,
read-write, or admin — the service adapts):
```bash
sudo -u postgres psql -c "CREATE ROLE mcp LOGIN PASSWORD 'CHANGE_ME';"
```
Or let the installer do it by adding `--create-db-role` in step 2 (requires the running user to
have `postgres` superuser access).

### 4. Connect an agent
The server speaks MCP over **Streamable HTTP** at `http://<host>:8080/mcp` (default). Example
for Claude Code:
```bash
claude mcp add --transport http postgres http://127.0.0.1:8080/mcp \
  --header "Authorization: Bearer <token-from-step-2>"
```
For remote access, front it with a TLS-terminating reverse proxy (e.g. nginx) — keep the app
bound to `127.0.0.1`.

> Re-run the self-tests any time: `sudo -u mcp-postgres /opt/mcp-postgres/venv/bin/mcp-postgres-selftest`
> — verifies the service is active, the MCP handshake and DB connectivity work, the reported
> capabilities match reality, and the config-file allowlist and read-only guard hold.

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
