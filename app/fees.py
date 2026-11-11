"""Fee estimates from Kalshi's published fee schedule (kalshi.com/docs/kalshi-fee-schedule.pdf):

    taker fee = round_up(M x 0.07   x C x P x (1-P))
    maker fee = round_up(M x 0.0175 x C x P x (1-P))

M defaults to 1 for takers and 0 for makers unless the series says otherwise.
Series fee_type values (from the API):
    quadratic                        -> taker 0.07, maker 0
    quadratic_with_maker_fees        -> taker 0.07, maker 0.0175
    quadratic_with_combo_maker_fees  -> taker 0.07, maker 0.035 (0.5 maker multiplier)
    flat                             -> non-standard; treated as quadratic here (ESTIMATE)
Series.fee_multiplier scales both. Rounded up to the cent here (conservative; direct
members are rounded to $0.0001, so real fees can be slightly lower).

These are ESTIMATES for edge screening. The fee actually charged comes back on the
order response (average_fee_paid) and on fills.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from .kalshi_client import d

TAKER = Decimal("0.07")
MAKER = {"quadratic": Decimal(0), "quadratic_with_maker_fees": Decimal("0.0175"),
         "quadratic_with_combo_maker_fees": Decimal("0.035"), "flat": Decimal(0)}


def fee(price, count, fee_type: str | None = "quadratic", multiplier=1, maker: bool = False) -> Decimal:
    p, c, m = d(price), d(count), d(multiplier if multiplier not in (None, "") else 1)
    coef = MAKER.get(fee_type or "quadratic", Decimal(0)) if maker else TAKER
    raw = m * coef * c * p * (1 - p)
    return raw.quantize(Decimal("0.01"), rounding=ROUND_CEILING)


def fee_per_contract(price, fee_type="quadratic", multiplier=1, maker=False, count=100) -> Decimal:
    """Per-contract fee averaged over a typical lot (rounding is per order)."""
    return fee(price, count, fee_type, multiplier, maker) / d(count)
