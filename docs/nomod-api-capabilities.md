# Nomod API — Capabilities Research

> Research findings for the Nomod payment-link integration. Documents what
> Nomod's API **actually supports** versus what the integration spec **wants**.
> Companion to `docs/nomod-integration-plan.md`.

**Researched:** 2026-05-22 · **Docs base:** https://nomod.com/docs/api-reference/introduction
**API base URL:** `https://api.nomod.com` · **Version prefix:** `/v1`
**Account:** PRODUCTION (real money) · **Currency for this integration:** AED only

---

## 0. TL;DR — three findings that reshape the integration

| # | What the spec assumed | What Nomod actually does | Impact |
|---|---|---|---|
| **A** | Nomod **webhooks** payment status to a bridge `/nomod-webhook` endpoint | **No webhooks exist.** The API reference has no webhooks page; Nomod's public feedback board confirms payment-status verification is "use the API" | Replace the webhook with a **polling worker**. No inbound public endpoint needed at all (a security win). |
| **B** | A test/sandbox account may exist | **No sandbox / test mode** is documented anywhere | Every test runs against the **production** account. Mitigations in the plan's testing section. |
| **C** | Editing the link amount re-generates the link | Update Link can change **only `status` and `title`** — **the amount is immutable** after creation | "Edit amount" = disable the old link + create a new one. Two API calls, not one. |

A fourth, separate correction (not Nomod's fault) is covered in the plan: **the live workflow does not call the bridge `/draft` endpoint** — it drafts via the `Claude AI` node directly to Anthropic. So the `should_send_payment` trigger fields must be emitted by that node's JSON, not by a bridge `/draft` response.

---

## 1. Authentication

- **Scheme:** API-key. Header **`X-API-KEY: <key>`** on *every* request.
- The key is generated (and revoked) in the **Nomod app settings**. One key per business per integration.
- Missing/invalid key → **HTTP 401** (`not_authenticated`).
- Changes made via the API are attributed to the business owner the key belongs to.
- **Security:** "If an unauthorized person gains access to your API key, they will be able to use your account and may create links." → Treat as a production secret: lives only in `~/hermes-bridge/.env` (`NOMOD_API_KEY`), gitignored, never in chat or workflow JSON.

Sample:
```
curl -X POST 'https://api.nomod.com/v1/links' \
  -H 'X-API-KEY: $NOMOD_API_KEY' \
  -H 'Content-Type: application/json' \
  -d '{ ... }'
```

## 2. Base URL, versioning, format

- RESTful, JSON in/out. Base URL `https://api.nomod.com`, all endpoints under `/v1`.
- No sandbox host. No separate test base URL.

## 3. Sandbox / test environment — **NONE**

Nothing in the docs describes a test mode, test keys, or a sandbox host. **All calls hit production.**
Mitigation (see plan §13): creating a Link costs nothing and moves no money — only a *completed card payment* does. So link-creation can be tested freely against production; only a true end-to-end "paid" test moves real money.

## 4. Create Link — `POST /v1/links`

The endpoint behind every payment link we send.

### Request body
| Field | Type | Req? | Notes |
|---|---|---|---|
| `currency` | string (ISO 4217, max 3) | **yes** | `"AED"`. Validate against `GET /v1/currencies`. |
| `items` | array | **yes** | ≥1 item. Each: `name`, `amount` (decimal **string** `"0.00"`), `quantity`. |
| `title` | string (≤50) | no | The link's name. |
| `note` | string (≤280) | no | Free-text description **shown to the customer**. |
| `discount_percentage` | int 0–100 | no | Structured discount. |
| `shipping_address_required` | bool | no | Leave false for charters. |
| `allow_tip` | bool | no | Leave false. |
| `custom_fields` | array (≤5) | no | Each `{name}` — prompts the customer to type a value. |
| `success_url` / `failure_url` | string (uri) | no | Post-payment redirects. |
| `allow_tabby` / `allow_tamara` | bool (default **true**) | no | Buy-now-pay-later methods. **Decision needed** — see plan. |
| `allow_service_fee` | bool (default **true**) | no | Whether Nomod's service fee applies. |
| `payment_expiry_limit` | int ≥1 | no | Auto-expire after N payments. |
| `expiry_date` | string `<date>` | no | Auto-expire at 23:59 of that date. **Only one of `payment_expiry_limit` / `expiry_date` may be set.** |

**Not available on link creation:** no `customer` field, no request-side `reference_id`, no `metadata`.
Consequences:
- **Customer name does not pre-fill** on the Nomod page. To show who it's for, put the name in `title`/`note`, or add a `custom_field` "Your name" (asks them to type it).
- **No idempotency key.** Two calls = two links. Duplicate prevention must be our own (the `payments` table — see plan §9).
- To correlate a payment back to a customer we rely on the **`link_id`** we store.

### Response (201)
```json
{
  "id": "<uuid>",                "// the link ID — store this"
  "reference_id": "string",      "// server-generated, read-only"
  "title": "string",
  "url": "https://...",          "// the payment link to send the customer"
  "amount": "0.00",              "// decimal string"
  "currency": "AED",
  "status": "enabled",           "// string enum: enabled | disabled"
  "discount": "0.00", "service_fee": "0.00", "tax": "0.00",
  "items": [ { "id": "<uuid>", "name": "...", "amount": "0.00",
               "total_amount": "0.00", "quantity": 1, "sku": "..." } ],
  "expiry_date": "<date>", "payment_expiry_limit": 1,
  "due_date": "<date-time>"
}
```
> The docs render `status` as a char-indexed object (`{"0":"e","1":"n",...}`) — a docs-rendering artifact. It is a **string** (`"enabled"`).

### Default expiry
The docs state **no default** expiry. If neither `expiry_date` nor `payment_expiry_limit` is sent, the link does not auto-expire. We will set `expiry_date` explicitly (plan §9, decision #1).

## 5. Payment page — what the customer sees

Built from the Create Link fields: `title`, `note`, the `items` (name/amount/quantity), total `amount` + `currency`, optional tip, optional shipping address, `custom_fields`, Tabby/Tamara options, and the service fee.

- **Customer name:** not pre-filled (no `customer` on links).
- **Booking description:** shown via `note` (≤280 chars).
- **"Special offer" / discount:** `discount_percentage` produces a structured discount the customer sees. There is no free-text "special offer" badge — discount framing otherwise goes in `note` text.

## 6. Link lifecycle

| Action | Endpoint | Notes |
|---|---|---|
| Retrieve | `GET /v1/links/{id}` | Same shape as Create response — read `status`, `expiry_date`. |
| Update | `PATCH /v1/links/{id}/edit` | **Body accepts only `status` (`enabled`\|`disabled`) and `title`.** Amount/items **cannot** be edited. |
| Delete | `DELETE /v1/links/{id}/delete` | "Archive the link." |

**Implication for "Edit amount":** disable the old link (`PATCH status=disabled`) and **create a new link**. The amount is fixed at creation.

## 7. Charges (payments) — how we learn a link was paid

There is no webhook, so payment status is **pulled**:

| Action | Endpoint | Notes |
|---|---|---|
| List charges | `GET /v1/charges` | Query params include **`link_id`**, `status`, `currency`, `customer_id`, `type`, `page`, `page_size`. |
| Get charge | `GET /v1/charges/{id}` | Full charge: `status`, `total`, `currency`, `refund_total`, `payment_method`, `events`, `error`. |

**Polling model:** for each open link, call `GET /v1/charges?link_id=<id>`. No charge → unpaid. A charge present → a payment was attempted/made; inspect its `status`.

⚠️ **The exact charge `status` enum is not cleanly documented** — the docs example renders it as a char array (spelling `"authorised"`). Candidate completed-payment values: `captured` / `succeeded` / `paid` / `authorised`. **This must be confirmed empirically with one real test payment before go-live** (plan §13, open question Q-C). Safe default until confirmed: treat any returned charge with a non-zero `total` and no failure `error` as *paid-pending-confirmation* and **route to the operator** rather than auto-confirming.

## 8. Refunds

| Action | Endpoint | Notes |
|---|---|---|
| Create refund | `POST /v1/checkout/{id}/refund` | Body: `amount` (integer, major unit, min 1), optional `reason`, `reference_id`, `metadata`. |
| Refund a charge | `POST /v1/charges/.../refund` (`refund-charge` in nav) | Charge-scoped variant. |

Refund `status`: `pending` \| `completed` \| `failed`. Refunds **are** API-supported — enough to add a `/refund` operator command later (Phase 2). Phase 1 keeps refunds in the Nomod dashboard (decision #7).

## 9. Webhooks — **NOT AVAILABLE**

- The full API-reference navigation (30 endpoints) has **no webhooks page**. Endpoints cover Links, Invoices, Charges, Checkout, Refunds, Contacts, Customers, Team, Lookup data — nothing for event subscriptions or callbacks.
- Nomod's public feedback board has a post **"Checkout API and Payment Status Verification"** (feedback.nomod.com) asking *"how should we verify payment status — is a webhook available or should we use an API?"* — marked **Completed**, i.e. the answer is the existing **Charges API**.

**Conclusion:** there is no payment-status push. The integration must **poll** `GET /v1/charges`. The spec's `/nomod-webhook` bridge endpoint is dropped; a polling worker replaces it. **Upside:** nothing external needs to reach the bridge — the integration adds **no new public attack surface**.

## 10. Rate limits & errors

- **Rate limits:** "We have throttling limits in place… contact us." **No published numbers.** `429` (`throttled`) on breach → the client must back off (exponential, jittered) and, on repeated failure, fail to the operator.

- **HTTP error codes:** `401` invalid key · `403` forbidden · `404` not found · `429` throttled · `500` server error · `503` unavailable (deploy in progress / Nomod down).

- **Semantic error codes** (in the JSON body): `not_authenticated`, `throttled`, `not_found`, `field_required`, `invalid_amount` ("amount must be greater than zero and within limits"), `invalid_currency`, `missing_required_items`, `exceeds_item_limit`, `invalid_status`, `already_deleted`, `invalid_refund_amount`, `refund_amount_exceeds`, `charge_not_paid`, `charge_already_refunded`.

- **Error body shape:** `{ "error": { "message": "string", "code": "string" } }`.

Handling summary (full matrix in plan §11):

| Response | Meaning | Action |
|---|---|---|
| `401` | Bad/expired key | Alert operator immediately, halt payment feature, do **not** retry. |
| `403` | Insufficient rights | Alert operator; halt. |
| `400` + `invalid_amount`/`invalid_currency` | Bad request data | Do not retry; surface to operator with the field. |
| `429` | Throttled | Backoff + retry; if still failing, route to operator. |
| `500`/`503` | Nomod-side | Retry with backoff; if persistent, route to operator, leave draft text-only. |

## 11. Pricing

- The API docs list **no per-link or per-API-call fee** — creating a link is free.
- Nomod's revenue is a **per-transaction processing fee** on completed payments (visible in the charge response as `service_fee`, `fee`, `fx_fee`, `network_cost`). The exact percentage is set by the Nomod **account's pricing plan**, not the API.
- **Action:** confirm the live account's processing-fee rate and whether `allow_service_fee` passes that fee to the customer — in the Nomod dashboard / contract (plan open question Q-E).

## 12. Endpoint quick-reference

| Need | Method + path |
|---|---|
| Create payment link | `POST /v1/links` |
| Get link (status/expiry) | `GET /v1/links/{id}` |
| Disable / rename link | `PATCH /v1/links/{id}/edit` |
| Archive link | `DELETE /v1/links/{id}/delete` |
| List charges (by `link_id`) | `GET /v1/charges` |
| Get one charge | `GET /v1/charges/{id}` |
| Refund | `POST /v1/checkout/{id}/refund` |
| Valid currencies | `GET /v1/currencies` |

## 13. Sources

- Introduction — https://nomod.com/docs/api-reference/introduction
- Authentication — https://nomod.com/docs/api-reference/authentication
- Requests & Responses (errors) — https://nomod.com/docs/api-reference/requests-responses
- Rate Limits — https://nomod.com/docs/api-reference/rate-limits
- Create Link — https://nomod.com/docs/api-reference/generate-link
- Get / Update / Delete Link — https://nomod.com/docs/api-reference/{retrieve,edit,delete}-link
- List / Get Charge — https://nomod.com/docs/api-reference/{fetch-charges,retrieve-charge}
- Create Refund — https://nomod.com/docs/api-reference/create-refund
- Get Currencies — https://nomod.com/docs/api-reference/list-currency
- Feedback (no webhook) — https://feedback.nomod.com/p/checkout-api-and-payment-status-verification
