# Node Tank Telemetry

Domain glossary for `node-tank`'s per-tank sensor telemetry: what each
published field *means*, independent of the Python that computes it.

## Language

**Fill rate / Drain rate**:
The instantaneous speed (liters/minute) at which a tank's level is
currently rising or falling, computed edge-to-edge from consecutive
qualifying level readings. Resets to `0` once flow stops (idle
timeout). A speedometer, not an odometer — it never expresses a total.
_Avoid_: Flow rate (ambiguous — doesn't say instantaneous vs. total),
consumption rate.

**Latch commit**:
The one-shot event where a tank's level has sustained inside the
full-band or empty-band (past `full_threshold_pct`/`empty_threshold_pct`,
held for the debounce delay) long enough to count as genuinely full or
empty, not sensor noise. Fires once per excursion into the band;
re-arms only after the level leaves the band.
_Avoid_: Threshold crossing (loses the debounce/one-shot nature),
full/empty event.

**Volume since full / Volume since empty**:
The liters moved since the tank's last full/empty latch commit — a
running total (net delta between the level at that commit and the
current level), distinct from fill/drain rate. Answers "how much have
I used" (fresh) or "how much has accumulated" (grey); fill/drain rate
answers "how fast, right now."
_Avoid_: Consumed liters (fresh-specific framing — the field itself is
tank-role-agnostic; the dashboard decides what "consumed" vs.
"accumulated" means per tank).

**`_at` vs. `_date` field naming**:
`_at` fields (`last_full_at`, `last_empty_at`, `last_inspected_at`) hold
a full instant (ISO-8601 with local UTC offset) — when precision or
auto-detection is involved. `_date` is reserved for genuine
calendar-date-only concepts with no time component. Every timestamp
field in this node's schema uses `_at`; none currently use `_date`.
_Avoid_: Using `_date` for anything that isn't calendar-date-only —
it silently collapses same-day repeat events into one indistinguishable
value.
