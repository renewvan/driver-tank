# Driver Tank Telemetry

Domain glossary for `driver-tank`'s per-tank sensor telemetry: what each
published field *means*, independent of the Python that computes it.

## Language

**Level sensor**:
The hardware in a tank that tells the driver how full it is. Each tank
has exactly one, of one of three kinds: **Sender**, **Voltage sensor**
or **Level probes**. The kind is a wiring fact, chosen per tank.
_Avoid_: Gauge (the display, not the sensor), resistive sensor (only
one of the three kinds).

**Sender**:
A continuous resistive level sensor (float or arm) whose resistance
tracks the level between an empty and a full value (e.g. 0–190 Ω EU,
240–33 Ω US). Gives a continuous level.
_Avoid_: Resistive sensor, gauge.

**Voltage sensor**:
A continuous level sensor that outputs a voltage proportional to the
level (e.g. a capacitive electrode with 0–2.5 V output). Gives a
continuous level. "Capacitive" describes how it senses, not what the
driver reads.
_Avoid_: Capacitive sensor (as a type name).

**Level probes**:
Conductive rods or screws at fixed heights; water bridging a probe to
the common rod marks the level as at least that height. Each probe is
a wet/dry point, so the level is known only in steps between the
probes' heights (e.g. a CBE 4-rod fresh probe, or a single pair of
screw probes at a grey tank's full mark). An open circuit on a dry
probe is the normal reading, not a fault.
_Avoid_: Resistive sensor, digital sensor, switch.

**Level mark**:
The volume % that one of a tank's **Level probes** stands for: its tip
height, corrected for the tank's shape. A tank's marks are the only
levels it can report besides 0.
_Avoid_: Step, threshold (threshold means alarm/full/empty bands).

**Stepped level**:
The level of a tank read by **Level probes**: the **Level mark** of the
highest wet probe, or 0 when all are dry. It is a lower bound ("at
least this full"), never an estimate of the true level.
_Avoid_: Approximate level, band midpoint.

**Latch commit**:
The one-shot event where a tank's level has sustained inside the
full-band or empty-band (past `full_threshold_pct`/`empty_threshold_pct`,
held for the debounce delay) long enough to count as genuinely full or
empty, not sensor noise. Fires once per excursion into the band;
re-arms only after the level leaves the band. On a **Stepped level**
it commits only when the band is certain: full when a probe at or
above the full threshold is wet; empty only if a probe sits at or
below the empty threshold.
_Avoid_: Threshold crossing (loses the debounce/one-shot nature),
full/empty event.

**Volume since full / Volume since empty**:
The liters moved since the tank's last full/empty latch commit — a
running total (net delta between the level at that commit and the
current level), distinct from fill/drain rate. Answers "how much have
I used" (fresh) or "how much has accumulated" (grey); fill/drain rate
answers "how fast, right now." Always 0 on a **Stepped level**: steps
between marks are not a measured volume.
_Avoid_: Consumed liters (fresh-specific framing — the field itself is
tank-role-agnostic; the dashboard decides what "consumed" vs.
"accumulated" means per tank).

**`_at` vs. `_date` field naming**:
`_at` fields (`last_full_at`, `last_empty_at`, `last_inspected_at`) hold
a full instant (ISO-8601 with local UTC offset) — when precision or
auto-detection is involved. `_date` is reserved for genuine
calendar-date-only concepts with no time component. Every timestamp
field in this driver's schema uses `_at`; none currently use `_date`.
_Avoid_: Using `_date` for anything that isn't calendar-date-only —
it silently collapses same-day repeat events into one indistinguishable
value.
