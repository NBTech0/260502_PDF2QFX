from __future__ import annotations

import re

import pdfplumber

from app.models.statement import AccountType, Statement
from app.models.transaction import Transaction
from app.parsers.base_parser import BaseParser, NoTransactionsFoundError, ParseError

# Column header strings to skip
_HEADER_PATTERNS = re.compile(
    r"^(date|description|transaction|withdrawals?|deposits?|balance|activity)$",
    re.IGNORECASE,
)

# Matches a date cell: "Mar 15", "Mar. 15", "03/15", "03-15"
_DATE_RE = re.compile(
    r"^([A-Za-z]{3}\.?\s+\d{1,2}|\d{1,2}[/-]\d{1,2})$"
)

# Dollar amount with optional commas
_AMOUNT_RE = re.compile(r"^\$?[\d,]+\.\d{2}$")

# Text fallback: date desc optional-amount optional-amount balance
_BANK_LINE_RE = re.compile(
    r"^(?P<date>[A-Za-z]{3}\.?\s+\d{1,2}|\d{1,2}[/-]\d{1,2})\s+"
    r"(?P<desc>.+?)\s{2,}"
    r"(?P<col1>[\d,]+\.\d{2})?"
    r"(?:\s+(?P<col2>[\d,]+\.\d{2}))?"
    r"(?:\s+(?P<balance>[\d,]+\.\d{2}))?\s*$"
)


class BMOBankParser(BaseParser):

    def parse(self, pdf_path: str) -> Statement:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                full_text = "\n".join(
                    (page.extract_text() or "") for page in pdf.pages
                )
                year = self._extract_statement_year(full_text)
                account_id = self._extract_account_number(full_text)
                account_type = self._detect_account_subtype(full_text)

                all_rows: list[list[str]] = []
                for page in pdf.pages:
                    tables = page.extract_tables({
                        "vertical_strategy": "lines",
                        "horizontal_strategy": "lines",
                        "snap_tolerance": 3,
                        "join_tolerance": 3,
                    })
                    found_table = False
                    for table in tables:
                        rows = self._filter_transaction_rows(table)
                        if rows:
                            all_rows.extend(rows)
                            found_table = True
                    if not found_table:
                        text = page.extract_text() or ""
                        rows = self._parse_text_fallback(text)
                        all_rows.extend(rows)
        except (OSError, Exception) as e:
            raise ParseError(f"Failed to open PDF: {e}") from e

        transactions, ledger_balance, ledger_date = self._build_transactions(all_rows, year)

        if not transactions:
            raise NoTransactionsFoundError(
                "No transactions found. The PDF may be scanned/image-based or have an unexpected layout."
            )

        return Statement(
            account_type=account_type,
            account_id=account_id,
            transactions=transactions,
            ledger_balance=ledger_balance,
            ledger_balance_date=ledger_date,
            source_file=pdf_path,
        )

    def _detect_account_subtype(self, text: str) -> AccountType:
        lower = text.lower()
        if "savings" in lower or "smart saver" in lower or "high interest" in lower:
            return AccountType.SAVINGS
        return AccountType.CHECKING

    def _filter_transaction_rows(self, table: list[list]) -> list[list[str]]:
        result = []
        for row in table:
            if row is None:
                continue
            cells = [str(c).strip() if c else "" for c in row]
            if not any(cells):
                continue
            # Skip header rows
            if cells[0] and _HEADER_PATTERNS.match(cells[0]):
                continue
            # Must have a date-like value in first cell
            if cells[0] and _DATE_RE.match(cells[0]):
                result.append(cells)
            # Multi-line description continuation: no date, no amounts
            elif cells[0] == "" and cells[1] != "" and all(c == "" for c in cells[2:]):
                result.append(cells)
        return result

    def _parse_text_fallback(self, text: str) -> list[list[str]]:
        rows = []
        for line in text.splitlines():
            m = _BANK_LINE_RE.match(line.strip())
            if m:
                rows.append([
                    m.group("date"),
                    m.group("desc"),
                    m.group("col1") or "",
                    m.group("col2") or "",
                    m.group("balance") or "",
                ])
        return rows

    def _build_transactions(
        self, rows: list[list[str]], year: int
    ) -> tuple[list[Transaction], float | None, object]:
        transactions: list[Transaction] = []
        ledger_balance: float | None = None
        ledger_date = None
        prev_month: int | None = None

        for cells in rows:
            # Ensure at least 5 columns (pad if shorter)
            while len(cells) < 5:
                cells.append("")

            date_cell = cells[0].strip()
            desc_cell = cells[1].strip()

            # Continuation row — append description to last transaction
            if not date_cell and transactions and desc_cell:
                transactions[-1].description += " " + desc_cell
                continue

            if not date_cell or not desc_cell:
                continue

            # Opening/Closing balance rows
            desc_lower = desc_cell.lower()
            if "closing balance" in desc_lower or "opening balance" in desc_lower:
                # Try to capture closing balance as ledger balance
                for cell in reversed(cells[2:]):
                    cell = cell.strip()
                    if _AMOUNT_RE.match(cell):
                        try:
                            ledger_balance = self._parse_dollar(cell)
                        except ValueError:
                            pass
                        break
                continue

            # Parse date
            try:
                tx_date = self._parse_date(date_cell, year, prev_month)
                prev_month = tx_date.month
            except ValueError:
                continue

            # Determine amount: withdrawals in col[2], deposits in col[3]
            withdrawal = cells[2].strip() if len(cells) > 2 else ""
            deposit = cells[3].strip() if len(cells) > 3 else ""

            amount: float | None = None
            if withdrawal and _AMOUNT_RE.match(withdrawal):
                try:
                    amount = -self._parse_dollar(withdrawal)
                except ValueError:
                    pass
            if amount is None and deposit and _AMOUNT_RE.match(deposit):
                try:
                    amount = self._parse_dollar(deposit)
                except ValueError:
                    pass

            if amount is None:
                continue

            transactions.append(Transaction(
                transaction_date=tx_date,
                posting_date=None,
                description=desc_cell,
                amount=amount,
                raw_row=cells[:],
            ))

        return transactions, ledger_balance, ledger_date
