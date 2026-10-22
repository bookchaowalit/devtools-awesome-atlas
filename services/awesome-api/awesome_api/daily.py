"""Fixed public provider and strict parsing for daily repository rankings."""

import json
import re
from datetime import date
from typing import Any

from .parser import canonical_repository

DAILY_SOURCE = {
    "id": "antonkomarev/github-trending-archive",
    "name": "GitHub Trending Archive",
    "url": "https://github.com/antonkomarev/github-trending-archive",
    "description": "อันดับ repository รายวันจาก snapshot ใน public GitHub Trending Archive; ไม่ใช่อันดับของ Awesome",
}


def validate_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("date ต้องอยู่ในรูป YYYY-MM-DD")
    date.fromisoformat(value)
    return value


def parse_daily(raw: bytes, requested_date: str) -> list[dict]:
    validate_date(requested_date)
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("date") != requested_date:
        raise ValueError("date ใน daily snapshot ไม่ตรงกับวันที่ร้องขอ")
    if payload.get("language") is not None:
        raise ValueError("daily snapshot ต้องเป็นรายการรวมทุกภาษา (language=null)")
    repos = payload.get("list")
    if not isinstance(repos, list) or not 1 <= len(repos) <= 1000:
        raise ValueError("daily snapshot ต้องมี repository 1 ถึง 1000 รายการ")
    records: dict[str, dict[str, Any]] = {}
    for rank, repository in enumerate(repos, start=1):
        repo_id = (
            canonical_repository(f"https://github.com/{repository}")
            if isinstance(repository, str)
            else None
        )
        if repo_id is None:
            raise ValueError("daily snapshot มี repository id ที่ไม่ถูกต้อง")
        if repo_id in records:
            continue
        _owner, name = repository.split("/", 1)
        name = re.split(r"[?#]", name.rstrip("/"))[0]
        if name.lower().endswith(".git"):
            name = name[:-4]
        records[repo_id] = {
            "id": repo_id,
            "name": name,
            "owner": repo_id.split("/")[0],
            "description": "",
            "url": f"https://github.com/{repo_id}",
            "kind": "project",
            "categories": [],
            "sections": [],
            "rank": rank,
            "snapshot_date": requested_date,
        }
    return list(records.values())
