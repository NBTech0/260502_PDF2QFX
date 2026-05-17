"""
Parser for BMO Personal Line of Credit statements.

Layout (text-only, no table structure):
    Item no. | Trans date | Posting date | Description | Amount ($)

Quirks handled:
- Two-column page: transactions on the left (x < 395), "at a glance" summary
  on the right — the summary text bleeds into some lines and is stripped.
- Date tokens like "Dec .18" and "Jan. .6" include stray dots that confuse
  the base _parse_date; a normalisation step removes them.
- Bold text is rendered by printing each character twice at a slight offset,
  producing tokens like "C CA AS SH H A AD DV VA AN NC CE E".  The
  _fix_doubled_text() function reconstructs the original string.
- Statement spans December of year N and January of year N+1.  The statement
  date shows N+1, so _extract_statement_year returns N (start of period).
- Some description rows are rendered a few pixels below their data row; they
  appear as ". . DESCRIPTION" continuation lines in the extracted text.
"""
from __future__ import annotations

import re

import pdfplumber

from app.models.statement import AccountType, Statement
from app.models.transaction import Transaction
from app.parsers.base_parser import (
    MONTH_MAP,
    BaseParser,
    NoTransactionsFoundError,
    ParseError,
)

# Transaction line: "1 Dec .18 Dec .18 CASH ADVANCE 1,000.00"
# Dates can include extra dots: "Jan. .6", "Dec .18", "Jan 11"
_DATE_PAT = r"[A-Za-z]{3}\.?\s*\.?\s*\d{1,2}"
_TRANS_RE = re.compile(
    r"^(\d+)\s+"
    r"(" + _DATE_PAT + r")\s+"
    r"(" + _DATE_PAT + r")\s*"
    r"(.*?)\s*"
    r"([\d,]+\.\d{2}(?:\s*CR?)?)\s*$",
    re.IGNORECASE,
)

# Amount pattern (for filtering continuation lines)
_AMOUNT_ONLY_RE = re.compile(r"^[\d,]+\.\d{2}(?:\s*CR?)?$", re.IGNORECASE)

# New balance line: "New account balance, Jan. 11 $64,151.50"
# Non-greedy .*? needed because the date portion contains digits (e.g. "Jan. 11")
_NEW_BAL_RE = re.compile(
    r"[Nn]ew\s+account\s+balance.*?([\d,]+\.\d{2})", re.IGNORECASE
)

# Previous balance line (used to detect cross-year period)
_PREV_BAL_RE = re.compile(r"[Pp]revious\s+balance[^A-Za-z]*([A-Za-z]{3})", re.IGNORECASE)

# Statement date line: "Stmtdate:\nJan.11, 2026"
_STMT_DATE_RE = re.compile(r"[Ss]tmt\s*date[:\s]*([A-Za-z]{3})", re.IGNORECASE)


def _fix_doubled_text(text: str) -> str:
    """
    Fix BMO's bold-text doubling artifact.

    BMO renders bold descriptions by printing each character twice at a
    slight offset.  pdfplumber merges them into overlapping short tokens:
        "C CA AS SH H A AD DV VA AN NC CE E"
    This function detects chains of 1-2 char tokens where each new token
    starts with the last char of the accumulated result, and collapses them:
        "CASH ADVANCE"
    """
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if len(tok) <= 2 and i + 1 < len(tokens):
            chain = tok
            j = i + 1
            while (
                j < len(tokens)
                and len(tokens[j]) <= 2
                and tokens[j][0] == chain[-1]
            ):
                chain += tokens[j][1:]
                j += 1
            if j > i + 2:          # genuine chain of 3+ tokens
                out.append(chain)
                i = j
                continue
        out.append(tok)
        i += 1
    return " ".join(out)


def _norm_date(s: str) -> str:
    """Remove stray leading dots from the day component.

    "Dec .18" → "Dec 18"
    "Jan. .6" → "Jan. 6"
    "Jan 11"  → "Jan 11"  (unchanged)
    """
    return re.sub(r"([A-Za-z]{3}\.?)\s*\.\s*(\d)", r"\1 \2", s.strip())


class BMOLOCParser(BaseParser):

    def parse(self, pdf_path: str) -> Statement:
        try:
            with pdfplumber.open(pdf_path) as pdf:
                full_text = "\n".join(
                    (page.extract_text() or "") for page in pdf.pages
                )
        except Exception as e:
            raise ParseError(f"Failed to open PDF: {e}") from e

        year = self._extract_statement_year(full_text)
        account_id = self._extract_account_number(full_text)
        new_balance = self._extract_new_balance(full_text)

        rows = self._parse_text(full_text)
        transactions = self._build_transactions(rows, year)

        if not transactions:
            raise NoTransactionsFoundError(
                "No transactions found in LOC statement."
            )

        return Statement(
            account_type=AccountType.LOC,
            account_id=account_id,
            transactions=transactions,
            ledger_balance=new_balance,
            source_file=pdf_path,
        )

    # ------------------------------------------------------------------
    # Year extraction: LOC statements span December (year N) through
    # January (year N+1).  The statement date is in January of N+1, so
    # the base extractor returns N+1.  We return N so that December
    # transactions parse correctly; _infer_year handles the Dec→Jan jump.
    # ------------------------------------------------------------------
    def _extract_statement_year(self, full_text: str) -> int:
        stmt_year = super()._extract_statement_year(full_text)
        m = _STMT_DATE_RE.search(full_text)
        if m:
            mon = MONTH_MAP.get(m.group(1)[:3].lower(), 0)
            if mon == 1:               # January only: statement spans Dec(N)/Jan(N+1)
                return stmt_year - 1
        return stmt_year

    # ------------------------------------------------------------------
    # Account number: "Account number: 2976 3066 605"
    # ------------------------------------------------------------------
    def _extract_account_number(self, full_text: str) -> str:
        m = re.search(
            r"[Aa]ccount\s+[Nn]umber[:\s]+(\d{4}\s+\d{4}\s+\d{3})",
            full_text,
        )
        if m:
            return m.group(1).strip()
        return super()._extract_account_number(full_text)

    def _extract_new_balance(self, full_text: str) -> float | None:
        m = _NEW_BAL_RE.search(full_text)
        if m:
            return float(re.sub(r"[^\d.]", "", m.group(1)))
        return None

    # ------------------------------------------------------------------
    # Text parsing
    # ------------------------------------------------------------------
    def _parse_text(self, full_text: str) -> list[tuple]:
        """
        Returns a list of (item, txdate_str, postdate_str, description, amount_str).
        """
        rows: list[tuple] = []
        pending: list | None = None

        for raw in full_text.splitlines():
            # 1. Strip "at a glance" right-column content.
            #    That column uses '!' as a separator artifact in pdfplumber output.
            line = raw.split("!")[0]
            # Truncate everything that follows the transaction amount — the
            # at-a-glance summary column is appended after it on some lines.
            # Use \s+ so we only truncate when there IS something after the amount.
            line = re.sub(r"([\d,]+\.\d{2}(?:\s*CR?)?)\s+.*$", r"\1", line, flags=re.IGNORECASE)
            # Strip orphaned trailing + / - left after the amount was cut
            line = re.sub(r"\s+[+\-]\s*$", "", line).strip()

            if not line:
                continue

            # 2. Try to match a new transaction line.
            m = _TRANS_RE.match(line)
            if m:
                if pending is not None:
                    rows.append(tuple(pending))
                desc = _fix_doubled_text(m.group(4).strip())
                pending = [
                    m.group(1),   # item no.
                    m.group(2),   # trans date string
                    m.group(3),   # posting date string
                    desc,         # description (may be empty)
                    m.group(5),   # amount string
                ]
                continue

            # 3. Continuation description line: ". . SOME DESCRIPTION"
            if pending is not None:
                # Starts with dots/spaces then text — typical for offset descriptions
                cont = re.match(r"^[.\s]+([A-Z].+)$", line, re.IGNORECASE)
                if cont:
                    extra = cont.group(1).strip()
                    if extra and not _AMOUNT_ONLY_RE.match(extra):
                        sep = " " if pending[3] else ""
                        pending[3] = (pending[3] + sep + extra).strip()

        if pending is not None:
            rows.append(tuple(pending))

        return rows

    def _build_transactions(
        self, rows: list[tuple], year: int
    ) -> list[Transaction]:
        transactions: list[Transaction] = []
        prev_month: int | None = None
        current_year: int = year  # advances when Dec → Jan rollover is detected

        for _item, txdate_str, postdate_str, desc, amount_str in rows:
            # Normalise dates (strip stray dots before the day number)
            txdate_norm = _norm_date(txdate_str)
            postdate_norm = _norm_date(postdate_str)

            try:
                tx_date = self._parse_date(txdate_norm, current_year, prev_month)
                # Track year advances so subsequent same-month transactions stay
                # in the correct year (e.g. multiple January rows all get year N+1)
                current_year = tx_date.year
                prev_month = tx_date.month
            except ValueError:
                continue

            post_date = None
            try:
                post_date = self._parse_date(postdate_norm, current_year, prev_month)
            except ValueError:
                pass

            is_credit = "cr" in amount_str.lower()
            cleaned = re.sub(r"[^\d.]", "", amount_str)
            if not cleaned:
                continue
            amount = float(cleaned)
            if not is_credit:
                amount = -amount   # advances / charges are negative

            transactions.append(
                Transaction(
                    transaction_date=tx_date,
                    posting_date=post_date,
                    description=desc if desc else "UNKNOWN",
                    amount=amount,
                )
            )

        return transactions
