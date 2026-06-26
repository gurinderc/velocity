# Velocity

Tracks iShares MSCI World Momentum Factor ETF (IWMO) daily holdings. Detects new entries and exits, scores 5 mathematical confirmation signals, and publishes a monthly rolling HTML report via GitHub Pages.

Each day is a collapsible block in a monthly page. On the 1st of each month, the current `index.html` is sealed as `YYYY-MM.html` and a fresh one starts. Every day is permanently archived — you never miss a signal.

**Confirmation score (0–5)** — one point per signal:

| # | Signal | What it checks |
|---|---|---|
| 1 | Price ROC (20d) > 0 | Momentum still running post-inclusion |
| 2 | Weight growing since entry | ETF adding conviction |
| 3 | Weight > 50th percentile | Meaningful position, not a token entry |
| 4 | Price above entry price | Not fading post-inclusion |
| 5 | Within 20% of 52-week high | Not overextended |

**4–5 = act · 3 = watch · 0–2 = wait**

When `GROQ_API_KEY` is set, the script calls **llama-3.3-70b-versatile** via Groq for a 3-sentence analyst note on each new entry. Groq free tier (1,000 req/day) is sufficient for one call per trading day.

---

## Structure

One repo, two branches:

```
main      ← velocity.py, velocity.sh, .gitignore, README.md
report    ← index.html, YYYY-MM.html, YYYY-MM.json  (GitHub Pages serves this)
```

The script and SQLite DB live on your cloud server. Only generated HTML and JSON are committed.

---

## Files

```
velocity.py     ← main tracker script
velocity.sh     ← cron wrapper (fetch → analyse → commit → push report branch)
.gitignore
README.md
```

---

## Prerequisites

- Linux cloud server with Python 3.9+
- `pip3 install requests`
- Groq API key (free): https://console.groq.com — email signup only
- GitHub account

---

## 1. Create the repo

```bash
gh repo create velocity --public
```

Or create it in the GitHub UI. Clone it:

```bash
git clone https://YOUR_PAT@github.com/yourusername/velocity.git ~/velocity
```

---

## 2. Push source files to `main`

```bash
cd ~/velocity
# copy in velocity.py, velocity.sh, .gitignore, README.md
git add .
git commit -m "init"
git push origin main
```

---

## 3. Create the `report` branch

```bash
git checkout --orphan report
git rm -rf .
echo "# Velocity Report" > README.md
git add README.md
git commit -m "init report branch"
git push origin report
git checkout main
```

`--orphan` creates a branch with no shared history with `main` — clean separation.

---

## 4. Enable GitHub Pages

1. GitHub repo → **Settings** → **Pages**
2. Source: **Deploy from a branch**
3. Branch: **report** · Folder: **/ (root)**
4. **Save**

Report will be live at `https://yourusername.github.io/velocity`

---

## 5. Clone the report branch on your server

```bash
mkdir -p ~/repos
git clone --branch report https://YOUR_PAT@github.com/yourusername/velocity.git ~/repos/velocity
```

This is a separate working directory checked out on the `report` branch. The script writes files here and pushes from here.

---

## 6. Create `.env`

```bash
cat > ~/velocity/.env << 'ENVEOF'
GROQ_API_KEY=gsk_your_key_here
ENVEOF
chmod 600 ~/velocity/.env
```

---

## 7. Configure `velocity.sh`

The defaults in `velocity.sh` are already set correctly:

```bash
SCRIPT_DIR="$HOME/velocity"
REPO="$HOME/repos/velocity"
```

Make it executable:

```bash
chmod +x ~/velocity/velocity.sh
```

---

## 8. Test run

```bash
python3 ~/velocity/velocity.py \
    --db ~/velocity/velocity.db \
    --report-dir ~/repos/velocity \
    --no-ai
```

Confirm `~/repos/velocity/index.html` is created, then test with Groq:

```bash
python3 ~/velocity/velocity.py \
    --db ~/velocity/velocity.db \
    --report-dir ~/repos/velocity
```

Check the log output for `Groq commentary OK`.

Then do a manual push to confirm the full flow:

```bash
~/velocity/velocity.sh
```

Visit `https://yourusername.github.io/velocity` — should be live within a minute.

---

## 9. Cron

```bash
crontab -e
```

Add:

```
# Velocity — weekdays 20:00 UTC (21:00 BST, after iShares updates)
0 20 * * 1-5 $HOME/velocity/velocity.sh >> $HOME/logs/velocity.log 2>&1
```

Create the logs directory if it doesn't exist:

```bash
mkdir -p ~/logs
```

---

## Command reference

| Flag | Default | Purpose |
|---|---|---|
| `--db PATH` | `./velocity.db` | SQLite database path |
| `--report-dir PATH` | `./velocity-report` | HTML/JSON output folder |
| `--no-ai` | off | Skip Groq even if key is set |
| `--report-only` | off | Regenerate HTML without fetching |
| `--mock-csv PATH` | off | Use a local CSV file for testing |

---

## Directory layout on server

```
~/velocity/                  ← main branch clone (source + DB)
├── velocity.py
├── velocity.sh
├── velocity.db              ← SQLite, ~4MB/year, never committed
├── .env                     ← chmod 600, never committed
├── .gitignore
└── README.md

~/repos/velocity/            ← report branch clone (GitHub Pages output)
├── index.html               ← current month, updated daily
├── 2026-06.json             ← current month data log
├── 2026-05.html             ← sealed previous month
├── 2026-05.json
└── ...
```

---

## iShares 403 fallback

If iShares blocks the Python fetch, replace the `python3` call in `velocity.sh` with:

```bash
curl -s -L \
  -H "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36" \
  -H "Referer: https://www.ishares.com/uk/" \
  "https://www.ishares.com/uk/individual/en/products/270051/ishares-msci-world-momentum-factor-ucits-etf/1506575576011.ajax?fileType=csv&fileName=IWMO_holdings&dataType=fund" \
  -o /tmp/velocity_latest.csv

python3 "$SCRIPT_DIR/velocity.py" \
    --mock-csv /tmp/velocity_latest.csv \
    --db       "$SCRIPT_DIR/velocity.db" \
    --report-dir "$REPO"
```
