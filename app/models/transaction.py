from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class Transaction:
    transaction_date: date
    posting_date: date | None
    description: str
    amount: float  # signed: negative = money out, positive = money in
    fitid: str = ""
    raw_row: list = field(default_factory=list, repr=False)
