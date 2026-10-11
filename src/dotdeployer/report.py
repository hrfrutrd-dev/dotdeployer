"""Self-contained HTML/JSON reports of a project and its plan.

``dotdeployer report --html report.html`` produces a single file with inlined
CSS - no CDN, no JavaScript requirements - that can be committed to a repo or
attached to a CI job summary.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from .plan import Plan
from .util import display_path, human_size, now_iso

_STATUS_LABEL = {
    "create": ("create", "#2ea043"),
    "update": ("update", "#d29922"),
    "drift": ("drift", "#db6d28"),
    "conflict": ("conflict", "#f85149"),
    "remove": ("remove", "#f85149"),
    "skip": ("skip", "#8b949e"),
    "unchanged": ("ok", "#8b949e"),
    "error": ("error", "#f85149"),
    "orphan": ("orphan", "#8b949e"),
}


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for current, _dirs, files in path.walk() if hasattr(path, "walk") else _walk(path):
            for name in files:
                try:
                    total += (Path(current) / name).stat().st_size
                except OSError:
                    continue
    except OSError:  # pragma: no cover
        return 0
    return total


def _walk(path: Path):
    import os

    return os.walk(path)


def build_report(project: Any, plan: Optional[Plan] = None, *, include_diff: bool = False) -> Dict[str, Any]:
    """Collect every fact worth reporting about a project."""


    config = project.config
    plan = plan or project.plan()
    checks = project.doctor() if plan is None else []
    entries: List[Dict[str, Any]] = []
    for change in plan.all_changes():
        entry = change.entry
        source = entry.source_path
        size = 0
        kind = "directory" if entry.mode == "mkdir" else "file"
        if source is not None and source.exists():
            if source.is_dir():
                kind = "directory"
                size = _dir_size(source)
            else:
                try:
                    size = source.stat().st_size
                except OSError:  # pragma: no cover
                    size = 0
        entries.append(
            {
                **change.to_dict(),
                "status_label": _STATUS_LABEL.get(change.status, (change.status, "#8b949e"))[0],
                "source_display": display_path(source) if source else None,
                "target_display": display_path(change.target),
                "size": size,
                "size_human": human_size(size),
                "kind": kind,
                "tags": list(entry.tags),
                "profiles": list(entry.profiles),
                "comment": entry.comment,
                "origin": entry.origin,
            }
        )
    backups = project.backups.sets()
    report: Dict[str, Any] = {
        "generated_at": now_iso(),
        "host": {
            "hostname": config.facts.hostname,
            "os": config.facts.os,
            "arch": config.facts.arch,
            "user": config.facts.user,
            "distro": config.facts.distro,
            "python": config.facts.python,
        },
        "config": config.describe(),
        "profiles": config.active_profiles(),
        "tags": config.tags(),
        "counts": plan.counts(),
        "summary": plan.summary_line(),
        "totals": {
            "entries": len(config.entries),
            "selected": len(plan.changes),
            "changes": len(plan.mutating),
            "conflicts": len(plan.conflicts),
            "drift": len(plan.with_status("drift")),
            "sources_bytes": sum(e["size"] for e in entries),
        },
        "entries": entries,
        "warnings": list(plan.warnings),
        "hooks": {phase: list(cmds) for phase, cmds in config.hooks.items() if cmds},
        "secrets": project.secrets.describe(),
        "backups": [
            {"id": item["id"], "count": item.get("count", 0), "created_at": item.get("created_at", "")}
            for item in backups
        ],
        "state": project.state.stats(),
        "doctor": [check.to_dict() for check in checks] if checks else None,
    }
    if include_diff:
        report["diffs"] = {
            change.entry.key: change.diff for change in plan.all_changes() if change.diff
        }
    return report


def to_json(report: Mapping[str, Any], *, indent: int = 2) -> str:
    return json.dumps(report, indent=indent, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem clamp(1rem, 5vw, 4rem) 4rem;
  font: 15px/1.55 ui-sans-serif, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  background: #f6f8fa; color: #1f2328; }
h1 { font-size: 1.65rem; margin: 0 0 .25rem; letter-spacing: -.02em; }
h2 { font-size: 1.1rem; margin: 2.2rem 0 .6rem; text-transform: uppercase; letter-spacing: .08em;
  color: #57606a; font-weight: 600; }
.sub { color: #57606a; margin-bottom: 1.5rem; }
.grid { display: grid; gap: .9rem; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
.card { background: #fff; border: 1px solid #d0d7de; border-radius: 10px; padding: .9rem 1rem; }
.card .value { font-size: 1.7rem; font-weight: 650; line-height: 1.1; }
.card .label { color: #57606a; font-size: .78rem; text-transform: uppercase; letter-spacing: .07em; }
table { width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #d0d7de;
  border-radius: 10px; overflow: hidden; font-size: .92rem; }
th, td { text-align: left; padding: .5rem .7rem; border-bottom: 1px solid #eaeef2; vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; color: #57606a; font-size: .78rem;
  text-transform: uppercase; letter-spacing: .06em; }
tr:last-child td { border-bottom: none; }
code, .mono { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace; font-size: .87em; }
.badge { display: inline-block; padding: .1rem .5rem; border-radius: 999px; font-size: .75rem;
  font-weight: 600; color: #fff; }
.tags span { display: inline-block; background: #ddf4ff; color: #0969da; border-radius: 6px;
  padding: 0 .35rem; margin-right: .25rem; font-size: .75rem; }
.warn { background: #fff8c5; border: 1px solid #d4a72c63; border-radius: 8px; padding: .6rem .8rem;
  margin: .4rem 0; }
.muted { color: #57606a; }
.bar { height: 8px; border-radius: 4px; background: #d0d7de; overflow: hidden; min-width: 60px; }
.bar > i { display: block; height: 100%; background: #0969da; }
details summary { cursor: pointer; color: #0969da; }
pre { background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: .7rem; overflow: auto;
  font-size: .82rem; }
footer { margin-top: 3rem; color: #57606a; font-size: .82rem; }
@media (prefers-color-scheme: dark) {
  body { background: #0d1117; color: #e6edf3; }
  .card, table, pre { background: #161b22; border-color: #30363d; }
  th { background: #161b22; }
  th, td { border-color: #21262d; }
  h2, .sub, .muted, .card .label { color: #8b949e; }
  .tags span { background: #0c2d6b; color: #79c0ff; }
  .warn { background: #2d2a1f; border-color: #d4a72c63; }
  details summary { color: #79c0ff; }
}
"""


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def render_html(report: Mapping[str, Any], *, title: str = "dotdeployer report") -> str:
    totals = report.get("totals") or {}
    entries = report.get("entries") or []
    host = report.get("host") or {}
    config = report.get("config") or {}

    max_size = max([e.get("size", 0) for e in entries] + [1])

    cards = [
        ("entries", totals.get("entries", 0), "managed"),
        ("selected", totals.get("selected", 0), "on this host"),
        ("changes", totals.get("changes", 0), "pending"),
        ("conflicts", totals.get("conflicts", 0), "unmanaged targets"),
        ("drift", totals.get("drift", 0), "edited locally"),
        ("sources", human_size(totals.get("sources_bytes", 0)).strip(), "content"),
    ]

    rows: List[str] = []
    for entry in sorted(entries, key=lambda e: (e.get("status") != "conflict", e.get("target_display", ""))):
        label, colour = _STATUS_LABEL.get(entry.get("status", ""), (entry.get("status", "?"), "#8b949e"))
        size = entry.get("size", 0)
        pct = max(2, int(100 * size / max_size)) if size else 0
        tags = "".join(f"<span>{_esc(t)}</span>" for t in entry.get("tags") or [])
        diff = entry.get("diff") or ""
        details = ""
        if diff:
            details = (
                "<details><summary>diff</summary><pre>"
                + _esc(diff)
                + "</pre></details>"
            )
        rows.append(
            "<tr>"
            f"<td><span class='badge' style='background:{colour}'>{_esc(label)}</span></td>"
            f"<td class='mono'>{_esc(entry.get('target_display'))}"
            f"<div class='muted'>{_esc(entry.get('entry'))}</div></td>"
            f"<td class='mono muted'>{_esc(entry.get('source_display') or '—')}</td>"
            f"<td>{_esc(entry.get('mode'))}</td>"
            f"<td><div class='bar'><i style='width:{pct}%'></i></div>"
            f"<span class='muted mono'>{_esc(entry.get('size_human'))}</span></td>"
            f"<td class='tags'>{tags}</td>"
            f"<td>{_esc(entry.get('detail') or entry.get('comment') or '')}{details}</td>"
            "</tr>"
        )

    warnings = report.get("warnings") or []
    warning_html = "".join(f"<div class='warn'>{_esc(w)}</div>" for w in warnings) or (
        "<p class='muted'>No warnings.</p>"
    )

    hook_rows = "".join(
        f"<tr><td class='mono'>{_esc(phase)}</td><td class='mono'>{_esc(cmd)}</td></tr>"
        for phase, commands in (report.get("hooks") or {}).items()
        for cmd in commands
    ) or "<tr><td colspan='2' class='muted'>No hooks configured.</td></tr>"

    backup_rows = "".join(
        f"<tr><td class='mono'>{_esc(item.get('id'))}</td><td>{_esc(item.get('count'))}</td>"
        f"<td class='muted'>{_esc(item.get('created_at'))}</td></tr>"
        for item in (report.get("backups") or [])
    ) or "<tr><td colspan='3' class='muted'>No backups yet.</td></tr>"

    doctor = report.get("doctor") or []
    doctor_html = ""
    if doctor:
        items = "".join(
            f"<tr><td>{_esc(c.get('status'))}</td><td>{_esc(c.get('name'))}</td>"
            f"<td>{_esc(c.get('message'))}</td></tr>"
            for c in doctor
            if c.get("status") in {"warn", "fail"}
        )
        empty_row = '<tr><td colspan="3" class="muted">All checks passed.</td></tr>'
        doctor_html = (
            "<h2>Problem checks</h2><table><thead>"
            "<tr><th>Level</th><th>Check</th><th>Message</th></tr></thead><tbody>"
            + (items or empty_row)
            + "</tbody></table>"
        )

    profiles = report.get("profiles") or []
    tags = report.get("tags") or []

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} · {_esc(host.get('hostname'))}</title>
<style>{_CSS}</style></head>
<body>
<h1>{_esc(title)}</h1>
<div class="sub">
  <strong>{_esc(host.get('hostname'))}</strong> · {_esc(host.get('os'))}/{_esc(host.get('arch'))}
  · user {_esc(host.get('user'))} · generated {_esc(report.get('generated_at'))}
</div>
<div class="grid">
{''.join(f"<div class='card'><div class='value'>{_esc(value)}</div><div class='label'>{_esc(label)}</div></div>" for _k, value, label in cards)}
</div>
<h2>Project</h2>
<table><tbody>
<tr><th>Config</th><td class="mono">{_esc(config.get('config'))}</td></tr>
<tr><th>Source root</th><td class="mono">{_esc(config.get('source_root'))}</td></tr>
<tr><th>State dir</th><td class="mono">{_esc(config.get('state_dir'))}</td></tr>
<tr><th>Profiles</th><td class="mono">{_esc(', '.join(profiles) or '—')}</td></tr>
<tr><th>Tags</th><td class="mono">{_esc(', '.join(tags) or '—')}</td></tr>
<tr><th>Summary</th><td class="mono">{_esc(report.get('summary'))}</td></tr>
</tbody></table>
<h2>Entries ({len(entries)})</h2>
<table>
<thead><tr><th>Status</th><th>Target</th><th>Source</th><th>Mode</th><th>Size</th><th>Tags</th><th>Notes</th></tr></thead>
<tbody>{''.join(rows) or "<tr><td colspan='7' class='muted'>No entries.</td></tr>"}</tbody>
</table>
{doctor_html}
<h2>Warnings</h2>
{warning_html}
<h2>Hooks</h2>
<table><thead><tr><th>Phase</th><th>Command</th></tr></thead><tbody>{hook_rows}</tbody></table>
<h2>Backups</h2>
<table><thead><tr><th>Set</th><th>Items</th><th>Created</th></tr></thead><tbody>{backup_rows}</tbody></table>
<footer>Generated by <span class="mono">dotdeployer</span> · {_esc(now_iso())} ·
host {_esc(host.get('hostname'))} · {len(entries)} entries scanned</footer>
</body></html>
"""


def write_report(project: Any, destination: Path, *, fmt: str = "html", include_diff: bool = True,
                 plan: Optional[Plan] = None) -> Path:
    report = build_report(project, plan, include_diff=include_diff)
    destination = Path(destination)
    if str(destination) == "-":  # `-o -` streams to stdout
        import sys

        text = to_json(report) if fmt == "json" else render_html(report)
        sys.stdout.write(text.rstrip("\n") + "\n")
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json" or destination.suffix.lower() == ".json":
        destination.write_text(to_json(report) + "\n", encoding="utf-8")
    else:
        destination.write_text(render_html(report), encoding="utf-8")
    return destination
