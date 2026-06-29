# CRM Schema — Google Sheets

8 tabs connected by `lead_id`. Setup script: `scripts/google-sheet-setup.gs`.

| Tab | Purpose | Columns | Agent writes? |
|-----|---------|---------|---------------|
| **Pipeline** | Formula-driven dashboard for humans | 12 | No — auto-populated |
| **Leads** | Identity, location, pipeline status, contact | 19 | Yes |
| **Intel** | Research outputs + 3 core commercial variables | 12 | Yes |
| **Outreach** | Draft, QA, approval, send, follow-up, reply | 20 | Yes |
| **Send Log** | One row per email sent (append-only) | 16 | Yes (append) |
| **Config** | Timing and limit settings | 5 cols | Rarely |
| **Clusters** | Territory health and mycelium sourcing state | 21 | Yes |
| **Archive** | Terminal leads moved from Leads | 21 | Yes (move) |

---

## Template Variables

These are the variables available for email template substitution. All must be resolved before QA.

| Variable | Source | Example |
|----------|--------|---------|
| `{contact_name}` | Leads.contact_name | "Tony DiMarco" |
| `{business_name}` | Leads.company_name | "Tony's Tavern" |
| `{city_state}` | Leads.city + Leads.state | "Chicago, IL" |
| `{businesses_helped}` | Config tab | "200+" |
| `{sender_name}` | Config / routing | "Gabriella Green" |
| `{sender_company}` | Config | "Green PayTech" |
| `{company_url}` | Config | "example.io" |

---

## Pipeline Tab — 12 Columns (Formula-Driven)

Auto-updates from Leads, Intel, and Outreach. Only shows active leads. **Do not edit this tab.**

| # | Column | Source | Notes |
|---|--------|--------|-------|
| 1 | `lead_id` | Leads | FILTER (excludes terminal stages) |
| 2 | `company_name` | Leads | INDEX/MATCH on lead_id |
| 3 | `stage` | Leads | INDEX/MATCH on lead_id |
| 4 | `vertical` | Leads | INDEX/MATCH on lead_id |
| 5 | `location_count` | Leads | INDEX/MATCH on lead_id |
| 6 | `contact_name` | Leads | INDEX/MATCH on lead_id |
| 7 | `key_signal` | Intel | INDEX/MATCH on lead_id |
| 8 | `approval_status` | Outreach | INDEX/MATCH on lead_id |
| 9 | `reply_status` | Outreach | INDEX/MATCH on lead_id |
| 10 | `days_in_stage` | Calculated | TODAY() - last_stage_change |
| 11 | `next_action` | Calculated | Formula based on stage |
| 12 | `priority` | Leads | INDEX/MATCH icp_tier on lead_id |

### `next_action` formula logic

| Stage | Next Action |
|-------|-------------|
| `new` | "Research needed" |
| `researching` | "Research in progress" |
| `researched` | "Ready to draft" |
| `pending_approval` | "Review draft in #approvals" |
| `approved` | "Ready to send" |
| `sent` | "Awaiting auto-transition" |
| `awaiting_reply` | "Waiting for reply" |
| `replied` | "Triage reply — decide next step" |
| `qualified` | "Ready for handoff" |

---

## Leads Tab — 19 Core Columns

The master record. Every lead has one row.

### Identity (A-I)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| A | `lead_id` | string | Orchestrator |
| B | `company_name` | string | Manual / Orchestrator |
| C | `domain` | string | Manual / Orchestrator |
| D | `city` | string | Research step / Manual |
| E | `state` | string | Research step / Manual |
| F | `vertical` | enum: `restaurant`, `pharmacy`, `car_dealership`, `ecommerce`, `services`, `multi_brand`, `other` | Research step |
| G | `sub_vertical` | string | Research step |
| H | `location_count` | integer | Research step |
| I | `icp_tier` | enum: `tier_1`, `tier_2`, `tier_3` | Research step |

### Pipeline (J-L)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| J | `stage` | enum (12 values — see schema/stages.md) | Orchestrator |
| K | `last_stage_change` | datetime | Orchestrator |
| L | `created_at` | datetime | Orchestrator |

### Contact (M-O)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| M | `contact_name` | string | Research / enrichment |
| N | `contact_title` | string | Research / enrichment |
| O | `contact_email` | string | Research / enrichment |

### Notes + Runtime (P-S)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| P | `notes` | text | Human / Orchestrator |
| Q | `contact_confidence` | enum: `HIGH`, `MEDIUM`, `LOW`, `UNVERIFIED` | Runtime / enrichment |
| R | `gate_fail_reason` | string | Runtime gate wrapper |
| S | `referenceable` | enum: `yes`, `no`, `not_asked` | Human / handoff |

Production Sheets may also include `contactability_score` after `notes`. This is an enrichment quality score, not an email verifier result:

- `5` = named decision maker plus verified email
- `4` = named likely decision maker plus risky/catch-all verified email
- `3` = named person but no verified email
- `2` = generic inbox only
- `1` = phone/contact form only
- `0` = no contact path

Name verified = a real person or decision-maker has been identified. Email confirmed = the address passed verification. Do not treat a named contact as send-ready unless `contact_email` also has valid verifier evidence.

`referenceable` controls whether a closed customer can be named in outreach. Default to `not_asked`. Do not name any customer unless `referenceable=yes`.

---

## Intel Tab — 12 Columns

Research findings and the 3 core commercial variables. One row per lead, keyed by `lead_id`.

| Col | Column | Type | Writer | Notes |
|-----|--------|------|--------|-------|
| A | `lead_id` | string | Orchestrator | Must match a row in Leads |
| B | `company_name` | string | **Formula** | Auto-populated from Leads. Do not edit. |
| C | `observed_processors` | text | Research step | e.g., "Toast POS, Square Online, DoorDash" |
| D | `observed_channels` | text | Research step | e.g., "in-store, online ordering, delivery" |
| E | `research_summary` | text | Research step | Compressed narrative of findings + gaps |
| F | `research_refreshed_at` | datetime | Research step | Used for stale detection |
| G | `key_signal` | text | Variable step | Single most important observation |
| H | `pain_hypothesis` | text | Variable step | One plausible operational problem |
| I | `hypothesis_confidence` | enum: `HIGH`, `MEDIUM`, `LOW`, `UNVERIFIED` | Variable step | |
| J | `recommended_angle` | text | Variable step | What to lead with in the email |
| K | `trigger_score` | integer | Research step | Composite score from trigger signals (see agents/research.md) |
| L | `trigger_type` | text | Research step | Primary trigger: expansion, refresh, ownership, digital, hiring, pain |

### The 3 Core Variables

These three fields are **required** before drafting. Everything else is optional evidence.

1. **`key_signal`** — The strongest observation about their payment environment. Synthesized from observed processors and channels. Written as a readable phrase: "12 locations on Toast POS with DoorDash and UberEats delivery, plus a separate Shopify gift card store." Not a list.

2. **`pain_hypothesis`** — A plausible operational problem created by payment complexity. One hypothesis, stated as a possibility. Must be supported by research at confidence ≥ MEDIUM.

3. **`recommended_angle`** — What to lead with in the email. A concrete, helpful suggestion or observation — not a sales pitch. "Start with a side-by-side view of your delivery commissions across platforms" not "We can optimize your payments."

### Confidence Tags

| Tag | Definition | Use in emails? |
|-----|-----------|----------------|
| **HIGH** | Multiple corroborating sources | Yes — state as observation |
| **MEDIUM** | Single credible source | Yes — hedge ("it looks like...") |
| **LOW** | Inference from patterns | No — flag for human review |
| **UNVERIFIED** | No evidence | Never — blocks QA |

---

## Outreach Tab — 20 Columns

The full lifecycle of outbound contact. One row per lead.

### Draft (C-F)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| A | `lead_id` | string | Orchestrator |
| B | `company_name` | string | **Formula** |
| C | `draft_subject` | string | Drafting step |
| D | `draft_body` | text | Drafting step |
| E | `sequence_step` | integer (1-4) | Orchestrator |
| F | `personalization_proof` | text | Drafting step |

### QA & Approval (G-J)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| G | `qa_result` | enum: `PASS`, `FAIL`, `pending` | QA step |
| H | `approval_status` | enum: `pending`, `approved`, `rejected`, `revised` | Human via #approvals |
| I | `approved_by` | string | Orchestrator |
| J | `approved_at` | datetime | Orchestrator |

### Send (K-M)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| K | `send_status` | enum: `not_sent`, `sent`, `failed` | Orchestrator (Gmail) |
| L | `sent_at` | datetime | Orchestrator |
| M | `gmail_message_id` | string | Gmail API response |

### Follow-up (N-O)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| N | `followup_count` | integer (0-3) | Orchestrator |
| O | `next_followup_date` | date | Orchestrator |

### Reply (P-R)

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| P | `reply_status` | enum (9 values) | Reply triage step |
| Q | `reply_summary` | text | Reply triage step |
| R | `reply_received_at` | datetime | Orchestrator |
| S | `gmail_thread_id` | string | Runtime / Gmail |
| T | `send_hold_reason` | string | Runtime gate wrapper |

**Reply status values:** `none`, `interested`, `objection`, `not_interested`, `unsubscribe`, `out_of_office`, `referral`, `ambiguous`, `bounce`

---

## Send Log Tab — 16 Columns

One row per outbound email (including follow-ups). Append-only — never update existing rows.

| Col | Column | Type |
|-----|--------|------|
| A | `send_id` | string |
| B | `lead_id` | string |
| C | `company_name` | string |
| D | `sequence_step` | integer (1-4) |
| E | `subject` | string |
| F | `body` | text |
| G | `pain_hypothesis_used` | text |
| H | `variables_used` | text |
| I | `personalization_proof` | text |
| J | `sent_at` | datetime |
| K | `approved_by` | string |
| L | `reply_received` | boolean |
| M | `reply_class` | enum (9 values) |
| N | `notes` | text |
| O | `gmail_thread_id` | string |
| P | `send_provider` | enum: `gmail`, `ses`, `listmonk` |

---

## Config Tab — Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `daily_send_cap` | 5 | Max emails per day |
| `warm_up_week` | 1 | Current warm-up week |
| `followup_day_1` | 3 | Days after send for follow-up 1 |
| `followup_day_2` | 7 | Days after send for follow-up 2 |
| `followup_day_3` | 14 | Days after send for follow-up 3 |
| `max_followups` | 3 | Max follow-ups before closing |
| `approval_stale_hours` | 48 | Flag approved-but-unsent |
| `pending_nudge_hours` | 24 | Nudge pending in #approvals |
| `pending_escalate_hours` | 72 | Escalate pending to #agent-main |
| `pending_archive_days` | 14 | Archive stale pending_approval |
| `research_stale_days` | 14 | Research needs refresh |
| `archive_after_days` | 60 | Move terminal leads to Archive |
| `sent_to_awaiting_hours` | 48 | Auto-advance sent → awaiting_reply |
| `replied_stale_hours` | 4 | Flag untriaged replies |
| `target_seed_cities` | blank | Comma-separated city/neighborhood seeds for territory research |
| `cluster_radius_profile` | `small_city` | `urban_core`, `dense_suburb`, `small_city`, or `sparse_regional` |
| `cluster_fresh_radius_mi` | 2.0 | Starting radius for new clusters |
| `cluster_active_radius_mi` | 2.0 | Radius while a cluster has no strong signal yet |
| `cluster_fertile_radius_mi` | 5.0 | Radius after reply signal makes a cluster fertile |
| `cluster_warm_1_close_radius_mi` | 10.0 | Radius after 1 closed deal |
| `cluster_warm_3_close_radius_mi` | 15.0 | Radius after 3+ closed deals |
| `cluster_fertile_threshold` | 0.08 | Reply rate to promote active to fertile |
| `cluster_exhausted_min_age_days` | 30 | Minimum age before exhaustion |
| `cluster_exhausted_max_response_rate` | 0.05 | Low reply threshold for exhaustion |
| `cluster_anastomosis_distance_mi` | 2.0 | Distance where similar clusters may merge |
| `cluster_max_radius_mi` | 15.0 | Hard cap for default small-city profile |
| `seed_min_leads_before_outreach` | 5 | Minimum qualified leads before outreach |
| `weekly_cluster_report_day` | `Monday` | Day for weekly territory report |
| `source_rotation_mode` | `balanced_by_cluster` | How research budget rotates across sources/geographies |

---

## Clusters Tab — 21 Columns

Territory state for mycelium sourcing and the morning/weekly territory report.

| Col | Column | Type |
|-----|--------|------|
| A | `cluster_id` | string |
| B | `cluster_name` | string |
| C | `city` | string |
| D | `state` | string |
| E | `vertical` | enum |
| F | `geo_center_lat` | float |
| G | `geo_center_long` | float |
| H | `radius_mi` | float |
| I | `lead_count` | integer |
| J | `contacted_count` | integer |
| K | `replied_count` | integer |
| L | `reply_rate` | float or percent string |
| M | `meetings_booked` | integer |
| N | `deals_closed` | integer |
| O | `status` | fresh / active / fertile / warm / exhausted |
| P | `dominant_pain` | text |
| Q | `dominant_processor` | text |
| R | `next_spread_target` | text |
| S | `cross_cluster_insight` | text |
| T | `created_at` | datetime |
| U | `last_activity_at` | datetime |

---

## Archive Tab — 21 Columns

Same 16 columns as Leads, plus:

| Col | Column | Type | Writer |
|-----|--------|------|--------|
| Q | `archived_at` | datetime | Orchestrator |
| R | `terminal_reason` | text | Orchestrator / Human |

When a lead in a terminal state passes `archive_after_days`, copy here and delete from Leads.

---

## Cross-Tab Connections

All tabs linked by `lead_id` (column A on every tab).

**Agent write rules:**
- Creating a lead → write to Leads (row) + Intel (row) + Outreach (row) with matching `lead_id`
- Research update → write to Intel tab
- Draft/approval/send/reply → write to Outreach tab
- Stage change → write to Leads tab (column J)
- Send → append to Send Log
- Column B on Intel and Outreach is a formula — never overwrite it

---

## Reply Matching

Match inbound replies to leads in this order:
1. Thread ID match (`gmail_message_id`) — best
2. Sender email match to `contact_email` in Leads
3. Sender domain match to `domain` in Leads
4. Fuzzy company name match
5. No match → post to #agent-main for human routing

---

## Send Warm-Up Schedule

| Week | Daily Cap |
|------|-----------|
| 1 | 5 |
| 2 | 10 |
| 3 | 15 |
| 4 | 20 |
| 5+ | +5/week |

---

## Dedup Rules

At `new` stage, check Leads tab:
1. Normalize company name (lowercase, strip Inc/LLC/Corp)
2. Match by `domain` or normalized name
3. Active match → block, flag in #agent-main
4. Terminal match → allow, note prior engagement
