#!/usr/bin/env python3
"""Minimal YouTrack REST client for Claude Code skills (stdlib only).

Config: env vars, else KEY=VALUE lines in ~/.config/youtrack/env,
else config.env next to this skill (committed, no secrets):
  YOUTRACK_URL    e.g. https://company.youtrack.cloud  (or https://host/youtrack)
  YOUTRACK_TOKEN  permanent token, "perm:...". Optional in a cloud session where
                  a network secret for the host adds the Authorization header.

Commands:
  me                              check auth
  issue ID                        full card: fields, links, comments, attachments
  search QUERY [--top N]          YouTrack query language, e.g. "project: ABC #Unresolved"
  links ID                        linked issues grouped by link type
  history ID                      custom-field changes (status timeline)
  analyze ID [--depth N]          analytics for the issue and its link graph
  context ID [--depth N]          markdown dump of texts for requirement checklists
  comment ID (--text T | --file F)  post a comment (write!)
Add --json to any read command for raw JSON.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

USER_F = "login,fullName"
VALUE_F = "name,login,fullName,text,minutes,presentation,isResolved"
CF_F = f"name,$type,value({VALUE_F})"
BRIEF_F = f"idReadable,summary,resolved,created,updated,customFields({CF_F})"
LINK_F = f"direction,linkType(name,sourceToTarget,targetToSource),issues({BRIEF_F})"
ISSUE_F = (
    f"idReadable,summary,description,created,updated,resolved,"
    f"reporter({USER_F}),project(shortName,name),tags(name),"
    f"customFields({CF_F}),links({LINK_F}),"
    f"comments(text,created,author({USER_F})),attachments(name,url,size,mimeType)"
)


# ---------------------------------------------------------------- transport
def read_env_file(path):
    cfg = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def load_config():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = {**read_env_file(os.path.join(here, "config.env")),
           **read_env_file(os.path.expanduser("~/.config/youtrack/env"))}
    url = os.environ.get("YOUTRACK_URL") or cfg.get("YOUTRACK_URL")
    token = os.environ.get("YOUTRACK_TOKEN") or cfg.get("YOUTRACK_TOKEN")
    if not url:
        sys.exit("error: set YOUTRACK_URL (env, ~/.config/youtrack/env or config.env)")
    return url.rstrip("/"), token


class YouTrack:
    def __init__(self, url, token):
        self.url, self.token = url, token

    def request(self, method, path, params=None, body=None):
        q = "?" + urllib.parse.urlencode(params) if params else ""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.url}/api/{path.lstrip('/')}{q}", data=data, method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     # no token: rely on the cloud proxy's network secret
                     **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            hint = "" if self.token or e.code != 401 else " (no YOUTRACK_TOKEN set and no network secret applied)"
            sys.exit(f"error: HTTP {e.code} {method} {path}{hint}: {e.read().decode(errors='replace')[:500]}")
        except OSError as e:  # URLError, connection reset, timeout
            sys.exit(f"error: cannot reach {self.url}: {getattr(e, 'reason', e)}")
        return json.loads(raw) if raw else None

    def get(self, path, **params):
        return self.request("GET", path, params)

    def paged(self, path, page=100, limit=2000, **params):
        out, skip = [], 0
        while skip < limit:
            chunk = self.get(path, **params, **{"$skip": skip, "$top": page})
            out += chunk
            if len(chunk) < page:
                break
            skip += page
        return out

    def issue(self, iid):
        return self.get(f"issues/{iid}", fields=ISSUE_F)

    def search(self, query, top=50):
        return self.paged("issues", limit=top, page=min(top, 100), query=query, fields=BRIEF_F)

    def activities(self, iid):
        return self.paged(
            f"issues/{iid}/activities", categories="CustomFieldCategory",
            fields=f"timestamp,author({USER_F}),field(name),added({VALUE_F}),removed({VALUE_F})")

    def work_items(self, iid):
        return self.paged(f"issues/{iid}/timeTracking/workItems",
                          fields=f"date,duration(minutes),author({USER_F}),text")


# ---------------------------------------------------------------- helpers
def ts(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") if ms else "—"


def fmt_value(v):
    if v is None:
        return "—"
    if isinstance(v, list):
        return ", ".join(fmt_value(x) for x in v) or "—"
    if isinstance(v, dict):
        return (v.get("presentation") or v.get("fullName") or v.get("name")
                or v.get("login") or v.get("text") or str(v.get("minutes", "")) or "?")
    return str(v)


def fields(issue):
    return {cf["name"]: cf.get("value") for cf in issue.get("customFields", [])}


def state(issue):
    f = fields(issue)
    for k in ("State", "Status", "Состояние", "Статус", "Stage"):
        if k in f:
            return fmt_value(f[k])
    return "Resolved" if issue.get("resolved") else "Open"


def link_label(link):
    lt = link["linkType"]
    if link["direction"] == "INWARD":
        return lt.get("targetToSource") or lt["name"]
    return lt.get("sourceToTarget") or lt["name"]


def hours(minutes):
    return f"{minutes / 60:.1f}h"


def crawl(yt, root_id, depth):
    """BFS over the link graph. Returns {id: full issue}, [(src, label, dst)]."""
    seen, edges, keys, frontier = {}, [], set(), [root_id]
    for level in range(depth + 1):
        nxt = []
        for iid in frontier:
            if iid in seen:
                continue
            seen[iid] = yt.issue(iid)
            src = seen[iid]["idReadable"]
            for link in seen[iid].get("links", []):
                for other in link.get("issues", []):
                    oid = other["idReadable"]
                    # each link is visible from both ends; keep the first direction seen
                    key = (frozenset((src, oid)), link["linkType"]["name"])
                    if key not in keys:
                        keys.add(key)
                        edges.append((src, link_label(link), oid))
                    if level < depth and oid not in seen:
                        nxt.append(oid)
        frontier = nxt
    return seen, edges


def time_in_states(acts, created, resolved):
    """From State-field activities, compute hours spent in each state."""
    changes = [a for a in acts if a.get("field", {}).get("name") in
               ("State", "Status", "Состояние", "Статус", "Stage")]
    changes.sort(key=lambda a: a["timestamp"])
    if not changes:
        return {}
    spans = Counter()
    first_removed = changes[0].get("removed")
    cur, since = fmt_value(first_removed) if first_removed else None, created
    for a in changes:
        if cur:
            spans[cur] += a["timestamp"] - since
        cur, since = fmt_value(a.get("added")), a["timestamp"]
    end = resolved or int(datetime.now(timezone.utc).timestamp() * 1000)
    if cur and not resolved:
        spans[cur + " (current)"] += end - since
    return {k: round(v / 3_600_000, 1) for k, v in spans.items()}


# ---------------------------------------------------------------- renderers
def render_issue(i, full=True):
    out = [f"# {i['idReadable']}: {i['summary']}", ""]
    out.append(f"- Project: {i.get('project', {}).get('name', '—')}")
    out.append(f"- Reporter: {fmt_value(i.get('reporter'))}")
    out.append(f"- Created: {ts(i.get('created'))} · Updated: {ts(i.get('updated'))} · Resolved: {ts(i.get('resolved'))}")
    if i.get("tags"):
        out.append(f"- Tags: {', '.join(t['name'] for t in i['tags'])}")
    for k, v in fields(i).items():
        out.append(f"- {k}: {fmt_value(v)}")
    out += ["", "## Description", "", (i.get("description") or "_(empty)_").strip(), ""]
    if i.get("links"):
        out.append("## Links")
        for link in i["links"]:
            for o in link.get("issues", []):
                out.append(f"- {link_label(link)} → {o['idReadable']} [{state(o)}] {o['summary']}")
        out.append("")
    if full and i.get("comments"):
        out.append(f"## Comments ({len(i['comments'])})")
        for c in i["comments"]:
            out += ["", f"**{fmt_value(c.get('author'))}** · {ts(c.get('created'))}", "", (c.get("text") or "").strip()]
        out.append("")
    if i.get("attachments"):
        out.append("## Attachments")
        for a in i["attachments"]:
            out.append(f"- {a['name']} ({a.get('mimeType')}, {a.get('size')} B)")
    return "\n".join(out)


REQ_RE = re.compile(
    r"^\s*(?:[-*+]\s+\[[ xX]\]|[-*+]\s|\d+[.)]\s|(?:AC|Given|When|Then|And)\b)"
    r"|\b(must|should|shall|need(?:s|ed)? to|required?|has to|"
    r"долж[нена]\w*|необходим\w*|нужно|требу\w*|требова\w*|обязательн\w*|следует)\b",
    re.IGNORECASE)


def candidate_requirements(text):
    """Heuristic: lines that look like list items, ACs or contain modal verbs."""
    return [l.strip() for l in (text or "").splitlines() if l.strip() and REQ_RE.search(l)]


def cmd_analyze(yt, args):
    issues, edges = crawl(yt, args.id, args.depth)
    root = issues[args.id] if args.id in issues else next(iter(issues.values()))
    rid = root["idReadable"]
    report = {"root": rid, "issues": {}, "edges": edges}
    for iid, i in issues.items():
        acts = yt.activities(iid)
        work = yt.work_items(iid) if not args.no_time else []
        f = fields(i)
        report["issues"][i["idReadable"]] = {
            "summary": i["summary"], "state": state(i),
            "resolved": bool(i.get("resolved")),
            "assignee": fmt_value(f.get("Assignee") or f.get("Исполнитель")),
            "priority": fmt_value(f.get("Priority") or f.get("Приоритет")),
            "type": fmt_value(f.get("Type") or f.get("Тип")),
            "created": ts(i.get("created")), "resolved_at": ts(i.get("resolved")),
            "age_days": round(((i.get("resolved") or datetime.now(timezone.utc).timestamp() * 1000)
                               - i["created"]) / 86_400_000, 1),
            "comments": len(i.get("comments", [])),
            "field_changes": len(acts),
            "reopened": sum(1 for a in acts if a.get("field", {}).get("name") in ("State", "Состояние")
                            and re.search(r"reopen|open|открыт", fmt_value(a.get("added")), re.I)
                            and a.get("removed") and re.search(r"done|fixed|closed|verified|готов|закрыт|решен",
                                                               fmt_value(a.get("removed")), re.I)),
            "time_in_state_h": time_in_states(acts, i["created"], i.get("resolved")),
            "spent": hours(sum((w.get("duration") or {}).get("minutes", 0) for w in work)) if work else None,
            "spent_by": {a: hours(m) for a, m in _sum_by_author(work).items()},
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    R = report["issues"]
    linked = [k for k in R if k != rid]
    out = [f"# Analytics: {rid} — {R[rid]['summary']}", "",
           f"Graph depth {args.depth}: {len(R)} issues, {len(edges)} links.", ""]
    out += ["## Root issue", ""]
    out += _issue_metrics(rid, R[rid])
    if linked:
        out += ["", "## Linked issues", "",
                "| Issue | Relation to root | Type | State | Assignee | Age, d | Spent |",
                "|---|---|---|---|---|---|---|"]
        rel = {d: l for s, l, d in edges if s == rid}
        for k in linked:
            m = R[k]
            out.append(f"| {k} {m['summary'][:60]} | {rel.get(k, '(indirect)')} | {m['type']} | {m['state']} "
                       f"| {m['assignee']} | {m['age_days']} | {m['spent'] or '—'} |")
        st = Counter(R[k]["state"] for k in linked)
        out += ["", "**State breakdown:** " + ", ".join(f"{s}: {n}" for s, n in st.most_common())]
        done = sum(1 for k in linked if R[k]["resolved"])
        out.append(f"**Progress:** {done}/{len(linked)} linked issues resolved ({done * 100 // len(linked)}%)")
        blockers = [k for s, l, k in edges if s == rid and re.search(r"depends on|is required for|blocked by|зависит", l, re.I)
                    and not R.get(k, {}).get("resolved", True)]
        if blockers:
            out.append(f"**Open blockers/dependencies:** {', '.join(sorted(set(blockers)))}")
        total = sum(_minutes(R[k]["spent"]) for k in R)
        if total:
            out.append(f"**Total spent across graph:** {hours(total)}")
    out += ["", "## Link graph", ""] + [f"- {s} —{l}→ {d}" for s, l, d in edges]
    print("\n".join(out))


def _sum_by_author(work):
    acc = defaultdict(int)
    for w in work:
        acc[fmt_value(w.get("author"))] += (w.get("duration") or {}).get("minutes", 0)
    return acc


def _minutes(h):
    return int(float(h[:-1]) * 60) if h else 0


def _issue_metrics(iid, m):
    out = [f"- State: {m['state']} · Type: {m['type']} · Priority: {m['priority']} · Assignee: {m['assignee']}",
           f"- Created {m['created']}, resolved {m['resolved_at']}, age {m['age_days']} d",
           f"- Comments: {m['comments']} · Field changes: {m['field_changes']} · Reopened: {m['reopened']}"]
    if m["time_in_state_h"]:
        out.append("- Time in state: " + ", ".join(f"{k} {v}h" for k, v in m["time_in_state_h"].items()))
    if m["spent"]:
        out.append(f"- Spent: {m['spent']} (" + ", ".join(f"{a} {h}" for a, h in m["spent_by"].items()) + ")")
    return out


def cmd_context(yt, args):
    issues, edges = crawl(yt, args.id, args.depth)
    if args.json:
        print(json.dumps({"issues": issues, "edges": edges}, ensure_ascii=False, indent=2))
        return
    out = []
    for iid, i in issues.items():
        out.append(render_issue(i, full=True))
        texts = [i.get("description") or ""] + [c.get("text") or "" for c in i.get("comments", [])]
        cands = [c for t in texts for c in candidate_requirements(t)]
        if cands:
            out += ["", "### Requirement-like lines (heuristic)", ""] + [f"- {re.sub(r'^[-*+]\s+', '', c)}" for c in cands]
        out += ["", "---", ""]
    out += ["## Link graph", ""] + [f"- {s} —{l}→ {d}" for s, l, d in edges]
    print("\n".join(out))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--json", action="store_true", help="raw JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("me")
    for name in ("issue", "links", "history"):
        sub.add_parser(name).add_argument("id")
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--top", type=int, default=50)
    for name in ("analyze", "context"):
        a = sub.add_parser(name); a.add_argument("id"); a.add_argument("--depth", type=int, default=1)
        if name == "analyze":
            a.add_argument("--no-time", action="store_true", help="skip time-tracking work items")
    c = sub.add_parser("comment"); c.add_argument("id")
    g = c.add_mutually_exclusive_group(required=True); g.add_argument("--text"); g.add_argument("--file")
    args = p.parse_args()
    yt = YouTrack(*load_config())

    if args.cmd == "me":
        r = yt.get("users/me", fields="login,fullName,email")
        print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else f"{r.get('fullName')} ({r.get('login')}) @ {yt.url}")
    elif args.cmd == "issue":
        i = yt.issue(args.id)
        print(json.dumps(i, ensure_ascii=False, indent=2) if args.json else render_issue(i))
    elif args.cmd == "search":
        res = yt.search(args.query, args.top)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            for i in res:
                print(f"{i['idReadable']}\t[{state(i)}]\t{i['summary']}")
            print(f"({len(res)} issues)", file=sys.stderr)
    elif args.cmd == "links":
        i = yt.issue(args.id)
        if args.json:
            print(json.dumps(i.get("links", []), ensure_ascii=False, indent=2))
        else:
            for link in i.get("links", []):
                if link.get("issues"):
                    print(f"## {link_label(link)}")
                    for o in link["issues"]:
                        print(f"- {o['idReadable']} [{state(o)}] {o['summary']}")
    elif args.cmd == "history":
        acts = yt.activities(args.id)
        if args.json:
            print(json.dumps(acts, ensure_ascii=False, indent=2))
        else:
            for a in sorted(acts, key=lambda a: a["timestamp"]):
                print(f"{ts(a['timestamp'])}  {fmt_value(a.get('author'))}: {a.get('field', {}).get('name')}: "
                      f"{fmt_value(a.get('removed'))} → {fmt_value(a.get('added'))}")
    elif args.cmd == "analyze":
        cmd_analyze(yt, args)
    elif args.cmd == "context":
        cmd_context(yt, args)
    elif args.cmd == "comment":
        text = args.text if args.text is not None else open(args.file, encoding="utf-8").read()
        r = yt.request("POST", f"issues/{args.id}/comments", {"fields": "id"}, {"text": text})
        print(f"comment {r.get('id')} posted to {args.id}")


if __name__ == "__main__":
    main()
