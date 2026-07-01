"""
Wiom Retention Campaign Dashboard
Run: python app.py → http://localhost:5000
"""

from flask import Flask, request, jsonify, render_template_string, Response
import requests as req
import csv, io, os, json
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Hindi month names for natural date reading by TTS
HINDI_MONTHS = {
    1:"January", 2:"February", 3:"March", 4:"April",
    5:"May", 6:"June", 7:"July", 8:"August",
    9:"September", 10:"October", 11:"November", 12:"December"
}
HINDI_DAYS = {
    1:"pehli", 2:"do", 3:"teen", 4:"chaar", 5:"paanch",
    6:"chhe", 7:"saat", 8:"aath", 9:"nau", 10:"das",
    11:"gyarah", 12:"barah", 13:"terah", 14:"chaudah", 15:"pandrah",
    16:"solah", 17:"satrah", 18:"atharah", 19:"unnis", 20:"bees",
    21:"ikkees", 22:"baaees", 23:"teis", 24:"chaubees", 25:"pachees",
    26:"chabbees", 27:"sattaees", 28:"atthaees", 29:"unattees", 30:"tees", 31:"ikattees"
}

def format_expiry_date(date_str):
    """Convert 2025-06-13 → 'terah June' for natural Hindi TTS reading."""
    if not date_str:
        return "recently"
    try:
        dt = datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
        day_hindi = HINDI_DAYS.get(dt.day, str(dt.day))
        month_name = HINDI_MONTHS.get(dt.month, "")
        return f"{day_hindi} {month_name}"
    except Exception:
        return date_str  # fallback to original if parse fails

load_dotenv()

app      = Flask(__name__)
API_KEY  = os.getenv("BOLNA_API_KEY")
AGENT_ID = os.getenv("BOLNA_AGENT_ID")
FROM_NUM = os.getenv("FROM_PHONE_NUMBER", "+919654231202")
BASE_URL = "https://api.bolna.ai"
HEADERS  = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

call_log     = []
callback_log = []

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
.main { display: flex; height: calc(100vh - 56px); overflow: hidden; }
.sidebar {
  width: 220px; background: #fff; border-right: 1px solid #e2e8f0;
  padding: 20px 0; flex-shrink: 0; overflow-y: auto;
}
.content { flex: 1; overflow-y: auto; padding: 24px; }

/* Sidebar nav */
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 11px 20px; cursor: pointer; font-size: 13px;
  color: #475569; font-weight: 600; transition: .15s;
  border-left: 3px solid transparent;
}
.nav-item:hover { background: #f0f4ff; color: #1e3a8a; }
.nav-item.active { background: #eff6ff; color: #1e3a8a; border-left-color: #2563eb; }
.nav-item .icon { font-size: 16px; width: 22px; text-align: center; }
.nav-sep { height: 1px; background: #e2e8f0; margin: 10px 16px; }

/* Stats row */
.stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 20px; }
.stat-card {
  background: #fff; border-radius: 10px; padding: 14px 16px;
  box-shadow: 0 1px 4px rgba(0,0,0,.07); text-align: center;
}
.stat-card .num { font-size: 26px; font-weight: 800; color: #1e3a8a; }
.stat-card .lbl { font-size: 11px; color: #64748b; margin-top: 2px; }
.stat-card.green .num { color: #059669; }
.stat-card.orange .num { color: #d97706; }
.stat-card.red .num { color: #dc2626; }
.stat-card.purple .num { color: #7c3aed; }

/* Section card */
.card { background: #fff; border-radius: 12px; padding: 24px; box-shadow: 0 1px 6px rgba(0,0,0,.07); margin-bottom: 20px; }
.card-title { font-size: 15px; font-weight: 700; color: #1e3a8a; margin-bottom: 18px; display: flex; align-items: center; gap: 8px; }

/* Form */
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-full { grid-column: 1/-1; }
.form-group label { display: block; font-size: 11px; font-weight: 700; color: #64748b; text-transform: uppercase; letter-spacing: .5px; margin-bottom: 5px; }
.form-group input, .form-group select {
  width: 100%; padding: 9px 12px; border: 1.5px solid #e2e8f0;
  border-radius: 8px; font-size: 13px; background: #fafbff; outline: none; transition: .2s;
}
.form-group input:focus, .form-group select:focus { border-color: #2563eb; background: #fff; }

/* Buttons */
.btn { padding: 10px 20px; border: none; border-radius: 8px; font-size: 13px; font-weight: 700; cursor: pointer; transition: .2s; display: inline-flex; align-items: center; gap: 7px; }
.btn-primary { background: #1e3a8a; color: #fff; }
.btn-primary:hover { background: #1d4ed8; }
.btn-success { background: #059669; color: #fff; }
.btn-success:hover { background: #047857; }
.btn-danger  { background: #dc2626; color: #fff; }
.btn-sm { padding: 5px 12px; font-size: 11px; }
.btn-outline { background: #fff; border: 1.5px solid #e2e8f0; color: #475569; }
.btn-outline:hover { border-color: #94a3b8; }

/* Upload zone */
.upload-zone {
  border: 2px dashed #93c5fd; border-radius: 10px;
  padding: 32px; text-align: center; cursor: pointer; background: #f0f7ff;
  transition: .2s; margin-bottom: 16px;
}
.upload-zone:hover { border-color: #2563eb; background: #dbeafe; }
.upload-zone input { display: none; }
.upload-zone .upload-icon { font-size: 40px; margin-bottom: 8px; }
.upload-zone .upload-text { color: #2563eb; font-weight: 700; font-size: 14px; }
.upload-zone .upload-sub  { color: #94a3b8; font-size: 12px; margin-top: 4px; }

/* Table */
.tbl-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
thead th { background: #f8faff; padding: 10px 12px; text-align: left; font-weight: 700; color: #475569; white-space: nowrap; border-bottom: 2px solid #e2e8f0; }
tbody td { padding: 10px 12px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
tbody tr:hover td { background: #f8faff; }

/* Badges */
.badge { display: inline-block; padding: 3px 9px; border-radius: 20px; font-size: 11px; font-weight: 700; white-space: nowrap; }
.b-pending   { background: #f1f5f9; color: #64748b; }
.b-queued    { background: #fef3c7; color: #92400e; }
.b-done      { background: #dcfce7; color: #166534; }
.b-failed    { background: #fee2e2; color: #991b1b; }
.b-pay       { background: #d1fae5; color: #065f46; }
.b-later     { background: #e0f2fe; color: #0369a1; }
.b-oot       { background: #fef9c3; color: #854d0e; }
.b-return    { background: #ede9fe; color: #5b21b6; }
.b-dont      { background: #fce7f3; color: #9d174d; }
.b-cb        { background: #e0e7ff; color: #3730a3; }
.b-na        { background: #f1f5f9; color: #475569; }

/* Inline edit */
.disp-select {
  border: 1.5px solid #e2e8f0; border-radius: 6px; padding: 4px 8px;
  font-size: 11px; background: #fafbff; cursor: pointer; max-width: 180px;
}
.voc-input {
  border: 1.5px solid #e2e8f0; border-radius: 6px; padding: 4px 8px;
  font-size: 11px; background: #fafbff; width: 160px;
}
.save-btn {
  background: #059669; color: #fff; border: none; border-radius: 5px;
  padding: 4px 10px; font-size: 10px; font-weight: 700; cursor: pointer; margin-left: 4px;
}
.save-btn:hover { background: #047857; }

/* Preview table */
.preview-wrap { overflow-x: auto; max-height: 220px; border: 1px solid #e2e8f0; border-radius: 8px; margin-top: 12px; }
.preview-wrap table { font-size: 11px; }
.preview-wrap thead th { position: sticky; top: 0; }

/* Toast */
.toast {
  position: fixed; bottom: 24px; right: 24px; padding: 13px 20px;
  border-radius: 10px; font-size: 13px; font-weight: 600; color: #fff;
  display: none; z-index: 9999; box-shadow: 0 4px 16px rgba(0,0,0,.25);
  max-width: 320px;
}

/* Progress bar */
.progress-bar { height: 6px; background: #e2e8f0; border-radius: 3px; margin-top: 12px; }
.progress-fill { height: 100%; background: #2563eb; border-radius: 3px; transition: width .4s; }

/* Info box */
.info-box { background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px; padding: 12px 16px; font-size: 12px; color: #1e40af; margin-bottom: 16px; }
.warn-box { background: #fefce8; border: 1px solid #fde68a; border-radius: 8px; padding: 12px 16px; font-size: 12px; color: #92400e; margin-bottom: 16px; }

/* Inline call result panel */
.result-panel { background: #f0fdf4; border: 1.5px solid #86efac; border-radius: 10px; padding: 16px; margin-top: 16px; display: none; }
.result-panel .rid { font-size: 11px; color: #64748b; word-break: break-all; }

.empty-state { text-align: center; padding: 48px 24px; color: #94a3b8; }
.empty-state .big { font-size: 40px; margin-bottom: 8px; }

/* Responsive stats */
@media (max-width: 900px) {
  .stats { grid-template-columns: repeat(3, 1fr); }
}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <h1>📞 Wiom Retention Campaign</h1>
  <div class="header-right" id="hTime"></div>
</div>

<div class="main">

  <!-- Sidebar -->
  <div class="sidebar">
    <div class="nav-item active" onclick="goTo('upload')" id="n-upload">
      <span class="icon">📤</span> Upload & Call
    </div>
    <div class="nav-item" onclick="goTo('single')" id="n-single">
      <span class="icon">📱</span> Single Call
    </div>
    <div class="nav-sep"></div>
    <div class="nav-item" onclick="goTo('log')" id="n-log">
      <span class="icon">📊</span> Call Log
    </div>
    <div class="nav-item" onclick="goTo('callbacks')" id="n-callbacks">
      <span class="icon">🔔</span> Callbacks
      <span id="cbBadge" style="background:#ef4444;color:#fff;border-radius:20px;padding:1px 7px;font-size:10px;margin-left:auto;display:none">0</span>
    </div>
    <div class="nav-sep"></div>
    <div class="nav-item" onclick="goTo('sop')" id="n-sop">
      <span class="icon">📖</span> SOP Rules
    </div>
  </div>

  <!-- Content -->
  <div class="content">

    <!-- Stats -->
    <div class="stats">
      <div class="stat-card"><div class="num" id="sTotal">0</div><div class="lbl">Total</div></div>
      <div class="stat-card orange"><div class="num" id="sQueued">0</div><div class="lbl">Queued</div></div>
      <div class="stat-card"><div class="num" id="sDone">0</div><div class="lbl">Completed</div></div>
      <div class="stat-card green"><div class="num" id="sWillPay">0</div><div class="lbl">Will Recharge</div></div>
      <div class="stat-card purple"><div class="num" id="sCB">0</div><div class="lbl">Callbacks</div></div>
      <div class="stat-card red"><div class="num" id="sDont">0</div><div class="lbl">Don't Want</div></div>
    </div>

    <!-- ══════ UPLOAD & CALL ══════ -->
    <div id="p-upload">
      <div class="card">
        <div class="card-title">📤 Upload Customer Data & Start Calls</div>

        <div class="info-box">
          📋 CSV mein yeh columns chahiye:<br/>
          <code style="font-size:11px">recipient_phone_number, customer_name, expiry_date, days_remaining, agent_name</code><br/>
          <a href="/sample-csv" style="color:#2563eb;font-size:11px;text-decoration:none">⬇️ Sample CSV download karo</a>
        </div>

        <!-- Drop zone -->
        <div class="upload-zone" onclick="document.getElementById('csvFile').click()" id="dropZone">
          <input type="file" id="csvFile" accept=".csv" onchange="handleFile(event)"/>
          <div class="upload-icon">📁</div>
          <div class="upload-text" id="uploadText">CSV file yahan click karke select karo</div>
          <div class="upload-sub">Sirf .csv files — max 10,000 rows</div>
        </div>

        <!-- Preview -->
        <div id="previewSection" style="display:none">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <span style="font-size:13px;font-weight:700;color:#1e3a8a" id="previewCount"></span>
            <button class="btn btn-sm btn-outline" onclick="clearFile()">✕ Clear</button>
          </div>
          <div class="preview-wrap" id="previewWrap"></div>
        </div>

        <!-- Start button -->
        <div style="margin-top:20px;display:flex;gap:12px;align-items:center;flex-wrap:wrap" id="startSection" style="display:none">
          <button class="btn btn-success" onclick="startBatchCalls()" id="startBtn" style="font-size:15px;padding:12px 28px">
            🚀 Start Calls
          </button>
          <span style="font-size:12px;color:#64748b" id="startInfo"></span>
        </div>

        <!-- Progress -->
        <div id="batchProgress" style="display:none;margin-top:16px">
          <div style="font-size:12px;color:#64748b;margin-bottom:6px" id="progressText"></div>
          <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
        </div>

        <!-- Result -->
        <div class="result-panel" id="batchResult">
          <div style="font-weight:700;color:#166534;margin-bottom:6px">✅ Calls Started!</div>
          <div class="rid" id="batchResultText"></div>
          <button class="btn btn-sm btn-primary" style="margin-top:10px" onclick="goTo('log')">📊 Call Log dekho</button>
        </div>
      </div>
    </div>

    <!-- ══════ SINGLE CALL ══════ -->
    <div id="p-single" style="display:none">
      <div class="card">
        <div class="card-title">📱 Single Customer Call</div>
        <div class="form-grid">
          <div class="form-group">
            <label>Customer Name *</label>
            <input id="s_name" placeholder="e.g. Ramesh Kumar"/>
          </div>
          <div class="form-group">
            <label>Phone Number * (with +91)</label>
            <input id="s_phone" placeholder="+919876543210"/>
          </div>
          <div class="form-group">
            <label>Plan Expiry Date</label>
            <input type="date" id="s_expiry"/>
          </div>
          <div class="form-group">
            <label>Days Remaining</label>
            <input type="number" id="s_days" placeholder="e.g. 4" min="1" max="15"/>
          </div>
        </div>
        <div style="margin-top:18px;display:flex;gap:10px">
          <button class="btn btn-primary" onclick="triggerSingle()" style="font-size:14px;padding:11px 24px">
            📞 Call Karo
          </button>
          <button class="btn btn-outline" onclick="clearSingle()">Clear</button>
        </div>
        <div class="result-panel" id="singleResult">
          <div style="font-weight:700;color:#166534;margin-bottom:4px">✅ Call Queued!</div>
          <div class="rid" id="singleResultId"></div>
        </div>
      </div>
    </div>

    <!-- ══════ CALL LOG ══════ -->
    <div id="p-log" style="display:none">
      <div class="card">
        <div class="card-title">📊 Call Log</div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px;flex-wrap:wrap;gap:8px">
          <span style="font-size:12px;color:#64748b" id="logCount">Loading...</span>
          <div style="display:flex;gap:8px">
            <button class="btn btn-sm btn-outline" onclick="loadLog()">🔄 Refresh</button>
            <a href="/export-csv" class="btn btn-sm btn-success" style="text-decoration:none">⬇️ Export CSV</a>
          </div>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Name</th>
                <th>Phone</th>
                <th>Expiry</th>
                <th>Days</th>
                <th>Call Status</th>
                <th>Disposition</th>
                <th>VOC / Remarks</th>
                <th>Recording</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody id="logBody">
              <tr><td colspan="10"><div class="empty-state"><div class="big">📭</div>Koi call abhi nahi hua</div></td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══════ CALLBACKS ══════ -->
    <div id="p-callbacks" style="display:none">

      <div class="card">
        <div class="card-title">🔔 Schedule Follow-Up Call</div>
        <div class="form-grid">
          <div class="form-group">
            <label>Customer Name *</label>
            <input id="cb_name" placeholder="Ramesh Kumar"/>
          </div>
          <div class="form-group">
            <label>Phone *</label>
            <input id="cb_phone" placeholder="+919876543210"/>
          </div>
          <div class="form-group">
            <label>Expiry Date</label>
            <input type="date" id="cb_expiry"/>
          </div>
          <div class="form-group">
            <label>Days Remaining</label>
            <input type="number" id="cb_days" min="1" max="15"/>
          </div>
          <div class="form-group">
            <label>Callback Date & Time *</label>
            <input type="datetime-local" id="cb_dt"/>
          </div>
          <div class="form-group">
            <label>Reason / Notes</label>
            <input id="cb_reason" placeholder="e.g. Out of town, returning Sunday"/>
          </div>
        </div>
        <div style="margin-top:16px;display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-primary" onclick="schedCB()">🔔 Schedule</button>
          <button class="btn btn-sm btn-outline" onclick="quickTime('morning')">+ Kal 10am</button>
          <button class="btn btn-sm btn-outline" onclick="quickTime('evening')">+ Aaj 6pm</button>
          <button class="btn btn-sm btn-outline" onclick="quickTime('2days')">+ 2 Din Baad</button>
        </div>
      </div>

      <div class="card">
        <div class="card-title">📋 Pending Callbacks</div>
        <div style="display:flex;justify-content:space-between;margin-bottom:12px">
          <span style="font-size:12px;color:#64748b" id="cbCount">Loading...</span>
          <div style="display:flex;gap:8px">
            <button class="btn btn-sm btn-outline" onclick="loadCBs()">🔄 Refresh</button>
            <a href="/export-callbacks" class="btn btn-sm btn-success" style="text-decoration:none">⬇️ Export</a>
          </div>
        </div>
        <div class="tbl-wrap">
          <table>
            <thead><tr><th>#</th><th>Name</th><th>Phone</th><th>Scheduled</th><th>Reason</th><th>Status</th><th>Action</th></tr></thead>
            <tbody id="cbBody">
              <tr><td colspan="7"><div class="empty-state"><div class="big">🔔</div>Koi callback pending nahi</div></td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- ══════ SOP ══════ -->
    <div id="p-sop" style="display:none">
      <div class="card">
        <div class="card-title">📖 Campaign Rules — DO's & DON'Ts</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
          <div style="background:#f0fdf4;border:1px solid #86efac;border-radius:8px;padding:16px">
            <div style="font-weight:700;color:#166534;margin-bottom:10px">✅ DO's</div>
            <ul style="margin-left:16px;line-height:2;font-size:12px;color:#166534">
              <li>Script exactly follow karo</li>
              <li>Customer ka naam verify karo</li>
              <li>Exact expiry date batao</li>
              <li>Dono options equally present karo</li>
              <li>Disposition + VOC immediately log karo</li>
              <li>Callback schedule karo agar customer busy ho</li>
            </ul>
          </div>
          <div style="background:#fef2f2;border:1px solid #fca5a5;border-radius:8px;padding:16px">
            <div style="font-weight:700;color:#991b1b;margin-bottom:10px">❌ DON'Ts</div>
            <ul style="margin-left:16px;line-height:2;font-size:12px;color:#991b1b">
              <li>Koi discount / offer mat dena</li>
              <li>Negotiate mat karo</li>
              <li>Pressure mat daalo</li>
              <li>"Last chance", "device jayega" mat kaho</li>
              <li>Recharge duration mat batao (pata nahi)</li>
              <li>Galat disposition mat lagao</li>
            </ul>
          </div>
        </div>

        <div class="card-title">🏷️ Disposition Guide</div>
        <table>
          <thead><tr><th>Disposition</th><th>Kab use karein</th><th>Next Action</th></tr></thead>
          <tbody>
            <tr><td><span class="badge b-pay">Will Recharge Today</span></td><td>Customer ne aaj karne ki baat ki</td><td>24 hrs mein verify karo</td></tr>
            <tr><td><span class="badge b-later">Will Recharge Later</span></td><td>Interested hai, abhi nahi</td><td>Window ke andar follow up karo</td></tr>
            <tr><td><span class="badge b-done">Already Recharged</span></td><td>Pehle se recharge kar liya</td><td>CRM update karo</td></tr>
            <tr><td><span class="badge b-oot">Out of Town</span></td><td>Customer bahar hai</td><td>Return date pe callback karo</td></tr>
            <tr><td><span class="badge b-return">Wants Device Return</span></td><td>Service nahi chahiye</td><td>VOC reason note karo</td></tr>
            <tr><td><span class="badge b-dont">Don't Want – Service Issue</span></td><td>Technical/slow internet</td><td>VOC log karo, TL escalate</td></tr>
            <tr><td><span class="badge b-dont">Don't Want – Personal</span></td><td>Shifted, switched etc</td><td>VOC note karo</td></tr>
            <tr><td><span class="badge b-cb">Callback Scheduled</span></td><td>Busy tha, time maanga</td><td>Schedule karo</td></tr>
            <tr><td><span class="badge b-na">Not Answered / Busy</span></td><td>No pickup / switched off</td><td>Retry — max 3x day</td></tr>
          </tbody>
        </table>

        <div class="card-title" style="margin-top:20px">💬 VOC — Common Reasons to Capture</div>
        <table>
          <thead><tr><th>Category</th><th>Sub-Reasons (note customer ke exact words)</th></tr></thead>
          <tbody>
            <tr><td>Service Issue</td><td>Internet slow, frequently disconnects, no signal, router problem</td></tr>
            <tr><td>Personal</td><td>Shifted to new house, moved city, using mobile data only, financial reason</td></tr>
            <tr><td>Price</td><td>Plan too expensive, competitor cheaper, not worth it</td></tr>
            <tr><td>Usage</td><td>Don't use internet, children gone, work from office now</td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->

<div class="toast" id="toast"></div>

<script>
// ─── Navigation ───────────────────────────────────────────────────────────────
const pages = ['upload','single','log','callbacks','sop'];

function goTo(pg) {
  pages.forEach(p => {
    document.getElementById('p-'+p).style.display = p===pg ? 'block' : 'none';
    document.getElementById('n-'+p).classList.toggle('active', p===pg);
  });
  if (pg==='log')       loadLog();
  if (pg==='callbacks') loadCBs();
  if (pg==='upload')    refreshStats();
}

// ─── Toast ────────────────────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = ok ? '#059669' : '#dc2626';
  t.style.display = 'block';
  setTimeout(() => t.style.display='none', 4000);
}

// ─── Clock ────────────────────────────────────────────────────────────────────
function tick() {
  document.getElementById('hTime').textContent =
    new Date().toLocaleTimeString('en-IN', {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
setInterval(tick, 1000); tick();

// ─── File Upload ──────────────────────────────────────────────────────────────
let uploadedRows = [];

function handleFile(e) {
  const f = e.target.files[0]; if (!f) return;
  document.getElementById('uploadText').textContent = '✅ ' + f.name;
  const reader = new FileReader();
  reader.onload = ev => {
    const lines = ev.target.result.trim().split('\n');
    const headers = lines[0].split(',').map(h=>h.trim());
    uploadedRows = lines.slice(1).filter(l=>l.trim()).map(l=>{
      const vals = l.split(',');
      const obj = {};
      headers.forEach((h,i) => obj[h] = (vals[i]||'').trim());
      return obj;
    });

    // Preview
    const count = uploadedRows.length;
    document.getElementById('previewCount').textContent = `${count} customers loaded`;
    document.getElementById('startInfo').textContent = `${count} calls trigger honge`;
    document.getElementById('startSection').style.display = 'flex';

    // Build preview table
    let html = '<table><thead><tr>' + headers.map(h=>`<th>${h}</th>`).join('') + '</tr></thead><tbody>';
    uploadedRows.slice(0,10).forEach(r => {
      html += '<tr>' + headers.map(h=>`<td>${r[h]||''}</td>`).join('') + '</tr>';
    });
    if (uploadedRows.length > 10) html += `<tr><td colspan="${headers.length}" style="text-align:center;color:#94a3b8;padding:8px">... aur ${uploadedRows.length-10} rows</td></tr>`;
    html += '</tbody></table>';
    document.getElementById('previewWrap').innerHTML = html;
    document.getElementById('previewSection').style.display = 'block';
  };
  reader.readAsText(f);
}

function clearFile() {
  document.getElementById('csvFile').value = '';
  document.getElementById('uploadText').textContent = 'CSV file yahan click karke select karo';
  document.getElementById('previewSection').style.display = 'none';
  document.getElementById('startSection').style.display = 'none';
  document.getElementById('batchResult').style.display = 'none';
  uploadedRows = [];
}

async function startBatchCalls() {
  const f = document.getElementById('csvFile').files[0];
  if (!f || uploadedRows.length === 0) { toast('Pehle CSV file select karo!', false); return; }

  const btn = document.getElementById('startBtn');
  btn.disabled = true; btn.textContent = '⏳ Starting...';

  document.getElementById('batchProgress').style.display = 'block';
  document.getElementById('progressText').textContent = 'Calls queue ho rahe hain...';
  document.getElementById('progressFill').style.width = '30%';

  const fd = new FormData();
  fd.append('file', f);

  try {
    const r = await fetch('/api/call/batch', {method:'POST', body:fd});
    const d = await r.json();

    document.getElementById('progressFill').style.width = '100%';

    if (d.success) {
      document.getElementById('progressText').textContent = `✅ ${d.total} calls queued successfully!`;
      document.getElementById('batchResult').style.display = 'block';
      document.getElementById('batchResultText').textContent =
        `Batch ID: ${d.batch_id || '—'} | Total: ${d.total} calls`;
      toast(`✅ ${d.total} calls started!`);
      refreshStats();
    } else {
      document.getElementById('progressText').textContent = 'Error: ' + d.error;
      toast('❌ ' + d.error, false);
    }
  } catch(err) {
    toast('❌ Network error: ' + err.message, false);
  }

  btn.disabled = false; btn.textContent = '🚀 Start Calls';
}

// ─── Single Call ──────────────────────────────────────────────────────────────
async function triggerSingle() {
  const name  = document.getElementById('s_name').value.trim();
  const phone = document.getElementById('s_phone').value.trim();
  const expiry = document.getElementById('s_expiry').value;
  const days   = document.getElementById('s_days').value;

  if (!name || !phone) { toast('Name aur phone zaroori hai!', false); return; }

  try {
    const r = await fetch('/api/call/single', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, phone, expiry, days})
    });
    const d = await r.json();
    if (d.success) {
      document.getElementById('singleResult').style.display = 'block';
      document.getElementById('singleResultId').textContent = 'Execution ID: ' + d.execution_id;
      toast(`✅ Call queued for ${name}!`);
      refreshStats();
    } else {
      toast('❌ ' + d.error, false);
    }
  } catch(err) { toast('❌ ' + err.message, false); }
}

function clearSingle() {
  ['s_name','s_phone','s_expiry','s_days'].forEach(id => document.getElementById(id).value='');
  document.getElementById('singleResult').style.display = 'none';
}

// ─── Call Log ─────────────────────────────────────────────────────────────────
const BADGE_MAP = {
  'queued':'b-queued', 'completed':'b-done', 'failed':'b-failed',
  'Will Recharge Today':'b-pay', 'Will Recharge Later':'b-later',
  'Already Recharged':'b-done', 'Out of Town':'b-oot',
  'Wants Device Return':'b-return', 'Device Already Returned':'b-return',
  "Don't Want – Service Issue":'b-dont', "Don't Want – Personal Reason":'b-dont',
  'Callback Scheduled':'b-cb', 'Not Answered / Busy':'b-na',
  'Wrong Number':'b-na', 'Pending':'b-pending'
};

const DISPOSITIONS = [
  "Pending","Will Recharge Today","Will Recharge Later","Already Recharged",
  "Out of Town","Wants Device Return","Device Already Returned",
  "Don't Want – Service Issue","Don't Want – Personal Reason",
  "Callback Scheduled","Not Answered / Busy","Wrong Number"
];

async function loadLog() {
  const r = await fetch('/api/log');
  const data = await r.json();
  document.getElementById('logCount').textContent = data.length + ' records';
  refreshStats(data);

  const tbody = document.getElementById('logBody');
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="10"><div class="empty-state"><div class="big">📭</div>Koi call abhi nahi hua</div></td></tr>';
    return;
  }

  tbody.innerHTML = [...data].reverse().map((c, i) => {
    const idx = data.length - i;
    const execId = c.execution_id || '';

    // Disposition dropdown
    const dispOpts = DISPOSITIONS.map(d =>
      `<option value="${d}" ${c.disposition===d?'selected':''}>${d}</option>`
    ).join('');
    const dispCell = `<select class="disp-select" id="disp_${idx}" onchange="saveRow(${idx-1},'${execId}')">${dispOpts}</select>`;

    // VOC input
    const vocCell = `<input class="voc-input" id="voc_${idx}" placeholder="Customer ne kya kaha..." value="${(c.voc||'').replace(/"/g,"'")}" onblur="saveRow(${idx-1},'${execId}')"/>`;

    // Recording
    let recCell = '—';
    if (c.recording_url) {
      recCell = `<audio controls style="height:26px;width:150px"><source src="${c.recording_url}" type="audio/mpeg"></audio>`;
    } else if (execId) {
      recCell = `<button class="btn btn-sm btn-outline" onclick="fetchRec('${execId}',${idx-1})" style="font-size:10px">Fetch 🎙️</button>`;
    }

    return `<tr id="row_${idx}">
      <td style="color:#94a3b8;font-size:11px">${idx}</td>
      <td><b>${c.name}</b></td>
      <td style="font-size:11px;color:#64748b">${c.phone}</td>
      <td style="font-size:11px">${c.expiry||'—'}</td>
      <td style="text-align:center">${c.days||'—'}</td>
      <td><span class="badge ${BADGE_MAP[c.status]||'b-pending'}">${c.status||'queued'}</span></td>
      <td>${dispCell}</td>
      <td>${vocCell}</td>
      <td>${recCell}</td>
      <td style="font-size:11px;color:#94a3b8;white-space:nowrap">${c.time}</td>
    </tr>`;
  }).join('');
}

async function saveRow(realIdx, execId) {
  const dispIdx = realIdx + 1;
  const displayIdx = document.querySelectorAll('#logBody tr').length - realIdx;

  // We need the actual index in reverse — find by execution_id
  const disp = document.getElementById(`disp_${displayIdx}`)?.value || '';
  const voc  = document.getElementById(`voc_${displayIdx}`)?.value  || '';

  await fetch('/api/update-disposition', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({execution_id: execId, disposition: disp, voc})
  });
}

async function fetchRec(execId, idx) {
  const r = await fetch(`/api/fetch-recording/${execId}`);
  const d = await r.json();
  if (d.recording_url) {
    toast('Recording mil gayi!');
    loadLog();
  } else {
    toast('Recording abhi available nahi — ' + (d.error||'check after call ends'), false);
  }
}

// ─── Stats ────────────────────────────────────────────────────────────────────
async function refreshStats(data) {
  if (!data) { const r = await fetch('/api/log'); data = await r.json(); }
  document.getElementById('sTotal').textContent   = data.length;
  document.getElementById('sQueued').textContent  = data.filter(c=>c.status==='queued').length;
  document.getElementById('sDone').textContent    = data.filter(c=>c.status==='completed').length;
  document.getElementById('sWillPay').textContent = data.filter(c=>['Will Recharge Today','Will Recharge Later','Already Recharged'].includes(c.disposition)).length;
  document.getElementById('sCB').textContent      = data.filter(c=>c.disposition==='Callback Scheduled').length;
  document.getElementById('sDont').textContent    = data.filter(c=>(c.disposition||'').startsWith("Don't Want")).length;

  const cbPend = data.filter(c=>c.disposition==='Callback Scheduled').length;
  const badge = document.getElementById('cbBadge');
  badge.textContent = cbPend;
  badge.style.display = cbPend > 0 ? 'inline' : 'none';
}

// ─── Callbacks ────────────────────────────────────────────────────────────────
function quickTime(when) {
  const now = new Date(); const dt = new Date();
  if (when==='morning') { dt.setDate(dt.getDate()+1); dt.setHours(10,0,0,0); }
  else if (when==='evening') { dt.setHours(18,0,0,0); if(dt<now) dt.setDate(dt.getDate()+1); }
  else if (when==='2days') { dt.setDate(dt.getDate()+2); dt.setHours(10,0,0,0); }
  const p=n=>String(n).padStart(2,'0');
  document.getElementById('cb_dt').value = `${dt.getFullYear()}-${p(dt.getMonth()+1)}-${p(dt.getDate())}T${p(dt.getHours())}:${p(dt.getMinutes())}`;
}

async function schedCB() {
  const name = document.getElementById('cb_name').value.trim();
  const phone = document.getElementById('cb_phone').value.trim();
  const dt = document.getElementById('cb_dt').value;
  if (!name || !phone || !dt) { toast('Name, phone aur time zaroori hai!', false); return; }

  const r = await fetch('/api/callback/schedule', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({
      name, phone,
      expiry: document.getElementById('cb_expiry').value,
      days:   document.getElementById('cb_days').value,
      reason: document.getElementById('cb_reason').value.trim(),
      scheduled_at: dt
    })
  });
  const d = await r.json();
  if (d.success) {
    toast('✅ Callback scheduled!');
    loadCBs();
    ['cb_name','cb_phone','cb_expiry','cb_days','cb_dt','cb_reason'].forEach(id=>document.getElementById(id).value='');
  } else {
    toast('❌ ' + d.error, false);
  }
}

async function loadCBs() {
  const r = await fetch('/api/callbacks');
  const data = await r.json();
  const pending = data.filter(c=>c.status==='pending').length;
  document.getElementById('cbCount').textContent = pending + ' pending';
  const badge = document.getElementById('cbBadge');
  badge.textContent = pending; badge.style.display = pending>0?'inline':'none';

  const tbody = document.getElementById('cbBody');
  if (!data.length) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty-state"><div class="big">🔔</div>Koi callback pending nahi</div></td></tr>';
    return;
  }
  const now = new Date();
  tbody.innerHTML = [...data].reverse().map((c,i)=>{
    const dt = new Date(c.scheduled_at);
    const overdue = dt < now && c.status==='pending';
    const timeStr = dt.toLocaleString('en-IN',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'});
    return `<tr style="${overdue?'background:#fff7ed':''}">
      <td style="color:#94a3b8">${data.length-i}</td>
      <td><b>${c.name}</b></td>
      <td style="font-size:11px">${c.phone}</td>
      <td style="${overdue?'color:#dc2626;font-weight:700':''}">
        ${overdue?'⚠️ ':''}${timeStr}
      </td>
      <td style="font-size:11px;color:#64748b">${c.reason||'—'}</td>
      <td><span class="badge ${c.status==='done'?'b-done':overdue?'b-dont':'b-cb'}">${c.status==='done'?'Done':overdue?'Overdue':'Pending'}</span></td>
      <td>
        <button class="btn btn-sm btn-primary" onclick="callNow('${c.phone}','${c.name}','${c.expiry||''}','${c.days||''}')">
          📞 Call Now
        </button>
      </td>
    </tr>`;
  }).join('');
}

async function callNow(phone, name, expiry, days) {
  const r = await fetch('/api/call/single', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({name, phone, expiry, days})
  });
  const d = await r.json();
  if (d.success) toast(`✅ Call triggered for ${name}!`);
  else toast('❌ ' + d.error, false);
  loadCBs();
}

// ─── Auto refresh ─────────────────────────────────────────────────────────────
setInterval(refreshStats, 30000);
refreshStats();
</script>
</body>
</html>
"""

# ─── API Routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/call/single", methods=["POST"])
def single_call():
    d = request.json
    raw_expiry     = d.get("expiry_date") or d.get("expiry", "")
    expiry_date    = format_expiry_date(raw_expiry)   # e.g. "terah June"
    days_remaining = d.get("days_remaining") or d.get("days", "")

    variables = {
        "customer_name":  d["name"],
        "expiry_date":    expiry_date,
        "days_remaining": str(days_remaining),
        "agent_name":     d.get("agent", "Jyoti"),
    }
    payload = {
        "agent_id": AGENT_ID,
        "recipient_phone_number": d["phone"],
        "from_phone_number": FROM_NUM,
        "user_data": variables,
        "variables": variables,
    }

    try:
        resp = req.post(f"{BASE_URL}/call", headers=HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        result  = resp.json()
        exec_id = result.get("execution_id", "")

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
        return jsonify({"success": True, "execution_id": exec_id})
    except Exception as e:
        # Return full Bolna error response for debugging
        try:
            bolna_error = resp.json()
        except:
            bolna_error = {}
        return jsonify({"success": False, "error": str(e), "bolna_response": bolna_error, "payload_sent": payload}), 400


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

    try:
        webhook_url = os.getenv("WEBHOOK_URL", "https://retentionwiom-production.up.railway.app/webhook")
        resp = req.post(
            f"{BASE_URL}/batches",
            headers={"Authorization": f"Bearer {API_KEY}"},
            data={
                "agent_id":           AGENT_ID,
                "from_phone_numbers": json.dumps([]),
                "webhook_url":        webhook_url,
                "retry_config": json.dumps({
                    "enabled": True,
                    "max_retries": 3,
                    "retry_on_statuses": ["no-answer", "busy"],
                    "retry_intervals_minutes": [120, 120, 120],
                }),
            },
            files={"file": (file.filename, modified_csv.encode(), "text/csv")},
            timeout=60,
        )
        resp.raise_for_status()
        result   = resp.json()
        batch_id = result.get("batch_id", "")

        for row in rows:
            call_log.append({
                "name":          row.get("customer_name", ""),
                "phone":         row.get("recipient_phone_number", ""),
                "expiry":        format_expiry_date(row.get("expiry_date", "")),
                "days":          row.get("days_remaining", ""),
                "status":        "queued",
                "disposition":   "Pending",
                "voc":           "",
                "recording_url": "",
                "execution_id":  batch_id,
                "time":          datetime.now().strftime("%d %b %H:%M"),
            })

        return jsonify({"success": True, "batch_id": batch_id, "total": len(rows)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/api/log")
def get_log():
    return jsonify(call_log)


@app.route("/api/update-disposition", methods=["POST"])
def update_disposition():
    d = request.json
    for entry in call_log:
        if entry.get("execution_id") == d.get("execution_id"):
            entry["disposition"] = d.get("disposition", "")
            entry["voc"]         = d.get("voc", "")
            break
    return jsonify({"success": True})


@app.route("/api/fetch-recording/<exec_id>")
def fetch_recording(exec_id):
    try:
        r    = req.get(f"{BASE_URL}/call/logs/{exec_id}", headers=HEADERS, timeout=10)
        data = r.json()
        recording = (
            data.get("recording_url") or data.get("audio_url") or
            data.get("recordingUrl")  or data.get("record_url") or ""
        )
        for entry in call_log:
            if entry.get("execution_id") == exec_id:
                if recording: entry["recording_url"] = recording
                if data.get("status"): entry["status"] = data["status"]
                break
        return jsonify({"success": True, "recording_url": recording, "raw": data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/webhook", methods=["POST"])
def webhook():
    data      = request.json or {}
    exec_id   = data.get("execution_id")
    status    = data.get("status", "completed")
    recording = data.get("recording_url") or data.get("audio_url") or data.get("recordingUrl") or ""

    # Extract transcript — handle both list and string formats
    transcript = data.get("transcript", [])
    turns = []
    if isinstance(transcript, list):
        for t in transcript:
            role = t.get("role","")
            text = t.get("content","") or t.get("text","")
            turns.append({"role": role, "text": text})
        full_text    = " ".join(t["text"] for t in turns).lower()
        customer_txt = " ".join(t["text"] for t in turns if t["role"] in ("user","human","customer")).lower()
    else:
        full_text    = str(transcript).lower()
        customer_txt = full_text

    # ── Auto-Disposition Logic (priority order) ─────────────────────────────
    disposition = "Pending"
    voc         = ""
    cb_needed   = False

    # 1. Already recharged
    if any(w in customer_txt for w in ["pehle se kar", "already", "ho gaya", "kar liya", "kara liya", "recharge ho"]):
        disposition = "Already Recharged"
        voc = "Customer ne bataya ki recharge pehle se ho gaya hai"

    # 2. Will recharge today
    elif any(w in customer_txt for w in ["aaj kar", "abhi karta", "kar deta", "kar deti", "aaj karwa", "haan karunga", "haan karungi", "theek hai karunga"]):
        disposition = "Will Recharge Today"
        voc = "Customer ne aaj recharge karne ki baat ki"

    # 3. Will recharge later
    elif any(w in customer_txt for w in ["kal kar", "parso", "baad mein kar", "karenge", "dekh lete", "soch ke"]):
        disposition = "Will Recharge Later"
        voc = "Customer ne baad mein recharge karne ki baat ki"

    # 4. Device already returned
    elif any(w in customer_txt for w in ["wapas kar diya", "de diya", "return kar diya", "le gaye", "wapas de"]):
        disposition = "Device Already Returned"
        voc = "Customer ne bataya device pehle se wapas kar diya"

    # 5. Wants device return — service issue
    elif any(w in customer_txt for w in ["slow", "problem", "issue", "kaam nahi", "nahi chala", "bahut slow", "signal nahi", "connection nahi"]):
        disposition = "Don't Want – Service Issue"
        # Extract VOC — what exactly was the problem
        if "slow" in customer_txt: voc = "Service issue: internet slow tha"
        elif "signal" in customer_txt: voc = "Service issue: signal nahi tha"
        elif "kaam nahi" in customer_txt or "nahi chala" in customer_txt: voc = "Service issue: internet kaam nahi kiya"
        else: voc = "Service issue (details call mein)"
        cb_needed = False

    # 6. Wants device return — personal reason
    elif any(w in customer_txt for w in ["nahi chahiye", "band karo", "wapas karna", "return karna", "lelo", "le lo device", "nahi lena", "shifted", "shift ho", "chale gaye"]):
        disposition = "Wants Device Return"
        if "shift" in customer_txt: voc = "Personal: ghar shift ho gaya"
        elif "nahi chahiye" in customer_txt: voc = "Personal: service nahi chahiye"
        else: voc = "Device return chahiye (reason: call mein)"

    # 7. Out of town
    elif any(w in customer_txt for w in ["gaon", "bahar", "gaya hoon", "travel", "bahar hoon", "sheher se", "wapas aaunga", "wapas aaungi"]):
        disposition = "Out of Town"
        voc = "Customer abhi bahar hai"
        cb_needed = True

    # 8. Callback requested
    elif any(w in customer_txt for w in ["busy", "abhi nahi", "baad mein call", "kal call", "time nahi", "thodi der", "bad me"]):
        disposition = "Callback Scheduled"
        voc = "Customer busy tha — callback chahiye"
        cb_needed = True

    # 9. Not answered / no response
    elif status in ("no-answer", "busy", "failed", "not-answered"):
        disposition = "Not Answered / Busy"
        voc = ""

    # ── Update call log entry ────────────────────────────────────────────────
    original = None
    for entry in call_log:
        if entry.get("execution_id") == exec_id:
            entry["status"]      = status
            if disposition != "Pending":
                entry["disposition"] = disposition
            if voc:
                entry["voc"] = voc
            if recording:
                entry["recording_url"] = recording
            original = entry
            break

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
                "days_remaining": str(d.get("days","")),
                "agent_name":     "Jyoti",
            },
            "variables": {
                "customer_name":  d["name"],
                "expiry_date":    d.get("expiry",""),
                "days_remaining": str(d.get("days","")),
                "agent_name":     "Jyoti",
            },
            "retry_config": {
                "enabled": True, "max_retries": 2,
                "retry_on_statuses": ["no-answer","busy"],
                "retry_intervals_minutes": [60,120],
            },
        }
        if scheduled_at: payload["scheduled_at"] = scheduled_at

        resp = req.post(f"{BASE_URL}/call", headers=HEADERS, json=payload, timeout=30)
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
    writer = csv.DictWriter(output, fieldnames=[
        "name","phone","expiry","days","status","disposition","voc","execution_id","time"
    ])
    writer.writeheader(); writer.writerows(call_log)
    return Response(output.getvalue(), mimetype="text/csv",
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    is_railway = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PROJECT_ID")

    if is_railway:
        print(f"\n✅ Running on Railway — port {port}")
        print("Webhook URL: https://YOUR-APP.up.railway.app/webhook")
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
