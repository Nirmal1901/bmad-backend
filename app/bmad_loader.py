"""
BmadLoader: scans the _bmad/ folder for agent persona files (*.md under
any <module>/agents/ directory), parses their frontmatter + embedded
<agent> tag, and returns structured Agent records.

Agent file shape (see _bmad/bmm/agents/analyst.md for a real example):

---
name: "analyst"
description: "Business Analyst"
---
...
<agent id="analyst.agent.yaml" name="Mary" title="Business Analyst" icon="📊" ...>
...
"""
import re
import yaml
from pathlib import Path
from dataclasses import dataclass

BMAD_ROOT = Path(__file__).resolve().parent.parent / "_bmad"

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
AGENT_TAG_RE = re.compile(
    r'<agent\s+id="([^"]*)"\s+name="([^"]*)"\s+title="([^"]*)"\s+icon="([^"]*)"',
)
IDENTITY_RE = re.compile(r"<identity>(.*?)</identity>", re.DOTALL)
MENU_ITEM_RE = re.compile(r'<item\s+cmd="([^"]*)"[^>]*>(.*?)</item>', re.DOTALL)


def _menu_items(text: str) -> list[str]:
    """Extract the agent's real menu items, e.g.
    '[DS] Dev Story: Write the next or specified story's tests and code.'
    This is the single clearest signal of what an agent actually
    produces — 'Developer' alone doesn't tell you it needs an existing
    story to work from rather than writing one."""
    items = []
    for cmd, label in MENU_ITEM_RE.findall(text):
        label = " ".join(label.split())  # collapse whitespace/newlines
        if label:
            items.append(label)
    return items


@dataclass
class DiscoveredAgent:
    id: str
    name: str
    title: str
    icon: str
    module: str
    source_path: str
    base_persona_md: str
    identity: str = ""
    menu_items: list = None
    suitable_roles: list = None


# Agents whose actual job is executing/implementing code rather than
# producing a document — these are noise for a PM profile who only
# wants BA/Architect/PM/UX/SM-style artifact agents. Matched against the
# .md filename stem (e.g. "dev.md" -> "dev"), not the display title, so
# it survives persona edits.
DEV_ONLY_SLUGS = {"dev", "quick-flow-solo-dev", "qa"}


def _roles_for(slug: str) -> list:
    if slug in DEV_ONLY_SLUGS:
        return ["developer"]
    return ["pm", "developer"]


def _module_of(path: Path) -> str:
    try:
        rel = path.relative_to(BMAD_ROOT)
        return rel.parts[0]
    except ValueError:
        return "unknown"


def discover_agents(bmad_root: Path = BMAD_ROOT) -> list[DiscoveredAgent]:
    """Walk <module>/agents/*.md (and one level of subfolders, e.g.
    tech-writer/tech-writer.md) and parse each into a DiscoveredAgent."""
    agents = []
    if not bmad_root.exists():
        return agents

    for agents_dir in bmad_root.glob("*/agents"):
        for md_file in agents_dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
            except Exception:
                continue

            fm_match = FRONTMATTER_RE.match(text)
            fm = {}
            if fm_match:
                try:
                    fm = yaml.safe_load(fm_match.group(1)) or {}
                except Exception:
                    fm = {}

            tag_match = AGENT_TAG_RE.search(text)
            module = _module_of(md_file)
            slug = fm.get("name") or md_file.stem

            if tag_match:
                agent_id, display_name, title, icon = tag_match.groups()
            else:
                agent_id = slug
                display_name = slug
                title = fm.get("description", slug)
                icon = ""

            identity_match = IDENTITY_RE.search(text)
            identity = " ".join(identity_match.group(1).split()) if identity_match else ""

            agents.append(DiscoveredAgent(
                id=f"{module}-{slug}",
                name=display_name,
                title=title or fm.get("description", slug),
                icon=icon,
                module=module,
                source_path=str(md_file.relative_to(bmad_root)),
                base_persona_md=text,
                identity=identity,
                menu_items=_menu_items(text),
                suitable_roles=_roles_for(md_file.stem),
            ))
    return agents


def sync_agents_to_db(db_session):
    """Idempotent upsert of discovered agents into the DB."""
    from app.models import Agent
    import json

    discovered = discover_agents()
    existing = {a.id: a for a in db_session.query(Agent).all()}

    for d in discovered:
        if d.id in existing:
            row = existing[d.id]
            row.name = d.name
            row.title = d.title
            row.icon = d.icon
            row.module = d.module
            row.source_path = d.source_path
            row.base_persona_md = d.base_persona_md
            row.identity = d.identity
            row.menu_items_json = json.dumps(d.menu_items or [])
            row.suitable_roles_json = json.dumps(d.suitable_roles or ["pm", "developer"])
        else:
            db_session.add(Agent(
                id=d.id, name=d.name, title=d.title, icon=d.icon,
                module=d.module, source_path=d.source_path,
                base_persona_md=d.base_persona_md,
                identity=d.identity,
                menu_items_json=json.dumps(d.menu_items or []),
                suitable_roles_json=json.dumps(d.suitable_roles or ["pm", "developer"]),
            ))
    db_session.commit()
    return discovered
