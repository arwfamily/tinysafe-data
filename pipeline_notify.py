"""
TinySafe — tell a human when the data pipeline needs attention.

Reads every .pipeline_status/*.json written during the run (gate, NHTSA,
FDA inbox, gap check). If anything blocked or warned, it opens ONE GitHub issue
titled "Data pipeline needs attention" — or comments on the one already open —
so the problem lands in the repo owner's email instead of sitting in a log.
When a later run is clean, it closes that issue with a note.

Uses the `gh` CLI with the workflow's GITHUB_TOKEN (needs `issues: write`).
Never fails the workflow itself; notification trouble is printed and ignored.

Run:  python pipeline_notify.py [repo_dir] [--context "daily sync"]
"""
import glob
import json
import os
import subprocess
import sys

TITLE = "Data pipeline needs attention"
LABEL = "pipeline"


def gh(*args):
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"[notify] gh {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
    return r


def main(repo=".", context="daily sync"):
    items, problems = [], False
    for p in sorted(glob.glob(os.path.join(repo, ".pipeline_status", "*.json"))):
        try:
            s = json.load(open(p))
        except Exception:
            continue
        name = s.get("source", os.path.basename(p))
        lines = []
        if s.get("blocked"):
            lines.append("**BLOCKED — yesterday's data was kept.**")
            lines += [f"- {f}" for f in s.get("fails", [])]
        if s.get("ok") is False and s.get("note"):
            lines.append(f"- {s['note']}")
        lines += [f"- {w}" for w in s.get("warnings", [])]
        if lines:
            problems = True
            body = "\n".join(lines)
            if s.get("items"):
                body += "\n\n<details><summary>records</summary>\n\n" + \
                        "\n".join(f"- `{x}`" for x in s["items"]) + "\n</details>"
            items.append(f"### {name}\n{body}")
        if s.get("added"):
            items.append(f"### {name} — added\n" + "\n".join(f"- {x}" for x in s["added"]))

    run_url = ""
    if os.environ.get("GITHUB_RUN_ID"):
        run_url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}")

    found = gh("issue", "list", "--state", "open", "--search", f'"{TITLE}" in:title',
               "--json", "number", "--limit", "1")
    try:
        open_no = (json.loads(found.stdout or "[]") or [{}])[0].get("number")
    except Exception:
        open_no = None

    if problems:
        body = f"**{context}** — [run log]({run_url})\n\n" + "\n\n".join(items)
        print(body)
        if open_no:
            gh("issue", "comment", str(open_no), "--body", body)
        else:
            gh("label", "create", LABEL, "--color", "D93F0B", "--force")
            gh("issue", "create", "--title", TITLE, "--label", LABEL, "--body", body)
    else:
        print(f"[notify] {context}: all clear")
        if open_no and context == "daily sync":
            gh("issue", "close", str(open_no), "--comment",
               f"Daily sync is clean again. [run log]({run_url})")
        # added-only notes (new NHTSA campaigns) are worth a look but not an alarm
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    ctx = "daily sync"
    if "--context" in args:
        i = args.index("--context")
        ctx = args[i + 1]
        args = args[:i] + args[i + 2:]
    try:
        sys.exit(main(args[0] if args else ".", ctx))
    except Exception as e:  # never break the workflow over a notification
        print(f"[notify] skipped: {e}")
        sys.exit(0)
