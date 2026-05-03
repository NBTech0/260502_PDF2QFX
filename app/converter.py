from __future__ import annotations

import os
from typing import Callable, Optional

from app.models.statement import AccountType
from app.parsers.bmo_account_overview import BMOAccountOverviewParser
from app.parsers.bmo_bank import BMOBankParser
from app.parsers.bmo_mastercard import BMOMastercardParser
from app.parsers.detector import detect_statement_type, is_account_overview
from app.validator import validate_statement
from app.writers.qfx_writer import write_qfx


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"


def convert_pdf_to_qfx(
    pdf_path: str,
    output_dir: str,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> str:
    def log(msg: str) -> None:
        if progress_callback:
            progress_callback(msg)

    log(f"Detecting statement type: {os.path.basename(pdf_path)}")

    if is_account_overview(pdf_path):
        parser = BMOAccountOverviewParser()
        log("Detected: BMO Account Overview")
    else:
        account_type = detect_statement_type(pdf_path)
        type_label = {
            AccountType.CREDITCARD: "BMO Mastercard",
            AccountType.CHECKING: "BMO Chequing",
            AccountType.SAVINGS: "BMO Savings",
        }.get(account_type, account_type.value)
        log(f"Detected: {type_label}")
        if account_type == AccountType.CREDITCARD:
            parser = BMOMastercardParser()
        else:
            parser = BMOBankParser()

    log("Parsing transactions...")
    statement = parser.parse(pdf_path)
    statement.source_file = pdf_path

    log(f"Found {len(statement.transactions)} transaction(s)")

    log("Validating against PDF totals...")
    validate_statement(statement, pdf_path)
    for msg in statement.validation_warnings:
        log(msg)

    base = os.path.splitext(os.path.basename(pdf_path))[0]
    out_path = _unique_path(os.path.join(output_dir, base + ".qfx"))

    log(f"Writing: {os.path.basename(out_path)}")
    write_qfx(statement, out_path)

    # Final status reflects validation outcome
    failed = any(w.startswith("WARN") for w in statement.validation_warnings)
    if failed:
        log(f"Done with WARNINGS -- check log above before importing into Quicken")
    else:
        log(f"Done! {os.path.basename(out_path)}")
    return out_path
