import pdfplumber

from app.models.statement import AccountType
from app.parsers.base_parser import UnknownStatementError


def is_account_overview(pdf_path: str) -> bool:
    """Return True if the PDF is a BMO 'Account Overview' web-print export."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            # Image-based (browser print): identify by PDF title metadata
            title = (pdf.metadata.get("Title") or "").lower()
            if "account overview" in title:
                return True
            # Text-based version: column headers unique to this format
            text = (pdf.pages[0].extract_text() or "").lower()
            return "money out" in text and "money in" in text
    except Exception:
        return False


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
