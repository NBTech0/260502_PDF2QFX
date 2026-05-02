import pdfplumber

from app.models.statement import AccountType
from app.parsers.base_parser import UnknownStatementError


def detect_statement_type(pdf_path: str) -> AccountType:
    try:
        with pdfplumber.open(pdf_path) as pdf:
            pages_to_check = pdf.pages[:2]
            combined = ""
            for page in pages_to_check:
                text = page.extract_text() or ""
                combined += text.lower() + "\n"
    except Exception as e:
        raise UnknownStatementError(f"Could not open PDF: {e}") from e

    if "mastercard" in combined or "master card" in combined or "credit card" in combined:
        return AccountType.CREDITCARD
    if "chequing" in combined or "checking" in combined:
        return AccountType.CHECKING
    if "savings" in combined or "smart saver" in combined or "high interest" in combined:
        return AccountType.SAVINGS
    # Fallback: look for bank statement column headers
    if "withdrawals" in combined or "deposits" in combined:
        return AccountType.CHECKING

    raise UnknownStatementError(
        "Could not determine statement type. "
        "Expected markers: 'mastercard', 'chequing', 'savings', or 'withdrawals'/'deposits'."
    )
