# Catalog Sync Plan — apply approved B2C catalog to all 5 copies (2026-06-02)

> Companion to `catalog-consolidation-review-2026-06-02.md` (the approved list).
> **Status: PLAN for approval. Nothing touches the 5 copies until operator says go + picks an approach.**

## The 5 targets (verified identical today)
| # | Target | How it's written | Notes |
|---|---|---|---|
| 1 | bridge `~/hermes-bridge/system-prompt.md` (§7–§10) | scp + restart | git-versioned; used by scorer + ≥8 gate |
| 2–5 | n8n workflow `azPIy9OcDwiPV5uY` nodes: **Build Prompt, Build Regen Prompt, Build Refine Prompt, Build Lead Prompt** | API PUT or Postgres `workflow_entity` | each embeds ~65 KB; 3 already byte-identical, 4th identical in catalog content |

## The change set (what gets applied — all approved)
1. **Remove ALL B2B** — Satoshi B2B rate (1,000+VAT, 24h 12,000+VAT); §1 "B2B partner ~50% off" section + "request license before quoting B2B" rule + `notes_for_zayn` B2B example; B2B add-on column.
2. **Add Eva 60** → Essential tier `| Eva 60 | 12 | 1,500 | eva-60 |`.
3. **Add full watersports section** (B2C): jetski Normal 600 / Supercharged GP 1,000 (models Yamaha Standard, Yamaha GP Supercharged, Sea-Doo); jetcar 790/1,200/1,690 (+capacities); eFoil 1,500/2,500; Seabob F5 1,200 / F9S 2,000; Flyfish 1,200/2,200; Flyboard 1,000/1,800; Banana 800/1,200; Donut 800/1,200; Wakeboard 1,200; Deep Sea Fishing 2,500/4hr; Self-Driving = Enquire.
4. **Jetcar 30-min 1,190 → 1,200** (+ capacities ≤2 / ≤4 adults).
5. **eFoil → 1,500/hr** (reconciles the old Satoshi add-on 1,000 → 1,500).
6. **Jetski:** drop the "1,800cc / 70 km/h" spec (that's the Jetcar's).
7. Keep all website "was→now" discounts + Bliss 55 anchor (1,400→1,100).

## Approach options
- **A — Runtime single source.** n8n prompt nodes stop embedding the catalog; they fetch it from the bridge at draft time. Eliminates copies for good, BUT adds a runtime dependency + latency to the live draft path (bridge down → drafts lose catalog). Biggest change, highest risk on the revenue path.
- **B — ⭐ Canonical file + generator + drift-check (RECOMMENDED).** One `catalog.md` is the source of truth; a `sync_catalog.py` script splices it into all 5 (between `<!--CATALOG-->` markers), backs up + verifies byte-identical; a daily drift-check alerts if the 5 ever diverge. Keeps current architecture (catalog stays embedded → no runtime dependency, no latency change). Solves "edit 5 places" permanently with minimal risk. Sets up for A later if ever wanted.
- **C — Manual one-time sync.** Apply the change set to all 5 now, carefully, no automation. Fastest; but the 5-copy drift risk returns on the next edit.

## Recommended execution (Option B)
1. Author canonical `catalog.md` = the approved B2C list above.
2. One-time: insert `<!--CATALOG_START--> … <!--CATALOG_END-->` markers around the catalog block in system-prompt.md + the 4 n8n prompt strings.
3. Build `sync_catalog.py`: reads canonical → splices into the 5 targets → backs up each → verifies the 5 catalog blocks are byte-identical.
4. **First run = apply the change set.**
5. Add a drift-check (cron or CI) diffing the 5 blocks; Telegram-alert on divergence.

## Safety / rollback (touches the live customer-facing drafter)
- **Backups first:** n8n full workflow export → `~/phase-1b-sync-backup-<ts>.json`; `system-prompt.md.bak-presync-<ts>`.
- **n8n writes:** API PUT (whitelist body name/nodes/connections/settings) or Postgres update — atomic + hot-reloads. These are `set` nodes (string assignment), so no `node --check` needed.
- **bridge:** scp system-prompt.md → `systemctl --user restart hermes-bridge` (health = port 8788) if it reads the prompt at startup.
- **Verify:** all 5 catalog blocks byte-identical post-sync; then send **1 test WhatsApp message** and confirm a draft quotes a watersport / Eva 60 correctly.
- **Rollback:** restore the backups (n8n re-PUT old export; mv system-prompt.md.bak back + restart). One step.
- **Timing:** daytime + test message per your protocol; 4-vCPU headroom now (writes are light anyway), batch them.

## Effort
B ≈ half-day (markers + generator + first run + drift-check). C ≈ 1–2h, no future protection. A ≈ 1+ day + ongoing runtime risk.
