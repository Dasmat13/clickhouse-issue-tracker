# ClickHouse Issue Tracker & Prioritizer

Tracks, scores, and triages open ClickHouse GitHub issues to find high-value, actionable bugs that have no pull requests and no assignees.

## Database
Issues are stored locally in SQLite at `clickhouse-tracker/issues.db`.

## Usage

### 1. Sync issues from ClickHouse repo
```bash
# Sync top 1000 issues
python3 tracker.py sync --limit 1000

# Or sync full backlog (up to 5000)
python3 tracker.py sync --limit 5000
```

### 2. Find Actionable High-Priority Issues (CLI)
```bash
# Top 20 actionable issues (automatically filters out issues with PRs/assignees)
python3 tracker.py list

# Filter by component (e.g., mergetree, json, analyzer, formats)
python3 tracker.py list --component mergetree
python3 tracker.py list --component json

# Search by keyword
python3 tracker.py list --query "memory leak"
```

### 3. View Component Breakdown
```bash
python3 tracker.py components
```

### 4. Interactive Web Dashboard
```bash
python3 tracker.py serve --port 8088
```
Open `http://localhost:8088` in your browser for live search, tier filtering, and one-click GitHub links.
