LEDGER & SIGNAL - portable build
=================================

WHAT IS IN HERE
  ledger.pyz         the whole app in one file (needs Python 3.10+, no pip installs)
  run-windows.bat    double-click this on Windows - runs all three checks below
  run.sh             same, for macOS/Linux

QUICK START (Windows)
  1. Install Python 3.10+ from python.org, ticking "Add python.exe to PATH".
  2. Double-click run-windows.bat.

  Or from a terminal, in this folder:
      python ledger.pyz status
      python ledger.pyz selftest
      python ledger.pyz probe --year 2024


ANSWERING "IS IT IMPORTING REAL VALUES?"
  Run:  python ledger.pyz probe --year 2024

  It downloads the House Clerk's real disclosure index and prints real
  representatives' names, filing dates and document ids - then downloads one
  real filing and shows the transactions parsed out of it.

  Every document id it prints is checkable by hand. Paste one into:
      https://disclosures-clerk.house.gov/PublicDisclosure/FinancialDisclosure

  If the names and dates match, the import is real. If the command errors, the
  message is the actual reason - it never falls back to invented data.

  Note: `probe` writes nothing to disk. Use `ingest-house` to actually store.


THE THREE COMMANDS
  status     Tries a real HTTP request to each source and reports ok / BLOCKED.
             A connect-only check would wrongly say "reachable" behind a proxy,
             so this issues a genuine request.

  selftest   Runs the full pipeline offline on a deterministic price path. Two
             filers make IDENTICAL trades and differ only in disclosure speed:

                 FAST  2-day lag    keeps 99% of its alpha
                 SLOW  120-day lag  keeps 30%

             Same trades, same returns, very different usefulness. That gap is
             what this product measures. No network needed.

  probe      Live fetch, described above.


BUILDING A REAL ledger.exe
  A Windows .exe must be built on Windows - PyInstaller bundles the host OS's
  Python runtime, so there is no cross-compile from Linux. From the repo root:

      py -m pip install pyinstaller
      py -m PyInstaller packaging\ledger.spec

  Produces dist\ledger.exe (~10 MB), which runs with no Python installed:

      ledger.exe probe --year 2024


OPTIONAL: READING SCANNED FILINGS
  Many older filings are scanned images with no text layer. `probe` will say so
  rather than guessing. To read them:

      pip install pdfminer.six          (digital PDFs)
      pip install pytesseract pdf2image (scans - also needs Tesseract + Poppler)


WHAT IS NOT HERE YET
  Price history, and therefore returns, alpha and rankings. Those need a price
  source; see the repo README. Until then the scoring engine runs only on the
  self-test's synthetic prices, and no real return figure is shown anywhere.
