# Genie Gateway — semantic cache on Lakebase

A **Databricks App** that sits in front of [Genie](https://docs.databricks.com/aws/en/genie/): callers change the base URL and use a **gateway ID** instead of a Genie Space ID. Repeated or semantically similar questions skip NL→SQL whenever safe cache lookups succeed and reuse cached SQL against your warehouse—typically fresher numbers without redoing Genie work.

**Lakebase** (Postgres + pgvector) stores embeddings and metadata per gateway. **FastAPI** exposes compatibility clones of Genie REST and MCP, plus simpler REST for dashboards or scripts. **Dash** (`/dash/`) covers gateways, playground, metrics, cache inspection, and settings.

## Features

| Area | What you get |
|------|----------------|
| **Semantic cache** | Vector similarity over stored query text; configurable threshold and TTL |
| **Throughput** | Per-gateway queue with backoff for bursts |
| **Isolation** | Multiple gateways → distinct Genie Space + warehouse + cache settings |
| **Protocols** | Genie-compatible REST (`/api/2.0/genie/...`), MCP (`/api/2.0/mcp/genie/...`), simplified REST (`/api/v1/...`), gateway admin (`/api/gateways`, `/api/settings`) |

## Architecture

```
Caller (OAuth JWT)
       │
       ▼
Databricks App  ──► Gateway config & logs (Lakebase)
       │                  │
       ├── Embeddings ────┤  (default: app OAuth M2M for serving scope)
       ├── Genie API ─────┤  (configurable SP vs delegated user)
       ├── SQL Warehouse ─┘  (re-run cached SQL for fresh results)
       └── pgvector cache ─── app SP → Lakebase
```

End users **do not** need Lakebase credentials for reads/writes of cache rows—the app’s service principal talks to Postgres. RBAC over gateways still relies on workspace identity where enforced.

## Deploy with Asset Bundles

Prerequisites: Databricks **Apps**, **Genie Space**, **SQL warehouse**, **Lakebase Autoscaling** project with pgvector, [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html) logged into the target workspace.

1. **Variables** — In `databricks.yml`, set `lakebase_database` to your Lakebase path (`projects/<project>/branches/<branch>/databases/<id>`). Adjust `genie_space_id` / profile / `app_name` as needed.
2. **Deploy**

   ```bash
   databricks bundle deploy -t demo --auto-approve
   # or: npm run bundle:deploy
   ```

3. **Lakebase once per app** — Grant the app service principal **CAN_MANAGE** on the Lakebase project, then in Postgres run `databricks_create_role('<app-sp-client-id>', 'SERVICE_PRINCIPAL')`. A dedicated schema (`LAKEBASE_SCHEMA` in `app.yml`) avoids manual grants on `public`. Tables are created by the app on first use—see [Postgres roles](https://docs.databricks.com/aws/en/oltp/projects/postgres-roles).

4. **Open the App URL** → **Gateways** → create a gateway (pick space + warehouse). Copy the overview endpoint snippet.

Interactive API docs ship with the app (Swagger/OpenAPI).

## Configuration highlights

| Variable / setting | Purpose |
|--------------------|---------|
| `AUTH_USE_APP_SERVICE_PRINCIPAL` | Default **true** in Apps: Genie, SQL statements, embeddings use app OAuth M2M (`DATABRICKS_CLIENT_ID` / `SECRET`). Set **false** for local runs that only have a user token. |
| `GENIE_FORCE_APP_SERVICE_PRINCIPAL` | Force Genie REST to SP even when sharing looks correct with user JWT. |
| `EMBEDDING_FORCE_APP_SERVICE_PRINCIPAL` | Prefer SP for embedding endpoint (helps when user JWT lacks `model-serving` scope). |
| `DATABRICKS_EMBEDDING_ENDPOINT` | Defaults to `databricks-gte-large-en`. |
| `shared_cache` / gateway settings | Shared vs identity-scoped cache entries; thresholds, TTL, QPM limits, normalization & validation toggles |

Full defaults live in `backend/app/config.py` and the **Settings** page in Dash.

## UI gallery

Screenshots live under [`docs/screenshots/`](docs/screenshots/): gateway list, metrics, cache, logs, settings, playground miss/hit, API reference.

## Local development

```bash
cd backend
cp .env.example .env   # DATABRICKS_HOST, SP or PAT as appropriate — avoid PAT in production Apps
pip install -r ../requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Browse **http://127.0.0.1:8000/dash/** (root redirects to Dash).

Optional: **docker-compose** for local Postgres + pgvector — see `docker-compose.pgvector.yml`.

## Notebooks

| Notebook | Purpose |
|----------|---------|
| [`notebooks/api_gateway_demo.ipynb`](notebooks/api_gateway_demo.ipynb) | REST / concurrent load patterns |
| [`notebooks/mcp_gateway_agent_demo.ipynb`](notebooks/mcp_gateway_agent_demo.ipynb) | MCP client demo |

Upload any required secrets to Workspace or use `.env` per notebook instructions—do not commit real tokens.

## Credits

Inspired by prior work including [genie-lakebase-cache](https://github.com/databricks-field-eng/genie-lakebase-cache) (Sean Kim, Databricks Field Eng) and related Genie gateway examples.

---

*Lakebase product:* [Databricks Lakebase](https://www.databricks.com/product/lakebase) · [OLTP docs](https://docs.databricks.com/aws/en/oltp/)
