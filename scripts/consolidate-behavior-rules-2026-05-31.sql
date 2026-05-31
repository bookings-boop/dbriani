-- Consolidate behavior_rules: 71 accumulated/contradictory edit_feedback rules
-- -> 28 canonical, de-duplicated, conflict-free rules (2026-05-31).
-- URL conflict resolved per operator: INCLUDE yacht website URLs (deactivates
-- the "never URLs / PDF-only" rule). Fully reversible: old rules are only
-- deactivated. Revert with:
--   UPDATE behavior_rules SET active=true WHERE created_via='edit_feedback'
--     AND id BETWEEN 91 AND 164;
--   UPDATE behavior_rules SET active=false WHERE created_via='consolidation_2026-05-31';
BEGIN;

UPDATE behavior_rules SET active=false
 WHERE active=true AND created_via='edit_feedback';

INSERT INTO behavior_rules (scope, rule_text, created_via, active) VALUES
('global', $$Keep WhatsApp drafts short, plain, and direct — one or two short lines per point, scannable. No filler or hype phrases (e.g. "sweet", "totally understand", "great news", "see you soon"), no exaggerated enthusiasm, and no emojis unless the customer uses them first.$$, 'consolidation_2026-05-31', true),
('global', $$Answer a customer's direct question plainly and immediately — no filler, and no deflecting follow-up question.$$, 'consolidation_2026-05-31', true),
('global', $$Ask one focused question at a time; collect the adult/guest count and yacht choice before quoting prices or discussing add-ons.$$, 'consolidation_2026-05-31', true),
('global', $$When the operator specifies exact options or exact wording, use only those — do not reword, reorder, substitute, or add alternatives.$$, 'consolidation_2026-05-31', true),
('global', $$Quote prices in AED only, formatted as "[Amount] AED" (the number before the code). Never reference GBP or other foreign-currency amounts.$$, 'consolidation_2026-05-31', true),
('global', $$State add-on prices as the price for the entire trip (e.g. "AED 2,000 for the trip"), not per hour — unless the item is genuinely per person.$$, 'consolidation_2026-05-31', true),
('global', $$Once a price has been communicated and accepted, do not repeat the total — confirm and move straight to the payment link.$$, 'consolidation_2026-05-31', true),
('global', $$Present yacht options cleanly: the primary option first, then "alternatively" for the next. Put each option on its own line as name + price + website URL, with no long descriptions, and end with one short call-to-action to choose.$$, 'consolidation_2026-05-31', true),
('global', $$When mentioning a specific yacht, include its website URL. Never claim a brochure, menu, or file is attached without actually including the link. When the operator says to send only a URL, send that URL alone.$$, 'consolidation_2026-05-31', true),
('global', $$Present location/navigation links on their own line with a plain text label and no emoji on the URL line.$$, 'consolidation_2026-05-31', true),
('global', $$When buying intent is clear or the price is agreed, send the payment link directly and without delay — just the link, with no booking summary, repeated total, or pleasantries. Suggest Apple Pay if available.$$, 'consolidation_2026-05-31', true),
('global', $$Never imply a booking is confirmed unless payment has been verified; when confirming an as-yet-unpaid booking, include the payment link.$$, 'consolidation_2026-05-31', true),
('global', $$Do not promise the host will "sort" add-ons — say the host will "help organize" them after the booking is confirmed.$$, 'consolidation_2026-05-31', true),
('global', $$When a conversation is clearly closed (e.g. the customer says thank you or goodbye), output only notes_for_zayn — do not draft a reply.$$, 'consolidation_2026-05-31', true),
('global', $$When a booking ends or a customer cancels or declines, keep the message short, neutral, and professional — no celebration, gladness, or event well-wishes. Acknowledge it and offer future help.$$, 'consolidation_2026-05-31', true),
('global', $$Keep apologies brief (a short sorry for a late reply, nothing more). If a manager failed to call a customer as promised, apologize and offer to proceed with the booking directly via WhatsApp.$$, 'consolidation_2026-05-31', true),
('global', $$In availability replies, state the facts directly and firmly (hours, price, yacht, constraints) — no apologies, blame, hype openers, or vague softening. When availability is scarce, state the high demand plainly; when something is unavailable, offer alternatives.$$, 'consolidation_2026-05-31', true),
('global', $$All Dubriani departures are from Dubai Harbour only — never mention or imply any other departure point (e.g. Business Bay).$$, 'consolidation_2026-05-31', true),
('global', $$Flowers and alcohol must be pre-ordered and delivered in advance — never imply they are available on board.$$, 'consolidation_2026-05-31', true),
('global', $$The VIP host is phone-based only, not physically onboard. Describe crew/host duties plainly, without analogies like "personal onboard assistant".$$, 'consolidation_2026-05-31', true),
('global', $$A 3-hour minimum is required to go around the Palm and see the Burj Al Arab — state this as a fact. If a booking's time is too short for the requested route, offer an extended or earlier start at a discounted rate before suggesting other yachts.$$, 'consolidation_2026-05-31', true),
('global', $$Never describe the SeaBob as a kids' activity or attach any age association to it.$$, 'consolidation_2026-05-31', true),
('global', $$Never mention the name "Zayn" in any customer-facing draft.$$, 'consolidation_2026-05-31', true),
('global', $$For multi-day charters, express the duration in hours (e.g. "24 hours", "48 hours"), not "2-day charter".$$, 'consolidation_2026-05-31', true),
('global', $$Sunseeker Satoshi 70 is offered at AED 2,000/hr (discounted from AED 3,000/hr).$$, 'consolidation_2026-05-31', true),
('global', $$The Élan 44 has a fixed price with no negotiation.$$, 'consolidation_2026-05-31', true),
('global', $$The SeaBob is available on the Satoshi only, at AED 1,200/hr or AED 2,000 for the full trip.$$, 'consolidation_2026-05-31', true),
('global', $$Add-on prices: bouquet of flowers AED 450; premium BBQ with private chef AED 295/person; fine dining with chef from AED 375/person.$$, 'consolidation_2026-05-31', true);

COMMIT;

SELECT count(*) FILTER (WHERE active) AS active_now,
       count(*) FILTER (WHERE active AND created_via='consolidation_2026-05-31') AS new_canonical,
       count(*) FILTER (WHERE NOT active AND created_via='edit_feedback') AS deactivated_old
  FROM behavior_rules;
