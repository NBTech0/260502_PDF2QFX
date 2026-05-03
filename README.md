# BMO PDF to QFX Converter

Windows desktop app that converts BMO Bank and BMO Mastercard PDF statements to Quicken QFX files.

## Supported statement types

- BMO chequing / savings — column layout: Date | Description | Withdrawals | Deposits | Balance
- BMO Mastercard — column layout: Transaction Date | Posting Date | Description | Amount
- BMO Account Overview (browser print) — OCR-based, paginated web-print format

## Requirements

- Python 3.11+
- [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) installed and on PATH (required for Account Overview PDFs)

```
pip install -r requirements.txt
```

## Usage

```
python main.py
```

Drag and drop PDF files onto the window, choose an output folder, and click **Convert All**. Each PDF produces a `.qfx` file in the selected folder.

## Validation

After parsing, the converter checks:

- Transaction count and net amount against the PDF's closing balance
- Per-transaction balance chain: `balance[i] + amount[i+1] ≈ balance[i+1]`

Any discrepancies are logged as `WARN` lines in the output panel.
