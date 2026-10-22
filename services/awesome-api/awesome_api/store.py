"""SQLite is a replaceable read projection; committed lake captures authorize writes."""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .daily import DAILY_SOURCE
from .lake import Lake, utc_now
from .parser import ROOT_ID

ROOT_SOURCE = {
    "id": ROOT_ID,
    "name": "Awesome",
    "owner": "sindresorhus",
    "url": "https://github.com/sindresorhus/awesome",
    "category": "meta",
    "categories": ["meta"],
}
SOURCE_COUNT = f"""(SELECT count(DISTINCT mentions.source_id) FROM memberships mentions
    JOIN sources mention_source ON mention_source.id=mentions.source_id
    WHERE mentions.project_id=p.id AND mention_source.active=1 AND mentions.source_id<>'{ROOT_ID}')"""


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL, url TEXT NOT NULL,
                    category TEXT NOT NULL, categories TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, last_synced_at TEXT, last_attempt_at TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL,
                    description TEXT NOT NULL, url TEXT NOT NULL, kind TEXT NOT NULL,
                    discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memberships (
                    source_id TEXT NOT NULL REFERENCES sources(id),
                    project_id TEXT NOT NULL REFERENCES projects(id), categories TEXT NOT NULL,
                    PRIMARY KEY (source_id, project_id)
                );
                CREATE INDEX IF NOT EXISTS memberships_project ON memberships(project_id);
                CREATE INDEX IF NOT EXISTS projects_updated ON projects(updated_at DESC);
                CREATE TABLE IF NOT EXISTS categories (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS applied_captures (event_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS daily_sources (
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,url TEXT NOT NULL,description TEXT NOT NULL,
                    last_synced_at TEXT,error TEXT
                );
                CREATE TABLE IF NOT EXISTS daily_snapshots (
                    source_id TEXT NOT NULL REFERENCES daily_sources(id),date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,event_id TEXT NOT NULL,PRIMARY KEY(source_id,date)
                );
                CREATE TABLE IF NOT EXISTS daily_entries (
                    source_id TEXT NOT NULL,date TEXT NOT NULL,rank INTEGER NOT NULL,
                    id TEXT NOT NULL,name TEXT NOT NULL,owner TEXT NOT NULL,url TEXT NOT NULL,
                    PRIMARY KEY(source_id,date,id),UNIQUE(source_id,date,rank),
                    FOREIGN KEY(source_id,date) REFERENCES daily_snapshots(source_id,date)
                );
            """)
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(memberships)")
            }
            if "sections" not in columns:
                db.execute(
                    "ALTER TABLE memberships ADD COLUMN sections TEXT NOT NULL DEFAULT '[]'"
                )
            self._source(db, ROOT_SOURCE)
            self._daily_source(db, DAILY_SOURCE)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _source(db, source):
        db.execute(
            """
            INSERT INTO sources(id,name,owner,url,category,categories)
            VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,owner=excluded.owner,url=excluded.url,
            category=excluded.category,categories=excluded.categories,active=1
        """,
            (
                source["id"],
                source["name"],
                source["owner"],
                source["url"],
                source["category"],
                json.dumps(source["categories"]),
            ),
        )

    def _apply(self, db, manifest, records):
        event_id = manifest["event_id"]
        if db.execute(
            "SELECT 1 FROM applied_captures WHERE event_id=?", (event_id,)
        ).fetchone():
            return
        source = manifest["source"]
        source_id = source["id"]
        captured_at = manifest["captured_at"]
        if manifest["role"] == "daily":
            self._apply_daily(db, manifest, records)
            db.execute("INSERT INTO applied_captures VALUES (?)", (event_id,))
            return
        self._source(db, source)
        if manifest["role"] == "root":
            if not records:
                raise ValueError("root catalog ต้องมีรายการก่อนแทนที่ projection")
            for record in records:
                self._source(
                    db,
                    {
                        "id": record["id"],
                        "name": record["name"],
                        "owner": record["owner"],
                        "url": record["url"],
                        "category": record["categories"][0],
                        "categories": record["categories"],
                    },
                )
        else:
            for record in records:
                if (
                    record["kind"] == "list"
                    and record["id"] != ROOT_ID
                    and not db.execute(
                        "SELECT 1 FROM sources WHERE id=?", (record["id"],)
                    ).fetchone()
                ):
                    self._source(
                        db,
                        {
                            "id": record["id"],
                            "name": record["name"],
                            "owner": record["owner"],
                            "url": record["url"],
                            "category": record["categories"][0],
                            "categories": record["categories"],
                        },
                    )
        db.execute("DELETE FROM memberships WHERE source_id=?", (source_id,))
        names = manifest.get("category_names", {})
        for record in records:
            db.execute(
                """
                INSERT INTO projects(id,name,owner,description,url,kind,discovered_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,owner=excluded.owner,url=excluded.url,
                description=CASE WHEN excluded.description='' THEN projects.description ELSE excluded.description END,
                kind=CASE WHEN projects.kind='list' THEN 'list' ELSE excluded.kind END,
                updated_at=excluded.updated_at
            """,
                (
                    record["id"],
                    record["name"],
                    record["owner"],
                    record["description"],
                    record["url"],
                    record["kind"],
                    captured_at,
                    captured_at,
                ),
            )
            db.execute(
                "INSERT INTO memberships(source_id,project_id,categories,sections) VALUES (?,?,?,?)",
                (
                    source_id,
                    record["id"],
                    json.dumps(record["categories"]),
                    json.dumps(record.get("sections") or []),
                ),
            )
            for category in record["categories"]:
                db.execute(
                    """INSERT INTO categories VALUES (?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name""",
                    (category, names.get(category, self._category_name(db, category))),
                )
        # Keep inactive snapshots as a cache: reconnecting sources retain real prior coverage.
        db.execute(
            """WITH RECURSIVE reachable(id) AS (
            SELECT ? UNION SELECT m.project_id FROM memberships m
            JOIN projects p ON p.id=m.project_id JOIN sources child ON child.id=p.id
            JOIN reachable parent ON parent.id=m.source_id WHERE p.kind='list'
        ) UPDATE sources SET active=(id IN (SELECT id FROM reachable))""",
            (ROOT_ID,),
        )
        db.execute(
            "DELETE FROM projects WHERE id NOT IN (SELECT project_id FROM memberships)"
        )
        db.execute(
            """UPDATE sources SET last_synced_at=?,last_attempt_at=?,error=NULL WHERE id=?""",
            (captured_at, captured_at, source_id),
        )
        db.execute("INSERT INTO applied_captures VALUES (?)", (event_id,))

    @staticmethod
    def _daily_source(db, source):
        db.execute(
            """INSERT INTO daily_sources(id,name,url,description) VALUES (?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,description=excluded.description""",
            (source["id"], source["name"], source["url"], source["description"]),
        )

    def _apply_daily(self, db, manifest, records):
        source_id, day, captured = (
            manifest["source"]["id"],
            manifest["snapshot_date"],
            manifest["captured_at"],
        )
        self._daily_source(db, manifest["source"])
        db.execute(
            """INSERT INTO daily_snapshots VALUES (?,?,?,?) ON CONFLICT(source_id,date) DO UPDATE SET
            captured_at=excluded.captured_at,event_id=excluded.event_id""",
            (source_id, day, captured, manifest["event_id"]),
        )
        db.execute(
            "DELETE FROM daily_entries WHERE source_id=? AND date=?", (source_id, day)
        )
        for record in records:
            db.execute(
                "INSERT INTO daily_entries VALUES (?,?,?,?,?,?,?)",
                (
                    source_id,
                    day,
                    record["rank"],
                    record["id"],
                    record["name"],
                    record["owner"],
                    record["url"],
                ),
            )
        db.execute(
            "UPDATE daily_sources SET last_synced_at=?,error=NULL WHERE id=?",
            (captured, source_id),
        )

    @staticmethod
    def _category_name(db, category):
        row = db.execute(
            "SELECT name FROM categories WHERE id=?", (category,)
        ).fetchone()
        return row[0] if row else category.replace("-", " ").title()

    def replace_capture(self, lake: Lake, manifest_path: Path):
        # Validate both raw + Bronze checksums before opening a write transaction.
        manifest, records = lake.read(manifest_path)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._apply(db, manifest, records)

    def rebuild(self, lake: Lake) -> int:
        captures = (
            lake.captures()
        )  # Validate ALL committed artifacts before modifying SQLite.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            for table in (
                "daily_entries",
                "daily_snapshots",
                "daily_sources",
                "memberships",
                "projects",
                "sources",
                "categories",
                "metadata",
                "applied_captures",
            ):
                # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- table comes from the constant tuple above
                db.execute(f"DELETE FROM {table}")
            self._source(db, ROOT_SOURCE)
            self._daily_source(db, DAILY_SOURCE)
            for manifest, records in captures:
                self._apply(db, manifest, records)
            if captures:
                db.execute(
                    "INSERT INTO metadata VALUES ('last_synced_at',?)",
                    (json.dumps(captures[-1][0]["captured_at"]),),
                )
        return len(captures)

    def source_detail(self, source_id: str) -> dict:
        with self.connection() as db:
            row = db.execute(
                "SELECT * FROM sources WHERE id=?", (source_id,)
            ).fetchone()
        source = dict(row)
        source["categories"] = json.loads(source["categories"])
        return source

    def attempted(self, source_id: str):
        with self.connection() as db:
            db.execute(
                "UPDATE sources SET last_attempt_at=? WHERE id=?",
                (utc_now(), source_id),
            )

    def failed(self, source_id: str, error: str):
        with self.connection() as db:
            db.execute(
                "UPDATE sources SET error=? WHERE id=?", (error[:300], source_id)
            )

    def metadata(self, key: str, value=None, *, write=False):
        with self.connection() as db:
            if write:
                db.execute(
                    "INSERT INTO metadata VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
                return value
            row = db.execute(
                "SELECT value FROM metadata WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _search(q):
        return (
            "%"
            + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            + "%"
        )

    def _items(self, db, rows, section_sources=None):
        items = []
        for row in rows:
            item = dict(row)
            memberships = db.execute(
                """SELECT m.source_id,m.categories,m.sections FROM memberships m
                JOIN sources s ON s.id=m.source_id WHERE project_id=? AND s.active=1""",
                (item["id"],),
            ).fetchall()
            item["source_ids"] = sorted(m["source_id"] for m in memberships)
            item["source_count"] = len(
                {m["source_id"] for m in memberships if m["source_id"] != ROOT_ID}
            )
            item["categories"] = sorted(
                {c for m in memberships for c in json.loads(m["categories"])}
            )
            item["sections"] = sorted(
                {
                    section
                    for m in memberships
                    for section in json.loads(m["sections"])
                    if section_sources is None or m["source_id"] in section_sources
                }
            )
            item["stars"], item["language"] = None, None
            items.append(item)
        return items

    def projects(
        self,
        *,
        q="",
        category="",
        source="",
        kind="project",
        sort="recent",
        limit=30,
        offset=0,
        ids=None,
        min_sources=0,
        section="",
        _extra=None,
        _section_sources=None,
        _resolve_daily=True,
    ):
        if ids is not None and _resolve_daily:
            return self._identified_projects(
                ids,
                q=q,
                category=category,
                source=source,
                kind=kind,
                sort=sort,
                limit=limit,
                offset=offset,
                min_sources=min_sources,
                section=section,
                _extra=_extra,
                _section_sources=_section_sources,
            )
        clauses, params = [], []
        if kind != "all":
            clauses.append("p.kind=?")
            params.append(kind)
        if q:
            clauses.append(
                "LOWER(p.name || ' ' || p.owner || ' ' || p.description) LIKE ? ESCAPE '\\'"
            )
            params.append(self._search(q))
        membership = ["m.project_id=p.id", "s.active=1"]
        if source:
            membership.append("m.source_id=?")
            params.append(source.lower())
        if category:
            membership.append(
                "EXISTS (SELECT 1 FROM json_each(m.categories) WHERE value=?)"
            )
            params.append(category)
        if section:
            membership.append(
                "EXISTS (SELECT 1 FROM json_each(m.sections) WHERE value=?)"
            )
            params.append(section)
        clauses.append(
            "EXISTS (SELECT 1 FROM memberships m JOIN sources s ON s.id=m.source_id WHERE "
            + " AND ".join(membership)
            + ")"
        )
        if ids is not None:
            if not ids:
                return {"items": [], "total": 0, "limit": limit, "offset": offset}
            clauses.append("p.id IN (" + ",".join("?" for _ in ids) + ")")
            params.extend(ids)
        if min_sources:
            clauses.append(f"{SOURCE_COUNT}>=?")
            params.append(min_sources)
        if _extra:
            clauses.append(_extra[0])
            params.extend(_extra[1])
        where = " AND ".join(clauses)
        order = (
            f"{SOURCE_COUNT} DESC,p.id"
            if sort == "mentions"
            else "LOWER(p.name),p.id"
            if sort == "name"
            else "p.updated_at DESC,p.id"
        )
        with self.connection() as db:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- where/order are built only from constant fragments; values are ? parameters
            total = db.execute(
                f"SELECT count(*) FROM projects p WHERE {where}", params
            ).fetchone()[0]
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- where/order are built only from constant fragments; values are ? parameters
            rows = db.execute(
                f"SELECT p.* FROM projects p WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            items = self._items(
                db, rows, {source.lower()} if source else _section_sources
            )
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @staticmethod
    def _daily_project(row, captured_at):
        return {
            "id": row["id"],
            "name": row["name"],
            "owner": row["owner"],
            "url": row["url"],
            "description": "",
            "kind": "project",
            "categories": [],
            "source_ids": [],
            "sections": [],
            "source_count": 0,
            "stars": None,
            "language": None,
            "discovered_at": captured_at,
            "updated_at": captured_at,
        }

    def _identified_projects(
        self,
        ids,
        *,
        q,
        category,
        source,
        kind,
        sort,
        limit,
        offset,
        min_sources,
        section,
        _extra=None,
        _section_sources=None,
    ):
        # Explicit IDs (max 200 at the API) can resolve bookmarks from both catalogs.
        # Ordinary discovery queries remain scoped to active Awesome memberships.
        ids = list(dict.fromkeys(ids))
        result = self.projects(
            ids=ids,
            q=q,
            category=category,
            source=source,
            kind=kind,
            sort=sort,
            limit=len(ids) or 1,
            offset=0,
            min_sources=min_sources,
            section=section,
            _extra=_extra,
            _section_sources=_section_sources,
            _resolve_daily=False,
        )
        items = result["items"]
        if ids and not (
            category or source or section or min_sources or kind == "list" or _extra
        ):
            with self.connection() as db:
                placeholders = ",".join("?" for _ in ids)
                active = {
                    row[0]
                    # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- placeholders is only '?' marks; ids are parameters
                    for row in db.execute(
                        """SELECT DISTINCT m.project_id FROM memberships m
                    JOIN sources s ON s.id=m.source_id AND s.active=1 WHERE m.project_id IN ("""
                        + placeholders
                        + ")",
                        ids,
                    )
                }
                for repo_id in ids:
                    if repo_id in active:
                        continue
                    row = db.execute(
                        """SELECT de.*,ds.captured_at FROM daily_entries de
                        JOIN daily_snapshots ds ON ds.source_id=de.source_id AND ds.date=de.date
                        WHERE de.id=? ORDER BY ds.date DESC,ds.captured_at DESC,de.source_id LIMIT 1""",
                        (repo_id,),
                    ).fetchone()
                    if row:
                        project = self._daily_project(row, row["captured_at"])
                        if (
                            not q
                            or q.lower()
                            in (
                                project["name"]
                                + " "
                                + project["owner"]
                                + " "
                                + project["description"]
                            ).lower()
                        ):
                            items.append(project)
        if sort == "mentions":
            items.sort(key=lambda item: (-item["source_count"], item["id"]))
        elif sort == "name":
            items.sort(key=lambda item: (item["name"].lower(), item["id"]))
        else:
            items.sort(key=lambda item: item["id"])
            items.sort(key=lambda item: item["updated_at"], reverse=True)
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "limit": limit,
            "offset": offset,
        }

    def categories(self):
        with self.connection() as db:
            rows = db.execute("""SELECT c.id,c.name,count(DISTINCT CASE WHEN p.kind='project' THEN p.id END) AS count
                FROM categories c JOIN memberships m ON EXISTS (SELECT 1 FROM json_each(m.categories) WHERE value=c.id)
                JOIN sources s ON s.id=m.source_id AND s.active=1 JOIN projects p ON p.id=m.project_id
                GROUP BY c.id,c.name ORDER BY LOWER(c.name),c.id""").fetchall()
        return [dict(row) for row in rows]

    def sources(self):
        with self.connection() as db:
            rows = db.execute(
                """SELECT s.id,s.name,s.owner,s.url,s.category,
                count(DISTINCT CASE WHEN p.kind='project' THEN p.id END) AS project_count,
                count(DISTINCT p.id) AS item_count,
                s.last_synced_at,s.error FROM sources s
                LEFT JOIN memberships m ON m.source_id=s.id LEFT JOIN projects p ON p.id=m.project_id
                WHERE s.active=1 AND s.id<>? GROUP BY s.id ORDER BY LOWER(s.name),s.id""",
                (ROOT_ID,),
            ).fetchall()
            sections = db.execute("""SELECT m.source_id,heading.value AS name,count(DISTINCT m.project_id) AS count
                FROM memberships m JOIN sources s ON s.id=m.source_id AND s.active=1,
                json_each(m.sections) heading GROUP BY m.source_id,heading.value ORDER BY heading.value""").fetchall()
        by_source: dict[str, list[dict[str, object]]] = {}
        for heading in sections:
            by_source.setdefault(heading["source_id"], []).append(
                {"name": heading["name"], "count": heading["count"]}
            )
        return [{**dict(row), "sections": by_source.get(row["id"], [])} for row in rows]

    def compare(
        self, left, right, *, mode="shared", kind="all", q="", limit=30, offset=0
    ):
        sources = {source["id"]: source for source in self.sources()}
        if left not in sources or right not in sources:
            raise KeyError("source ไม่พบหรือไม่ได้ active")
        clauses, parameters = ["m.source_id IN (?,?)"], [left, right]
        if kind != "all":
            clauses.append("p.kind=?")
            parameters.append(kind)
        with self.connection() as db:
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- clauses are constant fragments; values are ? parameters
            rows = db.execute(
                """SELECT m.project_id,count(DISTINCT m.source_id) AS mentions,
                max(CASE WHEN m.source_id=? THEN 1 ELSE 0 END) AS in_left FROM memberships m
                JOIN projects p ON p.id=m.project_id WHERE """
                + " AND ".join(clauses)
                + " GROUP BY m.project_id",
                [left, *parameters],
            ).fetchall()
        shared = sum(row["mentions"] == 2 for row in rows)
        left_only = sum(row["mentions"] == 1 and row["in_left"] for row in rows)
        right_only = len(rows) - shared - left_only
        left_match = "EXISTS(SELECT 1 FROM memberships cm WHERE cm.project_id=p.id AND cm.source_id=?)"
        right_match = left_match
        expressions = {
            "shared": f"{left_match} AND {right_match}",
            "left_only": f"{left_match} AND NOT {right_match}",
            "right_only": f"NOT {left_match} AND {right_match}",
            "all": f"({left_match} OR {right_match})",
        }
        body = self.projects(
            q=q,
            kind=kind,
            sort="mentions",
            limit=limit,
            offset=offset,
            _extra=(expressions[mode], [left, right]),
            _section_sources={left, right},
        )
        return {
            **body,
            "left": sources[left],
            "right": sources[right],
            "shared_count": shared,
            "left_only_count": left_only,
            "right_only_count": right_only,
            "union_count": len(rows),
            "overlap_percent": round(100 * shared / len(rows), 2) if rows else 0,
        }

    def daily_sources(self):
        with self.connection() as db:
            rows = db.execute("SELECT * FROM daily_sources ORDER BY id").fetchall()
            items = []
            for row in rows:
                dates = [
                    r[0]
                    for r in db.execute(
                        "SELECT date FROM daily_snapshots WHERE source_id=? ORDER BY date DESC",
                        (row["id"],),
                    )
                ]
                items.append(
                    {
                        **dict(row),
                        "latest_date": dates[0] if dates else None,
                        "dates": dates,
                    }
                )
        return items

    def daily_error(self, error):
        with self.connection() as db:
            db.execute(
                "UPDATE daily_sources SET error=? WHERE id=?",
                (error[:1000] if error else None, DAILY_SOURCE["id"]),
            )

    def daily(self, *, source=DAILY_SOURCE["id"], date=None, q="", limit=30, offset=0):
        source_info = next(
            (item for item in self.daily_sources() if item["id"] == source), None
        )
        if source_info is None:
            raise KeyError("ไม่พบ daily source")
        selected = date or source_info["latest_date"]
        with self.connection() as db:
            snapshot = db.execute(
                "SELECT * FROM daily_snapshots WHERE source_id=? AND date=?",
                (source, selected),
            ).fetchone()
            params = [source, selected]
            search = ""
            if q:
                search = " AND LOWER(COALESCE(p.name,de.name) || ' ' || de.owner || ' ' || COALESCE(p.description,'')) LIKE ? ESCAPE '\\'"
                params.append(self._search(q))
            sql = (
                """FROM daily_entries de LEFT JOIN projects p ON p.id=de.id AND EXISTS (
                SELECT 1 FROM memberships m JOIN sources s ON s.id=m.source_id AND s.active=1 WHERE m.project_id=p.id)
                WHERE de.source_id=? AND de.date=?"""
                + search
            )
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- sql is constant text plus a constant search clause; values are ? parameters
            total = db.execute("SELECT count(*) " + sql, params).fetchone()[0]
            # nosemgrep: python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- sql is constant text plus a constant search clause; values are ? parameters
            rows = db.execute(
                "SELECT de.* " + sql + " ORDER BY de.rank,de.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                project_row = db.execute(
                    """SELECT p.* FROM projects p WHERE p.id=? AND EXISTS (
                    SELECT 1 FROM memberships m JOIN sources s ON s.id=m.source_id AND s.active=1 WHERE m.project_id=p.id)""",
                    (row["id"],),
                ).fetchone()
                if project_row:
                    project = self._items(db, [project_row])[0]
                else:
                    project = self._daily_project(row, snapshot["captured_at"])
                items.append(
                    {
                        "rank": row["rank"],
                        "project": project,
                        "source_count": project["source_count"],
                    }
                )
        return {
            "items": items,
            "total": total,
            "limit": limit,
            "offset": offset,
            "source": source_info,
            "date": selected,
            "captured_at": snapshot["captured_at"] if snapshot else None,
        }

    def candidates(self, maximum: int):
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM sources WHERE active=1 AND id<>?", (ROOT_ID,)
            ).fetchall()
        preferred = (
            "artificial-intelligence",
            "machine-learning",
            "ai",
            "developer",
            "development",
            "self-host",
            "database",
            "design",
        )

        def rank(row):
            text = row["id"] + " " + row["category"]
            priority = any(term in text for term in preferred)
            return (row["last_attempt_at"] or "", not priority, row["id"])

        return [row["id"] for row in sorted(rows, key=rank)[:maximum]]

    def errors(self):
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,error FROM sources WHERE active=1 AND error IS NOT NULL ORDER BY id"
            ).fetchall()
            rows = [
                *rows,
                *db.execute(
                    "SELECT id,error FROM daily_sources WHERE error IS NOT NULL ORDER BY id"
                ).fetchall(),
            ]
        return [f"{row['id']}: {row['error']}" for row in rows]
