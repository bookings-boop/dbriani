# Supermemory Integration — Status

**Status as of 2026-05-20: REMOVED from the Phase 1B MVP.** The Supermemory
retrieval node has been deleted from `workflows/phase-1b-telegram.json`.
Phase 1B drafts entirely from the self-contained 27 KB system prompt.

## Why it was removed

Step 2 verification (`scripts/verify-supermemory.py` + direct API probes)
found the Supermemory account was not usable as a yacht knowledge base:

- **Connector scope was wrong.** The Supermemory Google Drive connector had
  been pointed at a Drive *parent* folder (effectively "all of Drive"), not
  the specific Dubriani knowledge subfolder. It ingested ~20 documents — ~16
  of them unrelated SEO / backlink / Google Ads / PR working files that live
  elsewhere in the Drive.
- **A customer PII file was swept in** — "customer list", a contact registry
  of IDs, names, emails, and phone numbers, stored in a different folder
  entirely.
- **No container tags.** Every ingested document had `containerTags: []`, so
  the originally-planned Step 3 tag filter (`containerTags: ["dubriani"]`)
  had nothing to filter on.

Feeding those search results into the drafting prompt would have injected
noise — and PII — into Claude's context. The system prompt is fully
self-contained (complete yacht catalog, pricing, hard floors, catering,
rules), so Phase 1B loses no drafting capability by running memory-less.

## Action taken

| By | Action |
|----|--------|
| Claude Code | Deleted the `Supermemory` node from the Phase 1B workflow. `Filter Inbound` now connects directly to `Build Prompt`. `Build Prompt`'s `memoryContext` field is a hardcoded empty string. |
| Zayn | Reconnecting the Supermemory Google Drive connector to the correct scope only: `/ZAYN_AI_BRAIN/Supermemory - Dubriani/`. |

## Open action — PII removal (Zayn)

⚠️ After the connector is re-pointed and re-ingestion completes, the
**"customer list" PII file must be manually deleted from the Supermemory
index** via the Supermemory dashboard. Re-scoping the connector stops
*future* ingestion of it but does not purge the already-indexed copy.

## Re-enabling (Phase 2)

Re-adding Supermemory to the workflow is a **Phase 2** task, separate from
the integrations build. It needs more than re-inserting the old node:

- Re-add an HTTP node calling `POST /v3/search`, authenticated via the
  `Bearer Auth account` credential (httpBearerAuth).
- Scope the search with `containerTags` once the re-ingested content is
  tagged — verify the real tag with `scripts/verify-supermemory.py`.
- Do **not** restore the old `memoryContext = JSON.stringify($json)`: it
  dumped the entire raw API response (including other customers' memories)
  into the prompt. Extract only the relevant chunk text. (This was risk R2.)
- `verify-supermemory.py`'s result parser is out of date with the Supermemory
  v3 response shape — text is nested under `chunks[].content`, not a
  top-level `content` field, and results carry no `containerTags` field.
  Fix the parser before relying on its display output.
