# SPDX-License-Identifier: Apache-2.0
"""Budget-bounded spend allocation, split by sensor uptime.

.. deprecated:: 2026-08-10
   SUPERSEDED by :mod:`orchard_chia.economics`, which is the ratified model.

   This one weights WALLETS by the mean uptime of their Trees. That is strongly
   sybil-resistant, but it also means a second Tree cannot increase an
   operator's share and a below-average one actively reduces it — a
   disincentive to deploy the very hardware the network exists to grow.

   The ratified model keeps the bounded-emission property that made this design
   worth building, and attaches weight to Trees instead of wallets. The service
   layer here (collector, planner, executor, audit, scheduler, locking) is
   sound and is intended to be reused; only the allocation arithmetic is
   superseded.

This is a DIFFERENT economic model from ``orchard_chia.payout``, and both exist
on purpose:

  * ``payout``     — per-Tree accrual. Every Tree earns ``daily_rate`` per day,
                     so total emission scales with fleet size and has no ceiling.
                     10,000 Trees mint 10,000 tokens a day.
  * ``allocation`` — a fixed budget per cycle, divided between wallets in
                     proportion to their AVERAGE sensor uptime. Total emission
                     is whatever you configured and nothing else.

The second is bounded, which is the whole point. Nothing in this package can
spend more than the cycle budget, however many Trees join.

The weighting deserves a note, because it is deliberate and surprising: a
wallet's weight is the MEAN uptime of its eligible tree/sensor pairs, not the
sum. One Tree at 100% and ten Trees at 100% weigh exactly the same. Adding a
badly-performing Tree to a wallet LOWERS its allocation. That is sybil
resistance bought at the price of any incentive to deploy more sensors — a
trade the owner made explicitly.

Layering, strictly one direction:

    collector  ->  engine  ->  planner  ->  executor
    (reads)        (pure)      (rules)      (signs)

``engine`` imports nothing but the standard library and is the only place
allocation arithmetic happens. ``executor`` is the only place a key is used.
Nothing upstream of ``executor`` can move funds, so every safety rule that
matters is enforced before anything is signable.
"""
