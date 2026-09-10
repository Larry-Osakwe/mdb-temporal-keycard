# Keycard demo runbook

How to run the Keycard-enabled reference architecture and the on-stage
demo: kill the worker mid-query, rotate a credential, and watch the workflow
finish anyway. Verified end to end against a live Keycard zone, a live Atlas
M0 cluster, and the real Voyage and OpenAI APIs.

## Accounts and prerequisites

Tooling: `uv`, Docker, the Temporal CLI, Node 20+ (see
[RUNBOOK.md](RUNBOOK.md) for install commands), and the
[Keycard CLI](https://docs.keycard.ai/cli) signed in to your zone
(`keycard auth signin --zone <zone-id> --org <org-id>`).

External accounts:

- MongoDB Atlas: a free M0 cluster works (it supports Vector Search). You need
  one database user and the `mongodb+srv://` connection string with the
  password embedded. Add the demo machine's IP to the Atlas network allowlist,
  and remember the venue's IP on event day.
- Voyage AI: an API key, and add a payment method to the account. The free tier
  is capped at 3 requests per minute, which turns a 40-chunk ingest into a
  15-minute grind (durable, but slow). The free token allowance still applies
  after adding one.
- OpenAI: an API key for the research agent.

## One-time setup

```bash
# 1. Provision the Keycard zone: worker application + client credential,
#    three vault-backed resources, dependencies. Writes KEYCARD_* to .env.
#    Idempotent; blank secret skips that resource so you can stage.
MONGODB_URI='mongodb+srv://...' VOYAGE_API_KEY='...' OPENAI_API_KEY='...' \
  uv run python -m infra.provision_keycard

# 2. Local infra defaults (MinIO + Temporal are local; no Temporal account)
cat .env.example | grep -A4 "S3_ENDPOINT_URL" >> .env   # or copy the S3/MinIO block by hand

# 3. Bring everything up, create the vector index, seed the corpus
make setup
make start          # MinIO, Temporal dev server, worker, trigger API, agent API + UI
make index          # one-time Atlas Vector Search index
make seed           # sample document through the full durable pipeline
```

The worker prints `[worker] Keycard mode: credentials minted from <zone-url>`
at startup. `.env` holds only the Keycard client credential and resource
identifiers; the Mongo, Voyage, and OpenAI secrets exist solely in the zone's
vault. The client secret itself is the local-demo posture: on a platform that
issues workload identity (EKS IRSA, Azure federated tokens, a SPIRE cluster),
the SDK's discovery picks up the platform token file instead and the worker
starts with no secrets at all; see the README's "last secret" section.

Seed extra documents (source_uri follows the key):

```bash
make seed FILE=./how-keycard-works.md KEY=docs/how-keycard-works.md
```

Seeding Keycard's own docs makes the finale land: the agent answers Keycard
questions from content that was ingested through Keycard-minted credentials.

## The demo

Windows to have open: the agent UI (http://localhost:5173), the Temporal UI
(http://localhost:8233), and the Keycard console on the zone's audit log.

1. **Start a research query.** Use the agent UI, or:

   ```bash
   curl -s -X POST http://localhost:8090/research \
     -H 'Content-Type: application/json' \
     -d '{"query":"How does Keycard mint credentials for agents?"}'
   # note the workflow_id in the response
   ```

2. **Kill the worker mid-flight** (about 10 seconds in, while tool activities
   are running):

   ```bash
   kill $(cat .local/worker.pid)
   ```

3. **Rotate the credential while the worker is down.** In the Keycard console:
   the Atlas resource -> Credentials tab -> edit the vaulted value. For a true
   rotation, first reset the database user's password in Atlas, then vault the
   new connection string. (Skipping this step still demonstrates
   resume-with-fresh-mints; the rotation makes the point that revocation does
   not strand in-flight work.)

4. **Restart the worker and watch it finish:**

   ```bash
   PYTHONUNBUFFERED=1 nohup uv run python -u -m pipeline.worker > .local/worker.log 2>&1 &
   echo $! > .local/worker.pid
   curl -s http://localhost:8090/research/<workflow_id>   # poll until COMPLETED
   ```

   Recovery re-runs the interrupted activity, the activity mints fresh (the
   rotated credential, if you rotated), and the workflow completes. In the
   Temporal UI, walk the workflow history: inputs and results only, nothing
   credential-shaped. In the Keycard console, the audit log shows every mint
   attributed to `temporal-pipeline-worker`.

## What to point at while it runs

- In the Temporal UI, open the activity retries. On the free Voyage tier,
  ingestion visibly absorbs rate-limit failures through retry policies without
  re-embedding completed chunks; in our verification the pipeline shrugged off
  roughly 300 rate-limit errors across two documents with zero lost work.
- Walk the workflow history: durable, replayable, persisted indefinitely, and
  credential-free. That last property is the reason minting happens inside
  activities rather than passing tokens through workflow state.
- Contrast per-execution with per-boot: Atlas and Voyage credentials mint per
  activity execution (the dual-resource activities declare both in one
  `@grant`), while the OpenAI key mints once per worker start, because the
  OpenAI Agents plugin builds its client before any activity exists. Per-call
  minting there needs a custom model provider (noted as follow-up work).

## Troubleshooting

- Worker exits at startup with a credential discovery error: `.env` is read by
  pydantic-settings, and the worker exports the Keycard client credential into
  the process environment itself. Check `KEYCARD_CLIENT_ID` /
  `KEYCARD_CLIENT_SECRET` are present in `.env`.
- `make seed` fails with `S3_BUCKET is not set`: the local MinIO block from
  `.env.example` is missing from `.env` (step 2 above).
- Research endpoint returns 503: the agent loads only when an OpenAI key is
  available; in Keycard mode that means the vaulted OpenAI resource exists and
  the worker restarted after it was vaulted.
- Embeds failing repeatedly with a rate-limit error: that is the Voyage free
  tier. The workflow will finish anyway; a payment method on the account makes
  it fast.
