from __future__ import annotations

import re

import pdfplumber

from app.models.statement import AccountType, Statement
from app.models.transaction import Transaction
from app.parsers.base_parser import BaseParser, NoTransactionsFoundError, ParseError

# Section header lines to skip
_SECTION_HEADERS = re.compile(
    r"^(purchases?|payments?\s+and\s+credits?|fees?|interest\s+charged|cash\s+advances?|"
    r"other\s+credits?|credits?|new\s+transactions?|previous\s+balance|total\s+).*$",
    re.IGNORECASE,
)

# Header row columns to skip
_COL_HEADERS = re.compile(
    r"^(transaction\s+date|posting\s+date|date|description|amount|activity)$",
    re.IGNORECASE,
)

# Date cell pattern — handles "Dec.4" (BMO no-space format), "Dec. 4", "03/15"
_DATE_RE = re.compile(
    r"^([A-Za-z]{3}\.?\s*\d{1,2}|\d{1,2}[/-]\d{1,2}|\d{4}-\d{2}-\d{2})$"
)

# Amount: "1,234.56" or "1,234.56 CR" or "$1,234.56CR"  (1-2 decimal places)
_AMOUNT_RE = re.compile(r"^\$?[\d,]+\.\d{1,2}\s*(?:CR)?$", re.IGNORECASE)

# BMO Mastercard text line: "Dec.4 Dec.6 DESCRIPTION REFNO AMOUNT"
# Reference number is optional — some lines (e.g. INTERESTPURCHASES) omit it.
_CC_LINE_RE = re.compile(
    r"^(?P<txdate>[A-Za-z]{3}\.?\s*\d{1,2})\s+"
    r"(?P<postdate>[A-Za-z]{3}\.?\s*\d{1,2})\s+"
    r"(?P<desc>.+?)\s+"
    r"(?:(?P<refno>\S+)\s+)?"        # optional reference number
    r"(?P<amount>[\d,]+\.\d{1,2}\s*(?:CR)?)\s*$",
    re.IGNORECASE,
)


class BMOMastercardParser(BaseParser):

    def parse(self, pdf_path: str) -> Statement:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                full_text = "\n".join(
                    (page.extract_text() or "") for page in pdf.pages
                )
                year = self._extract_statement_year(full_text)
                account_id = self._extract_account_number(full_text)

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

        transactions = self._build_transactions(all_rows, year)

        if not transactions:
            raise NoTransactionsFoundError(
                "No transactions found. The PDF may be scanned/image-based or have an unexpected layout."
            )

        # Deduplicate: same date, description, amount on adjacent rows (page-top reprints)
        transactions = self._deduplicate(transactions)

        return Statement(
            account_type=AccountType.CREDITCARD,
            account_id=account_id,
            transactions=transactions,
            source_file=pdf_path,
        )

    def _filter_transaction_rows(self, table: list[list]) -> list[list[str]]:
        result = []
        for row in table:
            if row is None:
                continue
            cells = [str(c).strip() if c else "" for c in row]
            if not any(cells):
                continue
            # Skip column headers
            if cells[0] and _COL_HEADERS.match(cells[0]):
                continue
            # Skip section headers
            if cells[1] and _SECTION_HEADERS.match(cells[1]):
                continue
            if cells[0] and _SECTION_HEADERS.match(cells[0]):
                continue
            # Valid row: first cell is a date
            if cells[0] and _DATE_RE.match(cells[0]):
                result.append(cells)
            # Continuation: no date, has description
            elif cells[0] == "" and len(cells) > 1 and cells[1]:
                result.append(cells)
        return result

    def _parse_text_fallback(self, text: str) -> list[list[str]]:
        rows = []
        for line in text.splitlines():
            m = _CC_LINE_RE.match(line.strip())
            if m:
                rows.append([
                    m.group("txdate"),
                    m.group("postdate"),
                    m.group("desc"),
                    # refno is discarded — not needed for QFX
                    m.group("amount"),
                ])
        return rows

    def _parse_amount(self, raw: str) -> float:
        is_credit = "cr" in raw.lower()
        cleaned = re.sub(r"[^\d.]", "", raw)
        if not cleaned:
            raise ValueError(f"Cannot parse amount: {raw!r}")
        value = float(cleaned)
        return value if is_credit else -value

    def _build_transactions(self, rows: list[list[str]], year: int) -> list[Transaction]:
        transactions: list[Transaction] = []
        prev_month: int | None = None

        for cells in rows:
            while len(cells) < 4:
                cells.append("")

            tx_date_cell = cells[0].strip()
            post_date_cell = cells[1].strip() if len(cells) > 1 else ""
            desc_cell = cells[2].strip() if len(cells) > 2 else ""

            # Find amount in last non-empty cell
            amount_cell = ""
            for cell in reversed(cells):
                cell = cell.strip()
                if _AMOUNT_RE.match(cell):
                    amount_cell = cell
                    break

            # Continuation row
            if not tx_date_cell and transactions and desc_cell:
                transactions[-1].description += " " + desc_cell
                continue

            if not tx_date_cell or not desc_cell or not amount_cell:
                continue

            # Skip balance/total lines
            if re.match(r"(new balance|previous balance|minimum payment|total)", desc_cell, re.IGNORECASE):
                continue

            try:
                tx_date = self._parse_date(tx_date_cell, year, prev_month)
                prev_month = tx_date.month
            except ValueError:
                continue

            post_date = None
            if post_date_cell and post_date_cell != tx_date_cell:
                try:
                    post_date = self._parse_date(post_date_cell, year, prev_month)
                except ValueError:
                    pass

            try:
                amount = self._parse_amount(amount_cell)
            except ValueError:
                continue

            transactions.append(Transaction(
                transaction_date=tx_date,
                posting_date=post_date,
                description=desc_cell,
                amount=amount,
                raw_row=cells[:],
            ))

        return transactions

    def _deduplicate(self, transactions: list[Transaction]) -> list[Transaction]:
        seen: set[tuple] = set()
        result = []
        for t in transactions:
            key = (t.transaction_date, t.posting_date, t.description, t.amount)
            if key not in seen:
                seen.add(key)
                result.append(t)
        return result
