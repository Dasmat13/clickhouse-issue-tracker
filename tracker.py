#!/usr/bin/env python3
"""
ClickHouse Issue Tracker & Prioritizer
Fetches, scores, groups, and triages ClickHouse GitHub issues.
"""

import sys
import os
import json
import sqlite3
import argparse
import datetime
import urllib.request
import yaml
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "issues.db")

def get_github_token():
    # 1. From env
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    # 2. From gh hosts.yml
    hosts_path = os.path.expanduser("~/.config/gh/hosts.yml")
    if os.path.exists(hosts_path):
        try:
            with open(hosts_path, "r") as f:
                cfg = yaml.safe_load(f)
                token = cfg.get("github.com", {}).get("oauth_token")
                if token:
                    return token
        except Exception:
            pass
    return None

def init_db(conn):
    conn.execute("""
    CREATE TABLE IF NOT EXISTS issues (
        number INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        comments_count INTEGER DEFAULT 0,
        reactions_count INTEGER DEFAULT 0,
        assignees TEXT,
        has_assignee INTEGER DEFAULT 0,
        labels TEXT,
        component TEXT,
        has_pr INTEGER DEFAULT 0,
        pr_numbers TEXT,
        priority_score INTEGER DEFAULT 0,
        tier TEXT,
        usefulness TEXT,
        raw_json TEXT
    );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tier ON issues(tier);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_score ON issues(priority_score);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_component ON issues(component);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_has_pr ON issues(has_pr);")
    conn.commit()

def calculate_triage(node):
    labels = [l.get("name", "") for l in node.get("labels", {}).get("nodes", [])]
    lower_labels = [l.lower() for l in labels]
    
    # Extract component (e.g. comp-mergetree)
    component = "other"
    for l in labels:
        if l.startswith("comp-"):
            component = l
            break
            
    assignees = [a.get("login", "") for a in node.get("assignees", {}).get("nodes", [])]
    has_assignee = 1 if len(assignees) > 0 else 0
    
    # Check for linked PRs in timeline
    pr_numbers = []
    for event in node.get("timelineItems", {}).get("nodes", []):
        evt_type = event.get("__typename")
        if evt_type == "CrossReferencedEvent":
            src = event.get("source", {})
            if src.get("__typename") == "PullRequest":
                pr_num = src.get("number")
                if pr_num:
                    pr_numbers.append(str(pr_num))
        elif evt_type == "ConnectedEvent":
            subj = event.get("subject", {})
            if subj.get("__typename") == "PullRequest":
                pr_num = subj.get("number")
                if pr_num:
                    pr_numbers.append(str(pr_num))
                    
    has_pr = 1 if len(pr_numbers) > 0 else 0
    
    comments = node.get("comments", {}).get("totalCount", 0)
    reactions = node.get("reactions", {}).get("totalCount", 0)
    
    # Calculate score
    score = 0
    # Label priority
    for l in lower_labels:
        if "ai_p1" in l:
            score += 50
        elif "ai_p2" in l:
            score += 30
        elif "crash" in l or "sigsegv" in l or "fatal" in l:
            score += 45
        elif "critical" in l:
            score += 40
        elif "potential bug" in l:
            score += 25
        elif "bug" in l:
            score += 20
        elif "performance" in l:
            score += 20
        elif "good first issue" in l:
            score += 25
        elif "need info" in l or "stale" in l:
            score -= 30
            
    # Reactions & comments weight
    score += reactions * 4
    score += min(comments, 15) * 2
    
    # Recency bonus
    created_at_str = node.get("createdAt", "")
    try:
        created_dt = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        now = datetime.datetime.now(datetime.timezone.utc)
        age_days = (now - created_dt).days
        if age_days < 14:
            score += 20
        elif age_days < 60:
            score += 10
        elif age_days > 730 and reactions == 0 and comments <= 1:
            score -= 25 # stale noise
    except Exception:
        pass
        
    # Actionability adjustments
    if has_pr:
        score -= 40
    if has_assignee:
        score -= 20
    if not has_pr and not has_assignee:
        score += 20

    # Categorize Tier
    if has_pr or has_assignee:
        tier = "Tier 4 (In Progress / Claimed)"
        usefulness = "claimed"
    elif any("good first issue" in l for l in lower_labels):
        tier = "Tier 2 (Good First Issue)"
        usefulness = "beginner_friendly"
    elif score >= 40:
        tier = "Tier 1 (High Priority Actionable)"
        usefulness = "high_value_bug"
    elif score >= 15:
        tier = "Tier 2 (Standard Actionable Bug)"
        usefulness = "actionable_bug"
    elif any("feature" in l or "rfc" in l for l in lower_labels):
        tier = "Tier 3 (Feature Request / RFC)"
        usefulness = "feature"
    else:
        tier = "Tier 5 (Low Priority / Noise)"
        usefulness = "low_priority"

    return {
        "number": node["number"],
        "title": node["title"],
        "url": node["url"],
        "state": node.get("state", "OPEN"),
        "created_at": node["createdAt"],
        "updated_at": node.get("updatedAt", node["createdAt"]),
        "comments_count": comments,
        "reactions_count": reactions,
        "assignees": json.dumps(assignees),
        "has_assignee": has_assignee,
        "labels": json.dumps(labels),
        "component": component,
        "has_pr": has_pr,
        "pr_numbers": ",".join(pr_numbers),
        "priority_score": score,
        "tier": tier,
        "usefulness": usefulness,
        "raw_json": json.dumps(node)
    }

def sync_issues(limit=500):
    token = get_github_token()
    if not token:
        print("Error: GitHub token not found in env or ~/.config/gh/hosts.yml")
        sys.exit(1)
        
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    
    headers = {
        "Authorization": f"bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "ClickHouse-Tracker"
    }
    
    cursor = None
    fetched = 0
    page = 1
    
    print(f"[*] Starting sync for up to {limit} ClickHouse open issues...")
    
    query = """
    query($cursor: String) {
      repository(owner: "ClickHouse", name: "ClickHouse") {
        issues(first: 100, states: OPEN, after: $cursor, orderBy: {field: CREATED_AT, direction: DESC}) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            number
            title
            url
            state
            createdAt
            updatedAt
            comments { totalCount }
            reactions { totalCount }
            assignees(first: 3) { nodes { login } }
            labels(first: 10) { nodes { name } }
            timelineItems(first: 5, itemTypes: [CROSS_REFERENCED_EVENT, CONNECTED_EVENT]) {
              nodes {
                __typename
                ... on CrossReferencedEvent {
                  source {
                    __typename
                    ... on PullRequest {
                      number
                    }
                  }
                }
                ... on ConnectedEvent {
                  subject {
                    __typename
                    ... on PullRequest {
                      number
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    """
    
    while fetched < limit:
        req_data = json.dumps({"query": query, "variables": {"cursor": cursor}}).encode("utf-8")
        req = urllib.request.Request("https://api.github.com/graphql", data=req_data, headers=headers)
        
        try:
            with urllib.request.urlopen(req) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            print(f"Sync error on page {page}: {e}")
            break
            
        data = result.get("data", {}).get("repository", {}).get("issues", {})
        nodes = data.get("nodes", [])
        if not nodes:
            break
            
        for node in nodes:
            t = calculate_triage(node)
            conn.execute("""
            INSERT OR REPLACE INTO issues (
                number, title, url, state, created_at, updated_at,
                comments_count, reactions_count, assignees, has_assignee,
                labels, component, has_pr, pr_numbers, priority_score,
                tier, usefulness, raw_json
            ) VALUES (
                :number, :title, :url, :state, :created_at, :updated_at,
                :comments_count, :reactions_count, :assignees, :has_assignee,
                :labels, :component, :has_pr, :pr_numbers, :priority_score,
                :tier, :usefulness, :raw_json
            )
            """, t)
            
        conn.commit()
        fetched += len(nodes)
        print(f"  [+] Page {page}: fetched {len(nodes)} issues (Total: {fetched}/{limit})")
        
        page_info = data.get("pageInfo", {})
        if not page_info.get("hasNextPage") or fetched >= limit:
            break
            
        cursor = page_info.get("endCursor")
        page += 1

    conn.close()
    export_json()
    print(f"[✔] Sync complete. Local database: {DB_PATH}")

def export_json():
    if not os.path.exists(DB_PATH):
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT number, title, url, component, priority_score, reactions_count, comments_count,
               tier, usefulness, has_pr, has_assignee, labels, created_at
        FROM issues
        ORDER BY priority_score DESC
    """).fetchall()
    conn.close()
    
    data = []
    for r in rows:
        item = dict(r)
        item['labels'] = json.loads(item['labels']) if item['labels'] else []
        data.append(item)
        
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'issues.json')
    with open(json_path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f"[✔] Exported {len(data)} issues to {json_path}")

def list_issues(tier=None, component=None, no_pr=True, limit=20, query_filter=None):
    if not os.path.exists(DB_PATH):
        print("Database not found. Run 'sync' first.")
        return
        
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    sql = "SELECT number, title, component, priority_score, reactions_count, comments_count, tier, has_pr, has_assignee, url FROM issues WHERE 1=1"
    params = []
    
    if no_pr:
        sql += " AND has_pr = 0 AND has_assignee = 0"
    if tier:
        sql += " AND tier LIKE ?"
        params.append(f"%{tier}%")
    if component:
        sql += " AND component LIKE ?"
        params.append(f"%{component}%")
    if query_filter:
        sql += " AND (title LIKE ? OR labels LIKE ?)"
        params.extend([f"%{query_filter}%", f"%{query_filter}%"])
        
    sql += " ORDER BY priority_score DESC LIMIT ?"
    params.append(limit)
    
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    
    if not rows:
        print("No matching issues found.")
        return
        
    print(f"\n{'#ID':<8} {'Score':<6} {'Reac':<5} {'Component':<22} {'Title'}")
    print("-" * 105)
    for r in rows:
        title = r["title"]
        if len(title) > 60:
            title = title[:57] + "..."
        comp = r["component"]
        if comp.startswith("comp-"):
            comp = comp[5:]
        print(f"#{r['number']:<7} {r['priority_score']:<6} {r['reactions_count']:<5} {comp:<22} {title}")
        print(f"         └─ {r['url']}")
    print("-" * 105)
    print(f"Showing {len(rows)} issues.\n")

def list_components():
    if not os.path.exists(DB_PATH):
        print("Database not found. Run 'sync' first.")
        return
        
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("""
        SELECT component, COUNT(*) as cnt,
               SUM(CASE WHEN has_pr = 0 AND has_assignee = 0 THEN 1 ELSE 0 END) as open_unclaimed
        FROM issues
        GROUP BY component
        ORDER BY cnt DESC
    """).fetchall()
    conn.close()
    
    print(f"\n{'Component':<30} {'Total Issues':<15} {'Actionable (No PR/Assignee)'}")
    print("-" * 65)
    for c, cnt, unclaimed in rows:
        print(f"{c:<30} {cnt:<15} {unclaimed}")
    print("-" * 65 + "\n")

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/issues"):
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute("""
                SELECT number, title, url, component, priority_score, reactions_count, comments_count,
                       tier, usefulness, has_pr, has_assignee, labels, created_at
                FROM issues
                ORDER BY priority_score DESC
            """).fetchall()
            conn.close()
            
            data = []
            for r in rows:
                item = dict(r)
                item["labels"] = json.loads(item["labels"]) if item["labels"] else []
                data.append(item)
                
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        # HTML UI
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        html = """<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>ClickHouse Issue Triage & Prioritizer</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 24px; background: #0d1117; color: #c9d1d9; }
        h1 { color: #58a6ff; margin-bottom: 8px; font-size: 22px; }
        .subtitle { color: #8b949e; margin-bottom: 20px; font-size: 14px; }
        .controls { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; background: #161b22; padding: 16px; border-radius: 8px; border: 1px solid #30363d; }
        input, select { background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; padding: 8px 12px; border-radius: 6px; font-size: 14px; outline: none; }
        input:focus, select:focus { border-color: #58a6ff; }
        .badge { padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; display: inline-block; }
        .badge-t1 { background: #d73a49; color: #fff; }
        .badge-t2 { background: #238636; color: #fff; }
        .badge-t3 { background: #1f6feb; color: #fff; }
        .badge-t4 { background: #6e7681; color: #fff; }
        .badge-comp { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; background: #161b22; border-radius: 8px; overflow: hidden; border: 1px solid #30363d; }
        th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid #21262d; font-size: 13px; }
        th { background: #1b2028; color: #8b949e; font-weight: 600; }
        tr:hover { background: #1f242c; }
        a { color: #58a6ff; text-decoration: none; font-weight: 500; }
        a:hover { text-decoration: underline; }
        .score-pill { font-weight: 700; color: #f0883e; }
        .stats { margin-left: auto; color: #8b949e; align-self: center; font-size: 13px; }
    </style>
</head>
<body>
    <h1>ClickHouse Issue Triage & Prioritizer</h1>
    <div class="subtitle">Live local cache sorting ClickHouse issues by priority, actionability (no PR/assignee), and module.</div>
    <div class="controls">
        <input type="text" id="search" placeholder="Search title or label..." style="width: 280px;" oninput="render()">
        <select id="filterTier" onchange="render()">
            <option value="">All Tiers</option>
            <option value="Tier 1" selected>Tier 1 (High Priority Actionable)</option>
            <option value="Tier 2">Tier 2 (Good First / Accessible)</option>
            <option value="Tier 3">Tier 3 (Feature / RFC)</option>
            <option value="Tier 4">Tier 4 (Claimed / Has PR)</option>
        </select>
        <select id="filterComponent" onchange="render()">
            <option value="">All Components</option>
        </select>
        <label style="display:flex; align-items:center; gap:6px; cursor:pointer;">
            <input type="checkbox" id="onlyNoPR" checked onchange="render()"> Actionable Only (No PR, Unassigned)
        </label>
        <div class="stats" id="stats">Loading...</div>
    </div>
    <table>
        <thead>
            <tr>
                <th style="width: 70px;">Score</th>
                <th style="width: 80px;">Issue</th>
                <th>Title</th>
                <th style="width: 140px;">Component</th>
                <th style="width: 160px;">Tier</th>
                <th style="width: 70px;">Reactions</th>
            </tr>
        </thead>
        <tbody id="tbody"></tbody>
    </table>
    <script>
        let allIssues = [];
        fetch('/api/issues')
            .then(r => r.json())
            .then(data => {
                allIssues = data;
                populateComponents();
                render();
            });

        function populateComponents() {
            const comps = new Set();
            allIssues.forEach(i => { if (i.component && i.component !== 'other') comps.add(i.component); });
            const sel = document.getElementById('filterComponent');
            Array.from(comps).sort().forEach(c => {
                const opt = document.createElement('option');
                opt.value = c;
                opt.textContent = c.replace('comp-', '');
                sel.appendChild(opt);
            });
        }

        function render() {
            const q = document.getElementById('search').value.toLowerCase();
            const tier = document.getElementById('filterTier').value;
            const comp = document.getElementById('filterComponent').value;
            const noPr = document.getElementById('onlyNoPR').checked;

            const filtered = allIssues.filter(i => {
                if (noPr && (i.has_pr || i.has_assignee)) return false;
                if (tier && !i.tier.includes(tier)) return false;
                if (comp && i.component !== comp) return false;
                if (q) {
                    const inTitle = i.title.toLowerCase().includes(q);
                    const inLabels = i.labels.some(l => l.toLowerCase().includes(q));
                    const inNum = String(i.number).includes(q);
                    if (!inTitle && !inLabels && !inNum) return false;
                }
                return true;
            });

            document.getElementById('stats').textContent = `Showing ${filtered.length} of ${allIssues.length} issues`;

            const tbody = document.getElementById('tbody');
            tbody.innerHTML = filtered.slice(0, 150).map(i => {
                let tierClass = 'badge-t4';
                if (i.tier.includes('Tier 1')) tierClass = 'badge-t1';
                else if (i.tier.includes('Tier 2')) tierClass = 'badge-t2';
                else if (i.tier.includes('Tier 3')) tierClass = 'badge-t3';

                const compName = i.component.replace('comp-', '');
                return `<tr>
                    <td><span class="score-pill">${i.priority_score}</span></td>
                    <td><a href="${i.url}" target="_blank">#${i.number}</a></td>
                    <td><a href="${i.url}" target="_blank" style="color:#c9d1d9;">${escapeHtml(i.title)}</a></td>
                    <td><span class="badge badge-comp">${compName}</span></td>
                    <td><span class="badge ${tierClass}">${i.tier.split(' ')[0] + ' ' + i.tier.split(' ')[1]}</span></td>
                    <td>👍 ${i.reactions_count}</td>
                </tr>`;
            }).join('');
        }

        function escapeHtml(str) {
            return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }
    </script>
</body>
</html>"""
        self.wfile.write(html.encode("utf-8"))

def serve(port=8080):
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    print(f"[*] Dashboard running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")

def main():
    parser = argparse.ArgumentParser(description="ClickHouse Issue Tracker & Prioritizer")
    subparsers = parser.add_subparsers(dest="cmd")
    
    sync_p = subparsers.add_parser("sync", help="Sync issues from ClickHouse repo")
    sync_p.add_argument("--limit", type=int, default=500, help="Max issues to sync (default: 500)")
    
    list_p = subparsers.add_parser("list", help="List prioritized actionable issues")
    list_p.add_argument("--tier", type=str, default=None, help="Filter by tier name (e.g. 'Tier 1')")
    list_p.add_argument("--component", type=str, default=None, help="Filter by component name (e.g. 'mergetree')")
    list_p.add_argument("--all", action="store_true", help="Include issues with PRs/assignees")
    list_p.add_argument("--query", type=str, default=None, help="Search string in title/labels")
    list_p.add_argument("--limit", type=int, default=20, help="Max rows to show (default: 20)")
    
    subparsers.add_parser("components", help="Show summary by component")
    
    serve_p = subparsers.add_parser("serve", help="Run local web dashboard")
    serve_p.add_argument("--port", type=int, default=8088, help="Port to listen on (default: 8088)")
    
    args = parser.parse_args()
    
    if args.cmd == "sync":
        sync_issues(limit=args.limit)
    elif args.cmd == "list":
        list_issues(tier=args.tier, component=args.component, no_pr=not args.all, limit=args.limit, query_filter=args.query)
    elif args.cmd == "components":
        list_components()
    elif args.cmd == "serve":
        serve(port=args.port)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
