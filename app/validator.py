from __future__ import annotations

import re

import pdfplumber

from app.models.statement import AccountType, Statement

_TOLERANCE = 0.02   # dollars — allow for rounding in multi-page statements


def _find_amount(text: str, *patterns: str) -> float | None:
    """Search text for the first pattern match and return it as a float."""
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = re.sub(r"[^\d.]", "", m.group(1))
            if raw:
                return float(raw)
    return None


def validate_statement(statement: Statement, pdf_path: str) -> None:
    """
    Reads summary figures from the PDF and compares them to the parsed
    transactions. Appends human-readable messages to statement.validation_warnings.
    Pass/fail lines are always appended so the log always shows a summary.
    """
    warnings = statement.validation_warnings

    try:
        with pdfplumber.open(pdf_path) as pdf:
            full_text = "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception as e:
        warnings.append(f"WARN  Could not re-read PDF for validation: {e}")
        return

    txns = statement.transactions
    n = len(txns)
    n_debit  = sum(1 for t in txns if t.amount < 0)
    n_credit = sum(1 for t in txns if t.amount >= 0)
    total_debits  = sum(t.amount for t in txns if t.amount < 0)   # negative
    total_credits = sum(t.amount for t in txns if t.amount >= 0)  # positive
    net = sum(t.amount for t in txns)

    warnings.append(
        f"TRANS {n} transactions parsed  "
        f"({n_debit} charges / {n_credit} payments)"
    )
    warnings.append(
        f"TOTAL Charges: ${abs(total_debits):,.2f}   "
        f"Payments/Credits: ${total_credits:,.2f}   "
        f"Net: ${net:+,.2f}"
    )

    if statement.account_type == AccountType.CREDITCARD:
        _validate_cc(full_text, net, warnings)
    else:
        _validate_bank(full_text, net, warnings)


def _validate_cc(text: str, net: float, warnings: list[str]) -> None:
    # BMO PDF compresses text: "PreviousBalance,Dec.5,2018 $23,211.46"
    # Use [^\n]* to skip any date text, then anchor on the $ sign
    prev = _find_amount(
        text,
        r"[Pp]revious\s*[Bb]alance[^\n]*?\$([\d,]+\.\d{2})",
        r"[Pp]revious\s*[Bb]alance[^\n]*([\d,]+\.\d{2})",
    )
    new_ = _find_amount(
        text,
        r"[Nn]ew\s*[Bb]alance[^\n]*?\$([\d,]+\.\d{2})",
        r"[Nn]ew\s*[Bb]alance[^\n]*([\d,]+\.\d{2})",
    )

    if prev is None or new_ is None:
        warnings.append("WARN  Could not find Previous/New Balance in PDF — skipping balance check")
        return

    warnings.append(f"PDF   Previous Balance: ${prev:,.2f}   New Balance: ${new_:,.2f}")

    # Convention: balance = amount you OWE (positive).
    # Our transactions: charges are negative, payments positive.
    # new_balance = previous_balance - net  (net is negative when you spent more than paid)
    expected_new = prev - net
    diff = abs(expected_new - new_)

    if diff <= _TOLERANCE:
        warnings.append(
            f"OK    Balance check PASSED  "
            f"(${prev:,.2f} - (${net:+,.2f}) = ${expected_new:,.2f}, expected ${new_:,.2f})"
        )
    else:
        warnings.append(
            f"WARN  Balance check FAILED  "
            f"Expected new balance ${expected_new:,.2f}, PDF says ${new_:,.2f}  "
            f"(difference ${diff:,.2f}) ({_diff_hint(diff)})"
        )


def _validate_bank(text: str, net: float, warnings: list[str]) -> None:
    opening = _find_amount(
        text,
        r"[Oo]pening\s+[Bb]alance[^0-9$]{0,20}([\d,]+\.\d{2})",
    )
    closing = _find_amount(
        text,
        r"[Cc]losing\s+[Bb]alance[^0-9$]{0,20}([\d,]+\.\d{2})",
    )

    if opening is None or closing is None:
        warnings.append("WARN  Could not find Opening/Closing Balance in PDF — skipping balance check")
        return

    warnings.append(f"PDF   Opening Balance: ${opening:,.2f}   Closing Balance: ${closing:,.2f}")

    expected_closing = opening + net
    diff = abs(expected_closing - closing)

    if diff <= _TOLERANCE:
        warnings.append(
            f"OK    Balance check PASSED  "
            f"(${opening:,.2f} + ${net:+,.2f} = ${expected_closing:,.2f}, expected ${closing:,.2f})"
        )
    else:
        warnings.append(
            f"WARN  Balance check FAILED  "
            f"Expected closing ${expected_closing:,.2f}, PDF says ${closing:,.2f}  "
            f"(difference ${diff:,.2f}) ({_diff_hint(diff)})"
        )


def _diff_hint(diff: float) -> str:
    if diff < 1:
        return "likely a rounding difference"
    return "possible missing or duplicate transactions"
