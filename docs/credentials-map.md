# Credentials Map

How each of the 4 secrets reaches the running N8N workflow. The committed
workflow JSON contains **no secrets** — it references N8N credentials by name
and reads the Telegram token from a container environment variable.

All real values live in `.env` (gitignored). Fill `.env` from `.env.example`.

| Secret | Mechanism | Set up in |
|--------|-----------|-----------|
| Anthropic API key | N8N credential, type `httpHeaderAuth`, name **`Anthropic API`** | §1 |
| WAHA API key | N8N credential, type `httpHeaderAuth`, name **`WAHA API`** | §2 |
| Supermemory token | N8N credential, type `httpBearerAuth`, name **`Bearer Auth account`** | §3 |
| Telegram bot token | N8N container env var `TELEGRAM_BOT_TOKEN` → `{{ $env.TELEGRAM_BOT_TOKEN }}` | §4 |

> Credential **names must match exactly** — the workflow links credentials by
> name on import. If a name differs, the node imports unlinked and must be
> re-picked by hand in the N8N editor.

---

## 1–3. Creating the N8N credentials

Two new credentials are needed: `Anthropic API` and `WAHA API`. The Supermemory
credential (`Bearer Auth account`) already exists — leave it.

### Option A — N8N editor UI (reliable, recommended)

For each credential: **N8N → Credentials → Add credential**.

**`Anthropic API`**
- Type: *Header Auth*
- Name (of credential): `Anthropic API`
- Header **Name**: `x-api-key`
- Header **Value**: the Anthropic key from `.env` (`ANTHROPIC_API_KEY`)

**`WAHA API`**
- Type: *Header Auth*
- Name (of credential): `WAHA API`
- Header **Name**: `X-Api-Key`
- Header **Value**: the WAHA key from `.env` (`WAHA_API_KEY`)

### Option B — N8N REST API (curl)

Requires an N8N API key (**N8N → Settings → n8n API → create**), stored in
`.env` as `N8N_API_KEY`. Run from the repo root so `.env` is sourced — the
secret never appears on the command line:

```bash
set -a; . ./.env; set +a

# Anthropic API
curl -sS -X POST "$N8N_BASE_URL/api/v1/credentials" \
  -H "X-N8N-API-KEY: $N8N_API_KEY" -H "Content-Type: application/json" \
  -d "{\"name\":\"Anthropic API\",\"type\":\"httpHeaderAuth\",\"data\":{\"name\":\"x-api-key\",\"value\":\"$ANTHROPIC_API_KEY\"}}"

# WAHA API
curl -sS -X POST "$N8N_BASE_URL/api/v1/credentials" \
  -H "X-N8N-API-KEY: $N8N_API_KEY" -H "Content-Type: application/json" \
  -d "{\"name\":\"WAHA API\",\"type\":\"httpHeaderAuth\",\"data\":{\"name\":\"X-Api-Key\",\"value\":\"$WAHA_API_KEY\"}}"
```

Each call returns JSON including an `id`. The workflow links by **name**, so
the id is optional — but you may record it in `.env` (`N8N_CRED_ANTHROPIC_ID`,
`N8N_CRED_WAHA_ID`) for reference.

### Supermemory (existing — not used by Phase 1B)

> Removed from the Phase 1B workflow — see `docs/supermemory-status.md`. The
> credential is kept in N8N for the Phase 2 re-enable.

Already present as `Bearer Auth account` (`httpBearerAuth`), id
`aKreN1AB4QYfQKlz`. To retrieve the token for Step 2's verification script:
**N8N → Credentials → Bearer Auth account → reveal**, then put it in `.env`
as `SUPERMEMORY_API_KEY`.

---

## 4. Telegram bot token — N8N container environment

The workflow's 8 Telegram HTTP nodes build their URL as
`https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/<method>`. N8N
resolves `$env.X` against **its own process environment** — so the token must
be present in the N8N container's environment, not just the local `.env`.

### 4.1 Identify how N8N runs (on the EC2 box)

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
# note the n8n container name, e.g. "n8n" or "<project>-n8n-1"

docker inspect <n8n-container> \
  -f 'compose_file={{ index .Config.Labels "com.docker.compose.project.config_files" }}{{"\n"}}work_dir={{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
```

If `compose_file` / `work_dir` are non-empty, N8N is **docker-compose**
managed — use §4.2. If empty, it was started with plain `docker run` — use §4.3.

### 4.2 docker-compose (expected case)

In the compose project directory there is a `.env` (compose's own) and a
compose YAML. Add the token to compose's `.env`:

```bash
echo 'TELEGRAM_BOT_TOKEN=<the bot token>' >> /path/to/compose/.env
```

In the compose YAML, under the **n8n** service, add the variable to
`environment:` (create the key if absent):

```yaml
  n8n:
    # ...existing config unchanged...
    environment:
      # ...existing vars unchanged...
      - TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
```

Recreate **only** the n8n service:

```bash
docker compose up -d n8n
```

### 4.3 plain `docker run` (fallback)

You must stop, remove, and re-run the container with the original flags plus
`-e TELEGRAM_BOT_TOKEN=...`. Capture the current run config first:

```bash
docker inspect <n8n-container> -f '{{json .Config.Env}}{{"\n"}}{{json .Mounts}}'
```

Then re-run preserving every volume mount and env var, adding the token.
(If it comes to this, paste the inspect output and we'll assemble the exact
command together.)

### 4.4 Verify + state safety

```bash
docker exec <n8n-container> printenv TELEGRAM_BOT_TOKEN   # should echo the token
```

- Workflows, credentials, and execution history persist in N8N's database
  (volume-backed). **Recreating the n8n container does not lose them.**
- The only thing interrupted by a restart is an execution running at that
  exact instant — do this in a quiet window (avoid 9 AM–11 PM Dubai peak).
- Ensure `N8N_BLOCK_ENV_ACCESS_IN_NODE` is **not** set to `true` (default is
  false → expressions may read `$env`). If it is true, `$env.TELEGRAM_BOT_TOKEN`
  resolves empty and every Telegram call fails.

> Exact line edits depend on the actual compose file. Paste the output of
> §4.1 and the n8n service block, and the precise diff will be provided.
