# BMO PDF to QFX Converter — Project Notes

## Overview
Windows desktop app (Python 3.11+, CustomTkinter) that converts BMO Bank, BMO Mastercard, BMO Line of Credit (LOC), and BMO Account Overview PDF statements to Quicken QFX files. Entry point: `main.py`.

## Project Structure
```
main.py
requirements.txt
app/
  converter.py           # Orchestrator: detect → parse → write QFX
  validator.py           # Post-parse validation + balance-chain check
  models/
    transaction.py       # Transaction dataclass
    statement.py         # Statement dataclass + AccountType enum
  parsers/
    base_parser.py       # ABC + shared utilities
    detector.py          # Auto-detects statement type from PDF text
    bmo_bank.py          # Regular BMO bank statements (table layout)
    bmo_mastercard.py    # BMO Mastercard statements
    bmo_loc.py           # BMO Personal Line of Credit statements (text layout)
    bmo_account_overview.py  # Browser-printed Account Overview PDFs (OCR)
  writers/
    qfx_writer.py        # Renders Statement → OFX v1.02 SGML .qfx
  gui/
    main_window.py       # Root window; persists output folder to %APPDATA%\BMOConverter\config.json
    file_list.py         # Scrollable file list with status
    drop_zone.py         # Drag-and-drop target
    log_panel.py         # Append-only thread-safe log
```

## Dependencies
```
customtkinter>=5.2.2
pdfplumber>=0.11.0
tkinterdnd2>=0.3.0
Pillow>=10.0.0
pytesseract>=0.3.10
```
**Tesseract OCR** must also be installed separately and on PATH (required for Account Overview PDFs only).

## Parser: BMO Account Overview (`bmo_account_overview.py`)
Handles browser-printed "Account Overview" PDFs — not true PDFs, rendered via CSS from BMO's web UI.

### Key behaviours
- Uses `pdfplumber` for text extraction; falls back to Tesseract OCR (`--psm 6`) when the page is image-only.
- `_rows_from_word_boxes`: groups OCR words into rows using a **fixed-anchor** y-tolerance band (50 px). The anchor is `band[0]["top"]` — do NOT change to `band[-1]["top"]` (expanding window), it merges adjacent rows and breaks everything.
- `_merge_wrapped_dates`: handles two-line dates ("Nov 14," / "2025"). When consuming the year-continuation row it also transfers:
  - `cells[1]` — description continuation
  - `cells[2]` — orphaned sign character (`-` or `+`) that CSS vertical alignment places slightly below the row's anchor, causing it to land in the year row instead of the main row
  - `cells[4]` — balance, if the main row has none
- `_parse_signed_amount`: strips a leading `8` when the source string has no `$` sign — Tesseract misreads `$` as `8` in this PDF format.
- `_build_transactions`: orphaned-sign handler (safety net) retroactively flips the sign of the previous transaction if a lone `-` or `+` row appears with no date/description.

### OCR sign issue (root cause documented)
BMO's CSS renders the `-` sign in the "Money out" column at a slightly lower vertical position than the rest of the row. In a 300 DPI render this is ~52 px from the band anchor, just 2 px past the 50 px tolerance, so it falls into the year-continuation row. `_merge_wrapped_dates` now transfers it back.

## Parser: BMO Line of Credit (`bmo_loc.py`)
Handles BMO Personal Line of Credit monthly statements (e.g. `January 11, 2026.pdf`).

### Key behaviours
- **Two-column layout**: transactions on the left side; an "at a glance" summary on the right. pdfplumber inserts `!` characters at column boundaries — each line is truncated at the first `!` to strip the right column.
- **Right-column overflow on transaction lines**: some transaction lines have the at-a-glance text appended after the amount (e.g. `1,000.00 - !Credit adjustments`). The line is further truncated at the first amount (`[\d,]+\.\d{2}(?:\s*CR?)?`) so everything after it is discarded.
- **Stray-dot dates**: BMO renders dates as `"Dec .18"`, `"Jan. .6"`, `"Jan 11"`. The `_norm_date()` function strips the stray dot before the day: `re.sub(r"([A-Za-z]{3}\.?)\s*\.\s*(\d)", r"\1 \2", s)`.
- **Bold-text doubling**: BMO renders bold descriptions by printing each character twice at a slight offset. pdfplumber merges them into short overlapping tokens: `"C CA AS SH H A AD DV VA AN NC CE E"`. `_fix_doubled_text()` detects chains of 1–2 char tokens where each starts with the last char of the accumulated result and collapses them back to the original string (e.g. `"CASH ADVANCE"`).
- **Cross-year statement**: the statement spans December (year N) through January (year N+1). The statement date is January of N+1, so `_extract_statement_year` overrides the base to return N. `_build_transactions` tracks `current_year` (updated from each `tx_date.year`) so that after the Dec→Jan rollover, all subsequent January transactions remain in year N+1.
- **Continuation description lines**: some descriptions render a few pixels below their data row and appear as `". . DESCRIPTION"` lines. The parser appends these to the pending transaction's description.
- **Amount regex**: `([\d,]+\.\d{2}(?:\s*CR?)?)` — the `CR?` suffix is fully optional (`(?:...)?`). Without this, plain amounts (e.g. `1,000.00`) fail to match.

### QFX output
LOC uses `CREDITCARDMSGSRSV1` / `<CCACCTFROM>` (same as Mastercard) with `INTU.BID=00001` (bank product, not Mastercard's `00017`).

### Tested PDFs
- `January 11, 2026.pdf` — 7 transactions (Dec 2025 – Jan 2026), balance check PASSED ($63,041.58 → $64,151.50)

## Validator (`validator.py`)
Runs after parsing. Routes by account type:
- **CREDITCARD** → `_validate_cc`: checks Previous Balance / New Balance.
- **LOC** → `_validate_loc`: checks "Previous balance" / "New account balance" (LOC-specific line labels).
- **CHECKING / SAVINGS** → `_validate_bank` + balance-chain check.

**Balance-chain check** (bank only): for each consecutive pair of transactions verifies `balance[i] + amount[i+1] ≈ balance[i+1]` (tolerance $0.02). Flags mismatches as `WARN` lines; prints `CHAIN N checks: M OK, K flagged` summary.

Raw per-row balances are read from `txn.raw_row[4]` (the OCR'd balance column stored on each Transaction).

## QFX Writer (`qfx_writer.py`)
OFX v1.02 SGML format. Key rules learned from Quicken import testing:

- **`<INTU.BID>` must be present** in `<SONRS>` — `00001` for bank/LOC, `00017` for Mastercard. Removing it causes Quicken OL-221-A on import.
- **`<LEDGERBAL>` must be present** for bank accounts — omitting it causes Quicken OL-221-A. Falls back to `0.00` if the PDF did not include a closing balance.
- **`<MEMO>` is always written** (not just when description > 32 chars) — Quicken replaces `<NAME>` with the linked account name for transfer transactions (e.g. "TF …"), hiding the reference number. `<MEMO>` preserves the full description in the notes field.
- `<NAME>` is capped at 32 chars; `<MEMO>` at 255 chars.
- FITID format: `{account_digits}{YYYYMMDD}{index:07d}`.

## GUI (`main_window.py`)
- Output folder is persisted to `%APPDATA%\BMOConverter\config.json` via a `StringVar` trace. Restored on startup; falls back to `~\Documents` if the saved path no longer exists.
- Convert button reset uses `self.after(0, lambda: self._convert_btn.configure(...))` — CTkButton does not accept a positional dict like standard tkinter, so the lambda form is required.
- Conversions run in a `daemon=True` background thread; GUI updates use `self.after(0, fn)`.

## Tested PDFs
- `Account overview - BMO.pdf` — 100 transactions, all balance-chain checks OK
- `Account overview - BMO - 2.pdf` — 100 transactions, all balance-chain checks OK
- `Account overview - BMO - 3.pdf` — 16 transactions, all balance-chain checks OK (Nov 14 sign fix verified)
- `January 11, 2026.pdf` (LOC) — 7 transactions (Dec 2025 – Jan 2026), balance check PASSED

## Known Limitations
- Account Overview PDFs with page breaks mid-transaction may produce flagged balance-chain entries; user can correct manually. In practice BMO does not break transactions across pages.
- Opening balance is not present in Account Overview PDFs; balance check uses closing balance only.
