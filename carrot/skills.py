"""SKILL.md-based skills: user-editable instruction packs for the chat agent.

A skill lives at carrot/data/skills/<slug>/SKILL.md with YAML-ish frontmatter:

    ---
    name: My Skill
    description: What this skill is for
    ---
    The instructions injected into the model when the skill is invoked.
"""
import os
import re
import shutil

SKILLS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "skills")


def ensure_skills_dir():
    os.makedirs(SKILLS_DIR, exist_ok=True)


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _parse_skill_md(text: str) -> dict:
    meta = {"name": "", "description": ""}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip().lower()
                    if key in meta:
                        meta[key] = value.strip()
            body = parts[2].lstrip("\n")
    return {**meta, "instructions": body.strip()}


def list_skills() -> list:
    ensure_skills_dir()
    skills = []
    for entry in sorted(os.listdir(SKILLS_DIR)):
        path = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                parsed = _parse_skill_md(f.read())
            skills.append({
                "slug": entry,
                "name": parsed["name"] or entry,
                "description": parsed["description"],
            })
    return skills


def get_skill(slug: str):
    path = os.path.join(SKILLS_DIR, slug, "SKILL.md")
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        parsed = _parse_skill_md(f.read())
    return {"slug": slug, "name": parsed["name"] or slug,
            "description": parsed["description"], "instructions": parsed["instructions"]}


def save_skill(name: str, description: str, instructions: str, slug: str = None) -> dict:
    ensure_skills_dir()
    slug = slug or slugify(name)
    skill_dir = os.path.join(SKILLS_DIR, slug)
    os.makedirs(skill_dir, exist_ok=True)
    content = f"---\nname: {name}\ndescription: {description}\n---\n\n{instructions.strip()}\n"
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write(content)
    return {"slug": slug, "name": name, "description": description}


def delete_skill(slug: str) -> bool:
    skill_dir = os.path.join(SKILLS_DIR, slug)
    if os.path.isdir(skill_dir):
        shutil.rmtree(skill_dir)
        return True
    return False
