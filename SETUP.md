# Local setup

## 1. Unzip and open

```bash
unzip smart-watchlist.zip
cd smart-watchlist
code .
```

## 2. Python environment

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
mkdir -p data
```

## 3. Verify it works before you change anything

```bash
PYTHONPATH=. python3 -m pytest tests/ -q   # expect 29 passed
PYTHONPATH=. python3 demo.py               # four scenarios
```

If both pass, the engine is intact and you have a working baseline to commit.

## 4. Git

```bash
cd ..                # repo root, where CLAUDE.md lives
git init
git add .
git commit -m "Signal engine, digest assembly, replay provider, 29 tests"
```

Commit at every working state. A 72-hour build with one commit at the end is a
72-hour build you cannot roll back.

## 5. Claude Code

Run `claude` from the repo root — the directory containing `CLAUDE.md`, not
`backend/`. It reads `CLAUDE.md` automatically and inherits the design
decisions, so it will not re-derive them or quietly contradict them.

Useful starting prompts:

    Read CLAUDE.md, then build the FastAPI routes listed under "Not built yet".
    Read CLAUDE.md, then write market/calendar.py with NSE hours and the
      session-fraction volume profile.
    Run the tests and explain any failure before fixing it.

Keep `PYTHONPATH=.` set, or add a `pytest.ini` with `pythonpath = .`.
