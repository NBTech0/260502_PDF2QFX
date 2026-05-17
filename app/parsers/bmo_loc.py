"""
Parser for BMO Personal Line of Credit statements.

Layout (word-box structure per transaction):
    Primary band:     Item# | TransMonth | TransDay | PostMonth | PostDay | Amount
    Description band: Word1 | Word2 | ... | [CR]
    Dot band:         .  .   (stray dots from "Mar." — skipped)

Quirks handled:
- Bold text is rendered by printing each character twice at a slight offset,
  which causes pdfplumber's extract_text() to merge adjacent bold rows into
  a single garbled line.  Fixed by using extract_words() with a 2pt y-tolerance
  and processing each page independently — word boxes correctly separate bold
  rows that extract_text() cannot.
- Two-column page: transactions on the left (x < 395).  The x-filter eliminates
  all right-column "at a glance" summary text.
- Date tokens like "Dec .18" and "Mar .25" include stray leading dots on the
  day component.  _norm_date() removes them before _parse_date().
- Statement spans December of year N and January of year N+1.  The statement
  date shows N+1, so _extract_statement_year returns N (start of period).
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

# New balance line: "New account balance, Jan. 11 $64,151.50"
# Non-greedy .*? needed because the date portion contains digits (e.g. "Jan. 11")
_NEW_BAL_RE = re.compile(
    r"[Nn]ew\s+account\s+balance.*?([\d,]+\.\d{2})", re.IGNORECASE
)

# Statement date line: "Stmtdate:\nJan.11, 2026"
_STMT_DATE_RE = re.compile(r"[Ss]tmt\s*date[:\s]*([A-Za-z]{3})", re.IGNORECASE)


def _norm_date(s: str) -> str:
    """Remove stray leading dots from the day component.

    "Dec .18" → "Dec 18"
    "Jan. .6" → "Jan. 6"
    "Mar .25" → "Mar 25"
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

        # Transaction rows are extracted via word-box grouping (not extract_text)
        # to handle BMO's bold-text doubling artifact correctly.
        rows = self._parse_text(pdf_path)
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
    # Word-box extraction
    # ------------------------------------------------------------------

    def _parse_text(self, pdf_path: str) -> list[tuple]:
        """
        Open pdf_path and extract transaction rows from each page using
        extract_words() rather than extract_text().

        Returns a list of (item, txdate_str, postdate_str, description, amount_str).
        """
        rows: list[tuple] = []
        try:
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    rows.extend(self._rows_from_page(page))
        except Exception as e:
            raise ParseError(f"Failed to open PDF: {e}") from e
        return rows

    def _rows_from_page(self, page) -> list[tuple]:
        """
        Extract transaction tuples from a single page.

        Strategy
        --------
        pdfplumber's extract_text() merges adjacent bold rows because BMO
        renders each bold character twice at a slight vertical offset (~3 pt).
        extract_words() with y_tolerance=2 keeps those sub-rows separate, and
        each one has a distinct y-anchor.  Filtering to x < 395 removes the
        right-column "at a glance" summary entirely.

        Each transaction consists of up to three consecutive y-bands:
          Primary  (starts with item number):
              [item#, txMonth, txDay, postMonth, postDay, amount_decimal]
          Description (3 pt below primary):
              [word, word, ..., CR]   — CR is present for credits
          Dot band (6 pt below primary):
              [., .]                  — artefact of "Mar." rendering; skipped

        A transaction is flushed (appended to rows) when either:
          - a dot band is encountered, or
          - a new primary band starts (handles items without a dot band).
        Non-primary, non-dot bands while no transaction is pending are
        silently ignored (section headers, legal text, etc.).
        """
        # 1. Extract words from the left transaction column only.
        words = page.extract_words(x_tolerance=2, y_tolerance=2)
        words = [w for w in words if w["x0"] < 395]
        words.sort(key=lambda w: (w["top"], w["x0"]))

        # 2. Group words into y-bands (tolerance = 2 pt).
        bands: list[tuple[float, list[str]]] = []
        cur_y: float | None = None
        cur_texts: list[str] = []
        for w in words:
            if cur_y is None or w["top"] - cur_y > 2:
                if cur_texts:
                    bands.append((cur_y, cur_texts))  # type: ignore[arg-type]
                cur_y, cur_texts = w["top"], [w["text"]]
            else:
                cur_texts.append(w["text"])
        if cur_texts:
            bands.append((cur_y, cur_texts))  # type: ignore[arg-type]

        # 3. Walk bands and assemble transactions.
        rows: list[tuple] = []
        pending: tuple | None = None   # (item_no, txdate, postdate, amount_base)
        pending_cr: bool = False
        desc_parts: list[str] = []
        primary_y: float = 0.0        # y-anchor of the current primary band

        def _expand(texts: list[str]) -> list[str]:
            """Split merged month+day tokens: 'May1' → ['May', '1'], 'May11' → ['May', '11']."""
            out: list[str] = []
            for t in texts:
                m = re.match(r"^([A-Za-z]{3}\.?)(\d{1,2})$", t)
                if m:
                    out.extend([m.group(1), m.group(2)])
                else:
                    out.append(t)
            return out

        def _flush() -> None:
            nonlocal pending, pending_cr, desc_parts
            if pending is not None:
                item_no, txdate, postdate, amount_base = pending
                desc = " ".join(desc_parts).strip()
                amount_str = amount_base + ("CR" if pending_cr else "")
                rows.append((item_no, txdate, postdate, desc, amount_str))
            pending = None
            pending_cr = False
            desc_parts = []

        for _anchor_y, texts in bands:
            # Dot-only band → flush the current transaction.
            if all(t == "." for t in texts):
                _flush()
                continue

            # Expand any merged month+day tokens before length checks.
            texts = _expand(texts)

            # Primary band: first token is a 1-2 digit item number,
            # followed by at least 5 more tokens (month day month day amount).
            if texts and re.match(r"^\d{1,2}$", texts[0]) and len(texts) >= 6:
                _flush()
                item_no   = texts[0]
                txdate    = f"{texts[1]} {texts[2]}"
                postdate  = f"{texts[3]} {texts[4]}"
                amount_base = texts[5]
                pending_cr  = False
                desc_parts  = []
                primary_y   = _anchor_y
                # Occasionally CR or extra tokens appear on the primary band.
                extra = texts[6:]
                if extra and extra[0].upper() == "CR":
                    pending_cr = True
                pending = (item_no, txdate, postdate, amount_base)
                continue

            # All other bands: only treat as description if within 10 pt of
            # the primary band's y-anchor (prevents footer text from being
            # appended to the last transaction when there is no dot band).
            if pending is not None and _anchor_y - primary_y <= 10:
                if texts and texts[-1].upper() == "CR":
                    pending_cr = True
                    texts = texts[:-1]
                desc_parts.extend(t for t in texts if t != ".")

        # Flush the last transaction (no trailing dot band on some pages).
        _flush()
        return rows

    # ------------------------------------------------------------------
    # Build Transaction objects
    # ------------------------------------------------------------------
    def _build_transactions(
        self, rows: list[tuple], year: int
    ) -> list[Transaction]:
        transactions: list[Transaction] = []
        prev_month: int | None = None
        current_year: int = year  # advances when Dec → Jan rollover is detected

        for _item, txdate_str, postdate_str, desc, amount_str in rows:
            # Normalise dates (strip stray dots before the day number)
            txdate_norm   = _norm_date(txdate_str)
            postdate_norm = _norm_date(postdate_str)

            try:
                tx_date = self._parse_date(txdate_norm, current_year, prev_month)
                # Track year advances so subsequent same-month transactions stay
                # in the correct year (e.g. multiple January rows all get year N+1)
                current_year = tx_date.year
                prev_month   = tx_date.month
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
