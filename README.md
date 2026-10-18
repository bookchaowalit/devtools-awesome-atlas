# Awesome Atlas

เว็บสำรวจ GitHub Awesome lists พร้อม backend แยกที่อัปเดตข้อมูลอัตโนมัติ
ดูหัวข้อและรายการย่อยของแต่ละลิสต์ เปรียบเทียบรายการซ้ำ จัดอันดับตามจำนวนลิสต์ที่แนะนำ
และอ่าน GitHub Trending รายวันจาก public archive

| ส่วน | ที่อยู่ | หน้าที่ |
|---|---|---|
| เว็บ | [apps/awesome-web](apps/awesome-web/README.md) | React, ค้นหาและเปรียบเทียบรายการ |
| API | [services/awesome-api](services/awesome-api/README.md) | FastAPI, worker และข้อมูลของแอป |

## รันบนเครื่อง

เปิด backend ก่อน:

```bash
cd services/awesome-api
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn awesome_api.app:app --host 127.0.0.1 --port 3510 --workers 1
```

เปิด terminal อีกหน้าสำหรับเว็บ:

```bash
cd apps/awesome-web
npm ci
npm run dev
```

เปิด `http://127.0.0.1:3500` เว็บส่งคำขอ API ผ่าน proxy ไปที่ port 3510
backend เริ่มดึงข้อมูลทันทีและพัก 30 นาทีหลังแต่ละรอบ ครอบคลุม Awesome lists ทีละชุด
หน้าเว็บแสดงเวลาซิงก์และความครอบคลุมจริง ข้อมูลจาก GitHub ใช้ public raw URLs โดยไม่ต้องใช้ token

## ตรวจงาน

```bash
cd apps/awesome-web
npm test
npm run build
```

```bash
cd services/awesome-api
.venv/bin/python -m pytest
```

## Railway

เว็บออนไลน์: [Awesome Atlas](https://awesome-web-production.up.railway.app)

ใช้สอง services ในโปรเจกต์ `awesome-atlas` โดย build จาก repository root:

| Service | Dockerfile | Port | Health |
|---|---|---|---|
| awesome-web | `apps/awesome-web/Dockerfile` | 3500 | `/health` |
| awesome-api | `services/awesome-api/Dockerfile` | 3510 | `/health` |

API ใช้ persistent volume ที่ `/data` และเว็บเรียก API ผ่านเครือข่ายภายใน
Railway variables ของ awesome-api มี `SENTRY_DSN` (error reporting; ไม่ตั้ง = ปิด, ดู `services/awesome-api/awesome_api/_sentry.py`)
และ `SENTRY_ENVIRONMENT`; awesome-web เป็นไฟล์ static บน Caddy ไม่มี server process ของแอปจึงไม่มี Sentry
การสร้าง domain อย่างเดียวไม่ยืนยันว่าเว็บออนไลน์: ตรวจ deployment และ HTTP ก่อนใช้งาน
รายละเอียดและสถานะจริงอยู่ใน [คู่มือ Railway](services/awesome-api/deployment/README.md)
และ [deployment record](services/awesome-api/deployment/railway-state.json)
