import json
import sqlite3
from datetime import date

import pytest
from fastapi.testclient import TestClient

from awesome_api.app import create_app
from awesome_api.config import Config
from awesome_api.daily import DAILY_SOURCE, parse_daily
from awesome_api.lake import Lake
from awesome_api.parser import ROOT_ID, parse_catalog, parse_projects
from awesome_api.store import Store
from awesome_api.sync import Coordinator

LEFT = "org/awesome-tools"
RIGHT = "org/awesome-databases"
NESTED = "org/awesome-nested"
DAY = "2026-10-04"
ROOT = b"## Tools\n- [Tools](https://github.com/org/awesome-tools)\n## Databases\n- [Databases](https://github.com/org/awesome-databases)"
TOOLS = b"""# Tools
## CLI
- [Alpha](https://github.com/a/alpha) - Useful alpha.
- [Beta](https://github.com/b/beta) - Beta tool.
### Automation
- [Alpha](https://github.com/A/ALPHA.git#setup)
- [Nested](https://github.com/org/awesome-nested)
"""
DATABASES = b"## Data\n- [Alpha](https://github.com/a/alpha)\n- [Gamma](https://github.com/c/gamma)"


def daily_raw(day=DAY, repos=None):
    return json.dumps(
        {
            "date": day,
            "language": None,
            "list": repos or ["a/alpha", "Ghost/Repo", "A/ALPHA.git#setup", "z/last"],
        }
    ).encode()


@pytest.fixture
def discovery(tmp_path):
    config = Config(runtime_dir=tmp_path, sync_enabled=False)
    store, lake = Store(tmp_path / "catalog.sqlite3"), Lake(tmp_path / "lake")
    payloads = {
        ROOT_ID: ROOT,
        LEFT: TOOLS,
        RIGHT: DATABASES,
        NESTED: b"## Nested\n- [Delta](https://github.com/d/delta)",
    }
    worker = Coordinator(
        config,
        store,
        lake,
        fetch=lambda source: payloads[source],
        daily_fetch=lambda source, day: None,
    )
    worker.sync()
    return config, store, lake, worker, payloads


def test_sections_preserve_duplicate_memberships_and_table_first_column():
    records = parse_projects(
        """## Animation
[Gallery](https://github.com/Org/Gallery) <br> A <b>gallery</b>. | <img src="image.png">
### Transitions
| [Gallery](https://github.com/org/gallery.git#top) | Other |
| [Second](https://github.com/org/second) - Useful. | [Other](https://github.com/ignore/secondcolumn) |
Some prose [Skip](https://github.com/ignore/prose)
""",
        LEFT,
        ["tools"],
    )
    assert [r["id"] for r in records] == ["org/gallery", "org/second"]
    assert records[0]["sections"] == ["Animation", "Transitions"]
    assert records[0]["description"] == "A gallery."


def test_mentions_sections_and_source_counts(discovery):
    config, store, lake, worker, _ = discovery
    with TestClient(
        create_app(config, store=store, lake=lake, coordinator=worker)
    ) as client:
        items = client.get("/api/v1/projects", params={"sort": "mentions"}).json()[
            "items"
        ]
        assert [item["id"] for item in items] == ["a/alpha", "b/beta", "c/gamma"]
        assert items[0]["source_count"] == 2
        assert set(items[0]["sections"]) == {"CLI", "Automation", "Data"}
        assert (
            client.get("/api/v1/projects", params={"min_sources": 2}).json()["total"]
            == 1
        )
        filtered = client.get(
            "/api/v1/projects",
            params={"source": LEFT, "section": "Automation", "kind": "all"},
        ).json()
        assert {p["id"] for p in filtered["items"]} == {"a/alpha", NESTED}
        assert next(p for p in filtered["items"] if p["id"] == "a/alpha")[
            "sections"
        ] == ["Automation", "CLI"]
        lists = client.get("/api/v1/projects", params={"kind": "list"}).json()["items"]
        assert next(p for p in lists if p["id"] == LEFT)["source_count"] == 0
        source = next(
            s for s in client.get("/api/v1/sources").json()["items"] if s["id"] == LEFT
        )
        assert source["item_count"] == 3 and source["project_count"] == 2
        assert {s["name"]: s["count"] for s in source["sections"]} == {
            "Automation": 2,
            "CLI": 2,
        }
        assert (
            client.get("/api/v1/projects", params={"min_sources": -1}).status_code
            == 422
        )
        assert (
            client.get("/api/v1/projects", params={"min_sources": 1001}).status_code
            == 422
        )


def test_long_heading_is_plain_bounded_and_filterable_by_returned_name(discovery):
    config, store, lake, worker, payloads = discovery
    payloads[LEFT] = (
        "## <b>" + "A" * 300 + "</b>\n- [Beta](https://github.com/b/beta)"
    ).encode()
    assert worker._capture(LEFT) is True
    source = next(s for s in store.sources() if s["id"] == LEFT)
    assert source["sections"] == [{"name": "A" * 256, "count": 1}]
    with TestClient(
        create_app(config, store=store, lake=lake, coordinator=worker)
    ) as client:
        result = client.get(
            "/api/v1/projects",
            params={"source": LEFT, "section": source["sections"][0]["name"]},
        ).json()
        assert result["total"] == 1 and result["items"][0]["sections"] == ["A" * 256]
        assert (
            client.get("/api/v1/projects", params={"section": "A" * 257}).status_code
            == 422
        )


def test_ios_setext_sections_split_tables_and_preserve_duplicate_repositories(
    discovery,
):
    config, store, lake, worker, payloads = discovery
    payloads[LEFT] = b"""Awesome iOS UI
==============

## Index
- [Animations and Transitions](#animations-and-transitions)
- [Badges](#badges)

Animations and Transitions
==========================
Project | Preview
------- | -------
[AnimatedTransitionGallery](https://github.com/shu223/AnimatedTransitionGallery) <br> Transitions | <img src="preview.png">
[Other Transition](https://github.com/example/transition) | Preview

Badges
------
[Gallery Again](https://github.com/SHU223/AnimatedTransitionGallery.git#example) | Preview
[Badge](https://github.com/example/badge) | Preview
"""
    assert worker._capture(LEFT) is True
    source = next(s for s in store.sources() if s["id"] == LEFT)
    assert {s["name"]: s["count"] for s in source["sections"]} == {
        "Animations and Transitions": 2,
        "Badges": 2,
    }
    assert source["item_count"] == 3
    with TestClient(
        create_app(config, store=store, lake=lake, coordinator=worker)
    ) as client:
        for section, expected in [
            (
                "Animations and Transitions",
                {"shu223/animatedtransitiongallery", "example/transition"},
            ),
            ("Badges", {"shu223/animatedtransitiongallery", "example/badge"}),
        ]:
            result = client.get(
                "/api/v1/projects", params={"source": LEFT, "section": section}
            ).json()
            assert {p["id"] for p in result["items"]} == expected
        gallery = client.get(
            "/api/v1/projects",
            params={"source": LEFT, "ids": "shu223/animatedtransitiongallery"},
        ).json()["items"][0]
        assert gallery["sections"] == ["Animations and Transitions", "Badges"]
        assert gallery["source_count"] == 1


def test_atx_h1_source_sections_exclude_fences_bullet_underlines_and_rules():
    records = parse_projects(
        """# Awesome Tools
# Components
- [Alpha](https://github.com/example/alpha)
- A bullet is not a heading
-------------------------
- [Beta](https://github.com/example/beta)
Project | Preview
------- | -------
[Gamma](https://github.com/example/gamma) | Preview

---

Text before a fenced example
```markdown
Fake section
============
- [Fake](https://github.com/example/fake)
```
------
- [Delta](https://github.com/example/delta)
""",
        LEFT,
        ["tools"],
    )
    assert [p["id"] for p in records] == [
        "example/alpha",
        "example/beta",
        "example/gamma",
        "example/delta",
    ]
    assert all(p["sections"] == ["Components"] for p in records)


def test_root_catalog_keeps_existing_atx_categories_and_ignores_top_h1():
    catalog = parse_catalog("""# Root Title
Another top title
=================
## Developer Tools
- [Tools](https://github.com/org/awesome-tools)
""")
    assert catalog.category_names == {"developer-tools": "Developer Tools"}
    assert catalog.records[0]["sections"] == ["Developer Tools"]


def test_compare_counts_modes_pagination_and_query(discovery):
    config, store, lake, worker, _ = discovery
    with TestClient(
        create_app(config, store=store, lake=lake, coordinator=worker)
    ) as client:
        params = {"left": LEFT, "right": RIGHT, "mode": "all", "limit": 1, "offset": 1}
        body = client.get("/api/v1/compare", params=params).json()
        assert (
            body["shared_count"] == 1
            and body["left_only_count"] == 2
            and body["right_only_count"] == 1
        )
        assert body["union_count"] == 4 and body["overlap_percent"] == 25
        assert body["total"] == 4 and len(body["items"]) == 1
        assert body["left"]["id"] == LEFT and body["right"]["id"] == RIGHT
        for mode, expected in [
            ("shared", {"a/alpha"}),
            ("left_only", {"b/beta", NESTED}),
            ("right_only", {"c/gamma"}),
        ]:
            result = client.get(
                "/api/v1/compare", params={"left": LEFT, "right": RIGHT, "mode": mode}
            ).json()
            assert {p["id"] for p in result["items"]} == expected
        queried = client.get(
            "/api/v1/compare",
            params={"left": LEFT, "right": RIGHT, "mode": "all", "q": "gamma"},
        ).json()
        assert queried["total"] == 1 and queried["union_count"] == 4
        projects = client.get(
            "/api/v1/compare",
            params={"left": LEFT, "right": RIGHT, "kind": "project", "mode": "all"},
        ).json()
        assert projects["union_count"] == 3 and projects["left_only_count"] == 1
        assert projects["overlap_percent"] == pytest.approx(33.33)
        for left, right, status in [
            (LEFT, LEFT, 400),
            (LEFT, "unknown/list", 404),
            ("bad", RIGHT, 400),
            (ROOT_ID, RIGHT, 404),
        ]:
            assert (
                client.get(
                    "/api/v1/compare", params={"left": left, "right": right}
                ).status_code
                == status
            )


def test_nested_sources_register_from_capture_and_stay_reachable(discovery):
    _, store, _, worker, payloads = discovery
    nested = next(s for s in store.sources() if s["id"] == NESTED)
    assert nested["last_synced_at"] is None and nested["item_count"] == 0
    worker.sync()
    assert store.projects(source=NESTED)["items"][0]["id"] == "d/delta"
    worker._capture(ROOT_ID)
    assert NESTED in {s["id"] for s in store.sources()}
    payloads[LEFT] = b"## CLI\n- [Alpha](https://github.com/a/alpha)"
    worker._capture(LEFT)
    assert NESTED not in {s["id"] for s in store.sources()}
    assert store.projects(ids=["d/delta"])["total"] == 0


def test_nested_cycle_does_not_keep_orphan_sources_active(discovery):
    _, store, _, worker, payloads = discovery
    payloads[NESTED] = b"## Backlinks\n- [Tools](https://github.com/org/awesome-tools)"
    worker.sync()
    payloads[ROOT_ID] = (
        b"## Databases\n- [Databases](https://github.com/org/awesome-databases)"
    )
    worker._capture(ROOT_ID)
    assert {s["id"] for s in store.sources()} == {RIGHT}


def test_nested_reconnect_restores_real_cached_memberships_and_coverage(discovery):
    _, store, _, worker, payloads = discovery
    worker.sync()
    previous = next(s for s in store.sources() if s["id"] == NESTED)
    payloads[LEFT] = b"## CLI\n- [Beta](https://github.com/b/beta)"
    worker._capture(LEFT)
    assert NESTED not in {s["id"] for s in store.sources()}
    payloads[LEFT] = TOOLS
    worker._capture(LEFT)
    restored = next(s for s in store.sources() if s["id"] == NESTED)
    assert restored["last_synced_at"] == previous["last_synced_at"]
    assert restored["item_count"] == 1
    assert store.projects(source=NESTED)["items"][0]["id"] == "d/delta"


def test_daily_parser_preserves_provider_rank_and_deduplicates():
    records = parse_daily(daily_raw(), DAY)
    assert [p["id"] for p in records] == ["a/alpha", "ghost/repo", "z/last"]
    assert [p["rank"] for p in records] == [1, 2, 4]
    assert records[1]["name"] == "Repo"
    assert all(p["snapshot_date"] == DAY for p in records)


@pytest.mark.parametrize(
    "raw",
    [
        b"not JSON",
        b"[]",
        daily_raw("2026-10-03"),
        b'{"date":"2026-10-04","language":null,"list":[]}',
        daily_raw(repos=["https://evil.test/a/repo"]),
        b'{"date":"2026-10-04","language":"python","list":["a/repo"]}',
    ],
)
def test_malformed_daily_snapshot_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_daily(raw, DAY)


def test_daily_api_real_snapshot_date_rank_and_awesome_enrichment(discovery):
    config, store, lake, worker, _ = discovery
    worker.daily_fetch = lambda source, day: daily_raw(day)
    assert worker._capture_daily(DAY) is True
    with TestClient(
        create_app(config, store=store, lake=lake, coordinator=worker)
    ) as client:
        body = client.get("/api/v1/daily").json()
        assert body["date"] == DAY and body["source"]["id"] == DAILY_SOURCE["id"]
        assert body["total"] == 3 and [i["rank"] for i in body["items"]] == [1, 2, 4]
        assert body["items"][0]["source_count"] == 2
        ghost = body["items"][1]["project"]
        assert ghost["id"] == "ghost/repo" and ghost["source_count"] == 0
        assert (
            ghost["source_ids"] == []
            and ghost["categories"] == []
            and ghost["sections"] == []
        )
        assert ghost["stars"] is None and ghost["language"] is None
        assert ghost["discovered_at"] == body["captured_at"]
        queried = client.get("/api/v1/daily", params={"q": "ghost", "limit": 1}).json()
        assert queried["total"] == 1 and queried["items"][0]["rank"] == 2
        assert client.get("/api/v1/daily", params={"q": "%"}).json()["total"] == 0
        assert (
            client.get("/api/v1/daily", params={"offset": 1, "limit": 1}).json()[
                "items"
            ][0]["rank"]
            == 2
        )
        sources = client.get("/api/v1/daily/sources").json()["items"]
        assert sources[0]["dates"] == [DAY] and sources[0]["latest_date"] == DAY
        assert (
            client.get("/api/v1/daily", params={"source": "unknown/repo"}).status_code
            == 404
        )
        for invalid_date in ["2026-02-30", "20261004", "../x", "2026-1-01"]:
            assert (
                client.get("/api/v1/daily", params={"date": invalid_date}).status_code
                == 422
            )
    worker._capture(ROOT_ID)
    assert store.daily()["total"] == 3
    assert "ghost/repo" not in {i["id"] for i in store.projects()["items"]}


def test_daily_latest_date_is_not_last_capture_date(discovery):
    _, store, _, worker, _ = discovery
    worker.daily_fetch = lambda source, day: daily_raw(day)
    worker._capture_daily(DAY)
    captured = store.daily()["captured_at"]
    worker._capture_daily("2026-10-03")
    source = store.daily_sources()[0]
    assert source["latest_date"] == DAY
    assert source["dates"] == [DAY, "2026-10-03"]
    assert source["last_synced_at"] > captured
    assert store.daily()["captured_at"] == captured
    assert store.daily(date="2026-10-03")["date"] == "2026-10-03"


def test_explicit_ids_resolve_daily_bookmarks_without_inflating_catalog(discovery):
    config, store, lake, worker, _ = discovery
    before = store.projects()["total"]
    worker.daily_fetch = lambda source, day: daily_raw(day)
    worker._capture_daily(DAY)
    with TestClient(
        create_app(config, store=store, lake=lake, coordinator=worker)
    ) as client:
        params = {
            "ids": "a/alpha,ghost/repo,z/last",
            "kind": "all",
            "sort": "mentions",
            "limit": 1,
            "offset": 1,
        }
        body = client.get("/api/v1/projects", params=params).json()
        assert body["total"] == 3 and body["items"][0]["id"] == "ghost/repo"
        ghost = body["items"][0]
        assert (
            ghost["source_count"] == 0
            and ghost["categories"] == []
            and ghost["sections"] == []
        )
        assert ghost["source_ids"] == [] and ghost["stars"] is None
        assert ghost["updated_at"] == store.daily()["captured_at"]
        assert client.get("/api/v1/projects").json()["total"] == before
        assert (
            client.get(
                "/api/v1/projects", params={"sort": "mentions", "min_sources": 1}
            ).json()["total"]
            == before
        )
        assert (
            client.get(
                "/api/v1/projects", params={"ids": "ghost/repo", "q": "ghost"}
            ).json()["total"]
            == 1
        )
        excluded_filters: list[dict[str, str | int]] = [
            {"q": "%"},
            {"q": "alpha"},
            {"kind": "list"},
            {"source": LEFT},
            {"category": "tools"},
            {"section": "CLI"},
            {"min_sources": 1},
        ]
        for excluded in excluded_filters:
            result = client.get(
                "/api/v1/projects", params={"ids": "ghost/repo", **excluded}
            ).json()
            assert result["total"] == 0
        # An active Awesome item failing q must not be reintroduced via daily fallback.
        assert (
            client.get(
                "/api/v1/projects", params={"ids": "a/alpha", "q": "missing"}
            ).json()["total"]
            == 0
        )
        by_name = client.get(
            "/api/v1/projects",
            params={"ids": "a/alpha,ghost/repo,z/last", "sort": "name"},
        ).json()
        assert [p["id"] for p in by_name["items"]] == [
            "a/alpha",
            "z/last",
            "ghost/repo",
        ]
        recent = client.get(
            "/api/v1/projects",
            params={"ids": "a/alpha,ghost/repo,z/last", "sort": "recent"},
        ).json()
        assert [p["id"] for p in recent["items"]] == ["ghost/repo", "z/last", "a/alpha"]
    rebuilt = Store(store.path.parent / "bookmarks-rebuilt.sqlite3")
    rebuilt.rebuild(lake)
    assert (
        rebuilt.projects(ids=["ghost/repo"])["items"]
        == store.projects(ids=["ghost/repo"])["items"]
    )
    assert rebuilt.projects()["total"] == before


def test_explicit_daily_ids_use_latest_known_snapshot_of_that_repository(discovery):
    _, store, _, worker, _ = discovery
    worker.daily_fetch = lambda source, day: daily_raw(
        day, ["Ghost/OldName"] if day == "2026-10-03" else ["Other/Repo"]
    )
    worker._capture_daily("2026-10-03")
    captured = store.daily(date="2026-10-03")["captured_at"]
    worker._capture_daily(DAY)
    item = store.projects(ids=["ghost/oldname"])["items"][0]
    assert item["name"] == "OldName" and item["updated_at"] == captured


def test_daily_search_uses_same_active_awesome_enrichment_as_render(discovery):
    _, store, _, worker, payloads = discovery
    worker.daily_fetch = lambda source, day: daily_raw(day, ["b/beta"])
    worker._capture_daily(DAY)
    assert store.daily(q="Beta tool")["total"] == 1
    payloads[ROOT_ID] = (
        b"## Databases\n- [Databases](https://github.com/org/awesome-databases)"
    )
    worker._capture(ROOT_ID)
    assert store.daily(q="Beta tool")["total"] == 0
    project = store.daily()["items"][0]["project"]
    assert project["description"] == "" and project["source_count"] == 0


def test_daily_missing_malformed_and_lake_failures_retain_previous_snapshot(
    discovery, monkeypatch
):
    _, store, lake, worker, _ = discovery
    worker.daily_fetch = lambda source, day: daily_raw(day)
    worker._capture_daily(DAY)
    previous = store.daily()
    worker.daily_fetch = lambda source, day: None
    assert worker._capture_daily(DAY) is None
    assert store.daily() == previous
    worker.daily_fetch = lambda source, day: daily_raw("2026-10-03")
    assert worker._capture_daily(DAY) is False
    assert store.daily() == {**previous, "source": store.daily_sources()[0]}
    assert worker.status()["state"] == "degraded"
    worker.daily_fetch = lambda source, day: daily_raw(day, ["new/repo"])
    monkeypatch.setattr(
        lake, "commit", lambda **kwargs: (_ for _ in ()).throw(OSError("lake failed"))
    )
    assert worker._capture_daily(DAY) is False
    assert store.daily()["items"] == previous["items"]


def test_daily_cycle_is_bounded_to_last_seven_utc_dates_and_keeps_errors(discovery):
    _, store, _, worker, _ = discovery
    calls = []
    worker.today = lambda: date(2026, 10, 4)

    def fetch(source, day):
        calls.append((source, day))
        if day == DAY:
            return b"bad JSON"
        return daily_raw(day)

    worker.daily_fetch = fetch
    assert worker.sync() is False
    assert [day for _, day in calls] == [
        "2026-10-04",
        "2026-10-03",
        "2026-10-02",
        "2026-10-01",
        "2026-09-30",
        "2026-09-29",
        "2026-09-28",
    ]
    assert all(source == DAILY_SOURCE["id"] for source, _ in calls)
    assert store.daily_sources()[0]["latest_date"] == "2026-10-03"
    assert (
        store.daily_sources()[0]["error"] and DAY in store.daily_sources()[0]["error"]
    )


def test_daily_and_sections_rebuild_replays_all_roles(discovery):
    _, store, lake, worker, _ = discovery
    worker.daily_fetch = lambda source, day: daily_raw(day)
    worker._capture_daily("2026-10-03")
    worker._capture_daily(DAY)
    expected = store.projects(kind="all")
    rebuilt = Store(store.path.parent / "rebuilt.sqlite3")
    assert rebuilt.rebuild(lake) == 5
    assert rebuilt.projects(kind="all") == expected
    assert rebuilt.daily() == store.daily()
    assert rebuilt.daily(date="2026-10-03")["date"] == "2026-10-03"


def test_old_sqlite_membership_schema_migrates_without_losing_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE memberships(source_id TEXT,project_id TEXT,categories TEXT,PRIMARY KEY(source_id,project_id))"
    )
    db.execute("INSERT INTO memberships VALUES ('source/list','a/alpha','[\"tools\"]')")
    db.commit()
    db.close()
    store = Store(path)
    with store.connection() as db:
        row = db.execute("SELECT * FROM memberships").fetchone()
        assert row["project_id"] == "a/alpha" and row["sections"] == "[]"


def test_schema_one_lake_records_without_sections_replay(tmp_path, monkeypatch):
    import pyarrow as pa

    import awesome_api.lake as module
    from awesome_api.store import ROOT_SOURCE

    lake = Lake(tmp_path / "lake")
    monkeypatch.setattr(
        module,
        "SCHEMA",
        pa.schema(
            [
                (name, field.type)
                for name, field in zip(module.SCHEMA.names, module.SCHEMA)
                if name != "sections"
            ]
        ),
    )
    records = [
        {
            "id": LEFT,
            "name": "Tools",
            "owner": "org",
            "description": "",
            "url": f"https://github.com/{LEFT}",
            "kind": "list",
            "categories": ["tools"],
        }
    ]
    manifest = lake.commit(source=ROOT_SOURCE, raw=ROOT, records=records, role="root")
    metadata = json.loads(manifest.read_bytes())
    metadata["schema_version"] = 1
    manifest.write_text(json.dumps(metadata))
    store = Store(tmp_path / "catalog.sqlite3")
    assert store.rebuild(lake) == 1
    item = store.projects(kind="list")["items"][0]
    assert item["sections"] == [] and item["source_count"] == 0
