# RABT (رابط)

Multi-role digital services marketplace (Libya): customers, engineers, companies, agents; payments, escrow, fraud detection.

## Layout
- `ledger-engine/` — double-entry ledger + escrow + SHA-256 hash chain (Python, FastAPI layer). See its README and AUDIT.md.

Other components (Flask API, Flutter unified app, separate Flutter admin app, PHP back-office) are not in this repository yet.

## Rules
- Never commit `.env`, API keys, `*.db`, or KYC files.
- The admin app must live in its own repository/pipeline and never ship inside the customer app.
