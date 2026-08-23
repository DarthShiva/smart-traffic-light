"""
simulation.py
-------------
A discrete-event traffic model used for two things:

  1. simulation / test mode - run the controller logic against synthetic
     traffic at high speed, without touching live state and without ever
     writing to the serial port;
  2. the "fixed timer vs adaptive" benchmark shown on the dashboard.

WHAT THE BENCHMARK ACTUALLY MEASURES
====================================
Both strategies are run over the SAME pre-generated arrival stream (same
seed, same per-lane Poisson arrivals, same saturation flow, same all-red
clearance), so the only difference between them is how long each green
lasts:

    fixed      every lane gets FIXED_TIMER_GREEN_SEC of green, always
    adaptive   traffic_logic.green_time(queue at the start of the phase)

Both rotate round robin in the same order, because that is the algorithm
this project implements - the benchmark is not allowed to quietly use a
different controller from the live one, so it calls the same
traffic_logic functions the live runner calls.

The measured quantity is CUMULATIVE WAITING TIME in vehicle-seconds:
every simulated second, every vehicle still queued contributes one
vehicle-second. Lower is better.

    pct_saved = (fixed - adaptive) / fixed * 100

These are MODEL numbers, not measurements taken from the live video
feeds, and the dashboard labels them as such. Nothing here is fabricated:
re-running with the same parameters reproduces the same figures exactly.
"""
from __future__ import annotations

import random
import time
from typing import Dict, List, Optional, Sequence

import traffic_logic
from constants import ALL_RED_SEC, FIXED_TIMER_GREEN_SEC, LANES

# Simulation resolution. 0.5s divides every duration this system can
# produce (10 + 0.5*n) exactly, so no phase is ever rounded.
STEP_SEC = 0.5

# Vehicles discharged per second of green from the lane that has the
# green. 0.5 veh/s == 1800 veh/h, a standard single-lane saturation flow.
DEFAULT_DISCHARGE_RATE = 0.5

# Vehicles per minute per lane. Chosen to sit below the intersection's
# capacity (4 lanes sharing one 0.5 veh/s server) so the model stays in a
# regime where queues are bounded; an oversaturated intersection queues
# without limit under ANY strategy and the comparison stops meaning
# anything.
DEFAULT_ARRIVAL_RATES = {"North": 6.0, "East": 3.0, "South": 8.0, "West": 2.0}

STRATEGY_FIXED = "fixed"
STRATEGY_ADAPTIVE = "adaptive"


def _clamp(value: object, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return max(low, min(high, number))


def build_arrivals(rates: Dict[str, float], duration_sec: float, seed: int,
                   lanes: Sequence[str] = LANES) -> List[Dict[str, int]]:
    """One list entry per simulation step: {lane: vehicles arriving}.

    Generated once and replayed for every strategy, which is what makes
    the comparison a controlled experiment rather than two unrelated runs.
    """
    rng = random.Random(seed)
    steps = int(round(duration_sec / STEP_SEC))
    arrivals = []
    per_step = {lane: max(0.0, rates.get(lane, 0.0)) / 60.0 * STEP_SEC for lane in lanes}
    for _ in range(steps):
        arrivals.append({lane: _poisson(rng, per_step[lane]) for lane in lanes})
    return arrivals


def _poisson(rng: random.Random, mean: float) -> int:
    """Knuth's algorithm. Means here are well below 1, so it terminates
    after a couple of iterations."""
    if mean <= 0:
        return 0
    limit = 2.718281828459045 ** -mean
    k, product = 0, 1.0
    while True:
        product *= rng.random()
        if product <= limit:
            return k
        k += 1
        if k > 200:  # unreachable for sane rates; guards against a bad input
            return k


def run(strategy: str,
        arrivals: List[Dict[str, int]],
        discharge_rate: float = DEFAULT_DISCHARGE_RATE,
        fixed_green_sec: float = FIXED_TIMER_GREEN_SEC,
        all_red_sec: float = ALL_RED_SEC,
        lanes: Sequence[str] = LANES,
        trace_limit: int = 40) -> dict:
    """Run one strategy over a pre-built arrival stream.

    Pure function: no shared state, no serial output, no HTTP.
    """
    lanes = list(lanes)
    queues = {lane: 0 for lane in lanes}
    served = {lane: 0 for lane in lanes}
    arrived = {lane: 0 for lane in lanes}
    green_seconds = {lane: 0.0 for lane in lanes}
    phase_counts = {lane: 0 for lane in lanes}

    cumulative_wait = 0.0
    max_queue = 0
    trace: List[dict] = []

    active: Optional[str] = None
    phase_remaining = 0.0
    in_all_red = True
    phase_remaining = all_red_sec
    discharge_credit = 0.0

    for step_arrivals in arrivals:
        for lane in lanes:
            n = step_arrivals.get(lane, 0)
            queues[lane] += n
            arrived[lane] += n

        if phase_remaining <= 0:
            if in_all_red:
                nxt = traffic_logic.next_lane(active, lanes)
                if strategy == STRATEGY_ADAPTIVE:
                    duration = traffic_logic.green_time(queues[nxt])
                else:
                    duration = float(fixed_green_sec)
                active = nxt
                in_all_red = False
                phase_remaining = duration
                discharge_credit = 0.0
                phase_counts[active] += 1
                if len(trace) < trace_limit:
                    trace.append({
                        "phase": len(trace) + 1,
                        "lane": active,
                        "queue_at_start": queues[active],
                        "green_sec": round(duration, 1),
                    })
            else:
                in_all_red = True
                phase_remaining = all_red_sec

        if not in_all_red and active is not None:
            green_seconds[active] += STEP_SEC
            discharge_credit += discharge_rate * STEP_SEC
            leaving = min(queues[active], int(discharge_credit))
            if leaving > 0:
                queues[active] -= leaving
                served[active] += leaving
                discharge_credit -= leaving

        total_queued = sum(queues.values())
        cumulative_wait += total_queued * STEP_SEC
        max_queue = max(max_queue, total_queued)
        phase_remaining -= STEP_SEC

    total_arrived = sum(arrived.values())
    total_served = sum(served.values())
    total_phases = sum(phase_counts.values())
    total_green = sum(green_seconds.values())

    return {
        "strategy": strategy,
        "cumulative_wait_veh_sec": round(cumulative_wait, 1),
        "avg_wait_per_vehicle_sec": round(cumulative_wait / total_arrived, 2) if total_arrived else 0.0,
        "vehicles_arrived": total_arrived,
        "vehicles_served": total_served,
        "vehicles_remaining": sum(queues.values()),
        "max_total_queue": max_queue,
        "green_phases": total_phases,
        "avg_green_sec": round(total_green / total_phases, 1) if total_phases else 0.0,
        "per_lane": {lane: {"arrived": arrived[lane], "served": served[lane],
                            "queued": queues[lane], "green_sec": round(green_seconds[lane], 1),
                            "phases": phase_counts[lane]} for lane in lanes},
        "trace": trace,
    }


def benchmark(seed: int = 1,
              duration_sec: float = 900.0,
              arrival_rates: Optional[Dict[str, float]] = None,
              discharge_rate: float = DEFAULT_DISCHARGE_RATE,
              fixed_green_sec: float = FIXED_TIMER_GREEN_SEC,
              lanes: Sequence[str] = LANES) -> dict:
    """Run both strategies over one shared arrival stream and compare.

    The returned dict is what the dashboard's `comparison` block renders.
    """
    lanes = list(lanes)
    seed = int(_clamp(seed, 0, 2 ** 31 - 1, 1))
    duration_sec = _clamp(duration_sec, 60.0, 7200.0, 900.0)
    discharge_rate = _clamp(discharge_rate, 0.05, 5.0, DEFAULT_DISCHARGE_RATE)
    fixed_green_sec = _clamp(fixed_green_sec, 5.0, 120.0, FIXED_TIMER_GREEN_SEC)

    rates_in = arrival_rates if isinstance(arrival_rates, dict) else DEFAULT_ARRIVAL_RATES
    rates = {lane: _clamp(rates_in.get(lane, DEFAULT_ARRIVAL_RATES.get(lane, 0.0)),
                          0.0, 240.0, 0.0) for lane in lanes}

    arrivals = build_arrivals(rates, duration_sec, seed, lanes)
    fixed = run(STRATEGY_FIXED, arrivals, discharge_rate, fixed_green_sec, ALL_RED_SEC, lanes)
    adaptive = run(STRATEGY_ADAPTIVE, arrivals, discharge_rate, fixed_green_sec, ALL_RED_SEC, lanes)

    wait_fixed = fixed["cumulative_wait_veh_sec"]
    wait_adaptive = adaptive["cumulative_wait_veh_sec"]
    if wait_fixed > 0:
        pct_saved = round((wait_fixed - wait_adaptive) / wait_fixed * 100.0, 1)
    else:
        # No queueing under either strategy: nothing was saved, and nothing
        # was lost. Reporting 100% here would be a fabricated result.
        pct_saved = 0.0

    # Primary metrics are vehicle COUNTS straight out of the model - no
    # conversion from seconds, no relabelling. Both strategies faced the
    # same arrivals, so "served" and "still queued at the end" are directly
    # comparable, and served + remaining == arrived for each strategy.
    arrived = fixed["vehicles_arrived"]
    served_fixed = fixed["vehicles_served"]
    served_adaptive = adaptive["vehicles_served"]
    extra_served = served_adaptive - served_fixed
    pct_more_served = (round(extra_served / served_fixed * 100.0, 1)
                       if served_fixed > 0 else 0.0)

    return {
        "is_simulation": True,
        "generated_at": time.time(),
        "vehicles_arrived": arrived,
        "vehicles_served_fixed": served_fixed,
        "vehicles_served_adaptive": served_adaptive,
        "vehicles_remaining_fixed": fixed["vehicles_remaining"],
        "vehicles_remaining_adaptive": adaptive["vehicles_remaining"],
        "extra_vehicles_served": extra_served,
        "pct_more_served": pct_more_served,
        "primary_units": "vehicles (whole vehicles cleared through the intersection)",
        "secondary_units": "vehicle-seconds of cumulative waiting time (lower is better)",
        "units": "vehicle-seconds of cumulative waiting time (lower is better)",
        "model": (f"Poisson arrivals, {discharge_rate:.2f} veh/s saturation flow, "
                  f"{ALL_RED_SEC:.0f}s all-red clearance, identical arrival stream for both "
                  f"strategies (seed {seed})."),
        "params": {
            "seed": seed,
            "duration_sec": duration_sec,
            "arrival_rates_veh_per_min": rates,
            "discharge_rate_veh_per_sec": discharge_rate,
            "fixed_green_sec": fixed_green_sec,
            "adaptive_rule": "10 + 0.5 * queue, capped at 60",
            "all_red_sec": ALL_RED_SEC,
        },
        "cumulative_wait_fixed": wait_fixed,
        "cumulative_wait_adaptive": wait_adaptive,
        "pct_saved": pct_saved,
        "fixed": fixed,
        "adaptive": adaptive,
    }
