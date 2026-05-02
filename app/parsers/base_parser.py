from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import date, datetime

from app.models.statement import Statement


class ParseError(Exception):
    pass


class UnknownStatementError(ParseError):
    pass


class NoTransactionsFoundError(ParseError):
    pass


MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class BaseParser(ABC):

    @abstractmethod
    def parse(self, pdf_path: str) -> Statement:
        ...

    def _parse_dollar(self, value: str) -> float:
        cleaned = re.sub(r"[^\d.]", "", value.strip())
        if not cleaned:
            raise ValueError(f"Cannot parse dollar amount: {value!r}")
        return float(cleaned)

    def _parse_date(self, value: str, statement_year: int, prev_month: int | None = None) -> date:
        value = value.strip().rstrip(".")

        # ISO format: 2024-03-15
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))

        # Numeric: 03/15 or 03-15
        m = re.match(r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?$", value)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            year = statement_year
            if m.group(3):
                y = int(m.group(3))
                year = y if y > 99 else 2000 + y
            return self._infer_year(month, day, year, prev_month)

        # Month name: "Mar 15", "Mar. 15", "Mar.15", "Dec.4" (BMO format, no space)
        m = re.match(r"([A-Za-z]{3,9})\.?\s*(\d{1,2})(?:,?\s*(\d{4}))?$", value)
        if m:
            month_str = m.group(1)[:3].lower()
            month = MONTH_MAP.get(month_str)
            if month is None:
                raise ValueError(f"Unknown month: {m.group(1)!r}")
            day = int(m.group(2))
            year = int(m.group(3)) if m.group(3) else statement_year
            return self._infer_year(month, day, year, prev_month)

        raise ValueError(f"Cannot parse date: {value!r}")

    def _infer_year(self, month: int, day: int, base_year: int, prev_month: int | None) -> date:
        # base_year is the START year of the statement period.
        # December → January rollover: previous tx was in Dec, this one is Jan → next year
        year = base_year
        if prev_month is not None and prev_month == 12 and month == 1:
            year = base_year + 1
        return date(year, month, day)

    def _extract_account_number(self, full_text: str) -> str:
        patterns = [
            # BMO Mastercard PDF: "CardNumber 5524890004034708"
            r"[Cc]ard\s*[Nn]umber\s+(\d{12,19})",
            # Generic account number with label
            r"[Aa]ccount\s+[Nn]umber[:\s]+[\*x]{0,4}([\d]{4,})",
            r"[Aa]ccount\s*#?\s*:?\s*([\*x\d]{4,}[-\s]?[\*x\d]{4,}[-\s]?[\*x\d]{0,4})",
            r"(\d{4}[-\s]\d{4}[-\s]\d{4})",
            r"ending\s+in\s+(\d{4})",
        ]
        for pat in patterns:
            m = re.search(pat, full_text)
            if m:
                return m.group(1).strip()
        return "UNKNOWN"

    def _extract_statement_year(self, full_text: str) -> int:
        # Try to find the period start year from "Dec.6,2018-Jan.5,2019" style ranges
        m = re.search(r"\b(20\d{2})\s*[-–]\s*[A-Za-z]", full_text)
        if m:
            return int(m.group(1))
        # Try "Dec.6,2018" or "December 6, 2018" in period header
        m = re.search(r"[A-Za-z]{3,9}\.?\s*\d{1,2},?\s*(20\d{2})", full_text)
        if m:
            return int(m.group(1))
        # Fallback: first 4-digit year in text
        m = re.search(r"\b(20\d{2})\b", full_text)
        if m:
            return int(m.group(1))
        return datetime.now().year
