---
name: delivery-zone-rules
description: Zone surcharges, cutoff times and refund thresholds for delivery pricing.
namespaces: [delivery]
requires:
  env: []
---

# Delivery zone rules

Only the `description` above reaches the system prompt - about one line. The model reads
this body with `skill_view` when it decides it needs it.

That is the whole optimization: fifty skills cost roughly 5K characters of prompt instead
of 200K. If a body ever reaches the prompt, it has been silently undone.

## Zones

| Zone | Surcharge | Cutoff |
|------|-----------|--------|
| A - central | none | 22:00 |
| B - ring | +8% | 21:00 |
| C - outer | +15% | 20:00 |

## Refund thresholds

- Late under 15 min: no refund, apology only.
- Late 15-40 min: refund the surcharge.
- Late over 40 min: full delivery fee refunded.

## What this skill does NOT decide

Whether to APPLY a price change. That is `pricing_apply`, and above 15% it requires a
human. This skill tells you what the rules are; the policy decides who may act on them.
