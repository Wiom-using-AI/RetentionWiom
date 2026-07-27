"""
Wiom Retention Campaign Dashboard
Run: python app.py → http://localhost:5000
"""

from flask import Flask, request, jsonify, render_template_string, Response
import requests as req
import csv, io, os, json, sys, threading, logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)
from datetime import datetime, timedelta, date
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# Windows console default (cp1252) can't encode emoji in our log prints — force UTF-8
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass
try:
    import psycopg2, psycopg2.extras
    _PG_AVAILABLE = True
except ImportError:
    _PG_AVAILABLE = False
from dotenv import load_dotenv

# Hindi month names for natural date reading by TTS
HINDI_MONTHS = {
    1:"January", 2:"February", 3:"March", 4:"April",
    5:"May", 6:"June", 7:"July", 8:"August",
    9:"September", 10:"October", 11:"November", 12:"December"
}
# Devanagari day names for expiry date (e.g. "तेरह June")
HINDI_DAYS = {
    1:"पहली", 2:"दो", 3:"तीन", 4:"चार", 5:"पाँच",
    6:"छह", 7:"सात", 8:"आठ", 9:"नौ", 10:"दस",
    11:"ग्यारह", 12:"बारह", 13:"तेरह", 14:"चौदह", 15:"पंद्रह",
    16:"सोलह", 17:"सत्रह", 18:"अठारह", 19:"उन्नीस", 20:"बीस",
    21:"इक्कीस", 22:"बाईस", 23:"तेईस", 24:"चौबीस", 25:"पच्चीस",
    26:"छब्बीस", 27:"सत्ताईस", 28:"अट्ठाईस", 29:"उनतीस", 30:"तीस", 31:"इकतीस"
}
# Devanagari number words for days_remaining (e.g. "उनतीस")
HINDI_NUMBERS = {
    0:"शून्य", 1:"एक", 2:"दो", 3:"तीन", 4:"चार", 5:"पाँच",
    6:"छह", 7:"सात", 8:"आठ", 9:"नौ", 10:"दस",
    11:"ग्यारह", 12:"बारह", 13:"तेरह", 14:"चौदह", 15:"पंद्रह",
    16:"सोलह", 17:"सत्रह", 18:"अठारह", 19:"उन्नीस", 20:"बीस",
    21:"इक्कीस", 22:"बाईस", 23:"तेईस", 24:"चौबीस", 25:"पच्चीस",
    26:"छब्बीस", 27:"सत्ताईस", 28:"अट्ठाईस", 29:"उनतीस", 30:"तीस",
}

def format_days_remaining(days_str):
    """Convert '29' → 'उनतीस' so TTS pronounces correctly in Hindi."""
    try:
        n = int(str(days_str).strip())
        return HINDI_NUMBERS.get(n, str(n))
    except Exception:
        return str(days_str)

def format_expiry_date(date_str):
    """Convert 2025-06-13 or 02-07-2026 → 'तेरह June' for natural Hindi TTS reading."""
    if not date_str:
        return "recently"
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(str(date_str).strip(), fmt)
            day_hindi = HINDI_DAYS.get(dt.day, str(dt.day))
            month_name = HINDI_MONTHS.get(dt.month, "")
            return f"{day_hindi} {month_name}"
        except Exception:
            continue
    return date_str

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False  # Allow Devanagari/Unicode in JSON responses
AGENT_ID = os.getenv("BOLNA_AGENT_ID") or "cf801aa5-ae92-4fc4-9345-5ac2ab9a3c7f"
FROM_NUM = os.getenv("FROM_PHONE_NUMBER", "")
BASE_URL = "https://api.bolna.ai"

# ── Cohort Renewal Sheet (Google Sheets, shared "Anyone with link — Viewer") ───
COHORT_SHEET_ID  = os.getenv("COHORT_SHEET_ID", "1ufKcLgiFKTn6Za524njMQZF9U7672_zLYHCixPAAQsA")
COHORT_SHEET_GID = os.getenv("COHORT_SHEET_GID", "475175495")
COHORT_CACHE_TTL = 300  # seconds
_cohort_cache = {}  # period -> {"data": ..., "ts": ...}

# API key — loaded from env, can also be set via /api/set-apikey endpoint
_api_key_override = None

def get_api_key():
    return _api_key_override or os.getenv("BOLNA_API_KEY") or ""

def get_headers():
    return {"Authorization": f"Bearer {get_api_key()}", "Content-Type": "application/json"}

# ── Persistent storage: PostgreSQL if available, else local JSON ──────────────
DATABASE_URL  = os.getenv("DATABASE_URL", "")
_DATA_DIR     = "/data" if os.path.isdir("/data") else _SCRIPT_DIR
LOG_FILE      = os.path.join(_DATA_DIR, "call_log.json")
CALLBACK_FILE = os.path.join(_DATA_DIR, "callback_log.json")

def _get_db():
    """Return a psycopg2 connection or None if not available."""
    if not DATABASE_URL or not _PG_AVAILABLE:
        return None
    try:
        return psycopg2.connect(DATABASE_URL, sslmode="require")
    except Exception:
        return None

def _init_db():
    conn = _get_db()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS call_log (
                    id SERIAL PRIMARY KEY,
                    data JSONB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS callback_log (
                    id SERIAL PRIMARY KEY,
                    data JSONB NOT NULL
                );
            """)
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def load_json(path, table=None):
    """Load from PostgreSQL if available, else from local JSON file."""
    if DATABASE_URL and table:
        conn = _get_db()
        if conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(f"SELECT data FROM {table} ORDER BY id")
                    rows = cur.fetchall()
                    return [r["data"] for r in rows]
            except Exception:
                pass
            finally:
                conn.close()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_json(path, data, table=None):
    """Save to PostgreSQL if available, else to local JSON file."""
    if DATABASE_URL and table:
        conn = _get_db()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table}")
                    for item in data:
                        cur.execute(f"INSERT INTO {table} (data) VALUES (%s)",
                                    (json.dumps(item, ensure_ascii=False),))
                conn.commit()
                return
            except Exception:
                pass
            finally:
                conn.close()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

_init_db()
call_log     = load_json(LOG_FILE,      table="call_log")
callback_log = load_json(CALLBACK_FILE, table="callback_log")

# ── Daily automation scheduler ────────────────────────────────────────────────
from automation import run_daily_campaign, update_call_record, get_today_calls

IST = pytz.timezone("Asia/Kolkata")

def _job_redial():
    log.info("Scheduler: Redial starting")
    result = run_daily_campaign(is_redial=True)
    log.info(f"Scheduler: Redial done — {result}")

def _monitor_and_redial():
    """Poll every 5 min until 90% of round-1 calls resolved, then auto-redial."""
    from automation import daily_batches
    date_str = date.today().strftime("%Y-%m-%d")
    max_wait = 240 * 60   # 4 hour safety cap
    waited   = 0
    interval = 300        # check every 5 minutes
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        batch  = daily_batches.get(date_str, {})
        calls  = batch.get("calls", [])
        if not calls:
            continue
        resolved = sum(1 for c in calls if c.get("status") not in {"queued","error",""})
        pct = resolved / len(calls) * 100
        log.info(f"Batch monitor: {resolved}/{len(calls)} resolved ({pct:.0f}%)")
        if pct >= 90:
            log.info("90% resolved — triggering redial")
            _job_redial()
            return
    log.info("Max wait reached — triggering redial anyway")
    _job_redial()

def _job_round1():
    log.info("Scheduler: Round 1 starting")
    result = run_daily_campaign()
    log.info(f"Scheduler: Round 1 done — {result}")
    threading.Thread(target=_monitor_and_redial, daemon=True).start()

scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(_job_round1, "cron", hour=12, minute=0, id="round1_daily")
scheduler.start()
log.info("Scheduler started — daily calls at 10:00 AM IST")

DISPOSITIONS = [
    "Pending",
    "Will Recharge Today",
    "Will Recharge Later",
    "Already Recharged",
    "Out of Town",
    "Wants Device Return",
    "Device Already Returned",
    "Don't Want – Service Issue",
    "Don't Want – Personal Reason",
    "Callback Scheduled",
    "Not Answered / Busy",
    "Wrong Number",
]

# ─────────────────────────────────────────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Wiom Retention Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'Segoe UI', sans-serif; background: #f0f4ff; color: #1e293b; font-size: 14px; }

/* Header */
.header {
  background: linear-gradient(135deg, #1e3a8a 0%, #2563eb 100%);
  color: #fff; padding: 0 28px;
  display: flex; align-items: center; justify-content: space-between;
  height: 56px; box-shadow: 0 2px 8px rgba(0,0,0,.2);
}
.header h1 { font-size: 18px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
.header-right { font-size: 12px; opacity: .8; }

/* Layout */
.main { height: calc(100vh - 56px); overflow-y: auto; }
.content { padding: 24px; max-width: 1200px; margin: 0 auto; }

/* Summary cards */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
.stat-card {
  background: #fff; border-radius: 10px; padding: 14px 16px;
  box-shadow: 0 1px 4px rgba(0,0,0,.07); text-align: center;
}
.stat-card .num { font-size: 26px; font-weight: 800; color: #1e3a8a; }
.stat-card .lbl { font-size: 11px; color: #64748b; margin-top: 2px; }
.stat-card .sub { font-size: 10px; color: #94a3b8; margin-top: 2px; }
.stat-card.green .num { color: #059669; }
.stat-card.orange .num { color: #d97706; }
.stat-card.purple .num { color: #7c3aed; }

/* Section card */
.card { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 6px rgba(0,0,0,.07); margin-bottom: 20px; }
.card-title { font-size: 15px; font-weight: 700; color: #1e3a8a; margin-bottom: 18px; display: flex; align-items: center; gap: 8px; }

/* Period tabs */
.tabs { display: flex; gap: 8px; margin-bottom: 20px; }
.tab-item {
  padding: 10px 20px; border-radius: 10px; font-size: 13px; font-weight: 700;
  color: #475569; background: #fff; cursor: pointer; box-shadow: 0 1px 4px rgba(0,0,0,.07);
  transition: .15s;
}
.tab-item:hover { color: #1e3a8a; }
.tab-item.active { background: #1e3a8a; color: #fff; }

/* Customer-type cards */
.type-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
@media (max-width: 900px) { .type-grid { grid-template-columns: 1fr; } }
.type-card { border: 1px solid #e2e8f0; border-radius: 10px; padding: 16px; }
.type-card-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
.type-card-title { font-weight: 700; font-size: 13px; color: #1e3a8a; }
.type-bracket-row { margin-bottom: 12px; }
.type-bracket-row:last-child { margin-bottom: 0; }
.type-bracket-lbl { display: flex; justify-content: space-between; font-size: 11px; color: #475569; margin-bottom: 4px; }
.stack-bar { display: flex; height: 10px; border-radius: 5px; overflow: hidden; background: #f1f5f9; }
.stack-seg { height: 100%; }
.type-bracket-legend { font-size: 10px; color: #94a3b8; margin-top: 3px; }
.pp-badge { font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 20px; background: #dcfce7; color: #166534; }
.pp-badge.neg { background: #fee2e2; color: #991b1b; }
.rate-bar-row { margin-bottom: 10px; }
.rate-bar-lbl { display: flex; justify-content: space-between; font-size: 11px; color: #475569; margin-bottom: 3px; }
.rate-bar-track { height: 8px; border-radius: 4px; background: #f1f5f9; }
.rate-bar-fill { height: 100%; border-radius: 4px; }
.rate-bar-frac { font-size: 10px; color: #94a3b8; margin-top: 2px; }

/* Buttons */
.btn { padding: 10px 20px; border: none; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; transition: .2s; display: inline-flex; align-items: center; gap: 7px; }
.btn-sm { padding: 5px 12px; font-size: 11px; }
.btn-outline { background: #fff; border: 1.5px solid #e2e8f0; color: #475569; }
.btn-outline:hover { border-color: #94a3b8; }

/* Table */
.tbl-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
thead th { background: #f8faff; padding: 10px 12px; text-align: left; font-weight: 700; color: #475569; white-space: nowrap; border-bottom: 2px solid #e2e8f0; }
tbody td { padding: 10px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
tbody tr:hover td { background: #f8faff; }

.empty-state { text-align: center; padding: 48px 24px; color: #94a3b8; }
.empty-state .big { font-size: 40px; margin-bottom: 8px; }

/* Responsive stats */
@media (max-width: 900px) {
  .stats { grid-template-columns: repeat(3, 1fr); }
}

/* Main view navigation bar */
.main-nav-bar {
  background: #fff; border-bottom: 2px solid #e2e8f0;
  display: flex; gap: 0; padding: 0 28px;
}
.mv-btn {
  background: none; border: none; padding: 14px 22px;
  font-size: 13px; font-weight: 700; color: #64748b;
  cursor: pointer; border-bottom: 3px solid transparent;
  margin-bottom: -2px; transition: .15s;
}
.mv-btn:hover { color: #1e3a8a; }
.mv-btn.active { color: #1e3a8a; border-bottom-color: #1e3a8a; }

/* R11 campaign specific */
.r11-meta { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 10px; padding: 16px 20px; margin-bottom: 20px; display: flex; gap: 32px; align-items: center; flex-wrap: wrap; }
.r11-meta-item { text-align: center; }
.r11-meta-item .val { font-size: 22px; font-weight: 800; color: #1e3a8a; }
.r11-meta-item .lbl { font-size: 11px; color: #64748b; margin-top: 2px; }
.disp-badge { display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 700; background: #f1f5f9; color: #475569; }
.disp-badge.green { background: #dcfce7; color: #166534; }
.disp-badge.amber { background: #fef3c7; color: #92400e; }
.disp-badge.red   { background: #fee2e2; color: #991b1b; }
.disp-badge.blue  { background: #dbeafe; color: #1e40af; }
.sched-status { display: inline-flex; align-items: center; gap: 6px; background: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 8px 14px; font-size: 12px; color: #166534; font-weight: 600; }
.sched-dot { width: 8px; height: 8px; border-radius: 50%; background: #22c55e; animation: pulse 1.5s infinite; }
@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <h1>📊 Wiom Retention Dashboard</h1>
  <div class="header-right" id="hTime"></div>
</div>

<!-- Main Navigation -->
<div class="main-nav-bar">
  <button class="mv-btn active" id="mv-report" onclick="switchView('report')">📊 Renewal Report</button>
  <button class="mv-btn" id="mv-r11" onclick="switchView('r11')">📞 R11 Campaign</button>
  <button class="mv-btn" id="mv-dispositions" onclick="switchView('dispositions')">📋 Retention Dispositions</button>
</div>

<div class="main">

<!-- ══════════════════════════════════════════════════════ VIEW: RENEWAL REPORT -->
<div id="view-report">
  <div class="content">

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <div style="font-size:12px;color:#64748b">
          Live report — sourced directly from the <a href="https://docs.google.com/spreadsheets/d/1ufKcLgiFKTn6Za524njMQZF9U7672_zLYHCixPAAQsA/edit?gid=475175495#gid=475175495" target="_blank" style="color:#2563eb">Cohort Google Sheet</a>.
          Cohort = how the customer was reached before their plan expired (<b>Call</b> = agent call, <b>AI Call</b> = AI voice call, <b>No Call</b> = no outreach).
        </div>
        <div style="display:flex;gap:8px;align-items:center;white-space:nowrap">
          <span style="font-size:11px;color:#94a3b8" id="cohortMeta"></span>
          <button class="btn btn-sm btn-outline" onclick="loadReport(true)">🔄 Refresh from Sheet</button>
        </div>
      </div>
    </div>

    <!-- Period Tabs -->
    <div class="tabs">
      <div class="tab-item active" onclick="switchPeriod('till21')" id="tab-till21">📆 Till 21st June (No AI Call)</div>
      <div class="tab-item" onclick="switchPeriod('after21')" id="tab-after21">🤖 After 21st June (AI Call)</div>
      <div class="tab-item" onclick="switchPeriod('fullaijuly')" id="tab-fullaijuly">🚀 100% AI from 4th July</div>
    </div>

    <!-- Summary -->
    <div class="stats">
      <div class="stat-card"><div class="num" id="sumTotal">0</div><div class="lbl">Total Customers</div></div>
      <div class="stat-card green"><div class="num" id="sumRate">0%</div><div class="lbl">Overall Renewal Rate</div><div class="sub" id="sumRateFrac"></div></div>
      <div class="stat-card" id="sumCallCard"><div class="num" id="sumCall">0%</div><div class="lbl">Call Renewal Rate</div><div class="sub" id="sumCallFrac"></div></div>
      <div class="stat-card purple" id="sumAiCard"><div class="num" id="sumAi">0%</div><div class="lbl">AI Call Renewal Rate</div><div class="sub" id="sumAiFrac"></div></div>
      <div class="stat-card orange" id="sumNoCallCard" style="display:none"><div class="num" id="sumNoCall">0%</div><div class="lbl">No Call Renewal Rate</div><div class="sub" id="sumNoCallFrac"></div></div>
      <div class="stat-card green" id="sumR15Card" style="display:none"><div class="num" id="sumR15">0%</div><div class="lbl">Retention till R15 (Day 4)</div><div class="sub" id="sumR15Frac"></div></div>
      <div class="stat-card purple" id="sumR11Card" style="display:none"><div class="num" id="sumR11">0%</div><div class="lbl">Retention till R11 (Day 0)</div><div class="sub" id="sumR11Frac"></div></div>
    </div>

    <div id="reportEmpty" class="empty-state" style="display:none">
      <div class="big">📈</div>Sheet load nahi ho payi — <span id="reportErr"></span>
    </div>

    <div id="reportBody" style="display:none">

      <!-- ══════ DATE-WISE ══════ -->
      <div class="card">
        <div class="card-title">📅 Renewal Comparison — Date-wise</div>
        <div style="position:relative;height:340px">
          <canvas id="dateChart"></canvas>
        </div>
        <div class="tbl-wrap" style="margin-top:16px">
          <table>
            <thead id="dateTblHead"></thead>
            <tbody id="dateTblBody"></tbody>
          </table>
        </div>
      </div>


      <!-- ══════ PLAN OPTED BY RENEWED CUSTOMERS ══════ -->
      <div class="card">
        <div class="card-title">💳 Plan Opted by Renewed Customers</div>
        <div style="font-size:12px;color:#64748b;margin:-10px 0 16px">Cohort breakdown per plan bracket, among renewed customers only.</div>
        <div class="tbl-wrap">
          <table>
            <thead id="planTblHead"></thead>
            <tbody id="planTblBody"></tbody>
          </table>
        </div>
      </div>

      <!-- ══════ CUSTOMER TYPE — PLAN BREAKDOWN ══════ -->
      <div class="card">
        <div class="card-title">🗂️ Customer Type — Plan Breakdown (Renewed)</div>
        <div id="typeBreakdownGrid" class="type-grid"></div>
      </div>

      <!-- ══════ CUSTOMER TYPE — CALL VS NO-CALL RENEWAL RATE ══════ -->
      <div class="card">
        <div class="card-title">📈 Customer Type — Renewal Rate by Cohort</div>
        <div id="typeRateGrid" class="type-grid"></div>
      </div>

      <!-- ══════ RENEWAL DAY DISTRIBUTION ══════ -->
      <div class="card">
        <div class="card-title">📆 Renewal Day — When Customers Recharge After AI Call</div>
        <div id="renewalDayNoDate" style="display:none;background:#fefce8;border:1px solid #fde68a;border-radius:8px;padding:12px 16px;font-size:12px;color:#92400e;margin-bottom:16px">
          ⚠️ <b>RENEWAL_DATE column not found in Google Sheet.</b> Add a <code>RENEWAL_DATE</code> column (date when customer recharged) to show this chart.
        </div>
        <div id="renewalDayHint" style="font-size:12px;color:#64748b;margin:-10px 0 16px">
          % of customers (out of total called) who recharged on each day after the AI call. Switch between "% of called" and "% of renewed".
        </div>
        <div style="display:flex;gap:8px;margin-bottom:16px;align-items:center">
          <span style="font-size:12px;color:#475569;font-weight:600">Show as:</span>
          <button class="btn btn-sm" id="rdBtnCalled" onclick="switchRdMode('called')" style="background:#1e3a8a;color:#fff">% of Called</button>
          <button class="btn btn-sm btn-outline" id="rdBtnRenewed" onclick="switchRdMode('renewed')">% of Renewed</button>
        </div>
        <div style="position:relative;height:300px">
          <canvas id="renewalDayChart"></canvas>
        </div>
        <div class="tbl-wrap" style="margin-top:16px">
          <table>
            <thead id="rdTblHead"></thead>
            <tbody id="rdTblBody"></tbody>
          </table>
        </div>
      </div>

    </div>

  </div><!-- /content -->
</div><!-- /view-report -->

<!-- ══════════════════════════════════════════════════════ VIEW: R11 CAMPAIGN -->
<div id="view-r11" style="display:none">
  <div class="content">

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:12px">
        <div>
          <div style="font-size:15px;font-weight:700;color:#1e3a8a">📞 R11 Auto-Campaign</div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">Customers whose plan expired 11 days ago — auto-called at 12:00 PM IST daily</div>
        </div>
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
          <div class="sched-status"><div class="sched-dot"></div><span id="r11NextRun">Loading…</span></div>
          <button class="btn btn-sm" style="background:#1e3a8a;color:#fff" onclick="triggerR11Now()">▶ Run Now</button>
          <button class="btn btn-sm btn-outline" onclick="loadR11Campaign(true)">🔄 Refresh</button>
        </div>
      </div>
    </div>

    <div class="r11-meta" id="r11Meta">
      <div class="r11-meta-item"><div class="val" id="r11Date">—</div><div class="lbl">R11 Date (Today −11)</div></div>
      <div class="r11-meta-item"><div class="val" id="r11MetaCount">—</div><div class="lbl">Customers in Metabase</div></div>
      <div class="r11-meta-item"><div class="val" id="r11CallsMade">—</div><div class="lbl">Calls Triggered Today</div></div>
      <div class="r11-meta-item"><div class="val" id="r11Answered">—</div><div class="lbl">Answered</div></div>
      <div class="r11-meta-item"><div class="val" id="r11Pending">—</div><div class="lbl">Pending / No Answer</div></div>
    </div>

    <!-- Disposition breakdown -->
    <div class="stats" id="r11DispCards"></div>

    <!-- Call table -->
    <div class="card">
      <div class="card-title">📋 Today's Calls</div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>Name</th><th>Phone</th><th>Expiry</th><th>Status</th><th>Disposition</th><th>VoC</th><th>Time</th></tr></thead>
          <tbody id="r11CallTbl"></tbody>
        </table>
      </div>
    </div>

  </div>
</div><!-- /view-r11 -->

<!-- ══════════════════════════════════════════════════════ VIEW: DISPOSITIONS -->
<div id="view-dispositions" style="display:none">
  <div class="content">

    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px">
        <div>
          <div style="font-size:15px;font-weight:700;color:#1e3a8a">📋 Retention Campaign Dispositions</div>
          <div style="font-size:12px;color:#64748b;margin-top:4px">All call dispositions with renewal status from Metabase</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="dispDays" onchange="loadDispositions()" class="btn btn-sm btn-outline" style="cursor:pointer">
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
            <option value="30" selected>Last 30 days</option>
            <option value="60">Last 60 days</option>
          </select>
          <button class="btn btn-sm btn-outline" onclick="loadDispositions(true)">🔄 Refresh</button>
        </div>
      </div>
    </div>

    <!-- Summary stats -->
    <div class="stats">
      <div class="stat-card"><div class="num" id="dTotalCalled">—</div><div class="lbl">Total Called</div></div>
      <div class="stat-card green"><div class="num" id="dRenewalRate">—</div><div class="lbl">Renewal Rate</div><div class="sub" id="dRenewalFrac"></div></div>
      <div class="stat-card orange"><div class="num" id="dAnswerRate">—</div><div class="lbl">Answer Rate</div><div class="sub" id="dAnswerFrac"></div></div>
      <div class="stat-card purple"><div class="num" id="dSameDayRate">—</div><div class="lbl">Same-Day Renewal</div><div class="sub" id="dSameDayFrac"></div></div>
    </div>

    <div id="dispEmpty" class="empty-state" style="display:none">
      <div class="big">📋</div><span id="dispErr">No data</span>
    </div>

    <div id="dispBody">
      <!-- Disposition breakdown -->
      <div class="card">
        <div class="card-title">📊 Disposition Breakdown</div>
        <div style="position:relative;height:300px"><canvas id="dispChart"></canvas></div>
      </div>

      <!-- Date-wise renewal from Metabase -->
      <div class="card">
        <div class="card-title">📅 Date-wise: R11 Cohort Renewal (from Metabase)</div>
        <div style="font-size:12px;color:#64748b;margin:-10px 0 16px">For each expiry date batch, how many customers recharged after being called.</div>
        <div style="position:relative;height:280px"><canvas id="dispRenewalChart"></canvas></div>
        <div class="tbl-wrap" style="margin-top:16px">
          <table>
            <thead><tr><th>Expiry Date</th><th>Not Yet Renewed</th></tr></thead>
            <tbody id="dispRenewalTbl"></tbody>
          </table>
        </div>
      </div>

      <!-- Disposition table -->
      <div class="card">
        <div class="card-title">📋 Disposition Details</div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>Disposition</th><th>Count</th><th>%</th><th>Renewed After Call</th></tr></thead>
            <tbody id="dispTbl"></tbody>
          </table>
        </div>
      </div>
    </div>

  </div>
</div><!-- /view-dispositions -->

</div><!-- /main -->

<script>
// ─── Clock ────────────────────────────────────────────────────────────────────
function tick() {
  document.getElementById('hTime').textContent =
    new Date().toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
setInterval(tick, 1000); tick();

// ─── Report Dashboard (backed entirely by the Google Sheet) ──────────────────
const COHORT_COLORS = { 'Call': '#2563eb', 'AI Call': '#7c3aed', 'No Call': '#f97316' };
let dateChartObj = null;
let currentPeriod = 'till21';

function switchPeriod(period) {
  currentPeriod = period;
  ['till21','after21','fullaijuly'].forEach(p => document.getElementById('tab-'+p).classList.toggle('active', p===period));
  loadReport();
}

async function loadReport(forceRefresh=false) {
  document.getElementById('reportEmpty').style.display = 'none';
  try {
    const params = new URLSearchParams({ period: currentPeriod });
    if (forceRefresh) params.set('refresh', '1');
    const res = await fetch('/api/cohort-data?' + params.toString());
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    document.getElementById('reportBody').style.display = 'block';
    document.getElementById('cohortMeta').textContent = data.row_count + ' rows synced';

    renderSummary(data.summary);
    renderDateChart(data, currentPeriod);
    renderDateTable(data, currentPeriod);
    renderPlanTable(data.cohorts, data.plan_breakdown);
    renderTypeBreakdown(data.cohorts, data.type_breakdown);
    renderTypeRate(data.cohorts, data.type_cohort_rate);
    loadRenewalDay(forceRefresh);
  } catch (e) {
    document.getElementById('reportBody').style.display = 'none';
    document.getElementById('reportEmpty').style.display = 'block';
    document.getElementById('reportErr').textContent = e.message;
  }
}

function renderSummary(s) {
  document.getElementById('sumTotal').textContent = s.total;
  document.getElementById('sumRate').textContent = s.rate + '%';
  document.getElementById('sumRateFrac').textContent = `${s.renewed}/${s.total}`;

  const isFullAI = currentPeriod === 'fullaijuly';

  const call = s.by_cohort['Call'] ?? {rate:0, renewed:0, total:0};
  document.getElementById('sumCallCard').style.display = isFullAI ? 'none' : '';
  document.getElementById('sumCall').textContent = call.rate + '%';
  document.getElementById('sumCallFrac').textContent = `${call.renewed}/${call.total}`;

  const hasAi = !!s.by_cohort['AI Call'];
  document.getElementById('sumAiCard').style.display = (hasAi && !isFullAI) ? '' : 'none';
  if (hasAi) {
    document.getElementById('sumAi').textContent = s.by_cohort['AI Call'].rate + '%';
    document.getElementById('sumAiFrac').textContent = `${s.by_cohort['AI Call'].renewed}/${s.by_cohort['AI Call'].total}`;
  }

  // No Call tile: only on till21 tab
  const noCallData = s.by_cohort['No Call'];
  const showNoCall = currentPeriod === 'till21' && !!noCallData;
  document.getElementById('sumNoCallCard').style.display = showNoCall ? '' : 'none';
  if (showNoCall) {
    document.getElementById('sumNoCall').textContent = noCallData.rate + '%';
    document.getElementById('sumNoCallFrac').textContent = `${noCallData.renewed}/${noCallData.total}`;
  }

  // R15 / R11 cards: only on 100% AI tab
  document.getElementById('sumR15Card').style.display = isFullAI ? '' : 'none';
  document.getElementById('sumR11Card').style.display = isFullAI ? '' : 'none';
  if (isFullAI) {
    document.getElementById('sumR15').textContent = s.day4_rate + '%';
    document.getElementById('sumR15Frac').textContent = `${s.day4}/${s.r15_eligible} (crossed Day 4)`;
    document.getElementById('sumR11').textContent = s.day0_rate + '%';
    document.getElementById('sumR11Frac').textContent = `${s.day0}/${s.total} (same day)`;
  }
}

function renderDateChart(data, period) {
  const ctx = document.getElementById('dateChart').getContext('2d');
  const isFullAI = period === 'fullaijuly';
  const datasets = [];
  data.cohorts.forEach(c => {
    const color = COHORT_COLORS[c] || '#64748b';
    datasets.push({
      label: isFullAI ? 'Overall' : c,
      data: data.series[c].map(p => p.rate),
      backgroundColor: color,
    });
    if (isFullAI) {
      datasets.push({
        label: 'Till R15 (Day 4)',
        data: data.series[c].map(p => p.day4_rate !== null ? p.day4_rate : 0),
        backgroundColor: '#10b981',
      });
      datasets.push({
        label: 'Till R11 (Day 0)',
        data: data.series[c].map(p => p.day0_rate),
        backgroundColor: '#f59e0b',
      });
    }
  });
  if (dateChartObj) dateChartObj.destroy();
  dateChartObj = new Chart(ctx, {
    type: 'bar',
    data: { labels: data.dates, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top' },
        tooltip: { callbacks: { afterLabel: (item) => {
          const label = item.dataset.label;
          const idx = item.dataIndex;
          // find the cohort that owns this dataset
          let p = null;
          for (const c of data.cohorts) {
            if (data.series[c] && data.series[c][idx]) { p = data.series[c][idx]; break; }
          }
          if (!p) return '';
          if (label === 'Till R15 (Day 4)') return p.day4_rate !== null ? `${p.day4}/${p.r15_eligible} (crossed Day 4)` : '— (no data)';
          if (label === 'Till R11 (Day 0)') return p.day0 != null ? `${p.day0}/${p.total} (same day)` : '— (no data)';
          return `${p.renewed}/${p.total} renewed`;
        }}}
      },
      scales: {
        y: { beginAtZero: true, max: 100, title: { display: true, text: 'Renewal Rate (%)' } },
        x: { title: { display: true, text: isFullAI ? 'Expiry Date' : 'Plan Expiry Date (Cohort Day)' } }
      }
    }
  });
}

function renderDateTable(data, period) {
  const isFullAI = period === 'fullaijuly';
  const headers = isFullAI
    ? ['<th>Expiry Date</th><th>Call Date</th><th>Overall</th><th>Till R15 (Day 4)</th><th>Till R11 (Day 0)</th>']
    : ['<th>Date</th>' + data.cohorts.map(c => `<th>${c}</th>`).join('')];
  document.getElementById('dateTblHead').innerHTML = '<tr>' + headers.join('') + '</tr>';

  const body = document.getElementById('dateTblBody');
  const html = data.dates.map((date, i) => {
    if (isFullAI) {
      // merge all cohorts: sum totals, pick first non-empty call_date
      let merged = { rate:0, renewed:0, total:0, day0:0, day0_rate:0, day4:0, day4_rate:null, r15_eligible:0, call_date:'' };
      for (const c of data.cohorts) {
        const p = data.series[c][i];
        if (!p) continue;
        merged.renewed     += p.renewed || 0;
        merged.total       += p.total || 0;
        merged.day0        += p.day0 || 0;
        merged.day4        += p.day4 || 0;
        merged.r15_eligible+= p.r15_eligible || 0;
        if (!merged.call_date && p.call_date) merged.call_date = p.call_date;
        if (p.day4_rate !== null) merged.day4_rate = 1; // flag that day4 exists
      }
      merged.rate     = merged.total ? Math.round(merged.renewed*100/merged.total*10)/10 : 0;
      merged.day0_rate= merged.total ? Math.round(merged.day0*100/merged.total*10)/10 : 0;
      const d4 = merged.day4_rate !== null && merged.r15_eligible
        ? `${Math.round(merged.day4*100/merged.r15_eligible*10)/10}% <span style="color:#94a3b8;font-size:11px">(${merged.day4}/${merged.r15_eligible})</span>`
        : `<span style="color:#94a3b8">—</span>`;
      return `<tr>
        <td>${date}</td>
        <td style="color:#64748b">${merged.call_date || '—'}</td>
        <td>${merged.rate}% <span style="color:#94a3b8;font-size:11px">(${merged.renewed}/${merged.total})</span></td>
        <td style="color:#10b981">${d4}</td>
        <td style="color:#f59e0b">${merged.day0_rate}% <span style="color:#94a3b8;font-size:11px">(${merged.day0}/${merged.total})</span></td>
      </tr>`;
    } else {
      const cells = data.cohorts.map(c => {
        const p = data.series[c][i];
        return `<td>${p.rate}% <span style="color:#94a3b8;font-size:11px">(${p.renewed}/${p.total})</span></td>`;
      }).join('');
      return `<tr><td>${date}</td>${cells}</tr>`;
    }
  }).join('');
  const colspan = isFullAI ? 5 : 1 + data.cohorts.length;
  body.innerHTML = html || `<tr><td colspan="${colspan}"><div class="empty-state"><div class="big">📅</div>No data</div></td></tr>`;
}

// ─── Plan Opted by Renewed Customers ─────────────────────────────────────────
function renderPlanTable(cohorts, rows) {
  const head = document.getElementById('planTblHead');
  head.innerHTML = '<tr><th>Plan Amount</th>' + cohorts.map(c => `<th>${c}</th>`).join('') +
    '<th>Total</th><th>Split (' + cohorts.join(' vs ') + ')</th></tr>';

  const body = document.getElementById('planTblBody');
  body.innerHTML = rows.map(r => {
    const cells = cohorts.map(c => {
      const cd = r.by_cohort[c] || {count:0, pct:0};
      return `<td><b style="color:${COHORT_COLORS[c]||'#1e3a8a'}">${cd.count}</b> <span style="color:#94a3b8">${cd.pct}%</span></td>`;
    }).join('');
    const bar = cohorts.map(c => {
      const cd = r.by_cohort[c] || {count:0};
      const pct = r.total ? (cd.count / r.total * 100) : 0;
      return `<div class="stack-seg" style="width:${pct}%;background:${COHORT_COLORS[c]||'#64748b'}"></div>`;
    }).join('');
    return `<tr>
      <td><b>${r.bracket}</b></td>
      ${cells}
      <td>${r.total}</td>
      <td style="min-width:180px">
        <div class="stack-bar">${bar}</div>
        <div class="type-bracket-legend">${r.pct_of_total}% of renewals</div>
      </td>
    </tr>`;
  }).join('') || `<tr><td colspan="${cohorts.length+3}"><div class="empty-state"><div class="big">💳</div>No data</div></td></tr>`;
}

// ─── Customer Type — Plan Breakdown ──────────────────────────────────────────
function renderTypeBreakdown(cohorts, types) {
  const grid = document.getElementById('typeBreakdownGrid');
  if (!types.length) {
    grid.innerHTML = '<div class="empty-state"><div class="big">🗂️</div>No data</div>';
    return;
  }
  grid.innerHTML = types.map(t => {
    const rows = t.brackets.map(b => {
      const bar = cohorts.map(c => {
        const cnt = b.by_cohort[c] || 0;
        const pct = b.total ? (cnt / b.total * 100) : 0;
        return `<div class="stack-seg" style="width:${pct}%;background:${COHORT_COLORS[c]||'#64748b'}"></div>`;
      }).join('');
      const legend = cohorts.map(c => `${c}: ${b.by_cohort[c] || 0}`).join(' · ');
      return `<div class="type-bracket-row">
        <div class="type-bracket-lbl"><span>${b.bracket}</span><span>${b.total} customers</span></div>
        <div class="stack-bar">${bar}</div>
        <div class="type-bracket-legend">${legend}</div>
      </div>`;
    }).join('');
    return `<div class="type-card">
      <div class="type-card-head">
        <span class="type-card-title">${t.type}</span>
        <span style="font-size:11px;color:#64748b">${t.total_renewed} renewed</span>
      </div>
      ${rows}
    </div>`;
  }).join('');
}

// ─── Customer Type — Renewal Rate by Cohort ──────────────────────────────────
function renderTypeRate(cohorts, types) {
  const grid = document.getElementById('typeRateGrid');
  if (!types.length) {
    grid.innerHTML = '<div class="empty-state"><div class="big">📈</div>No data</div>';
    return;
  }
  grid.innerHTML = types.map(t => {
    const callRate = t.cohorts['Call']?.rate;
    const noCallRate = t.cohorts['No Call']?.rate;
    let badge = '';
    if (callRate !== undefined && noCallRate !== undefined) {
      const diff = Math.round((callRate - noCallRate) * 10) / 10;
      badge = `<span class="pp-badge ${diff<0?'neg':''}">${diff>=0?'+':''}${diff}pp</span>`;
    }
    const bars = cohorts.map(c => {
      const cd = t.cohorts[c] || {total:0, renewed:0, rate:0};
      return `<div class="rate-bar-row">
        <div class="rate-bar-lbl"><span>${c}</span><span>${cd.rate}%</span></div>
        <div class="rate-bar-track"><div class="rate-bar-fill" style="width:${cd.rate}%;background:${COHORT_COLORS[c]||'#64748b'}"></div></div>
        <div class="rate-bar-frac">${cd.renewed}/${cd.total}</div>
      </div>`;
    }).join('');
    return `<div class="type-card">
      <div class="type-card-head">
        <span class="type-card-title">${t.type}</span>
        ${badge}
      </div>
      ${bars}
    </div>`;
  }).join('');
}

// ─── Renewal Day Distribution ────────────────────────────────────────────────
let renewalDayChartObj = null;
let rdMode = 'called';   // 'called' | 'renewed'
let _rdData = null;

function switchRdMode(mode) {
  rdMode = mode;
  document.getElementById('rdBtnCalled').style.cssText  = mode==='called'  ? 'background:#1e3a8a;color:#fff' : '';
  document.getElementById('rdBtnRenewed').style.cssText = mode==='renewed' ? 'background:#1e3a8a;color:#fff' : '';
  document.getElementById('rdBtnCalled').className  = mode==='called'  ? 'btn btn-sm' : 'btn btn-sm btn-outline';
  document.getElementById('rdBtnRenewed').className = mode==='renewed' ? 'btn btn-sm' : 'btn btn-sm btn-outline';
  if (_rdData) renderRenewalDay(_rdData);
}

async function loadRenewalDay(forceRefresh=false) {
  const params = new URLSearchParams({ period: currentPeriod });
  if (forceRefresh) params.set('refresh', '1');
  try {
    const res = await fetch('/api/renewal-day-data?' + params.toString());
    _rdData = await res.json();
    if (_rdData.error) throw new Error(_rdData.error);
    document.getElementById('renewalDayNoDate').style.display = _rdData.has_renewal_date ? 'none' : '';
    renderRenewalDay(_rdData);
  } catch(e) {
    console.error('Renewal day fetch error', e);
  }
}

function renderRenewalDay(data) {
  const pctKey = rdMode === 'called' ? 'pct_of_called' : 'pct_of_renewed';
  const label  = rdMode === 'called' ? '% of Called' : '% of Renewed';
  const dayLabels = data.days.map(d => d === '8+' ? 'Day 8+' : 'Day ' + d);

  // Exclude No Call from renewal day chart — it's AI call analysis only
  const cohorts = data.cohorts.filter(c => c !== 'No Call');

  const datasets = cohorts.map(c => ({
    label: c,
    data: data.days.map(d => (data.by_cohort[c]?.days[d]?.[pctKey] ?? 0)),
    backgroundColor: (COHORT_COLORS[c] || '#64748b') + 'cc',
    borderColor: COHORT_COLORS[c] || '#64748b',
    borderWidth: 2,
    borderRadius: 4,
  }));

  const ctx = document.getElementById('renewalDayChart').getContext('2d');
  if (renewalDayChartObj) renewalDayChartObj.destroy();
  renewalDayChartObj = new Chart(ctx, {
    type: 'bar',
    data: { labels: dayLabels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top' },
        tooltip: { callbacks: {
          afterLabel: (item) => {
            const d = data.days[item.dataIndex];
            const cd = data.by_cohort[item.dataset.label];
            const cnt = cd?.days[d]?.count ?? 0;
            return `${cnt} customers`;
          }
        }}
      },
      scales: {
        y: { beginAtZero: true, max: 100,
             title: { display: true, text: label + ' (%)' } },
        x: { title: { display: true, text: 'Days after AI Call' } }
      }
    }
  });

  // Table
  document.getElementById('rdTblHead').innerHTML =
    '<tr><th>Renewal Day</th>' + cohorts.map(c =>
      `<th>${c} (${label})</th>`).join('') + '<th>Total Renewals</th></tr>';

  const body = document.getElementById('rdTblBody');
  body.innerHTML = data.days.map((d) => {
    const dayLbl = d === '8+' ? 'Day 8+' : 'Day ' + d;
    let grandTotal = 0;
    const cells = cohorts.map(c => {
      const entry = data.by_cohort[c]?.days[d] || {count:0, pct_of_called:0, pct_of_renewed:0};
      grandTotal += entry.count;
      const pct = entry[pctKey];
      const color = COHORT_COLORS[c] || '#1e3a8a';
      return `<td><b style="color:${color}">${pct}%</b> <span style="color:#94a3b8;font-size:11px">(${entry.count})</span></td>`;
    }).join('');
    return `<tr><td><b>${dayLbl}</b></td>${cells}<td>${grandTotal}</td></tr>`;
  }).join('');

  // Summary row
  const summaryRow = '<tr style="background:#f8faff;font-weight:700"><td>Total Called</td>' +
    cohorts.map(c => `<td>${data.by_cohort[c]?.total_called ?? 0} called / ${data.by_cohort[c]?.total_renewed ?? 0} renewed</td>`).join('') +
    '<td></td></tr>';
  document.getElementById('rdTblBody').innerHTML += summaryRow;
}

loadReport();  // default period = till21

// ─── Main View Switch ─────────────────────────────────────────────────────────
function switchView(view) {
  ['report','r11','dispositions'].forEach(v => {
    document.getElementById('view-'+v).style.display = v===view ? '' : 'none';
    document.getElementById('mv-'+v).classList.toggle('active', v===view);
  });
  if (view==='r11')           loadR11Campaign();
  if (view==='dispositions')  loadDispositions();
}

// ─── R11 Campaign Tab ─────────────────────────────────────────────────────────
const DISP_COLORS = {
  'Will Recharge Today':   '#22c55e',
  'Will Recharge Later':   '#84cc16',
  'Already Recharged':     '#059669',
  'Callback Scheduled':    '#3b82f6',
  'Out of Town':           '#f59e0b',
  'Not Answered / Busy':   '#94a3b8',
  'Wants Device Return':   '#f97316',
  'Device Already Returned':'#a78bfa',
  "Don't Want – Service Issue": '#ef4444',
  "Don't Want – Personal Reason": '#f43f5e',
  'Wrong Number':          '#e2e8f0',
  'Pending':               '#cbd5e1',
};

async function loadR11Campaign(forceRefresh=false) {
  try {
    const r = await fetch('/api/r11-campaign' + (forceRefresh ? '?refresh=1' : ''));
    const d = await r.json();

    document.getElementById('r11Date').textContent       = d.r11_date || '—';
    document.getElementById('r11MetaCount').textContent  = d.r11_count != null ? d.r11_count : '—';
    document.getElementById('r11CallsMade').textContent  = d.calls_made;
    document.getElementById('r11Answered').textContent   = d.answered;
    document.getElementById('r11Pending').textContent    = d.pending;
    document.getElementById('r11NextRun').textContent    = d.next_run ? 'Next: ' + d.next_run : 'Scheduled 12:00 PM IST';

    // Disposition cards
    const cards = document.getElementById('r11DispCards');
    const disp = d.disposition_counts || {};
    cards.innerHTML = Object.entries(disp).sort((a,b)=>b[1]-a[1]).map(([k,v]) => `
      <div class="stat-card">
        <div class="num" style="color:${DISP_COLORS[k]||'#1e3a8a'};font-size:22px">${v}</div>
        <div class="lbl">${k}</div>
      </div>`).join('') || '<div style="color:#94a3b8;font-size:13px;padding:12px">No calls yet today</div>';

    // Call table
    const tbl = document.getElementById('r11CallTbl');
    const calls = d.calls || [];
    if (!calls.length) {
      tbl.innerHTML = '<tr><td colspan="7"><div class="empty-state"><div class="big">📞</div>No calls today yet</div></td></tr>';
    } else {
      const dispBadgeClass = disp => {
        if (['Will Recharge Today','Already Recharged'].includes(disp)) return 'green';
        if (['Will Recharge Later','Callback Scheduled'].includes(disp)) return 'blue';
        if (["Don't Want – Service Issue","Don't Want – Personal Reason","Wants Device Return"].includes(disp)) return 'red';
        if (disp === 'Not Answered / Busy') return '';
        return 'amber';
      };
      tbl.innerHTML = [...calls].reverse().map(c => `<tr>
        <td><b>${c.name||'—'}</b></td>
        <td>${c.phone||'—'}</td>
        <td>${c.expiry||'—'}</td>
        <td>${c.status||'—'}</td>
        <td><span class="disp-badge ${dispBadgeClass(c.disposition)}">${c.disposition||'Pending'}</span></td>
        <td style="color:#64748b;font-size:11px;max-width:200px">${c.voc||''}</td>
        <td style="color:#94a3b8;font-size:11px">${c.time||''}</td>
      </tr>`).join('');
    }
  } catch(e) {
    console.error('R11 campaign load error', e);
  }
}

async function triggerR11Now() {
  if (!confirm('Trigger R11 campaign calls right now?')) return;
  const r = await fetch('/api/auto-campaign/trigger', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({})});
  const d = await r.json();
  alert(d.message || 'Campaign triggered');
  setTimeout(() => loadR11Campaign(true), 3000);
}

// ─── Retention Dispositions Tab ───────────────────────────────────────────────
let dispChartObj = null;
let dispRenewalChartObj = null;

async function loadDispositions(forceRefresh=false) {
  document.getElementById('dispEmpty').style.display = 'none';
  document.getElementById('dispBody').style.display = '';
  const days = document.getElementById('dispDays').value;
  try {
    const params = new URLSearchParams({days});
    if (forceRefresh) params.set('refresh','1');
    const r = await fetch('/api/retention-dispositions?' + params.toString());
    const d = await r.json();
    if (d.error) throw new Error(d.error);

    // Summary stats
    document.getElementById('dTotalCalled').textContent    = d.total_called;
    document.getElementById('dRenewalRate').textContent    = d.renewal_rate + '%';
    document.getElementById('dRenewalFrac').textContent    = `${d.renewed}/${d.total_called}`;
    document.getElementById('dAnswerRate').textContent     = d.answer_rate + '%';
    document.getElementById('dAnswerFrac').textContent     = `${d.answered}/${d.total_called}`;
    document.getElementById('dSameDayRate').textContent    = d.same_day_rate + '%';
    document.getElementById('dSameDayFrac').textContent    = `same-day renewals`;

    // Disposition bar chart
    const dispEntries = Object.entries(d.disposition_counts||{}).sort((a,b)=>b[1]-a[1]);
    const ctx1 = document.getElementById('dispChart').getContext('2d');
    if (dispChartObj) dispChartObj.destroy();
    dispChartObj = new Chart(ctx1, {
      type: 'bar',
      data: {
        labels: dispEntries.map(([k])=>k),
        datasets: [{
          label: 'Calls',
          data: dispEntries.map(([,v])=>v),
          backgroundColor: dispEntries.map(([k])=>DISP_COLORS[k]||'#94a3b8'),
        }]
      },
      options: {
        responsive: true, maintainAspectRatio: false, indexAxis: 'y',
        plugins: { legend: { display: false } },
        scales: { x: { beginAtZero: true } }
      }
    });

    // Disposition table
    const total = d.total_called || 1;
    document.getElementById('dispTbl').innerHTML = dispEntries.map(([k,v]) => `<tr>
      <td><span class="disp-badge" style="background:${DISP_COLORS[k]||'#f1f5f9'}20;color:${DISP_COLORS[k]||'#475569'}">${k}</span></td>
      <td><b>${v}</b></td>
      <td>${Math.round(v/total*100)}%</td>
      <td>${d.renewal_by_disp?.[k] != null ? d.renewal_by_disp[k]+'%' : '—'}</td>
    </tr>`).join('') || '<tr><td colspan="4"><div class="empty-state"><div class="big">📋</div>No data</div></td></tr>';

    // Date-wise not-yet-renewed from Metabase
    const dates = (d.metabase_renewal||[]).map(r=>r.expiry_date);
    const totals = (d.metabase_renewal||[]).map(r=>r.total);
    const ctx2 = document.getElementById('dispRenewalChart').getContext('2d');
    if (dispRenewalChartObj) dispRenewalChartObj.destroy();
    if (dates.length) {
      dispRenewalChartObj = new Chart(ctx2, {
        type: 'bar',
        data: {
          labels: dates,
          datasets: [{
            label: 'Not Yet Renewed',
            data: totals,
            backgroundColor: '#7c3aed',
            borderRadius: 4,
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { position:'top' } },
          scales: { y: { beginAtZero:true, title:{display:true,text:'Customers Not Yet Renewed'} } }
        }
      });
      document.getElementById('dispRenewalTbl').innerHTML = (d.metabase_renewal||[]).map(r => `<tr>
        <td>${r.expiry_date}</td>
        <td>${r.total}</td>
      </tr>`).join('');
    } else {
      document.getElementById('dispRenewalTbl').innerHTML = '<tr><td colspan="2" style="color:#94a3b8;text-align:center">Metabase data unavailable — check METABASE_DB_ID env var</td></tr>';
    }

  } catch(e) {
    document.getElementById('dispEmpty').style.display = '';
    document.getElementById('dispBody').style.display  = 'none';
    document.getElementById('dispErr').textContent = e.message;
  }
}
</script>
</body>
</html>
"""

# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/set-apikey", methods=["POST"])
def set_apikey():
    global _api_key_override
    data = request.json or {}
    key = data.get("key", "").strip()
    if not key:
        return jsonify({"success": False, "error": "No key provided"}), 400
    _api_key_override = key
    return jsonify({"success": True, "message": "API key set successfully"})


@app.route("/api/auto-campaign/trigger", methods=["POST"])
def manual_trigger():
    """Manually trigger today's campaign (for testing or emergencies)."""
    d = request.json or {}
    target_date = d.get("target_date") or (date.today() - timedelta(days=11)).strftime("%Y-%m-%d")
    is_redial   = d.get("redial", False)
    threading.Thread(target=run_daily_campaign, kwargs={"target_date": target_date, "is_redial": is_redial}, daemon=True).start()
    return jsonify({"success": True, "message": f"Campaign triggered for {target_date}", "redial": is_redial})

@app.route("/api/auto-campaign/today")
def today_calls():
    """Return today's auto-call records."""
    return jsonify(get_today_calls())

@app.route("/api/auto-campaign/status")
def campaign_status():
    """Show scheduler status and next run time."""
    jobs = []
    for job in scheduler.get_jobs():
        jobs.append({"id": job.id, "next_run": str(job.next_run_time)})
    return jsonify({"scheduler": "running", "jobs": jobs, "today_calls": len(get_today_calls())})

@app.route("/debug-calllog")
def debug_calllog():
    """Show full call log with all fields for debugging."""
    return jsonify(call_log[-5:])  # last 5 entries

@app.route("/debug")
def debug():
    return jsonify({
        "env_BOLNA_API_KEY_set":    bool(os.getenv("BOLNA_API_KEY")),
        "env_BOLNA_AGENT_ID_set":   bool(os.getenv("BOLNA_AGENT_ID")),
        "app_API_KEY_set":          bool(get_api_key()),
        "app_API_KEY_first4":       (get_api_key() or "")[:4] or "NOT_SET",
        "app_AGENT_ID_set":         bool(AGENT_ID),
        "app_AGENT_ID_first4":      (AGENT_ID or "")[:4] or "NOT_SET",
        "PORT":                     os.getenv("PORT", "not set"),
        "RAILWAY_ENV":              os.getenv("RAILWAY_ENVIRONMENT", "not set"),
    })


@app.route("/api/call/single", methods=["POST"])
def single_call():
    d = request.json
    raw_expiry     = d.get("expiry_date") or d.get("expiry", "")
    expiry_date    = format_expiry_date(raw_expiry)
    days_remaining = d.get("days_remaining") or d.get("days", "")

    variables = {
        "customer_name":  d["name"],
        "expiry_date":    expiry_date,
        "days_remaining": format_days_remaining(days_remaining),
        "agent_name":     d.get("agent", "Jyoti"),
    }
    webhook_url = os.getenv("WEBHOOK_URL", "https://retentionwiom-production.up.railway.app/webhook")
    payload = {
        "agent_id": AGENT_ID,
        "recipient_phone_number": d["phone"],
        "user_data": variables,
        "variables": variables,
        "webhook_url": webhook_url,
    }

    try:
        resp = req.post(f"{BASE_URL}/call", headers=get_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        result  = resp.json()
        exec_id = result.get("execution_id") or result.get("id") or ""

        call_log.append({
            "name":          d["name"],
            "phone":         d["phone"],
            "expiry":        expiry_date,
            "days":          days_remaining,
            "status":        "queued",
            "disposition":   "Pending",
            "voc":           "",
            "recording_url": "",
            "execution_id":  exec_id,
            "time":          datetime.now().strftime("%d %b %H:%M"),
        })
        save_json(LOG_FILE, call_log, table="call_log")
        return jsonify({"success": True, "execution_id": exec_id})
    except Exception as e:
        # Return full Bolna error response for debugging
        try:
            bolna_error = resp.json()
        except:
            bolna_error = {}
        return jsonify({"success": False, "error": str(e), "bolna_response": bolna_error, "payload_sent": payload}), 400


@app.route("/api/sync-bolna", methods=["POST"])
def sync_bolna():
    """Fetch today's call history from Bolna and add missing entries to call_log."""
    try:
        synced = 0
        existing_ids = {e.get("execution_id") for e in call_log}

        # Try Bolna call history endpoints
        data = []
        for ep in [
            f"{BASE_URL}/v1/logs",
            f"{BASE_URL}/call/logs",
            f"{BASE_URL}/execution/logs",
            f"{BASE_URL}/v1/calls",
        ]:
            r = req.get(ep, headers=get_headers(), params={"limit": 100}, timeout=15)
            if r.status_code == 200:
                result = r.json()
                data = result if isinstance(result, list) else result.get("data", result.get("calls", result.get("logs", [])))
                break

        for item in data:
            call_id = item.get("id") or item.get("execution_id") or item.get("call_id") or ""
            if not call_id or call_id in existing_ids:
                continue

            # Only add today's calls
            created = item.get("created_at", "") or item.get("initiated_at", "")
            if created:
                try:
                    call_date = datetime.fromisoformat(created.replace("Z", "+00:00")).strftime("%d %b")
                    today = datetime.now().strftime("%d %b")
                    if call_date != today:
                        continue
                except Exception:
                    pass

            ctx = item.get("context_details", {}) or {}
            recipient = ctx.get("recipient_data", {}) or {}
            tel = item.get("telephony_data", {}) or {}

            recording = (item.get("recording_url") or item.get("combined_audio_url") or
                         f"https://api.bolna.ai/recordings/call/{call_id}")

            new_entry = {
                "name":          recipient.get("customer_name", "") or tel.get("to_number", ""),
                "phone":         tel.get("to_number", "") or recipient.get("recipient_phone_number", ""),
                "expiry":        recipient.get("expiry_date", ""),
                "days":          recipient.get("days_remaining", ""),
                "status":        item.get("status", "completed"),
                "disposition":   "Pending",
                "voc":           "",
                "recording_url": recording,
                "execution_id":  call_id,
                "time":          datetime.now().strftime("%d %b %H:%M"),
            }
            call_log.append(new_entry)
            existing_ids.add(call_id)
            synced += 1

        save_json(LOG_FILE, call_log, table="call_log")
        return jsonify({"success": True, "synced": synced, "total": len(call_log)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/call/batch", methods=["POST"])
def batch_call():
    file = request.files.get("file")
    if not file:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    content = file.read().decode("utf-8")
    rows    = list(csv.DictReader(io.StringIO(content)))

    # Convert expiry_date to Hindi format in every row before sending to Bolna
    for row in rows:
        if row.get("expiry_date"):
            row["expiry_date"] = format_expiry_date(row["expiry_date"])

    # Rebuild CSV with converted dates
    if rows:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
        modified_csv = out.getvalue()
    else:
        modified_csv = content

    webhook_url = os.getenv("WEBHOOK_URL", "https://retentionwiom-production.up.railway.app/webhook")

    # ── Try Bolna batch API endpoints ────────────────────────────────────────
    batch_resp = None
    for batch_url in [f"{BASE_URL}/v1/batches", f"{BASE_URL}/batches", f"{BASE_URL}/batch"]:
        try:
            r = req.post(
                batch_url,
                headers={"Authorization": f"Bearer {get_api_key()}"},
                data={
                    "agent_id":           AGENT_ID,
                    "from_phone_numbers": json.dumps([]),
                    "webhook_url":        webhook_url,
                    "retry_config": json.dumps({
                        "enabled": True, "max_retries": 3,
                        "retry_on_statuses": ["no-answer", "busy"],
                        "retry_intervals_minutes": [120, 120, 120],
                    }),
                },
                files={"file": (file.filename, modified_csv.encode("utf-8"), "text/csv; charset=utf-8")},
                timeout=60,
            )
            if r.status_code not in (404, 405):
                r.raise_for_status()
                batch_resp = r
                break
        except Exception:
            continue

    # ── Fallback: fire individual /call for each row ──────────────────────────
    if not batch_resp:
        success_count = 0
        errors = []
        for row in rows:
            try:
                variables = {
                    "customer_name":  row.get("customer_name", ""),
                    "expiry_date":    row.get("expiry_date", ""),
                    "days_remaining": format_days_remaining(row.get("days_remaining", "")),
                    "agent_name":     row.get("agent_name", "Jyoti"),
                }
                pr = req.post(f"{BASE_URL}/call", headers=get_headers(), json={
                    "agent_id": AGENT_ID,
                    "recipient_phone_number": row.get("recipient_phone_number", ""),
                    "user_data": variables, "variables": variables,
                    "webhook_url": webhook_url,
                }, timeout=30)
                pr.raise_for_status()
                exec_id = pr.json().get("execution_id") or pr.json().get("id") or ""
                call_log.append({
                    "name": row.get("customer_name", ""), "phone": row.get("recipient_phone_number", ""),
                    "expiry": row.get("expiry_date", ""), "days": row.get("days_remaining", ""),
                    "status": "queued", "disposition": "Pending", "voc": "",
                    "recording_url": "", "execution_id": exec_id,
                    "time": datetime.now().strftime("%d %b %H:%M"),
                })
                success_count += 1
            except Exception as ex:
                errors.append(str(ex))
        save_json(LOG_FILE, call_log, table="call_log")
        return jsonify({"success": True, "batch_id": "individual-calls",
                        "total": success_count, "errors": errors})

    try:
        result   = batch_resp.json()
        batch_id = result.get("batch_id") or result.get("id") or "batch"
        for row in rows:
            call_log.append({
                "name": row.get("customer_name", ""), "phone": row.get("recipient_phone_number", ""),
                "expiry": row.get("expiry_date", ""), "days": row.get("days_remaining", ""),
                "status": "queued", "disposition": "Pending", "voc": "",
                "recording_url": "", "execution_id": batch_id,
                "time": datetime.now().strftime("%d %b %H:%M"),
            })
        save_json(LOG_FILE, call_log, table="call_log")
        return jsonify({"success": True, "batch_id": batch_id, "total": len(rows)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/log")
def get_log():
    return jsonify(call_log)


# ─── Cohort Renewal Dashboard (backed by Google Sheet) ────────────────────────
def _normalize_cohort(raw):
    c = (raw or "").strip().lower()
    if c == "ai call":
        return "AI Call"
    if c == "call":
        return "Call"
    if c == "no call":
        return "No Call"
    return raw.strip() if raw else "Unknown"


def _parse_cohort_date(d):
    for fmt in ("%b %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt)
        except ValueError:
            continue
    return None


def _fetch_cohort_rows():
    url = f"https://docs.google.com/spreadsheets/d/{COHORT_SHEET_ID}/export?format=csv&gid={COHORT_SHEET_GID}"
    resp = req.get(url, timeout=30)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return list(csv.DictReader(io.StringIO(resp.text)))


def _parse_price(raw):
    raw = (raw or "").strip()
    try:
        val = float(raw)
        return val if val > 0 else None
    except ValueError:
        return None


def _bucket_stat(bucket_map, key, renewed):
    b = bucket_map.setdefault(key, {"total": 0, "renewed": 0})
    b["total"] += 1
    if renewed:
        b["renewed"] += 1


def _rate(stat):
    return round(stat["renewed"] / stat["total"] * 100, 1) if stat["total"] else 0


CUSTOMER_TYPES = ["Migrated", "Legacy", "Pay G"]


def _customer_type(raw):
    t = (raw or "").strip()
    return t if t in CUSTOMER_TYPES else None


PRICE_BRACKETS = ["₹45 & below", "₹46-300", "₹301-600", "₹601-1500", "₹1500+"]


def _price_bracket(price):
    if price <= 45:
        return "₹45 & below"
    if price <= 300:
        return "₹46-300"
    if price <= 600:
        return "₹301-600"
    if price <= 1500:
        return "₹601-1500"
    return "₹1500+"


_AI_CALL_START  = datetime(2026, 6, 22)  # AI calls started after 21st June
_JULY_START     = datetime(2026, 7,  1)  # July starts
_FULL_AI_START  = datetime(2026, 7,  4)  # 100% AI automation from 4th July

def _in_period(dt, period):
    if not dt:
        return False
    if period == "till21":
        return dt < _AI_CALL_START
    if period == "after21":
        return _AI_CALL_START <= dt < _FULL_AI_START
    if period == "fullaijuly":
        return dt >= _FULL_AI_START
    return True


def _build_cohort_dashboard_data(period="all"):
    rows = _fetch_cohort_rows()
    cohorts_seen = []
    date_agg = {}       # date -> cohort -> {total, renewed, day0, day4, r15_eligible, call_date}
    overall = {"total": 0, "renewed": 0, "day0": 0, "day4": 0, "r15_eligible": 0}
    cohort_totals = {}  # cohort -> {total, renewed}
    from datetime import date as _date
    _today = _date.today()
    matched_rows = 0
    bracket_cohort_renewed = {}       # bracket -> cohort -> renewed count
    type_bracket_cohort_renewed = {}  # type -> bracket -> cohort -> renewed count
    type_cohort_totals = {}           # type -> cohort -> {total, renewed}

    for r in rows:
        date_raw = (r.get("PLAN_EXPIRED_ON") or "").strip()
        dt = _parse_cohort_date(date_raw)

        if not _in_period(dt, period):
            continue

        # 22 Jun was AI Call's small pilot-batch launch day — exclude it entirely
        # (all cohorts) so it doesn't skew the day-wise comparison.
        if dt and dt.month == 6 and dt.day == 22:
            continue

        cohort = _normalize_cohort(r.get("Cohort"))

        matched_rows += 1
        if cohort not in cohorts_seen:
            cohorts_seen.append(cohort)
        renewed = (r.get("Renewal Status") or "").strip().lower() == "yes"
        renewal_day_raw = (r.get("Renewal day") or "").strip()
        try:
            renewal_day_int = int(renewal_day_raw) if renewal_day_raw else None
        except:
            renewal_day_int = None
        day0 = renewed and renewal_day_int == 0
        day4 = renewed and renewal_day_int is not None and renewal_day_int <= 4

        call_date_raw = (r.get("Call Date") or "").strip()
        call_dt = _parse_cohort_date(call_date_raw)
        r15_eligible = call_dt is not None and (_today - call_dt.date()).days >= 4

        overall["total"] += 1
        if renewed:
            overall["renewed"] += 1
        if day0:
            overall["day0"] += 1
        if r15_eligible:
            overall["r15_eligible"] += 1
            if day4:
                overall["day4"] += 1
        _bucket_stat(cohort_totals, cohort, renewed)

        if dt:
            bucket = date_agg.setdefault(dt, {}).setdefault(cohort, {
                "total": 0, "renewed": 0, "day0": 0,
                "day4": 0, "r15_eligible": 0, "call_date": call_dt
            })
            bucket["total"] += 1
            if renewed:
                bucket["renewed"] += 1
            if day0:
                bucket["day0"] += 1
            if r15_eligible:
                bucket["r15_eligible"] += 1
                if day4:
                    bucket["day4"] += 1
            if call_dt and not bucket["call_date"]:
                bucket["call_date"] = call_dt

        price = _parse_price(r.get("Plan Amount"))
        if price is not None and renewed:
            bracket = _price_bracket(price)
            bc = bracket_cohort_renewed.setdefault(bracket, {})
            bc[cohort] = bc.get(cohort, 0) + 1

        ctype = _customer_type(r.get("A"))
        if ctype:
            tstat = type_cohort_totals.setdefault(ctype, {}).setdefault(cohort, {"total": 0, "renewed": 0})
            tstat["total"] += 1
            if renewed:
                tstat["renewed"] += 1
            if price is not None and renewed:
                bracket = _price_bracket(price)
                tbc = type_bracket_cohort_renewed.setdefault(ctype, {}).setdefault(bracket, {})
                tbc[cohort] = tbc.get(cohort, 0) + 1

    dates_sorted = sorted(date_agg.keys())
    preferred_order = ["Call", "AI Call", "No Call"]
    cohorts = [c for c in preferred_order if c in cohorts_seen] + \
              [c for c in cohorts_seen if c not in preferred_order]

    series = {}
    for c in cohorts:
        pts = []
        for dt in dates_sorted:
            stat = date_agg[dt].get(c, {"total": 0, "renewed": 0, "day0": 0, "day4": 0, "r15_eligible": 0, "call_date": None})
            day0_rate = round(stat["day0"] / stat["total"] * 100, 1) if stat["total"] else 0
            day4_rate = round(stat["day4"] / stat["r15_eligible"] * 100, 1) if stat["r15_eligible"] else None
            call_date_str = stat["call_date"].strftime("%d %b") if stat["call_date"] else ""
            pts.append({
                "total": stat["total"], "renewed": stat["renewed"], "rate": _rate(stat),
                "day0": stat["day0"], "day0_rate": day0_rate,
                "day4": stat["day4"], "day4_rate": day4_rate,
                "r15_eligible": stat["r15_eligible"],
                "call_date": call_date_str,
            })
        series[c] = pts

    # Plan opted by renewed customers — bracket x cohort, renewed only
    present_brackets = [b for b in PRICE_BRACKETS if b in bracket_cohort_renewed]
    cohort_renewed_totals = {c: sum(bracket_cohort_renewed[b].get(c, 0) for b in present_brackets) for c in cohorts}
    grand_renewed_total = sum(cohort_renewed_totals.values())

    plan_breakdown = []
    for b in present_brackets:
        row_total = sum(bracket_cohort_renewed[b].get(c, 0) for c in cohorts)
        by_cohort = {}
        for c in cohorts:
            cnt = bracket_cohort_renewed[b].get(c, 0)
            pct = round(cnt / cohort_renewed_totals[c] * 100, 1) if cohort_renewed_totals[c] else 0
            by_cohort[c] = {"count": cnt, "pct": pct}
        plan_breakdown.append({
            "bracket": b, "total": row_total, "by_cohort": by_cohort,
            "pct_of_total": round(row_total / grand_renewed_total * 100, 1) if grand_renewed_total else 0,
        })

    # Customer type (Migrated / Legacy / Pay G) x bracket, renewed only
    type_breakdown = []
    for t in CUSTOMER_TYPES:
        if t not in type_bracket_cohort_renewed:
            continue
        brackets_for_type = [b for b in PRICE_BRACKETS if b in type_bracket_cohort_renewed[t]]
        rows = []
        total_renewed = 0
        for b in brackets_for_type:
            cohort_counts = type_bracket_cohort_renewed[t][b]
            row_total = sum(cohort_counts.values())
            total_renewed += row_total
            rows.append({"bracket": b, "total": row_total, "by_cohort": {c: cohort_counts.get(c, 0) for c in cohorts}})
        type_breakdown.append({"type": t, "total_renewed": total_renewed, "brackets": rows})

    # Customer type x cohort renewal rate (all customers of that type, not just renewed)
    type_cohort_rate = []
    for t in CUSTOMER_TYPES:
        if t not in type_cohort_totals:
            continue
        entry_cohorts = {}
        for c in cohorts:
            stat = type_cohort_totals[t].get(c, {"total": 0, "renewed": 0})
            entry_cohorts[c] = {"total": stat["total"], "renewed": stat["renewed"], "rate": _rate(stat)}
        type_cohort_rate.append({"type": t, "cohorts": entry_cohorts})

    return {
        "dates": [dt.strftime("%d %b") for dt in dates_sorted],
        "cohorts": cohorts,
        "series": series,
        "row_count": matched_rows,
        "summary": {
            "total": overall["total"],
            "renewed": overall["renewed"],
            "rate": _rate(overall),
            "day0": overall["day0"],
            "day0_rate": round(overall["day0"] / overall["total"] * 100, 1) if overall["total"] else 0,
            "day4": overall["day4"],
            "r15_eligible": overall["r15_eligible"],
            "day4_rate": round(overall["day4"] / overall["r15_eligible"] * 100, 1) if overall["r15_eligible"] else 0,
            "by_cohort": {c: {"total": cohort_totals[c]["total"], "renewed": cohort_totals[c]["renewed"],
                               "rate": _rate(cohort_totals[c])} for c in cohorts},
        },
        "plan_breakdown": plan_breakdown,
        "type_breakdown": type_breakdown,
        "type_cohort_rate": type_cohort_rate,
    }


def _build_renewal_day_data(period="after21"):
    """
    Reads the 'Renewal day' column directly from the sheet (values: 0, 1, 2, 3...).
    Returns distribution: day -> {count, pct_of_called, pct_of_renewed} per cohort.
    """
    rows = _fetch_cohort_rows()

    RENEWAL_DAY_COL = "Renewal day"
    has_renewal_day = bool(rows) and RENEWAL_DAY_COL in rows[0]

    day_dist = {}     # cohort -> {day_key: count}
    total_called = {} # cohort -> total customers
    total_renewed = {}

    for r in rows:
        date_raw = (r.get("PLAN_EXPIRED_ON") or "").strip()
        dt = _parse_cohort_date(date_raw)
        if not _in_period(dt, period):
            continue
        if dt and dt.month == 6 and dt.day == 22:
            continue

        cohort = _normalize_cohort(r.get("Cohort"))
        renewed = (r.get("Renewal Status") or "").strip().lower() == "yes"

        total_called.setdefault(cohort, 0)
        total_called[cohort] += 1
        total_renewed.setdefault(cohort, 0)
        if renewed:
            total_renewed[cohort] += 1

        if not renewed or not has_renewal_day:
            continue

        day_raw = (r.get(RENEWAL_DAY_COL) or "").strip()
        if not day_raw:
            continue
        try:
            day_val = int(float(day_raw))
        except (ValueError, TypeError):
            continue

        day_key = day_val if day_val <= 7 else "8+"
        day_dist.setdefault(cohort, {})
        day_dist[cohort][day_key] = day_dist[cohort].get(day_key, 0) + 1

    preferred_order = ["Call", "AI Call", "No Call"]
    cohorts = [c for c in preferred_order if c in total_called] + \
              [c for c in total_called if c not in preferred_order]

    all_days = sorted(
        {d for cd in day_dist.values() for d in cd},
        key=lambda x: (1, 8) if x == "8+" else (0, int(x))
    )

    result_by_cohort = {}
    for c in cohorts:
        dist = day_dist.get(c, {})
        tc = total_called.get(c, 0)
        tr = total_renewed.get(c, 0)
        result_by_cohort[c] = {
            "total_called": tc,
            "total_renewed": tr,
            "days": {
                str(d): {
                    "count": dist.get(d, 0),
                    "pct_of_called": round(dist.get(d, 0) / tc * 100, 1) if tc else 0,
                    "pct_of_renewed": round(dist.get(d, 0) / tr * 100, 1) if tr else 0,
                }
                for d in all_days
            }
        }

    return {
        "cohorts": cohorts,
        "days": [str(d) for d in all_days],
        "by_cohort": result_by_cohort,
        "has_renewal_date": has_renewal_day,
        "renewal_col_used": RENEWAL_DAY_COL if has_renewal_day else None,
    }


@app.route("/api/renewal-day-data")
def api_renewal_day_data():
    import time
    period = request.args.get("period", "after21")
    if period not in ("all", "till21", "after21", "fullaijuly"):
        period = "after21"
    force = request.args.get("refresh") == "1"
    cache_key = "renewal_day_" + period
    now = time.time()
    cached = _cohort_cache.get(cache_key)
    if not force and cached and (now - cached["ts"] < COHORT_CACHE_TTL):
        return jsonify(cached["data"])
    try:
        data = _build_renewal_day_data(period)
    except Exception as e:
        if cached:
            return jsonify(cached["data"])
        return jsonify({"error": str(e)}), 502
    _cohort_cache[cache_key] = {"data": data, "ts": now}
    return jsonify(data)


@app.route("/api/cohort-data")
def api_cohort_data():
    import time
    period = request.args.get("period", "all")
    if period not in ("all", "till21", "after21", "fullaijuly"):
        period = "all"
    force = request.args.get("refresh") == "1"
    now = time.time()
    cached = _cohort_cache.get(period)
    if not force and cached is not None and (now - cached["ts"] < COHORT_CACHE_TTL):
        return jsonify(cached["data"])
    try:
        data = _build_cohort_dashboard_data(period)
    except Exception as e:
        if cached is not None:
            return jsonify(cached["data"])
        return jsonify({"error": f"Could not fetch cohort sheet: {e}"}), 502
    _cohort_cache[period] = {"data": data, "ts": now}
    return jsonify(data)


@app.route("/api/update-disposition", methods=["POST"])
def update_disposition():
    d = request.json
    for entry in call_log:
        if entry.get("execution_id") == d.get("execution_id"):
            entry["disposition"] = d.get("disposition", "")
            entry["voc"]         = d.get("voc", "")
            break
    save_json(LOG_FILE, call_log, table="call_log")
    return jsonify({"success": True})


@app.route("/api/fetch-recording/<exec_id>")
def fetch_recording(exec_id):
    try:
        # Try multiple Bolna endpoints for call details
        data = {}
        for endpoint in [
            f"{BASE_URL}/v1/logs/{exec_id}",
            f"{BASE_URL}/call/{exec_id}",
            f"{BASE_URL}/execution/{exec_id}",
        ]:
            r = req.get(endpoint, headers=get_headers(), timeout=10)
            if r.status_code == 200:
                data = r.json()
                break

        recording = (
            data.get("recording_url") or data.get("audio_url") or
            data.get("recordingUrl")  or data.get("record_url") or
            data.get("combined_audio_url") or ""
        )
        # Fallback: construct from exec_id (Bolna standard format)
        if not recording:
            recording = f"https://api.bolna.ai/recordings/call/{exec_id}"

        # Also try to extract transcript for auto-disposition
        transcript = data.get("transcript", [])
        status = data.get("status", "")

        for entry in call_log:
            if entry.get("execution_id") == exec_id:
                if recording:
                    entry["recording_url"] = recording
                if status:
                    entry["status"] = status
                break

        return jsonify({
            "success": True,
            "recording_url": recording,
            "status": status,
            "raw": data
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


webhook_log = []  # store last 50 webhook payloads for debugging

@app.route("/webhook-log")
def view_webhook_log():
    return jsonify(webhook_log[-20:])

@app.route("/webhook-test", methods=["GET"])
def webhook_test():
    """Simulate a Bolna webhook — use to test auto-disposition without a real call."""
    fake = {
        "execution_id": request.args.get("exec_id", "test-123"),
        "status": request.args.get("status", "completed"),
        "recording_url": request.args.get("rec", ""),
        "transcript": [
            {"role": "assistant", "content": "आपका व्योम recharge खत्म हो गया था।"},
            {"role": "user",      "content": request.args.get("customer_said", "haan aaj kar deta hoon")},
        ]
    }
    webhook_log.append({"time": datetime.now().strftime("%d %b %H:%M:%S"), "data": fake, "source": "test"})
    return jsonify({"sent": fake, "tip": "Now check /webhook-log to see it arrived"})

@app.route("/webhook", methods=["POST"])
def webhook():
    data      = request.json or {}
    # Save raw webhook for debugging
    webhook_log.append({"time": datetime.now().strftime("%d %b %H:%M:%S"), "data": data})
    if len(webhook_log) > 50:
        webhook_log.pop(0)

    exec_id   = data.get("id") or data.get("execution_id") or data.get("run_id") or data.get("call_id") or ""
    status    = data.get("status", "completed")
    recording = (data.get("recording_url") or data.get("audio_url") or
                 data.get("recordingUrl") or data.get("combined_audio_url") or
                 data.get("record_url") or "")
    # Construct recording URL from exec_id if not provided (Bolna standard format)
    if not recording and exec_id:
        recording = f"https://api.bolna.ai/recordings/call/{exec_id}"

    # Extract transcript — if null in webhook, fetch from Bolna API
    transcript = data.get("transcript") or data.get("conversation_transcript")
    if not transcript and exec_id and status == "completed":
        try:
            for ep in [f"{BASE_URL}/v1/logs/{exec_id}", f"{BASE_URL}/call/{exec_id}"]:
                tr = req.get(ep, headers=get_headers(), timeout=10)
                if tr.status_code == 200:
                    td = tr.json()
                    transcript = td.get("transcript") or td.get("conversation_transcript")
                    if not recording:
                        recording = (td.get("recording_url") or td.get("combined_audio_url")
                                     or f"https://api.bolna.ai/recordings/call/{exec_id}")
                    webhook_log.append({"time": datetime.now().strftime("%d %b %H:%M:%S"),
                                        "fetched_transcript": str(transcript)[:300]})
                    break
        except Exception:
            pass

    turns = []
    if isinstance(transcript, list):
        for t in transcript:
            role = t.get("role","")
            text = t.get("content","") or t.get("text","") or ""
            turns.append({"role": role, "text": text})
        full_text    = " ".join(t["text"] for t in turns).lower()
        customer_txt = " ".join(t["text"] for t in turns if t["role"] in ("user","human","customer")).lower()
    elif isinstance(transcript, str) and transcript:
        full_text    = transcript.lower()
        customer_txt = full_text
    else:
        full_text    = ""
        customer_txt = ""

    # ── Auto-Disposition Logic (priority order) ─────────────────────────────
    # Keywords in BOTH Roman (Bolna sometimes romanizes) AND Devanagari (actual transcript)
    disposition = "Pending"
    voc         = ""
    cb_needed   = False

    # 1. Already recharged — must have "recharge" + confirmation (not just "ho gaya")
    if any(w in customer_txt for w in [
        "recharge ho gaya", "recharge kar liya", "recharge kara liya", "pehle se recharge",
        "already recharge", "recharge hua", "recharge kar chuka", "recharge ho chuka",
        "रिचार्ज हो गया", "रिचार्ज कर लिया", "रिचार्ज करा लिया", "पहले से रिचार्ज",
        "रिचार्ज हो चुका", "रिचार्ज कर चुका", "करवा लिया", "हो गया रिचार्ज"
    ]):
        disposition = "Already Recharged"
        voc = "Customer ne bataya ki recharge pehle se ho gaya hai"

    # 2. Will recharge today
    elif any(w in customer_txt for w in [
        "aaj kar", "abhi karta", "kar deta hoon", "kar deti hoon", "aaj karwa", "aaj recharge",
        "haan karunga", "haan karungi", "kar lunga aaj", "abhi karta hoon",
        "आज कर", "आज रिचार्ज", "अभी करता", "कर देता हूँ", "कर देती हूँ",
        "हाँ करूंगा", "हाँ करूंगी", "कर लूंगा आज", "अभी करता हूँ"
    ]):
        disposition = "Will Recharge Today"
        voc = "Customer ne aaj recharge karne ki baat ki"

    # 3. Not answered / no response (check before "will recharge later" to avoid false match)
    elif status in ("no-answer", "busy", "failed", "not-answered"):
        disposition = "Not Answered / Busy"
        voc = ""

    # 4. Out of town (check before "will recharge later" — "karenge" appears in both)
    elif any(w in customer_txt for w in [
        "gaon", "bahar", "gaya hoon", "travel", "bahar hoon", "sheher se", "wapas aaunga", "wapas aaungi", "village",
        "गाँव", "गांव", "बाहर", "गया हूँ", "गए हुए", "बाहर हूँ", "शहर से", "वापस आऊंगा", "वापस आऊंगी",
        "घर नहीं", "आ कर", "आकर", "लौट कर", "लौटकर", "बाहर गए", "गए हैं"
    ]):
        disposition = "Out of Town"
        voc = "Customer abhi bahar hai"
        cb_needed = True

    # 5. Will recharge later
    elif any(w in customer_txt for w in [
        "kal kar", "parso", "baad mein kar", "karenge", "dekh lete", "soch ke", "baad mein",
        "कल कर", "परसों", "बाद में", "करेंगे", "देख लेते", "सोच के", "कल करेंगे",
        "बाद में करते", "थोड़ी देर", "कुछ दिन", "अगले हफ्ते"
    ]):
        disposition = "Will Recharge Later"
        voc = "Customer ne baad mein recharge karne ki baat ki"

    # 6. Device already returned
    elif any(w in customer_txt for w in [
        "wapas kar diya", "de diya", "return kar diya", "le gaye", "wapas de",
        "वापस कर दिया", "दे दिया", "रिटर्न कर दिया", "ले गए", "वापस दे दिया", "जमा कर दिया"
    ]):
        disposition = "Device Already Returned"
        voc = "Customer ne bataya device pehle se wapas kar diya"

    # 7. Service issue
    elif any(w in customer_txt for w in [
        "slow", "problem", "issue", "kaam nahi", "nahi chala", "signal nahi", "connection nahi",
        "स्लो", "धीमा", "काम नहीं", "नहीं चला", "सिग्नल नहीं", "कनेक्शन नहीं", "इंटरनेट नहीं",
        "बहुत slow", "नेट नहीं"
    ]):
        disposition = "Don't Want – Service Issue"
        if any(w in customer_txt for w in ["slow","स्लो","धीमा"]): voc = "Service issue: internet slow tha"
        elif any(w in customer_txt for w in ["signal","सिग्नल"]): voc = "Service issue: signal nahi tha"
        else: voc = "Service issue: internet kaam nahi kiya"

    # 8. Wants device return — personal reason
    elif any(w in customer_txt for w in [
        "nahi chahiye", "band karo", "wapas karna", "return karna", "lelo", "nahi lena", "shifted", "shift ho",
        "नहीं चाहिए", "बंद करो", "वापस करना", "रिटर्न करना", "ले लो", "नहीं लेना", "शिफ्ट हो", "चले गए"
    ]):
        disposition = "Wants Device Return"
        if any(w in customer_txt for w in ["shift","शिफ्ट"]): voc = "Personal: ghar shift ho gaya"
        elif any(w in customer_txt for w in ["nahi chahiye","नहीं चाहिए"]): voc = "Personal: service nahi chahiye"
        else: voc = "Device return chahiye (reason: call mein)"

    # 9. Callback requested
    elif any(w in customer_txt for w in [
        "busy", "abhi nahi", "baad mein call", "kal call", "time nahi", "thodi der",
        "बिज़ी", "अभी नहीं", "बाद में कॉल", "कल कॉल", "टाइम नहीं", "थोड़ी देर"
    ]):
        disposition = "Callback Scheduled"
        voc = "Customer busy tha — callback chahiye"
        cb_needed = True

    # ── Update OR create call log entry ─────────────────────────────────────
    original = None
    for entry in call_log:
        if entry.get("execution_id") == exec_id:
            entry["status"] = status
            if disposition != "Pending":
                entry["disposition"] = disposition
            if voc:
                entry["voc"] = voc
            if recording and not entry.get("recording_url"):
                entry["recording_url"] = recording
            original = entry
            break

    # If call was made directly via Bolna (not through dashboard), create new entry
    if not original and exec_id and status == "completed":
        ctx = data.get("context_details", {}) or {}
        recipient = ctx.get("recipient_data", {}) or {}
        new_entry = {
            "name":          recipient.get("customer_name", "") or data.get("to_number", ""),
            "phone":         data.get("telephony_data", {}).get("to_number", "") or recipient.get("recipient_phone_number", ""),
            "expiry":        recipient.get("expiry_date", ""),
            "days":          recipient.get("days_remaining", ""),
            "status":        status,
            "disposition":   disposition,
            "voc":           voc,
            "recording_url": recording,
            "execution_id":  exec_id,
            "time":          datetime.now().strftime("%d %b %H:%M"),
        }
        call_log.append(new_entry)
        original = new_entry

    save_json(LOG_FILE, call_log, table="call_log")

    # ── Update automation tracker + write to Google Sheet ────────────────────
    update_call_record(exec_id, status, disposition, voc)

    # ── Auto-add to callback list ────────────────────────────────────────────
    if cb_needed and original:
        next_day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT10:00")
        callback_log.append({
            "name":         original.get("name",""),
            "phone":        original.get("phone",""),
            "expiry":       original.get("expiry",""),
            "days":         original.get("days",""),
            "reason":       voc or disposition,
            "scheduled_at": next_day,
            "execution_id": "",
            "status":       "pending",
            "created_at":   datetime.now().strftime("%d %b %H:%M"),
        })
        save_json(CALLBACK_FILE, callback_log, table="callback_log")

    return jsonify({"received": True})


@app.route("/api/callback/schedule", methods=["POST"])
def schedule_callback():
    d = request.json
    try:
        scheduled_at = d.get("scheduled_at", "")
        payload = {
            "agent_id": AGENT_ID,
            "recipient_phone_number": d["phone"],
            "user_data": {
                "customer_name":  d["name"],
                "expiry_date":    d.get("expiry",""),
                "days_remaining": format_days_remaining(d.get("days","")),
                "agent_name":     "Jyoti",
            },
            "variables": {
                "customer_name":  d["name"],
                "expiry_date":    d.get("expiry",""),
                "days_remaining": format_days_remaining(d.get("days","")),
                "agent_name":     "Jyoti",
            },
            "retry_config": {
                "enabled": True, "max_retries": 2,
                "retry_on_statuses": ["no-answer","busy"],
                "retry_intervals_minutes": [60,120],
            },
        }
        if scheduled_at: payload["scheduled_at"] = scheduled_at

        resp = req.post(f"{BASE_URL}/call", headers=get_headers(), json=payload, timeout=30)
        resp.raise_for_status()
        exec_id = resp.json().get("execution_id","")

        callback_log.append({
            "name":         d["name"], "phone": d["phone"],
            "expiry":       d.get("expiry",""), "days": d.get("days",""),
            "reason":       d.get("reason",""), "scheduled_at": scheduled_at,
            "execution_id": exec_id, "status": "pending",
            "created_at":   datetime.now().strftime("%d %b %H:%M"),
        })
        return jsonify({"success": True, "execution_id": exec_id})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/callbacks")
def get_callbacks():
    return jsonify(callback_log)


@app.route("/export-csv")
def export_csv():
    output = io.StringIO()
    fields = ["name","phone","expiry","days","status","disposition","voc","recording_url","execution_id","time"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in call_log:
        writer.writerow({f: row.get(f, "") for f in fields})
    return Response(
        output.getvalue().encode("utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition":"attachment; filename=wiom_calls.csv"})


@app.route("/export-callbacks")
def export_callbacks():
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "name","phone","expiry","days","reason","scheduled_at","status","execution_id","created_at"
    ])
    writer.writeheader(); writer.writerows(callback_log)
    return Response(output.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":"attachment; filename=wiom_callbacks.csv"})


@app.route("/sample-csv")
def sample_csv():
    data = (
        "recipient_phone_number,customer_name,expiry_date,days_remaining,agent_name\n"
        "+919876543210,Ramesh Kumar,2025-06-13,4,Jyoti\n"
        "+919123456789,Sunita Sharma,2025-06-10,7,Jyoti\n"
        "+918765432100,Mukesh Verma,2025-06-08,9,Jyoti\n"
    )
    return Response(data, mimetype="text/csv",
        headers={"Content-Disposition":"attachment; filename=sample_contacts.csv"})


def _metabase_query(sql):
    """Run a native SQL query against Metabase and return rows as list of dicts."""
    url = f"{os.getenv('METABASE_URL','https://metabase.wiom.in')}/api/dataset"
    headers = {
        "x-api-key": os.getenv("METABASE_API_KEY",""),
        "Content-Type": "application/json",
    }
    payload = {
        "database": int(os.getenv("METABASE_DB_ID","1")),
        "type": "native",
        "native": {"query": sql},
    }
    r = req.post(url, headers=headers, json=payload, timeout=60)
    r.raise_for_status()
    result = r.json()
    log.warning(f"METABASE RAW KEYS: {list(result.keys())} | data keys: {list(result.get('data',{}).keys())} | cols: {result.get('data',{}).get('cols',[])} | rows: {result.get('data',{}).get('rows',[])}")
    cols = [c["name"] for c in result.get("data",{}).get("cols",[])]
    rows = result.get("data",{}).get("rows",[])
    return [dict(zip(cols, row)) for row in rows]


@app.route("/api/r11-campaign")
def r11_campaign():
    """Today's R11 auto-campaign status: Metabase count + call_log dispositions."""
    today     = date.today()
    r11_date  = (today - timedelta(days=11)).strftime("%Y-%m-%d")
    today_str = today.strftime("%d %b")

    RENEWED = {"Already Recharged", "Will Recharge Today"}

    # Filter call_log: calls made today whose plan expired on r11_date, not yet renewed
    r11_date_display = (today - timedelta(days=11)).strftime("%d %b")  # e.g. "13 Jul"
    today_calls = [
        c for c in call_log
        if (c.get("time","") or "").startswith(today_str)
        and (c.get("disposition","Pending") or "Pending") not in RENEWED
    ]

    disp_counts = {}
    for c in today_calls:
        d = c.get("disposition","Pending") or "Pending"
        disp_counts[d] = disp_counts.get(d, 0) + 1

    answered = sum(1 for c in today_calls if c.get("status") not in ("queued","no-answer","busy","failed","error",""))
    pending  = len(today_calls) - answered

    # Metabase: count of R11 customers for today's R11 date
    r11_count = None
    r11_error = None
    try:
        sql = f"""
        SELECT COUNT(DISTINCT CONCAT(ROUTER_NAS_ID, '|', COALESCE(DEVICE_ID, ''))) AS cnt
        FROM PROD_DB.PUBLIC.T_ROUTER_USER_MAPPING
        WHERE OTP NOT IN ('FREE', 'PAY_ONLINE', 'CASH', 'ROAM')
          AND MOBILE <> ''
          AND DEVICE_LIMIT = 10
          AND DATE(DATEADD('minute', 330, OTP_EXPIRY_TIME)) = '{r11_date}'
        """
        rows = _metabase_query(sql)
        log.warning(f"R11 Metabase rows: {rows}")
        if rows:
            r11_count = list(rows[0].values())[0] if rows[0] else None
    except Exception as e:
        log.warning(f"R11 Metabase count failed: {e}")
        r11_error = str(e)

    # Next scheduled run
    next_run = None
    for job in scheduler.get_jobs():
        if job.id == "round1_daily":
            nrt = job.next_run_time
            if nrt:
                next_run = nrt.astimezone(IST).strftime("%d %b %I:%M %p IST")

    return jsonify({
        "r11_date":          r11_date,
        "today":             today.strftime("%Y-%m-%d"),
        "r11_count":         r11_count,
        "r11_error":         r11_error,
        "r11_raw":           rows if 'rows' in dir() else None,
        "calls_made":        len(today_calls),
        "answered":          answered,
        "pending":           pending,
        "next_run":          next_run,
        "disposition_counts": disp_counts,
        "calls":             today_calls[-200:],
    })


DISPOSITION_RENEWAL_SQL = """
SELECT
    DATE(DATEADD('minute', 330, OTP_EXPIRY_TIME))::VARCHAR AS expiry_date,
    COUNT(DISTINCT CONCAT(ROUTER_NAS_ID, '|', COALESCE(DEVICE_ID, ''))) AS total
FROM PROD_DB.PUBLIC.T_ROUTER_USER_MAPPING
WHERE OTP NOT IN ('FREE', 'PAY_ONLINE', 'CASH', 'ROAM')
  AND MOBILE <> ''
  AND DEVICE_LIMIT = 10
  AND DATE(DATEADD('minute', 330, OTP_EXPIRY_TIME))
        BETWEEN DATEADD('day', -{days}, CURRENT_DATE()) AND DATEADD('day', -11, CURRENT_DATE())
GROUP BY DATE(DATEADD('minute', 330, OTP_EXPIRY_TIME))
ORDER BY expiry_date DESC
"""

_disp_cache = {}

@app.route("/api/retention-dispositions")
def retention_dispositions():
    """Disposition breakdown from call_log + date-wise renewal from Metabase."""
    import time as _time
    days  = int(request.args.get("days", 30))
    force = request.args.get("refresh") == "1"
    ckey  = f"disp_{days}"
    now   = _time.time()

    cached = _disp_cache.get(ckey)
    if not force and cached and (now - cached["ts"] < 300):
        return jsonify(cached["data"])

    # ── Disposition breakdown from call_log ──────────────────────────────────
    cutoff = datetime.now() - timedelta(days=days)
    RENEWED = {"Already Recharged", "Will Recharge Today"}
    recent_calls = []
    for c in call_log:
        if (c.get("disposition","Pending") or "Pending") in RENEWED:
            continue  # exclude already renewed
        try:
            t = datetime.strptime(c.get("time",""), "%d %b %H:%M")
            t = t.replace(year=datetime.now().year)
            if t >= cutoff:
                recent_calls.append(c)
        except Exception:
            recent_calls.append(c)  # include if date unreadable

    disp_counts = {}
    for c in recent_calls:
        d = c.get("disposition","Pending") or "Pending"
        disp_counts[d] = disp_counts.get(d, 0) + 1

    total_called = len(recent_calls)
    answered     = sum(1 for c in recent_calls if c.get("status") not in ("queued","no-answer","busy","failed","error",""))
    answer_rate  = round(answered / total_called * 100, 1) if total_called else 0

    # Renewal = Will Recharge Today + Already Recharged
    renewed      = sum(v for k,v in disp_counts.items() if k in ("Will Recharge Today","Already Recharged"))
    renewal_rate = round(renewed / total_called * 100, 1) if total_called else 0
    same_day     = disp_counts.get("Will Recharge Today", 0)
    same_day_rate= round(same_day / total_called * 100, 1) if total_called else 0

    # ── Date-wise renewal from Metabase ──────────────────────────────────────
    metabase_rows = []
    try:
        sql = DISPOSITION_RENEWAL_SQL.replace("{days}", str(days + 11))
        rows = _metabase_query(sql)
        for r in rows:
            metabase_rows.append({
                "expiry_date":  str(r.get("EXPIRY_DATE") or r.get("expiry_date","")),
                "total":        int(r.get("TOTAL") or r.get("total") or 0),
            })
    except Exception as e:
        log.warning(f"Dispositions Metabase query failed: {e}")

    data = {
        "total_called":      total_called,
        "answered":          answered,
        "answer_rate":       answer_rate,
        "renewed":           renewed,
        "renewal_rate":      renewal_rate,
        "same_day_rate":     same_day_rate,
        "disposition_counts": disp_counts,
        "renewal_by_disp":   {},
        "metabase_renewal":  metabase_rows,
    }
    _disp_cache[ckey] = {"data": data, "ts": now}
    return jsonify(data)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_railway = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")

    if is_railway:
        print(f"\n✅ Running on Railway — port {port}")
        print("Webhook URL: https://retentionwiom-production.up.railway.app/webhook")
        app.run(host="0.0.0.0", port=port, debug=False)
    else:
        # Local — try ngrok
        try:
            from pyngrok import ngrok
            public_url = ngrok.connect(port).public_url
            print("\n" + "="*55)
            print("  Wiom Retention Dashboard")
            print(f"  Local  : http://localhost:{port}")
            print(f"  Public : {public_url}")
            print(f"\n  ✅ Webhook URL (paste in Bolna):")
            print(f"  {public_url}/webhook")
            print("="*55 + "\n")
        except Exception as e:
            print(f"\n  ⚠️  ngrok error: {e}")
            print(f"  Dashboard: http://localhost:{port}\n")

        app.run(debug=False, port=port, use_reloader=False)
