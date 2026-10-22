"""A bounded Markdown extractor; only canonical GitHub repository links survive."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

ROOT_ID = "sindresorhus/awesome"
OWNER = re.compile(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?\Z")
REPO = re.compile(r"[a-zA-Z0-9_.-]{1,100}\Z")
LINK = re.compile(
    r"(?<!!)\[([^\]\n]+)\](?:\(\s*(<?https?://[^\s)]+>?)\s*(?:[\"'][^\"']*[\"'])?\s*\)|\[([^\]\n]*)\])"
)
REFERENCE = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*<?(https?://[^\s>]+)>?")
BULLET = re.compile(r"^\s*(?:[-*+] |\d+[.)] )")


def canonical_repository(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip("<>"))
        if parts.scheme not in {"http", "https"} or parts.hostname != "github.com":
            return None
        if parts.username or parts.password or parts.port is not None:
            return None
        path = parts.path.strip("/").split("/")
        if len(path) != 2:
            return None
        owner, repo = path
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if (
            not OWNER.fullmatch(owner)
            or not REPO.fullmatch(repo)
            or repo in {".", ".."}
        ):
            return None
        if owner.lower() in {
            "topics",
            "collections",
            "settings",
            "marketplace",
            "sponsors",
            "orgs",
            "users",
        }:
            return None
        return f"{owner}/{repo}".lower()
    except ValueError:
        return None


def slug(text: str) -> str:
    text = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    )
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "other"


@dataclass
class Catalog:
    records: list[dict]
    category_names: dict[str, str]


def _lines(markdown: str):
    references = {}
    in_fence = False
    visible = []
    for line in markdown.splitlines():
        if re.match(r"^\s{0,3}(`{3,}|~{3,})", line):
            in_fence = not in_fence
            visible.append(
                ""
            )  # Removed examples must not join surrounding Setext lines.
            continue
        if in_fence:
            continue
        match = REFERENCE.match(line)
        if match:
            references[match.group(1).strip().lower()] = match.group(2)
            visible.append("")
        else:
            visible.append(line)
    return visible, references


def _setext_headings(lines):
    """Normalize adjacent, single-line Setext headings outside code and table rows."""
    underline = re.compile(r" {0,3}(=+|-+)[ \t]*\Z")
    rule = re.compile(r" {0,3}(?:(?:-\s*){3,}|(?:\*\s*){3,}|(?:_\s*){3,})\Z")
    normalized = []
    index = 0
    while index < len(lines):
        line = lines[index]
        marker = (
            underline.fullmatch(lines[index + 1]) if index + 1 < len(lines) else None
        )
        title = line.strip()
        plain_title = (
            re.match(r" {0,3}\S", line)
            and not line.startswith("\t")
            and title
            and not title.startswith(("#", ">", "<"))
            and "|" not in line
            and not BULLET.match(line)
            and not rule.fullmatch(line)
            and not underline.fullmatch(line)
        )
        if marker and plain_title:
            level = "#" if marker.group(1).startswith("=") else "##"
            normalized.append(f"{level} {title}")
            index += 2
        else:
            normalized.append(line)
            index += 1
    return normalized


def _extract(markdown: str, *, source_id: str, categories: list[str], catalog: bool):
    lines, references = _lines(markdown)
    if not catalog:
        lines = _setext_headings(lines)
    records: dict[str, dict[str, Any]] = {}
    category_names: dict[str, str] = {}
    current_categories = categories
    current_sections: list[str] = []
    for line in lines:
        heading_pattern = (
            r"^\s{0,3}#{2,6}\s+(.+?)\s*#*\s*$"
            if catalog
            else r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$"
        )
        heading = re.match(heading_pattern, line)
        if heading:
            name = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading.group(1)).strip()
            name = (
                re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", name)).strip()[:256].strip()
            )
            current_sections = [name] if name else []
            if catalog:
                if not name or name.lower() in {
                    "contents",
                    "table of contents",
                    "related",
                    "contributing",
                }:
                    current_categories = []
                else:
                    category_id = slug(name)
                    category_names[category_id] = name
                    current_categories = [category_id]
            continue
        if not BULLET.match(line):
            # Some lists use Markdown tables. Read only the first repository column.
            table_line = line.lstrip()
            if "|" not in table_line or not table_line.startswith(("[", "|")):
                continue
            line = table_line.lstrip("|").split("|", 1)[0]
        for match in LINK.finditer(line):
            name, url, reference = match.groups()
            url = url or references.get((reference or name).strip().lower(), "")
            repo_id = canonical_repository(url)
            if repo_id is None or repo_id == source_id:
                continue
            # Badge links wrapping images cannot be project records.
            if name.startswith("!"):
                continue
            owner, repo = repo_id.split("/")
            description = re.sub(r"^\s*[-–—:]\s*", "", line[match.end() :].strip())
            description = re.sub(r"<br\s*/?>", " ", description, flags=re.IGNORECASE)
            description = re.sub(
                r"\s+", " ", re.sub(r"<[^>]+>", "", description)
            ).strip()
            kind = (
                "list"
                if catalog or re.search(r"(?:^|[-_.])awesome(?:$|[-_.])", repo)
                else "project"
            )
            if repo_id in records:
                record = records[repo_id]
                record["categories"] = list(
                    dict.fromkeys(record["categories"] + current_categories)
                )
                record["sections"] = list(
                    dict.fromkeys(record["sections"] + current_sections)
                )
                if not record["description"] and description:
                    record["description"] = description[:2000]
                continue
            records[repo_id] = {
                "id": repo_id,
                "name": name.strip()[:200],
                "owner": owner,
                "description": description[:2000],
                "url": f"https://github.com/{repo_id}",
                "kind": kind,
                "categories": list(current_categories or ["other"]),
                "sections": list(current_sections),
            }
    return Catalog(list(records.values()), category_names)


def parse_catalog(markdown: str) -> Catalog:
    return _extract(markdown, source_id=ROOT_ID, categories=[], catalog=True)


def parse_projects(markdown: str, source_id: str, categories: list[str]) -> list[dict]:
    return _extract(
        markdown, source_id=source_id, categories=categories, catalog=False
    ).records
