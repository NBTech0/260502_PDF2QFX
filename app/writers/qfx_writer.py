from __future__ import annotations

from datetime import date, datetime

from app.models.statement import AccountType, Statement
from app.models.transaction import Transaction


class QFXWriteError(Exception):
    pass


# No blank line between header and <OFX> — matches BMO's own QFX downloads
_OFX_HEADER = """\
OFXHEADER:100
DATA:OFXSGML
VERSION:102
SECURITY:NONE
ENCODING:USASCII
CHARSET:1252
COMPRESSION:NONE
OLDFILEUID:NONE
NEWFILEUID:NONE
"""

_BMO_INTU_BID_CC   = "00017"   # BMO Mastercard (from reference file)
_BMO_INTU_BID_BANK = "00001"   # BMO Bank (institution number 001)
_BMO_BANKID        = "00001"


def _fmt_date(d: date | None) -> str:
    if d is None:
        return datetime.now().strftime("%Y%m%d") + "000000"
    return d.strftime("%Y%m%d") + "000000"


def _fmt_server_dt() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + ".000[-5:EST]"


def _trntype(amount: float) -> str:
    return "CREDIT" if amount >= 0 else "DEBIT"


def _fitid(txn: Transaction, account_id: str, index: int) -> str:
    # Prefix with account ID (digits only) to ensure global uniqueness, matching BMO format
    acct_digits = "".join(c for c in account_id if c.isdigit())[:16]
    return acct_digits + txn.transaction_date.strftime("%Y%m%d") + f"{index:07d}"


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )


def _render_transactions(transactions: list[Transaction], account_id: str) -> str:
    lines = []
    for i, txn in enumerate(transactions, start=1):
        fitid = _fitid(txn, account_id, i)
        lines.append("<STMTTRN>")
        lines.append(f"<TRNTYPE>{_trntype(txn.amount)}")
        lines.append(f"<DTPOSTED>{_fmt_date(txn.posting_date or txn.transaction_date)}")
        lines.append(f"<TRNAMT>{txn.amount:.2f}")
        lines.append(f"<FITID>{fitid}")
        name = _esc(txn.description[:32])
        lines.append(f"<NAME>{name}")
        if len(txn.description) > 32:
            lines.append(f"<MEMO>{_esc(txn.description[:255])}")
        lines.append("</STMTTRN>")
    return "\n".join(lines)


def _date_range(transactions: list[Transaction]) -> tuple[str, str]:
    if not transactions:
        now = _fmt_date(None)
        return now, now
    dates = [t.transaction_date for t in transactions]
    return _fmt_date(min(dates)), _fmt_date(max(dates))


def _signon_block(server_dt: str, intu_bid: str) -> str:
    return (
        "<SIGNONMSGSRSV1>\n"
        "<SONRS>\n"
        "<STATUS>\n"
        "<CODE>0\n"
        "<SEVERITY>INFO\n"
        "<MESSAGE>OK\n"
        "</STATUS>\n"
        f"<DTSERVER>{server_dt}\n"
        "<LANGUAGE>ENG\n"
        f"<INTU.BID>{intu_bid}\n"
        "</SONRS>\n"
        "</SIGNONMSGSRSV1>"
    )


def _bank_body(stmt: Statement, txn_block: str, dt_start: str, dt_end: str) -> str:
    acct_type = stmt.account_type.value  # CHECKING or SAVINGS
    bal_block = ""
    if stmt.ledger_balance is not None:
        bal_dt = _fmt_date(stmt.ledger_balance_date)
        bal_block = (
            "<LEDGERBAL>\n"
            f"<BALAMT>{stmt.ledger_balance:.2f}\n"
            f"<DTASOF>{bal_dt}\n"
            "</LEDGERBAL>\n"
        )
    return (
        "<BANKMSGSRSV1>\n"
        "<STMTTRNRS>\n"
        "<TRNUID>1\n"
        "<STATUS>\n"
        "<CODE>0\n"
        "<SEVERITY>INFO\n"
        "<MESSAGE>OK\n"
        "</STATUS>\n"
        "<STMTRS>\n"
        f"<CURDEF>{stmt.currency}\n"
        "<BANKACCTFROM>\n"
        f"<BANKID>{_BMO_BANKID}\n"
        f"<ACCTID>{_esc(stmt.account_id)}\n"
        f"<ACCTTYPE>{acct_type}\n"
        "</BANKACCTFROM>\n"
        "<BANKTRANLIST>\n"
        f"<DTSTART>{dt_start}\n"
        f"<DTEND>{dt_end}\n"
        f"{txn_block}\n"
        "</BANKTRANLIST>\n"
        f"{bal_block}"
        "</STMTRS>\n"
        "</STMTTRNRS>\n"
        "</BANKMSGSRSV1>"
    )


def _cc_body(stmt: Statement, txn_block: str, dt_start: str, dt_end: str) -> str:
    bal_block = ""
    if stmt.ledger_balance is not None:
        bal_dt = _fmt_date(stmt.ledger_balance_date)
        bal_block = (
            "<LEDGERBAL>\n"
            f"<BALAMT>{stmt.ledger_balance:.2f}\n"
            f"<DTASOF>{bal_dt}\n"
            "</LEDGERBAL>\n"
            "<AVAILBAL>\n"
            f"<BALAMT>{stmt.ledger_balance:.2f}\n"
            f"<DTASOF>{bal_dt}\n"
            "</AVAILBAL>\n"
        )
    return (
        "<CREDITCARDMSGSRSV1>\n"
        "<CCSTMTTRNRS>\n"
        "<TRNUID>1\n"
        "<STATUS>\n"
        "<CODE>0\n"
        "<SEVERITY>INFO\n"
        "<MESSAGE>OK\n"
        "</STATUS>\n"
        "<CCSTMTRS>\n"
        f"<CURDEF>{stmt.currency}\n"
        "<CCACCTFROM>\n"
        f"<ACCTID>{_esc(stmt.account_id)}\n"
        "</CCACCTFROM>\n"
        "<BANKTRANLIST>\n"
        f"<DTSTART>{dt_start}\n"
        f"<DTEND>{dt_end}\n"
        f"{txn_block}\n"
        "</BANKTRANLIST>\n"
        f"{bal_block}"
        "</CCSTMTRS>\n"
        "</CCSTMTTRNRS>\n"
        "</CREDITCARDMSGSRSV1>"
    )


def write_qfx(statement: Statement, output_path: str) -> None:
    server_dt = _fmt_server_dt()
    intu_bid = _BMO_INTU_BID_CC if statement.account_type == AccountType.CREDITCARD else _BMO_INTU_BID_BANK
    txn_block = _render_transactions(statement.transactions, statement.account_id)
    dt_start, dt_end = _date_range(statement.transactions)

    signon = _signon_block(server_dt, intu_bid)

    if statement.account_type == AccountType.CREDITCARD:
        body = _cc_body(statement, txn_block, dt_start, dt_end)
    else:
        body = _bank_body(statement, txn_block, dt_start, dt_end)

    content = _OFX_HEADER + "<OFX>\n" + signon + "\n" + body + "\n</OFX>\n"

    try:
        with open(output_path, "w", encoding="ascii", errors="replace") as f:
            f.write(content)
    except OSError as e:
        raise QFXWriteError(f"Could not write QFX file: {e}") from e
