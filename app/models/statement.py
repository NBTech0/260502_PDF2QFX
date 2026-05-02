from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum

from .transaction import Transaction


class AccountType(Enum):
    CHECKING = "CHECKING"
    SAVINGS = "SAVINGS"
    CREDITCARD = "CREDITCARD"


@dataclass
class Statement:
    account_type: AccountType
    account_id: str
    currency: str = "CAD"
    transactions: list[Transaction] = field(default_factory=list)
    ledger_balance: float | None = None
    ledger_balance_date: date | None = None
    statement_date: date | None = None
    source_file: str = ""
    validation_warnings: list[str] = field(default_factory=list)
