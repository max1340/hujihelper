#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HUJI Moodle Calendar Sync Script
Syncs Moodle events from the iCal export feed, parses them,
saves to local JSON/ICS, and pushes to Firebase Realtime Database.
Scheduled to run twice daily (08:00 and 20:00).
"""

import os
import sys
import json
import time
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
try:
    from zoneinfo import ZoneInfo
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:
    ISRAEL_TZ = timezone(timedelta(hours=3))

MOODLE_URL = (
    "https://moodle.huji.ac.il/2026-27/calendar/export_execute.php?"
    "userid=80891&authtoken=4097c2345fc9e8671654ad6fc22f506a1796a2e6&preset_what=all&preset_time=recentupcoming"
)

FB_RTDB_BASE = "https://pc-building-b3d74-default-rtdb.europe-west1.firebasedatabase.app/huji_study_app"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICS_FILE = os.path.join(SCRIPT_DIR, "moodle_export.ics")
JSON_FILE = os.path.join(SCRIPT_DIR, "moodle_events.json")
LOG_FILE = os.path.join(SCRIPT_DIR, "moodle_sync.log")

COURSE_MAP = {
    "80153": "la",    # אלגברה ליניארית 1
    "83312": "mech",  # מכניקה ויחסות פרטית
    "80114": "am",    # מתמטיקה שימושית 1
    "76639": "cs",    # מבוא למדעי המחשב
}

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        print(f"Failed writing to log file: {e}", file=sys.stderr)

def fetch_moodle_ics(url=MOODLE_URL):
    log("Fetching iCal feed from Moodle HUJI...")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) HUJI-StudyApp/1.0"
    }
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        content = resp.read().decode("utf-8")
    log(f"Successfully downloaded {len(content)} bytes of iCal data.")
    return content

def unfold_lines(text):
    unfolded = []
    for line in text.splitlines():
        line = line.rstrip("\r\n")
        if line.startswith(" ") or line.startswith("\t"):
            if unfolded:
                unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded

def parse_iso_dt(dt_str):
    if not dt_str:
        return None
    dt_str = dt_str.strip()
    try:
        if dt_str.endswith("Z"):
            base_dt = datetime.strptime(dt_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return base_dt.astimezone(ISRAEL_TZ)
        elif "T" in dt_str:
            clean_str = dt_str[:15]
            return datetime.strptime(clean_str, "%Y%m%dT%H%M%S").replace(tzinfo=ISRAEL_TZ)
        else:
            return datetime.strptime(dt_str[:8], "%Y%m%d").replace(tzinfo=ISRAEL_TZ)
    except Exception as e:
        log(f"Date parse warning for '{dt_str}': {e}")
        return None

def clean_event_title(t):
    s = t.strip()
    s = re.sub(r"^יש להגיש את\s*['\"]?", "", s)
    s = re.sub(r"['\"]?\s*$", "", s)
    s = re.sub(r"^תאריך הגשה\s*", "", s)
    s = re.sub(r"\s+closes$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+is due$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+opens$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^הגשה\s+", "", s)
    s = re.sub(r"^נפתח ב\s+", "", s)
    return s.strip()

def detect_type_and_icon(summary, desc):
    text = (summary + " " + desc).lower()
    
    if any(k in text for k in ["מבחן", "בחינה", "בחינת", "מועד א", "מועד ב"]):
        return "exam", "🔴"
    if any(k in text for k in ["בוחן", "quiz", "קוויז"]):
        return "quiz", "✏️"
    if any(k in text for k in ["מבדק"]):
        return "check", "📝"
    if any(k in text for k in ["תרגיל בית", "הגשה", "להגיש", "יש להגיש", "תאריך הגשה", "due", "is due"]):
        return "assignment", "📋"
    if any(k in text for k in ["נוכחות", "הרצאה", "תרגול"]):
        return "lecture", "🎓"
    if any(k in text for k in ["הצבעה", "סקר", "שעת קבלה"]):
        return "poll", "🗳️"
    return "milestone", "📌"

def parse_moodle_events(ics_text):
    lines = unfold_lines(ics_text)
    events = []
    cur = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            parts = line.split(":", 1)
            raw_key = parts[0].split(";")[0] # strip params
            val = parts[1]
            cur[raw_key] = val

    log(f"Found {len(events)} raw VEVENT blocks in iCal.")
    
    parsed = []
    openings = {}
    standalone_openings = []

    for ev in events:
        uid = ev.get("UID", "")
        summary = ev.get("SUMMARY", "").replace(r"\,", ",").replace(r"\n", " ").strip()
        desc = ev.get("DESCRIPTION", "").replace(r"\,", ",").replace(r"\n", "\n").replace(r"\;", ";").strip()
        categories = ev.get("CATEGORIES", "")
        
        # Course detection
        course_key = "gen"
        for c_num, c_k in COURSE_MAP.items():
            if c_num in categories or c_num in summary or c_num in desc:
                course_key = c_k
                break
        
        dt_start = parse_iso_dt(ev.get("DTSTART", ""))
        dt_end = parse_iso_dt(ev.get("DTEND", ""))
        
        if not dt_start:
            continue
            
        # Skip lecture attendance checks ("נוכחות בהרצאות")
        if "נוכחות" in summary or "נוכחות" in desc:
            continue

        ev_type, icon = detect_type_and_icon(summary, desc)
        cleaned_title = clean_event_title(summary)
        is_opening = summary.startswith("נפתח ב") or summary.lower().endswith(" opens")

        ev_obj = {
            "id": uid,
            "c": course_key,
            "t": cleaned_title,
            "d": dt_start.strftime("%Y-%m-%d"),
            "time": dt_start.strftime("%H:%M"),
            "end_d": dt_end.strftime("%Y-%m-%d") if dt_end else None,
            "end_time": dt_end.strftime("%H:%M") if dt_end else None,
            "type": ev_type,
            "i": icon,
            "desc": desc,
            "categories": categories,
            "isMoodle": True,
            "timestamp": int(dt_start.timestamp() * 1000)
        }

        if is_opening:
            openings[(course_key, cleaned_title)] = (ev_obj["d"], ev_obj["time"])
            standalone_openings.append(ev_obj)
            continue

        parsed.append(ev_obj)

    # Attach opening metadata to matching deadline events, or keep standalone opening events
    deadline_keys = {(ev["c"], ev["t"]) for ev in parsed}
    for ev in parsed:
        op = openings.get((ev["c"], ev["t"]))
        if op:
            ev["open_d"], ev["open_time"] = op

    for op_ev in standalone_openings:
        if (op_ev["c"], op_ev["t"]) not in deadline_keys:
            op_ev["open_d"] = op_ev["d"]
            op_ev["open_time"] = op_ev["time"]
            parsed.append(op_ev)

    # Deduplicate events referencing the same assignment/quiz deadline on the same date
    deduped = []
    seen = set()
    for ev in parsed:
        key = (ev["c"], ev["d"], ev["t"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)

    # Sort chronologically
    deduped.sort(key=lambda x: (x["d"], x["time"]))
    log(f"Successfully processed {len(deduped)} events (after deduplication and cleaning).")
    return deduped

def sync_to_firebase(events):
    log("Pushing synced events to Firebase Realtime Database...")
    now_ts = int(time.time() * 1000)
    
    headers = {"Content-Type": "application/json"}
    
    # Push events
    events_url = f"{FB_RTDB_BASE}/moodleEvents.json"
    ev_data = json.dumps(events, ensure_ascii=False).encode("utf-8")
    req_ev = urllib.request.Request(events_url, data=ev_data, headers=headers, method="PUT")
    with urllib.request.urlopen(req_ev, timeout=20) as resp:
        log(f"Firebase moodleEvents status: {resp.status}")

    # Push lastSync
    sync_url = f"{FB_RTDB_BASE}/moodleLastSync.json"
    sync_meta = {
        "timestamp": now_ts,
        "dateFormatted": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "eventCount": len(events),
        "status": "success"
    }
    sync_data = json.dumps(sync_meta).encode("utf-8")
    req_sync = urllib.request.Request(sync_url, data=sync_data, headers=headers, method="PUT")
    with urllib.request.urlopen(req_sync, timeout=20) as resp:
        log(f"Firebase moodleLastSync status: {resp.status}")

def run_sync():
    log("--- Starting HUJI Moodle Calendar Sync ---")
    try:
        ics_content = fetch_moodle_ics()
        
        # Save local ics copy
        with open(ICS_FILE, "w", encoding="utf-8") as f:
            f.write(ics_content)
            
        parsed_events = parse_moodle_events(ics_content)
        
        # Save local json copy
        with open(JSON_FILE, "w", encoding="utf-8") as f:
            json.dump(parsed_events, f, ensure_ascii=False, indent=2)
            
        # Push to Firebase
        try:
            sync_to_firebase(parsed_events)
        except Exception as fb_err:
            log(f"Firebase sync warning (local files still updated): {fb_err}")
            
        log(f"Sync complete! {len(parsed_events)} events saved locally and pushed to cloud.")
        return 0
    except Exception as e:
        log(f"Sync error: {e}")
        return 1

if __name__ == "__main__":
    sys.exit(run_sync())
