from __future__ import annotations

import os
import re
from datetime import date

import pdfplumber

from app.models.statement import AccountType, Statement
from app.models.transaction import Transaction
from app.parsers.base_parser import (
    BaseParser, NoTransactionsFoundError, ParseError, MONTH_MAP,
)

# Full date: "May 04, 2026" or "February 9, 2026" or "Apr 23,2026" (OCR — no space after comma)
_FULL_DATE_RE = re.compile(
    r"^([A-Za-z]{3,9})\s+(\d{1,2}),?\s*(\d{4})$"
)

# Partial date without year: "May 04," or "May 04"
_PARTIAL_DATE_RE = re.compile(
    r"^([A-Za-z]{3,9})\s+(\d{1,2}),?$"
)

# Transaction line: full-date  description  signed-amount  balance
# Uses \s+ (not \s{2,}) so OCR single-spaced output also matches.
_TX_LINE_RE = re.compile(
    r"^([A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\s+"   # full date
    r"(.+?)\s+"                                      # description (non-greedy)
    r"([-+]\s*[S$]?\s*[\d,]+\.\d{2})\s+"           # signed amount ($ or S for OCR)
    r"(-?[S$]?\s*[\d,]+\.\d{2})\s*$"               # balance
)

# Column header / UI rows to skip
_SKIP_RE = re.compile(
    r"^(date|description|money|balance|showing|current|available|funds|"
    r"direct|transfer|pay\s+bill|interac|overview|statements|"
    r"items|previous|next|\d+-\d+\s+of\s+\d+|view|download|"
    r"[<>]|↑|↓)",
    re.IGNORECASE,
)

# Common Tesseract installation paths on Windows
# (binary path, tessdata directory or None to leave TESSDATA_PREFIX unset)
_TESSERACT_CANDIDATES = [
    (r"C:\Program Files\Tesseract-OCR\tesseract.exe",           None),
    (r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",     None),
    (r"C:\Users\Public\Tesseract-OCR\tesseract.exe",            None),
    # MacroCreator ships 4.x binary but no tessdata — pair with ProperConvert data
    (r"C:\Program Files\MacroCreator\Bin\tesseract\tesseract.exe",
     r"C:\Program Files (x86)\ProperSoft\ProperConvert\tessdata"),
    (r"C:\Program Files\MacroCreator\Bin\tesseract\tesseract.exe",
     r"C:\Program Files (x86)\Power Automate Desktop\tessdata"),
]


def _get_pytesseract():
    """Return the pytesseract module after locating a working Tesseract install."""
    try:
        import pytesseract
    except ImportError:
        raise ParseError(
            "This PDF is image-based and requires OCR to process.\n"
            "Install pytesseract:  pip install pytesseract\n"
            "Install Tesseract:    https://github.com/UB-Mannheim/tesseract/wiki"
        )

    # Try default/PATH location first
    try:
        pytesseract.get_tesseract_version()
        return pytesseract
    except Exception:
        pass

    # Search known Windows installation paths
    for exe_path, tessdata_dir in _TESSERACT_CANDIDATES:
        if not os.path.isfile(exe_path):
            continue
        pytesseract.pytesseract.tesseract_cmd = exe_path
        if tessdata_dir:
            os.environ["TESSDATA_PREFIX"] = tessdata_dir
        try:
            pytesseract.get_tesseract_version()
            return pytesseract
        except Exception:
            continue

    raise ParseError(
        "Tesseract OCR not found. Download and install it from:\n"
        "https://github.com/UB-Mannheim/tesseract/wiki\n"
        "(Required for image-based BMO Account Overview PDFs.)"
    )


class BMOAccountOverviewParser(BaseParser):
    """
    Parses BMO Online Banking 'Account Overview' web-print PDFs.
    Columns: Date | Description | Money out | Money in | Balance
    Dates include the full year; transactions are listed newest-first.

    Supports both text-based PDFs (word-position extraction) and image-based
    PDFs printed from a browser (OCR via pytesseract / Tesseract).
    """

    def parse(self, pdf_path: str) -> Statement:
        # Reset per-document state so re-use of the parser instance is safe
        self._col_bounds: list[float] | None = None

        try:
            with pdfplumber.open(pdf_path) as pdf:
                # Try text extraction for metadata; fall back gracefully for OCR pages
                full_text = "\n".join(
                    (page.extract_text() or "") for page in pdf.pages
                )
                # If text-based extraction is empty, OCR page 1 for metadata.
                # This also populates self._col_bounds from the page-1 header so
                # subsequent pages can reuse the same column boundaries.
                if not full_text.strip():
                    rows0 = self._ocr_page_to_rows(pdf.pages[0])
                    full_text = "\n".join(" ".join(r) for r in rows0)

                account_id = self._extract_acct_id(full_text)
                account_type = self._detect_subtype(full_text)
                ledger_balance = self._extract_current_balance(full_text)
                period_year = self._extract_period_year(full_text)

                all_rows: list[list[str]] = []
                for page in pdf.pages:
                    all_rows.extend(self._extract_rows(page, period_year))

        except ParseError:
            raise
        except (OSError, Exception) as e:
            raise ParseError(f"Failed to open PDF: {e}") from e

        transactions = self._build_transactions(all_rows)

        if not transactions:
            raise NoTransactionsFoundError(
                "No transactions found. The PDF may have an unexpected layout.\n"
                "If this is an image-based PDF, ensure Tesseract OCR is installed."
            )

        # PDF lists newest-first; reverse to oldest-first for QFX
        transactions.reverse()

        return Statement(
            account_type=account_type,
            account_id=account_id,
            transactions=transactions,
            ledger_balance=ledger_balance,
            source_file=pdf_path,
        )

    # ------------------------------------------------------------------ #
    # Metadata extraction
    # ------------------------------------------------------------------ #

    def _extract_acct_id(self, text: str) -> str:
        # "29766 3066-605" from the page title
        m = re.search(r"(\d{5}\s+\d{4}-\d{3})", text)
        if m:
            return m.group(1)
        return self._extract_account_number(text)

    def _detect_subtype(self, text: str) -> AccountType:
        lower = text.lower()
        if "savings" in lower or "smart saver" in lower:
            return AccountType.SAVINGS
        return AccountType.CHECKING

    def _extract_current_balance(self, text: str) -> float | None:
        # Handles same-line ("Current balance $8,663.02") and next-line
        # ("Current balance\n$8,663.02") layouts from OCR column splitting.
        m = re.search(
            r"[Cc]urrent\s+balance[^$\d]{0,20}\$?\s*([\d,]+\.\d{2})",
            text,
        )
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return None

    def _extract_period_year(self, text: str) -> int:
        # "Showing: Nov 08, 2025 ...May 03, 2026" → use the last (most recent) year
        years = re.findall(r"\b(20\d{2})\b", text[:800])
        if years:
            return int(years[-1])
        from datetime import datetime
        return datetime.now().year

    # ------------------------------------------------------------------ #
    # Row extraction from a single page
    # ------------------------------------------------------------------ #

    def _extract_rows(self, page, period_year: int) -> list[list[str]]:
        """
        Try word-position extraction first (text-based PDFs).
        Fall back to positional OCR for image-based PDFs.
        """
        words = page.extract_words(x_tolerance=3, y_tolerance=3)
        if words:
            raw_rows = self._rows_from_word_boxes(
                [{"text": w["text"], "left": w["x0"], "top": w["top"],
                  "right": w["x1"], "bottom": w["bottom"]}
                 for w in words],
                page.width,
            )
        else:
            raw_rows = self._ocr_page_to_rows(page)

        return self._merge_wrapped_dates(
            [r for r in raw_rows if not self._is_ui_row(r)],
            period_year,
        )

    def _ocr_page_to_rows(self, page) -> list[list[str]]:
        """
        Render page to image, run Tesseract with word-level data,
        then reconstruct rows by spatial position — identical to the
        PDF word-position approach but on OCR pixel coordinates.
        """
        tess = _get_pytesseract()
        img = page.to_image(resolution=300)
        pil_img = img.original

        data = tess.image_to_data(
            pil_img, lang="eng", output_type=tess.Output.DICT
        )

        word_boxes = []
        n = len(data["text"])
        for i in range(n):
            text = (data["text"][i] or "").strip()
            conf = int(data["conf"][i])
            if text and conf > 10:
                left = data["left"][i]
                top  = data["top"][i]
                word_boxes.append({
                    "text":  text,
                    "left":  left,
                    "top":   top,
                    "right": left + data["width"][i],
                    "bottom": top + data["height"][i],
                })

        if not word_boxes:
            return []

        # OCR pixels need a larger vertical tolerance than PDF points because
        # multi-line date cells place "May 04," and "2026" ~70px apart at 300 DPI.
        return self._rows_from_word_boxes(word_boxes, pil_img.width, y_tol=50.0)

    def _is_ui_row(self, cells: list[str]) -> bool:
        # Only filter on the date column (col 0). Checking description would
        # incorrectly drop real transactions starting with INTERAC, TRANSFER, etc.
        return bool(_SKIP_RE.match(cells[0].strip()))

    # ------------------------------------------------------------------ #
    # Shared word-position → cell-row builder (used for both PDF and OCR)
    # ------------------------------------------------------------------ #

    def _rows_from_word_boxes(
        self,
        word_boxes: list[dict],
        content_width: float,
        y_tol: float | None = None,
    ) -> list[list[str]]:
        """
        Group word dicts (with keys: text, left, top, right, bottom)
        into horizontal bands, then assign each word to one of 5 columns
        based on its horizontal centre.

        word_boxes must use consistent coordinate units (either all PDF
        points from pdfplumber, or all OCR pixels).

        y_tol: vertical tolerance for grouping words into the same band.
               Defaults to 0.6 % of content_width, which works for PDF points.
               Pass 50.0 for 300-DPI OCR images where multi-line date cells
               spread the date and description ~35 px apart vertically.
        """
        # --- band grouping ---
        bands: list[list[dict]] = []
        tol = y_tol if y_tol is not None else max(4.0, content_width * 0.006)
        for w in sorted(word_boxes, key=lambda x: x["top"]):
            for band in reversed(bands):
                if abs(w["top"] - band[0]["top"]) <= tol:
                    band.append(w)
                    break
            else:
                bands.append([w])

        # Reuse column bounds computed from the first page's header row so that
        # pages 2+ (which may only show "Balance" in the header) get consistent
        # column assignments.
        if self._col_bounds is None:
            self._col_bounds = self._detect_col_bounds(bands, content_width)
        col_bounds = self._col_bounds

        result: list[list[str]] = []
        for band in bands:
            cells = ["", "", "", "", ""]
            for w in sorted(band, key=lambda x: x["left"]):
                x_mid = (w["left"] + w["right"]) / 2
                col = self._col_for_x(x_mid, col_bounds)
                cells[col] += (" " if cells[col] else "") + w["text"]
            result.append(cells)
        return result

    def _detect_col_bounds(self, bands: list, content_width: float) -> list[float]:
        """Find column split positions from the header row, or use defaults."""
        for band in bands:
            by_text = {w["text"].lower(): w for w in band}
            if "date" in by_text and "balance" in by_text:
                key_cols = ["date", "description", "out", "in", "balance"]
                centers = sorted(
                    (by_text[k]["left"] + by_text[k]["right"]) / 2
                    for k in key_cols
                    if k in by_text
                )
                if len(centers) >= 4:
                    return [
                        (centers[i] + centers[i + 1]) / 2
                        for i in range(len(centers) - 1)
                    ][:4]

        # Fallback: fractions of content width derived from the observed
        # header positions on a 2550-px wide 300-DPI page-1 OCR rendering:
        #   Date≈280, Desc≈620, out≈1530, in≈1910, Balance≈2200
        #   midpoints: 450 (17.6%), 1075 (42.2%), 1720 (67.5%), 2055 (80.6%)
        return [
            content_width * 0.18,
            content_width * 0.43,
            content_width * 0.67,
            content_width * 0.80,
        ]

    def _col_for_x(self, x: float, bounds: list[float]) -> int:
        for i, b in enumerate(bounds):
            if x < b:
                return i
        return len(bounds)

    # ------------------------------------------------------------------ #
    # Wrapped-date merging (handles "May 04," / "2026" split across lines)
    # ------------------------------------------------------------------ #

    def _merge_wrapped_dates(
        self, rows: list[list[str]], period_year: int
    ) -> list[list[str]]:
        result: list[list[str]] = []
        i = 0
        while i < len(rows):
            cells = list(rows[i])
            date_cell = cells[0].strip()

            # Partial date "May 04," — look ahead for the year on the next band
            if _PARTIAL_DATE_RE.match(date_cell):
                year_str = str(period_year)
                if i + 1 < len(rows):
                    nxt = rows[i + 1]
                    nxt_date = nxt[0].strip()
                    if re.match(r"^\d{4}$", nxt_date):
                        year_str = nxt_date
                        if nxt[1].strip():          # description continuation on year line
                            cells[1] += " " + nxt[1].strip()
                        # Transfer orphaned sign from money_out column of year row
                        nxt_sign = nxt[2].strip() if len(nxt) > 2 else ""
                        if nxt_sign in ("-", "+") and len(cells) > 2:
                            cells[2] = (nxt_sign + " " + cells[2]).strip() if cells[2].strip() else nxt_sign
                        # Transfer balance if year row has one and main row doesn't
                        if not cells[4].strip() and len(nxt) > 4 and nxt[4].strip():
                            cells[4] = nxt[4]
                        i += 1
                cells[0] = date_cell.rstrip(",").strip() + ", " + year_str
                result.append(cells)
                i += 1
                continue

            # Bare year row not already consumed above
            if re.match(r"^\d{4}$", date_cell):
                desc = cells[1].strip()
                if desc and result:
                    result[-1][1] += " " + desc
                i += 1
                continue

            result.append(cells)
            i += 1

        return result

    # ------------------------------------------------------------------ #
    # Text-line parsing (for OCR output)
    # ------------------------------------------------------------------ #

    def _parse_text_lines(self, text: str, period_year: int) -> list[list[str]]:
        rows: list[list[str]] = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line or _SKIP_RE.match(line):
                i += 1
                continue

            # Partial date at start of line: combine with next line's year
            m_partial = re.match(r"^([A-Za-z]{3,9}\s+\d{1,2},?)\s*(.*?)$", line)
            if m_partial and _PARTIAL_DATE_RE.match(m_partial.group(1)):
                date_str = m_partial.group(1).rstrip(",").strip()
                rest = m_partial.group(2).strip()
                year_str = str(period_year)
                if i + 1 < len(lines):
                    nxt = lines[i + 1].strip()
                    ym = re.match(r"^(\d{4})\s*(.*)", nxt)
                    if ym:
                        year_str = ym.group(1)
                        extra = ym.group(2).strip()
                        if extra:
                            rest = (rest + " " + extra).strip()
                        i += 1
                line = f"{date_str}, {year_str} {rest}"

            row = self._parse_tx_line(line)
            if row:
                rows.append(row)
            elif rows and line and not _SKIP_RE.match(line):
                # Possible description continuation
                if not re.match(r"^[\d$,\-+]", line):
                    rows[-1][1] += " " + line

            i += 1
        return rows

    def _parse_tx_line(self, line: str) -> list[str] | None:
        m = _TX_LINE_RE.match(line)
        if not m:
            return None
        amount_raw = m.group(3)
        is_out = amount_raw.lstrip().startswith("-")
        money_out = amount_raw if is_out else ""
        money_in  = amount_raw if not is_out else ""
        return [m.group(1), m.group(2), money_out, money_in, m.group(4)]

    # ------------------------------------------------------------------ #
    # Date / amount parsing
    # ------------------------------------------------------------------ #

    def _parse_full_date(self, value: str) -> date:
        value = value.strip().rstrip(",").strip()
        m = _FULL_DATE_RE.match(value)
        if not m:
            raise ValueError(f"Cannot parse date: {value!r}")
        month = MONTH_MAP.get(m.group(1)[:3].lower())
        if month is None:
            raise ValueError(f"Unknown month in: {value!r}")
        return date(int(m.group(3)), month, int(m.group(2)))

    def _parse_signed_amount(self, value: str) -> float:
        is_negative = "-" in value
        cleaned = re.sub(r"[^\d.]", "", value)
        if not cleaned:
            raise ValueError(f"Cannot parse amount: {value!r}")
        # Tesseract misreads '$' as '8'; strip the spurious leading '8'
        # when the source string has no '$'.
        if "$" not in value and cleaned.startswith("8") and len(cleaned) > 1:
            cleaned = cleaned[1:]
        amount = float(cleaned)
        return -amount if is_negative else amount

    def _parse_balance(self, value: str) -> float | None:
        """Parse a running-balance cell like '$8,663.02' or '-$1,509.74'."""
        value = value.strip()
        if not value or not re.search(r"\d", value):
            return None
        try:
            is_negative = value.startswith("-")
            cleaned = re.sub(r"[^\d.]", "", value)
            return -float(cleaned) if is_negative else float(cleaned)
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    # Build Transaction objects
    # ------------------------------------------------------------------ #

    def _build_transactions(self, rows: list[list[str]]) -> list[Transaction]:
        # Pre-pass: extract the balance value for every row so we can look ahead.
        # Rows are newest-first from the PDF.  The balance-derived amount for row i
        # is:  balance[i] - balance[next_older_row]
        # which equals the transaction's true signed amount.
        for cells in rows:
            while len(cells) < 5:
                cells.append("")
        row_balances: list[float | None] = [
            self._parse_balance(cells[4].strip()) for cells in rows
        ]

        transactions: list[Transaction] = []

        for i, cells in enumerate(rows):
            date_cell = cells[0].strip()
            desc_cell = cells[1].strip()
            balance_cell = cells[4].strip()

            # Description continuation row
            if not date_cell and desc_cell and transactions:
                transactions[-1].description += " " + desc_cell
                continue

            # Orphaned sign row: OCR placed "-" or "+" in the amount column on its
            # own band (just outside the 50px y-tolerance of the transaction row).
            # Apply it retroactively to the previous transaction's amount.
            if not date_cell and not desc_cell and transactions:
                sign_cell = cells[2].strip()
                if sign_cell == "-" and transactions[-1].amount > 0:
                    transactions[-1].amount = -transactions[-1].amount
                elif sign_cell == "+" and transactions[-1].amount < 0:
                    transactions[-1].amount = -transactions[-1].amount
                continue

            if not date_cell or not desc_cell:
                continue

            if _SKIP_RE.match(date_cell):
                continue

            try:
                tx_date = self._parse_full_date(date_cell)
            except ValueError:
                continue

            # Words from long descriptions can spill into the money_out or money_in
            # column area due to OCR layout.  Absorb any cell that contains no
            # digits (cannot be an amount) back into the description.
            for col_idx in (2, 3):
                cell_val = cells[col_idx].strip() if col_idx < len(cells) else ""
                if cell_val and not re.search(r"\d", cell_val):
                    desc_cell = (desc_cell + " " + cell_val).strip()
                    cells[col_idx] = ""

            # Parse OCR amount from money_out / money_in columns.
            amount_ocr: float | None = None
            for raw in (cells[2].strip(), cells[3].strip()):
                if raw and re.search(r"\d", raw):
                    try:
                        amount_ocr = self._parse_signed_amount(raw)
                        break
                    except ValueError:
                        pass

            # Derive amount from the running balance column.  Rows are newest-first
            # so look AHEAD (larger index = older transaction) for the next balance.
            amount_bal: float | None = None
            current_balance = row_balances[i]
            if current_balance is not None:
                for j in range(i + 1, min(i + 6, len(rows))):
                    if row_balances[j] is not None:
                        amount_bal = round(current_balance - row_balances[j], 2)
                        break

            # Choose the authoritative amount.
            # The balance column is ground-truth; if OCR amount disagrees by more
            # than $0.02, the amount column was misread (e.g. "$1,246.97" → "81,246.97").
            if amount_ocr is None:
                amount = amount_bal
            elif amount_bal is not None and abs(amount_ocr - amount_bal) > 0.02:
                amount = amount_bal   # OCR misread; trust balance
            else:
                amount = amount_ocr

            if amount is None or amount == 0:
                continue

            transactions.append(Transaction(
                transaction_date=tx_date,
                posting_date=None,
                description=desc_cell,
                amount=amount,
                raw_row=cells[:],
            ))

        return transactions
