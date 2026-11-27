# 01 — Business process (As-Is / To-Be)

Awesome Atlas คือเว็บสาธารณะ (ไม่มี login) ให้นักพัฒนาค้นหา เปรียบเทียบ และจัดอันดับโปรเจกต์จาก GitHub Awesome lists และอ่าน GitHub Trending รายวัน โดย `awesome-api` ซิงก์ README สาธารณะทุก 30 นาทีเข้า lake ของตัวเอง แล้ว `awesome-web` อ่านผ่าน proxy

Last reviewed: 2026-11-28 · commit `4ac71a0`

## As-Is (จากโค้ดวันนี้)

```mermaid
flowchart TD
  START["awesome-api process start (Railway, 1 replica, volume /data)"] --> WORKER["in-process sync worker: run now, then sleep 1800 s"]
  WORKER --> ROOT["fetch root catalog sindresorhus/awesome (raw.githubusercontent.com only, no token)"]
  ROOT -->|ok: same commit path as lists| LAND
  ROOT -.->|fetch fails: error recorded, round continues with sources already in SQLite| PICK
  SQLITE -->|sources exist only after root capture is in SQLite| PICK["pick up to 16 least-recently-tried active sources"]
  PICK --> FETCH["fetch list README (HTTPS, 12 s timeout, max 4 MiB)"]
  WORKER --> DAILY["fetch up to 7 daily snapshots from github-trending-archive"]
  FETCH --> PARSE{"parser finds GitHub repo links?"}
  PARSE -->|no or fetch error| KEEP["keep last good snapshot, status degraded"]
  PARSE -->|yes| LAND["immutable landing readme.md"]
  DAILY --> DCHK{"daily payload?"}
  DCHK -->|404 day| DSKIP["skip, keep old snapshot"]
  DCHK -->|empty, too big or bad| DERR["daily error recorded"]
  DCHK -->|valid| DLAND["landing snapshot.json"]
  DLAND --> BRONZE
  LAND --> BRONZE["Bronze Parquet + committed manifest (SHA-256)"]
  BRONZE --> SQLITE["SQLite projection catalog.sqlite3"]
  SQLITE -->|nested kind=list rows, same transaction as the list capture| REGSRC["register new source for next round"]
  SQLITE --> API["awesome.v1 API: projects, sources, categories, compare, daily, status"]
  API --> WEB["awesome-web (React + Caddy) proxies /api/v1/*"]
  WEB --> USER["developer: search, compare lists, mention ranking, daily archive"]
  USER --> SAVED["saved projects in browser localStorage only"]
  CLI["CLI rebuild: replay committed manifests, no network (stop the writer first)"] -.-> SQLITE
```

Sources: `services/awesome-api/awesome_api/sync.py`, `services/awesome-api/awesome_api/app.py`, `services/awesome-api/awesome_api/lake.py`, `services/awesome-api/awesome_api/parser.py`, `services/awesome-api/awesome_api/cli.py`, `services/awesome-api/README.md`, `services/awesome-api/deployment/README.md`, `apps/awesome-web/src/App.tsx`, `PRODUCT.md`

ข้อเท็จจริงจากโค้ดและเอกสาร:

- worker อยู่ใน process ของ API ไม่มี scheduler ภายนอก; ต้องคงหนึ่ง replica เพราะไม่มี distributed lock
- ไม่มี managed backup และไม่มี retention อัตโนมัติของ lake บน volume (`services/awesome-api/deployment/README.md`)
- coverage ล่าสุดที่บันทึกไว้คือ 1,481 จาก 1,944 sources, `state: degraded` (`PRODUCT.md`, 2026-10-22) [ต้องยืนยัน] ค่าปัจจุบันบน production

## To-Be (`apps/awesome-web/DOCPACK/01-prd.md` §Roadmap/phase, `services/awesome-api/docs/source-inventory.md` §Risk and action)

```mermaid
flowchart TD
  WORKER["sync worker"] --> LAKE["lake + SQLite on volume"]
  LAKE --> BACKUP["backup + retention policy for captures"]
  WORKER --> COVER["coverage expanded across discovered lists"]
  LAKE --> CAP["capability awesome-discovery extracted"]
  CAP --> API["awesome.v1 API"]
  API --> WEB["awesome-web"]
```

Sources: `apps/awesome-web/DOCPACK/01-prd.md` (phase ถัดไป ยังเป็น [ต้องยืนยัน] ในเอกสาร), `services/awesome-api/docs/source-inventory.md`, `services/awesome-api/docs/data-product-charter.md`

## Gap

| ID | As-Is → To-Be | Status | Owner | Evidence |
|---|---|---|---|---|
| G1 | lake/SQLite บน Railway volume ไม่มี managed backup → มี backup ของ capture | todo | owner | `services/awesome-api/docs/source-inventory.md` Risk row "ทุกแหล่ง" (open); `deployment/README.md` §ข้อมูลและการกู้คืน |
| G2 | เก็บทุก committed capture ไม่มีการลบ → กำหนด retention และติดตาม volume usage | todo | claude | `services/awesome-api/docs/data-product-charter.md`; `source-inventory.md` |
| G3 | coverage บางส่วน (state degraded) → ขยาย coverage ลิสต์ โดยยังไม่มีเป้าตัวเลข | decide | owner | `apps/awesome-web/DOCPACK/01-prd.md` §ตัวชี้วัด และ §Roadmap |
| G4 | API อยู่ใน repo นี้ → แยกเป็น capability `awesome-discovery` | decide | owner | `apps/awesome-web/DOCPACK/01-prd.md` Extraction `pending` |
| G5 | worker หนึ่ง process ไม่มี lock → ถ้าจะขยาย replica ต้องแยก worker และเพิ่ม distributed lock | decide | owner | `services/awesome-api/README.md` §การอัปเดต |

## Open questions

- `apps/awesome-web/PRODUCT.md` ยังเขียนว่า "ไม่อ้างว่ามี hosted deployment" ขัดกับ README และ `PRODUCT.md` ที่ root (R-03 ใน PRD)
- จะวัดผู้ใช้หรือไม่ (ไม่มี analytics ในโค้ด) และเปิดสาธารณะหรือเก็บเป็น portfolio (`DOCPACK/00-product-card.md` §8)
- web เป็นส่วนหนึ่งของระบบเดียวกันในไฟล์นี้ ไม่ได้แยก `apps/awesome-web/docs/diagrams/01-business-process.md`
