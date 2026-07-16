"""
Wiom Retention Campaign — Daily Auto-Call Automation
- 10:00 AM: Pull R11 list from Metabase → trigger Bolna batch
- After 2 hours: re-dial no-answer / short calls
- Dispositions written back to Google Sheet via Apps Script
"""

import os, json, time, logging, threading
from datetime import datetime, timedelta, date
import requests as req
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
METABASE_URL     = os.getenv("METABASE_URL",     "https://metabase.wiom.in")
METABASE_API_KEY = os.getenv("METABASE_API_KEY", "mb_4AzXyjQzXTX+KXN4eEycVnZJpFn95h6IVGaRAZb1NIk=")
METABASE_DB_ID   = int(os.getenv("METABASE_DB_ID", "1"))   # Snowflake DB id in Metabase

BOLNA_API_KEY = os.getenv("BOLNA_API_KEY", "")
BOLNA_AGENT_ID = os.getenv("BOLNA_AGENT_ID", "")
BOLNA_BASE    = "https://api.bolna.ai"

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://retentionwiom-production.up.railway.app/webhook")

# Google Apps Script endpoint for writing dispositions to sheet
GSHEET_SCRIPT_URL = os.getenv(
    "GSHEET_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbxU5V444Xw4NYsdclVx-lyF83qdHIMEXhvue5TP-CAcrEjUc9D1J_d0N1p4Dxl9lXT7/exec"
)

BOLNA_HEADERS = {"Authorization": f"Bearer {BOLNA_API_KEY}", "Content-Type": "application/json"}

# ── R11 SQL — dynamically uses today - 11 days ────────────────────────────────
R11_SQL = """
WITH test_lcos AS (
    SELECT DISTINCT LCO_ACCOUNT_ID
    FROM PROD_DB.PUBLIC.TEST_LCO_ACCOUNT_ID
),
current_active AS (
    SELECT
        t.ROUTER_NAS_ID,
        t.MOBILE,
        t.SELECTED_PLAN_ID,
        t.TRANSACTION_ID,
        t.CREATED_BY,
        t.charges,
        DATE(DATEADD('minute', 330, t.OTP_EXPIRY_TIME))  AS expiry_date,
        DATEADD('minute', 330, t.OTP_EXPIRY_TIME)        AS plan_expiry_time,
        DATE(DATEADD('minute', 330, t.CREATED_ON))       AS created_date
    FROM PROD_DB.PUBLIC.T_ROUTER_USER_MAPPING t
    WHERE t.OTP = 'DONE'
      AND t.DEVICE_LIMIT = 10
      AND t.MOBILE > '5999999999'
      AND t.CREATED_BY NOT IN (SELECT LCO_ACCOUNT_ID FROM test_lcos)
      AND DATE(DATEADD('minute', 330, t.CREATED_ON)) <= CURRENT_DATE()
      AND DATEADD('day', 15, DATE(DATEADD('minute', 330, t.OTP_EXPIRY_TIME))) >= CURRENT_DATE()
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY t.ROUTER_NAS_ID
        ORDER BY DATE(DATEADD('minute', 330, t.CREATED_ON)) DESC
    ) = 1
),
wg_customers AS (
    SELECT NASID, DEVICE_ID, NAME
    FROM PROD_DB.PUBLIC.T_WG_CUSTOMER
    WHERE _FIVETRAN_DELETED = FALSE
    QUALIFY ROW_NUMBER() OVER (PARTITION BY NASID ORDER BY ADDED_TIME DESC NULLS LAST) = 1
),
hierarchy AS (
    SELECT PARTNER_ACCOUNT_ID PARTNER_ID, PARTNER_NAME AS lco_name, ZONE
    FROM PROD_DB.PUBLIC.HIERARCHY_BASE
    WHERE DEDUP_FLAG = 1
),
recharge_counts AS (
    SELECT ROUTER_NAS_ID, COUNT(*) AS nmbr_recharge
    FROM PROD_DB.PUBLIC.T_ROUTER_USER_MAPPING
    WHERE OTP = 'DONE' AND DEVICE_LIMIT = 10 AND MOBILE > '5999999999'
      AND CREATED_BY NOT IN (SELECT LCO_ACCOUNT_ID FROM test_lcos)
    GROUP BY ROUTER_NAS_ID
),
old_nas AS (
    SELECT DISTINCT ROUTER_NAS_ID
    FROM PROD_DB.PUBLIC.T_ROUTER_USER_MAPPING
    WHERE OTP = 'DONE' AND DEVICE_LIMIT = 10 AND MOBILE > '5999999999'
      AND CREATED_BY NOT IN (SELECT LCO_ACCOUNT_ID FROM test_lcos)
      AND DATE(DATEADD('minute', 330, OTP_ISSUED_TIME)) < '2026-01-26'
),
migration_dates AS (
    SELECT t.ROUTER_NAS_ID, MIN(DATE(DATEADD('minute', 330, t.CREATED_ON))) AS first_migration_date
    FROM PROD_DB.PUBLIC.T_ROUTER_USER_MAPPING t
    JOIN PROD_DB.PUBLIC.T_PLAN_CONFIGURATION pc ON t.SELECTED_PLAN_ID = pc.ID
    WHERE t.OTP = 'DONE' AND t.DEVICE_LIMIT = 10 AND t.MOBILE > '5999999999'
      AND t.CREATED_BY NOT IN (SELECT LCO_ACCOUNT_ID FROM test_lcos)
      AND pc.COMBINED_SETTING_ID = 22
    GROUP BY t.ROUTER_NAS_ID
)
SELECT
    ca.ROUTER_NAS_ID  AS router_nasid,
    wc.DEVICE_ID,
    wc.NAME           AS customer_name,
    h.lco_name,
    h.ZONE,
    ca.MOBILE,
    ca.plan_expiry_time,
    ca.expiry_date    AS plan_expired_on,
    CASE
        WHEN o.ROUTER_NAS_ID IS NULL               THEN 'Pay G'
        WHEN md.first_migration_date IS NOT NULL
         AND md.first_migration_date <= CURRENT_DATE() THEN 'Migrated'
        ELSE 'Legacy'
    END AS customer_type,
    pc.SPEED_LIMIT_MBPS AS plan_speed,
    rc.nmbr_recharge,
    ca.charges
FROM current_active ca
JOIN PROD_DB.PUBLIC.T_PLAN_CONFIGURATION pc ON ca.SELECTED_PLAN_ID = pc.ID
LEFT JOIN wg_customers wc  ON ca.ROUTER_NAS_ID = wc.NASID
LEFT JOIN hierarchy h      ON ca.CREATED_BY = h.PARTNER_ID
LEFT JOIN old_nas o        ON ca.ROUTER_NAS_ID = o.ROUTER_NAS_ID
LEFT JOIN migration_dates md ON ca.ROUTER_NAS_ID = md.ROUTER_NAS_ID
LEFT JOIN recharge_counts rc ON ca.ROUTER_NAS_ID = rc.ROUTER_NAS_ID
WHERE plan_expired_on = '{target_date}'
ORDER BY ca.expiry_date
"""

# ── In-memory batch tracker ───────────────────────────────────────────────────
# {date_str: {"batch_id": ..., "calls": [...], "triggered_at": ..., "redial_done": bool}}
daily_batches = {}


# ── Metabase API ──────────────────────────────────────────────────────────────
def fetch_r11_from_metabase(target_date: str):
    """Run R11 SQL via Metabase API and return list of customer dicts."""
    sql = R11_SQL.format(target_date=target_date)
    url = f"{METABASE_URL}/api/dataset"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": METABASE_API_KEY,
    }
    payload = {
        "database": METABASE_DB_ID,
        "type": "native",
        "native": {"query": sql},
    }
    log.info(f"Fetching R11 list from Metabase for date: {target_date}")
    r = req.post(url, headers=headers, json=payload, timeout=120)
    r.raise_for_status()
    data = r.json()

    cols = [c["name"].lower() for c in data["data"]["cols"]]
    rows = data["data"]["rows"]
    customers = [dict(zip(cols, row)) for row in rows]
    log.info(f"Fetched {len(customers)} customers for {target_date}")
    return customers


# ── Hindi date/number formatting ──────────────────────────────────────────────
HINDI_DAYS = {
    1:"पहली",2:"दो",3:"तीन",4:"चार",5:"पाँच",6:"छह",7:"सात",8:"आठ",
    9:"नौ",10:"दस",11:"ग्यारह",12:"बारह",13:"तेरह",14:"चौदह",15:"पंद्रह",
    16:"सोलह",17:"सत्रह",18:"अठारह",19:"उन्नीस",20:"बीस",21:"इक्कीस",
    22:"बाईस",23:"तेईस",24:"चौबीस",25:"पच्चीस",26:"छब्बीस",27:"सत्ताईस",
    28:"अट्ठाईस",29:"उनतीस",30:"तीस",31:"इकतीस"
}
HINDI_MONTHS = {
    1:"January",2:"February",3:"March",4:"April",5:"May",6:"June",
    7:"July",8:"August",9:"September",10:"October",11:"November",12:"December"
}
HINDI_NUMBERS = {
    0:"शून्य",1:"एक",2:"दो",3:"तीन",4:"चार",5:"पाँच",6:"छह",7:"सात",
    8:"आठ",9:"नौ",10:"दस",11:"ग्यारह",12:"बारह",13:"तेरह",14:"चौदह",
    15:"पंद्रह",16:"सोलह",17:"सत्रह",18:"अठारह",19:"उन्नीस",20:"बीस",
    21:"इक्कीस",22:"बाईस",23:"तेईस",24:"चौबीस",25:"पच्चीस",26:"छब्बीस",
    27:"सत्ताईस",28:"अट्ठाईस",29:"उनतीस",30:"तीस"
}

def hindi_date(d):
    s = str(d).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(s[:10], fmt)
            return f"{HINDI_DAYS.get(dt.day, str(dt.day))} {HINDI_MONTHS.get(dt.month, '')}"
        except:
            continue
    return s

def hindi_days(n):
    try:
        return HINDI_NUMBERS.get(int(n), str(n))
    except:
        return str(n)


# ── Bolna batch trigger ───────────────────────────────────────────────────────
def trigger_bolna_calls(customers, batch_label="auto"):
    """Fire individual Bolna calls for each customer. Returns list of call records."""
    records = []
    today = date.today()
    for c in customers:
        expiry_raw  = str(c.get("plan_expired_on") or c.get("expiry_date") or "")
        expiry_fmt  = hindi_date(expiry_raw)

        # days remaining = 15 - (today - expiry_date)
        try:
            exp_dt = datetime.strptime(expiry_raw[:10], "%Y-%m-%d").date()
            days_left = 15 - (today - exp_dt).days
            days_left = max(0, days_left)
        except:
            days_left = 4

        phone = str(c.get("mobile") or "").strip()
        if not phone.startswith("+"):
            phone = "+91" + phone

        name = str(c.get("customer_name") or "Customer").strip() or "Customer"

        variables = {
            "customer_name":  name,
            "expiry_date":    expiry_fmt,
            "days_remaining": hindi_days(days_left),
            "agent_name":     "Jyoti",
        }
        payload = {
            "agent_id": BOLNA_AGENT_ID,
            "recipient_phone_number": phone,
            "user_data": variables,
            "variables": variables,
            "webhook_url": WEBHOOK_URL,
        }
        try:
            r = req.post(f"{BOLNA_BASE}/call", headers=BOLNA_HEADERS, json=payload, timeout=30)
            r.raise_for_status()
            exec_id = r.json().get("execution_id") or r.json().get("id") or ""
            records.append({
                "execution_id": exec_id,
                "phone": phone,
                "name": name,
                "expiry": expiry_fmt,
                "days": str(days_left),
                "nasid": c.get("router_nasid",""),
                "zone": c.get("zone",""),
                "lco": c.get("lco_name",""),
                "customer_type": c.get("customer_type",""),
                "status": "queued",
                "disposition": "Pending",
                "call_time": datetime.now().strftime("%d %b %H:%M"),
                "batch_label": batch_label,
            })
            log.info(f"Call triggered: {phone} → exec_id={exec_id}")
        except Exception as e:
            log.error(f"Call failed for {phone}: {e}")
            records.append({
                "execution_id": "", "phone": phone, "name": name,
                "expiry": expiry_fmt, "days": str(days_left),
                "nasid": c.get("router_nasid",""), "status": "error",
                "disposition": "Pending", "call_time": datetime.now().strftime("%d %b %H:%M"),
                "batch_label": batch_label,
            })
    return records


# ── Google Sheet write-back ───────────────────────────────────────────────────
FINAL_DISPOSITIONS = {
    "Will Recharge Today", "Will Recharge Later", "Already Recharged",
    "Wants Device Return", "Device Already Returned",
    "Don't Want – Service Issue", "Don't Want – Personal Reason",
    "Wrong Number",
}

def write_disposition_to_sheet(record: dict):
    """
    POST to Google Apps Script — upsert logic:
    one row per phone + expiry_date.
    Only updates if new disposition is more final than existing.
    """
    disposition = record.get("disposition", "Pending")
    # Skip writing pending/callback — wait for final disposition
    if disposition in {"Pending", "Callback Scheduled", "Not Answered / Busy", "Out of Town"}:
        # Still write if it's a redial final result
        if record.get("batch_label", "").startswith("round1"):
            log.info(f"Skipping non-final disposition for round1: {disposition}")
            return

    try:
        today = date.today().strftime("%Y-%m-%d")
        payload = {
            "action":       "upsert_disposition",   # Apps Script will update if exists
            "phone":        record.get("phone",""),
            "name":         record.get("name",""),
            "nasid":        record.get("nasid",""),
            "expiry_date":  today,                   # key for dedup
            "disposition":  disposition,
            "call_status":  record.get("status",""),
            "call_time":    datetime.now().strftime("%d %b %H:%M"),
            "execution_id": record.get("execution_id",""),
            "zone":         record.get("zone",""),
            "lco":          record.get("lco",""),
            "customer_type":record.get("customer_type",""),
            "is_final":     disposition in FINAL_DISPOSITIONS,
        }
        req.post(GSHEET_SCRIPT_URL, json=payload, timeout=15)
        log.info(f"Sheet upsert: {record.get('phone')} → {disposition}")
    except Exception as e:
        log.error(f"Sheet write failed: {e}")


# ── Redial logic ──────────────────────────────────────────────────────────────
REDIAL_STATUSES = {"no-answer", "busy", "failed", "not-answered", "Pending", "Not Answered / Busy"}

def get_redial_list(date_str: str):
    """Return calls from today's batch that need redial."""
    batch = daily_batches.get(date_str, {})
    calls = batch.get("calls", [])
    redial = [c for c in calls if c.get("disposition") in REDIAL_STATUSES
              or c.get("status") in {"no-answer","busy","failed","queued","error"}]
    log.info(f"Redial list for {date_str}: {len(redial)} calls")
    return redial


# ── Main daily job ────────────────────────────────────────────────────────────
def run_daily_campaign(target_date: str = None, is_redial: bool = False):
    """
    Main entry point.
    target_date: YYYY-MM-DD (defaults to today - 11 days = R11)
    is_redial: True = only re-call no-answer/pending from today's batch
    """
    if not target_date:
        target_date = (date.today() - timedelta(days=11)).strftime("%Y-%m-%d")

    date_str = date.today().strftime("%Y-%m-%d")
    batch_label = f"{'redial' if is_redial else 'round1'}_{date_str}"

    log.info(f"=== {'REDIAL' if is_redial else 'ROUND 1'} started — target_date={target_date} ===")

    try:
        if is_redial:
            customers_raw = get_redial_list(date_str)
            # Convert back to metabase-style dicts for trigger
            customers = [{"mobile": c["phone"].replace("+91",""),
                          "customer_name": c["name"],
                          "plan_expired_on": "",
                          "expiry_date": c.get("expiry",""),
                          "router_nasid": c.get("nasid",""),
                          "zone": c.get("zone",""),
                          "lco_name": c.get("lco",""),
                          "customer_type": c.get("customer_type","")} for c in customers_raw]
        else:
            customers = fetch_r11_from_metabase(target_date)

        if not customers:
            log.info("No customers to call today.")
            return {"success": True, "called": 0}

        records = trigger_bolna_calls(customers, batch_label=batch_label)

        if not is_redial:
            daily_batches[date_str] = {
                "target_date":   target_date,
                "calls":         records,
                "triggered_at":  datetime.now().isoformat(),
                "redial_done":   False,
            }
        else:
            # Append redial records
            daily_batches.setdefault(date_str, {}).setdefault("calls", []).extend(records)
            daily_batches[date_str]["redial_done"] = True

        log.info(f"=== {'REDIAL' if is_redial else 'ROUND 1'} complete — {len(records)} calls triggered ===")
        return {"success": True, "called": len(records), "batch_label": batch_label}

    except Exception as e:
        log.error(f"Campaign run failed: {e}")
        return {"success": False, "error": str(e)}


def update_call_record(execution_id: str, status: str, disposition: str, voc: str = ""):
    """Called from webhook to update a call record and write to sheet."""
    today = date.today().strftime("%Y-%m-%d")
    batch = daily_batches.get(today, {})
    for record in batch.get("calls", []):
        if record.get("execution_id") == execution_id:
            record["status"]      = status
            record["disposition"] = disposition
            record["voc"]         = voc
            write_disposition_to_sheet(record)
            return True
    return False


def get_today_calls():
    """Return today's call records for dashboard."""
    today = date.today().strftime("%Y-%m-%d")
    return daily_batches.get(today, {}).get("calls", [])
