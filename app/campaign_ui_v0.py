import os
import json
import logging
import base64
import smtplib
import re
import threading
import pandas as pd
import pytz
import streamlit as st
import time
import gspread
from openai import AzureOpenAI, AsyncAzureOpenAI
from datetime import date, datetime, timedelta, time as dt_time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pytracking.html import adapt_html
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
from google.oauth2.service_account import Credentials
# === CONFIG & GLOBALS ===
BASE_TRACK_URL    = os.environ['BASE_TRACK_URL']
SHEET_NAME        = os.environ['SHEET_NAME']
SHEET_NAME2       = os.environ['SHEET_NAME2']
SERVICE_ACCOUNT   = os.environ['GOOGLE_CREDENTIALS_JSON']
STATE_FILE        = "config/campaign_state.json"
SENDGRID_API_KEY  = os.environ['SENDGRID_API_KEY']
OPEN_AI_KEY       = os.environ['OPEN_AI_KEY']
OPEN_AI_ENDPOINT  = os.environ['OPEN_AI_ENDPOINT']
client = AzureOpenAI(
    api_key=OPEN_AI_KEY,
    api_version="2024-08-01-preview",
    azure_endpoint=OPEN_AI_ENDPOINT
)
def get_gspread_client():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise ValueError("GOOGLE_CREDENTIALS_JSON not set in environment!")

    creds_dict = json.loads(creds_json)
    credentials = Credentials.from_service_account_info(creds_dict, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ])
    return gspread.authorize(credentials)

html_body = None
gc = get_gspread_client()
all_zones = pytz.common_timezones
default_tz = "Asia/Kolkata"
default_idx = all_zones.index(default_tz) if default_tz in all_zones else 0
st.title("📧 Campaign Manager")

with st.sidebar:
    # 1) Which campaign-state workbook?
    campaign_state = "Test_campaign_state"

    # 2) Load all rows from that sheet, if provided
    state_rows = []
    if campaign_state:
        try:
            ws = gc.open(campaign_state).worksheet("campaign_state")
            state_rows = ws.get_all_records()
        except Exception:
            state_rows = []

    # 3) Pick which row by SHEET_NAME
    sheet_options  = [r["SHEET_NAME"] for r in state_rows]
    selected_sheet = st.selectbox(
        "Pick campaign (SHEET_NAME)",
        options=[""] + sheet_options,
        key="selected_sheet"
    )

    # 4) Load button writes into session_state
    if st.button("🔄 Load Campaign Config"):
        if not campaign_state or not selected_sheet:
            st.error("Enter workbook and select a campaign first.")
        else:
            rec = next((r for r in state_rows if r["SHEET_NAME"] == selected_sheet), None)
            if not rec:
                st.error("Row not found.")
            else:
                # stash loaded values
                st.session_state["_loaded_tz"]        = rec.get("Timezone", all_zones[default_idx])
                st.session_state["_loaded_tpl"]       = rec.get("Email Template", "")
                st.session_state["_loaded_batch"]     = int(rec.get("Batch delay", 5))
                # parse start/end times
                h_s, m_s, _ = rec.get("start_time_ist", "09:00:00").split(":")
                h_e, m_e, _ = rec.get("end_time_ist",   "18:00:00").split(":")
                st.session_state["_loaded_start"]     = dt_time(int(h_s), int(m_s))
                st.session_state["_loaded_end"]       = dt_time(int(h_e), int(m_e))
                # parse date
                try:
                    st.session_state["_loaded_date"] = datetime.fromisoformat(rec.get("Start Date")).date()
                except:
                    st.session_state["_loaded_date"] = datetime.now().date()
                # text fields
                st.session_state["_loaded_subject"]   = rec.get("Subject", "")
                st.session_state["_loaded_body"]      = rec.get("Body", "")
                st.session_state["_loaded_note"]      = rec.get("Footnote", "")
                # sheets
                st.session_state["_loaded_sheet1"]    = rec.get("SHEET_NAME", "")
                st.session_state["_loaded_sheet2"]    = rec.get("Open Tracking sheet", "")
                st.success("✅ Loaded campaign config!")

    # 5) Timezone dropdown (will pick up session_state on rerun)
    tz_default = st.session_state.get("_loaded_tz", all_zones[default_idx])
    tz_idx     = all_zones.index(tz_default)
    time_zone  = st.selectbox(
        "Select Timezone",
        all_zones,
        index=tz_idx,
        key="time_zone"
    )

    # 6) Template picker
    template_dir    = "templates"
    template_files  = sorted(fn for fn in os.listdir(template_dir) if fn.endswith(".html"))
    tpl_default     = st.session_state.get("_loaded_tpl", template_files[0])
    tpl_idx         = template_files.index(tpl_default)
    selected_template = st.selectbox(
        "Email Template",
        template_files,
        index=tpl_idx,
        key="selected_template"
    )

    # 7) Batch size & delay
    batch_size = st.number_input(
        "Batch Size", 1, 100,
        value=st.session_state.get("batch_size", 5),
        key="batch_size"
    )
    batch_delay = st.number_input(
        "Batch Delay (Minutes)", 1, 600,
        value=st.session_state.get("_loaded_batch", 5),
        key="batch_delay"
    )

    # 8) Status & track sheets
    sheet_name_input  = st.text_input(
        "Google Sheet(EMAIL-status)",
        value=st.session_state.get("_loaded_sheet1", "EmailStatus"),
        key="sheet_name_input"
    )
    TRACKING_WB = "MailTracking"
    TEST_SHEET     = "email-opentrackng(test)"
    try:
        track_wb = gc.open(TRACKING_WB)
        track_sheets = [ws.title for ws in track_wb.worksheets()]
    except Exception:
        track_sheets = []
    if TEST_SHEET not in track_sheets:
        track_sheets.append(TEST_SHEET)

    default_track = st.session_state.get("_loaded_sheet2", track_sheets[0] if track_sheets else "")
    sheet_name_input2 = st.selectbox(
        "Mail Tracking worksheet",
        options=track_sheets,
        index=track_sheets.index(default_track) if default_track in track_sheets else 0,
        key="sheet_name_input2"
    )

    # 9) Start/End time with dynamic labels
    start_time = st.time_input(
        f"Start Time ({time_zone})",
        value=st.session_state.get("_loaded_start", dt_time(9, 0)),
        key="start_time"
    )
    end_time   = st.time_input(
        f"End Time   ({time_zone})",
        value=st.session_state.get("_loaded_end",   dt_time(18, 0)),
        key="end_time"
    )

    # 10) Start date
    date = st.date_input(
        "Start Date",
        value=st.session_state.get("_loaded_date", datetime.now().date()),
        key="date"
    )

    # 11) Subject, body, footnote
    subject = st.text_input(
        "Subject",
        value=st.session_state.get("_loaded_subject", "Hello from AnavCloud 👋"),
        key="subject"
    )
    body = st.text_area(
        "Email Body (bullets supported)",
        value=st.session_state.get("_loaded_body", "- Welcome!\n- This is a demo"),
        height=150,
        key="body"
    )
    footnote = st.text_input(
        "Footnote",
        value=st.session_state.get("_loaded_note", "This is an automated email."),
        key="footnote"
    )

    # 12) Action buttons
    start_btn = st.button("🚀 Start Campaign")
    stop_btn  = st.button("🛑 Stop Campaign")
    preview   = st.button("Preview the mail")

SHEET_NAME = sheet_name_input
SHEET_NAME2 = sheet_name_input2
camp =  campaign_state
# Time Zone Setup
TZ    = pytz.timezone(st.session_state["time_zone"])
sh, sm = start_time.hour, start_time.minute
eh, em = end_time.hour,   end_time.minute
START = dt_time(sh, sm)
END   = dt_time(eh, em)
TEMPLATE_PATH = f"templates/{selected_template}"
# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
logger = logging.getLogger()
sender_config = json.loads(os.environ["SENDER_CONFIG"])
creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise ValueError("GOOGLE_CREDENTIALS_JSON not set in environment!")


# load senders & clients
# with open("config\sender_config.json") as f:
#     SENDERS = json.load(f)
SENDERS = sender_config
creds_dict = json.loads(creds_json)
credentials = Credentials.from_service_account_info(creds_dict)
# Read leads directly from SHEET_NAME
gc = get_gspread_client()
lead_sheet = gc.open(SHEET_NAME).sheet1
lead_df = pd.DataFrame(lead_sheet.get_all_records())

# Filter: Only leads that haven't been sent
# Fetch all records (automatically skips header)
all_records = lead_sheet.get_all_records()

CLIENTS = []
for idx, record in enumerate(all_records):
    # Google Sheets rows start at 1, header is at row 1,
    # so data begins at row 2 → idx+2
    sheet_row = idx + 2

    # Only include unsent leads, but keep the row reference
    if record.get("STATUS", "").strip().upper() != "SENT":
        record["_row_index"] = sheet_row
        CLIENTS.append(record)

TOTAL = len(CLIENTS)

NS         = len(SENDERS)

# Google Sheet init

sheet = gc.open(SHEET_NAME).sheet1
vals  = sheet.get_all_values()
if len(vals) <= 1:
    sheet.clear()
    sheet.append_row(["SENDER","Email_ID","STATUS","TIMESTAMP"])

# state
if "scheduler" not in st.session_state:
    st.session_state.scheduler = BackgroundScheduler(timezone=TZ)

if "campaign_running" not in st.session_state:
    st.session_state.campaign_running = False



def load_state(sheet_name):
    if os.path.exists(STATE_FILE):
        try:
            raw = json.load(open(STATE_FILE))
            return raw.get(sheet_name, {"campaign_row_state": 0, "campaign_flag": 0})
        except Exception as e:
            logger.warning("Bad state file, resetting due to error: %s", e)
    return {"campaign_row_state": 0, "campaign_flag": 0}

def save_state(sheet_name, new_state):
    full_state = {}
    if os.path.exists(STATE_FILE):
        try:
            full_state = json.load(open(STATE_FILE))
        except Exception as e:
            logger.warning("Bad state file, starting fresh: %s", e)
    full_state[sheet_name] = new_state
    print(f"New state>>>>>>>>>>>>>>>>>>>{new_state}")
    with open(STATE_FILE, "w") as f:
        json.dump(full_state, f, indent=2)
def save_state_to_sheet(sheet_name, new_state):
    # Open (or create) the campaign_state worksheet
    try:
        ws = gc.open(camp).worksheet("campaign_state")
    except gspread.exceptions.WorksheetNotFound:
        wb = gc.open(camp)
        ws = wb.add_worksheet("campaign_state", rows="100", cols="15")
        ws.append_row([
            "SHEET_NAME","campaign_row_state","campaign_flag",
            "Subject","Body","Footnote","start_time_ist","end_time_ist",
            "Timezone","Email Template","Batch delay","Start Date","Open Tracking sheet"
        ])

    # Ensure header row is correct
    headers = ws.row_values(1)
    if not headers or headers[0] != "SHEET_NAME":
        ws.clear()
        headers = [
            "SHEET_NAME","campaign_row_state","campaign_flag",
            "Subject","Body","Footnote","start_time_ist","end_time_ist",
            "Timezone","Email Template","Batch delay","Start Date","Open Tracking sheet"
        ]
        ws.append_row(headers)

    # Normalize new_state values to strings
    str_state = {}
    for k,v in new_state.items():
        # Convert date → ISO string
        if isinstance(v, dt_time):
            str_state[k] = v.isoformat()
        else:
            str_state[k] = str(v)

    # Find existing row
    all_vals = ws.get_all_values()
    found_idx = None
    for idx, row in enumerate(all_vals[1:], start=2):
        if row[0] == sheet_name:
            found_idx = idx
            break

    # Build the row in header order
    row_to_write = [ str_state.get(col, "") for col in headers ]

    if found_idx:
        # update the exact columns
        cell_range = f"A{found_idx}:{chr(ord('A')+len(headers)-1)}{found_idx}"
        ws.update(cell_range, [row_to_write])
    else:
        ws.append_row(row_to_write)








def load_state_from_sheet(sheet_name):
    try:
        state_sheet = gc.open(camp).worksheet("campaign_state")
        records = state_sheet.get_all_records()
        for row in records:
            if row["SHEET_NAME"] == sheet_name:
                return {
                    "campaign_row_state": int(row.get("campaign_row_state", 0)),  #
                    "campaign_flag": int(row.get("campaign_flag", 0))
                }
    except Exception as e:
        logger.warning("Couldn't load state from sheet: %s", e)

    return {"campaign_row_state": 0, "campaign_flag": 0}



state = load_state_from_sheet(SHEET_NAME)
print(f"State >>>>>>>>>>>>>>>>>>{state}")


# helpers
def convert_bullets_to_html(text):
    lines, html, stack = text.strip().splitlines(), [], []
    def close(level):
        while stack and stack[-1][0] >= level:
            html.append(f"</{stack.pop()[1]}>")
    for line in lines:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        txt    = line.strip()
        if (m := re.match(r"^[-*]\s+(.*)", txt)):
            close(indent)
            if not stack or stack[-1][1] != "ul":
                html.append("<ul>"); stack.append((indent,"ul"))
            html.append(f"<li>{m.group(1)}</li>")
        else:
            close(0)
            html.append(f"<p>{txt}</p>")
    close(0)
    return "\n".join(html)
def generate_html_from_text(raw_text: str) -> str:
    """
    Ask OpenAI to convert plaintext email body into HTML.
    Returns inner HTML (no <html>/<body> wrapper).
    """
    system = "You are a professional HTML email generator. Make sure no error is generated while formating it."
    user   = (
        "Convert the following plaintext email body into HTML. "
        "Wrap paragraphs in <p>…</p>, Identify where are points and list them under bullets in <ul><li>…</li></ul>. "
        "Preserve line breaks and basic formatting. **STRICTY** never use quoatation marks for enclosing the body like **'''**"
        "Do not include <html> or <body> tags.  \n\n, ALWAYS BE CAREFUL WHILE Formatting dont put any special characters"
        f"EMAIL BODY:\n{raw_text.strip()}"
    )
    resp = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": system},
            {"role": "user",   "content": user}
        ],
        temperature=0
    )
    return resp.choices[0].message.content.strip()


def in_window():
    now_t = datetime.now(TZ).time()
    return (START <= now_t < END) if START < END else (now_t >= START or now_t < END)

def save_template_for_reference(sheet_name: str, html_body: str):
    dir_path = "sent_templates"
    os.makedirs(dir_path, exist_ok=True)  # Make folder if not exists
    file_path = os.path.join(dir_path, f"{sheet_name}_template.html")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html_body)

def inject_pixel(html: str, metadata_dict: dict) -> str:
    # 1) Get current time in IST, with offset
    ist      = pytz.timezone("Asia/Kolkata")
    now_ist  = datetime.now(ist)
    sent_time = now_ist.isoformat()     

   
    metadata = metadata_dict.copy()
    metadata["sent_time"] = sent_time

 
    payload = {"metadata": metadata}

    payload_json = json.dumps(payload)
    token        = base64.urlsafe_b64encode(payload_json.encode()) \
                      .decode().rstrip("=")


    img_tag = f'<img src="{BASE_TRACK_URL}/{token}" width="1" height="1"/>'


    return html.replace("</body>", f"{img_tag}</body>")

def send_via_smtp(fr, to, subj, html, srv, port, user, pwd):
    try:
        msg = MIMEMultipart("alternative")
        msg["From"], msg["To"], msg["Subject"] = fr, to, subj
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(srv, port) as s:
            s.starttls(); s.login(user, pwd); s.sendmail(fr, to, msg.as_string())
        logger.info("SMTP sent %s→%s", fr, to)
        return True
    except Exception as e:
        logger.error("SMTP err %s→%s: %s", fr, to, e)
        return False

def send_via_sendgrid(fr, to, subj, html):
    if not SENDGRID_API_KEY:
        logger.error("Missing SENDGRID_API_KEY"); return False
    try:
        mail = Mail(from_email=fr, to_emails=to, subject=subj, html_content=html)
        mail.reply_to = "anavcloudsoftware@gmail.com"
        resp = SendGridAPIClient(SENDGRID_API_KEY).send(mail)
        ok   = resp.status_code in (200,202)
        logger.info("SG %s %s→%s", resp.status_code, fr, to)
        return ok
    except Exception as e:
        logger.error("SG err %s→%s: %s", fr, to, e)
        return False
if preview:
    try:
        html_body = generate_html_from_text(body)
        tmpl = open(TEMPLATE_PATH).read()
        sample_html = tmpl.format(
            NAME="Test User",
            BODY=html_body,
            SENDER_NAME="AnavCloud Team",
            FOOTNOTE=footnote
        )

        st.subheader("Email Preview")
        st.components.v1.html(sample_html, height=400, scrolling=True)

    except Exception as e:
        st.error(f"Failed to generate preview: {e}")
        logger.error("Preview generation failed: %s", e)

today_ist = datetime.now(TZ).date()
if today_ist < date:
    st.info(f"⏳ Campaign is scheduled to begin on {date.strftime('%B %d, %Y')}.")
def send_batch():
    global state

    # 1) Load saved pointers
    campaign_row_state = state["campaign_row_state"]
    campaign_flag      = state["campaign_flag"]

    # 2) Fetch all rows (excluding header) and compute totals
    all_records = lead_sheet.get_all_records()
    total_rows  = len(all_records)
    logger.info(f"▶ Resuming at absolute row index: {campaign_row_state} of {total_rows}")

    # 3) Pre-flight checks
    if today_ist < date:
        st.info(f"⏳ Campaign starts on {date.strftime('%B %d, %Y')}")
        return

    if campaign_row_state >= total_rows:
        logger.info("✅ All clients processed; stopping.")
        st.session_state.scheduler.shutdown(wait=False)
        st.session_state.campaign_running = False
        return

    if not in_window():
        logger.info(f"⏰ Outside window {START}–{END}; skipping batch.")
        return

    # 4) Ensure email body/template are ready
    if "html_body" not in st.session_state or not st.session_state.html_body:
        try:
            st.session_state.html_body = generate_html_from_text(body)
        except Exception as e:
            logger.error("OpenAI gen failed: %s", e)
            st.error(f"Generation failed: {e}")
            st.session_state.scheduler.shutdown(wait=False)
            st.session_state.campaign_running = False
            return

    html_body = st.session_state.html_body
    if not st.session_state.get("template_saved", False):
        save_template_for_reference(SHEET_NAME, html_body)
        st.session_state.template_saved = True

    sent = 0

    # 5) Send up to batch_size emails
    while sent < batch_size and campaign_row_state < total_rows:
        record    = all_records[campaign_row_state]
        sheet_row = campaign_row_state + 1  # +2 → header + 0-index
        campaign_row_state += 1             # advance pointer

        status_raw = record.get("STATUS", "").strip().upper()
        if status_raw == "SENT":
            # skip already-sent rows
            continue

        # Prepare email
        to      = record["Email_ID"]
        name    = record.get("NAME", "")
        sender  = SENDERS[campaign_flag]
        fr      = sender["email"]
        sname   = sender.get("name", "Team")

        logger.info(f"📩 Sending to row {sheet_row}: {to}")

        # Build & send
        try:
            tmpl = open(TEMPLATE_PATH).read()
            html_full= tmpl.format(NAME=name, BODY=html_body, SENDER_NAME=sname, FOOTNOTE=footnote)
            html_trkd= adapt_html(
                          html_text=inject_pixel(html_full, {
                            "email": to,
                            "sender": fr,
                            "sheet": SHEET_NAME2,
                            "sheet_name": SHEET_NAME,
                            "subject": subject,
                            "timezone": time_zone,
                            "date": date.strftime("%Y-%m-%d"),
                            "template": selected_template
                        }),
                          click_tracking=False,
                          open_tracking=False,
                          extra_metadata={"email": to},
                          base_open_tracking_url=BASE_TRACK_URL
                      )
        except Exception as e:
            logger.error("Template load failed: %s", e)
            break

        ts = datetime.now(TZ).isoformat()
        ok = False
        try:
            if sender.get("transport", "gmail").lower() == "outlook":
                ok = send_via_sendgrid(fr, to, subject, html_trkd)
                if not ok:
                    logger.warning("SendGrid failed, falling back to SMTP.")
            if not ok:
                ok = send_via_smtp(
                    fr, to, subject, html_trkd,
                    sender["smtp_server"], sender["smtp_port"],
                    fr, sender.get("password", "")
                )
        except Exception as e:
            logger.error("Send error: %s", e)
            st.error(f"Send failed: {e}")
            st.session_state.scheduler.shutdown(wait=False)
            st.session_state.campaign_running = False
            # Optionally log a failure row
            lead_sheet.insert_row([fr, to, "Failed", ts], index=2)
            return

        status = "SENT" if ok else "FAILED"
        logger.info(f"{status} → {fr} → {to}")

        # 6) Write status back to the exact sheet row
        headers = lead_sheet.row_values(1)
        col_map = {h: i+1 for i, h in enumerate(headers)}

        # Update STATUS
        lead_sheet.update_cell(sheet_row, col_map["STATUS"], status)

        # Update or create SENDER
        if "SENDER" not in col_map:
            lead_sheet.insert_cols([["SENDER"]], col=len(headers)+1)
            headers = lead_sheet.row_values(1)
            col_map = {h: i+1 for i, h in enumerate(headers)}
        lead_sheet.update_cell(sheet_row, col_map["SENDER"], fr)

        # Update or create TIMESTAMP
        if "TIMESTAMP" not in col_map:
            lead_sheet.insert_cols([["TIMESTAMP"]], col=col_map["SENDER"]+1)
            headers = lead_sheet.row_values(1)
            col_map = {h: i+1 for i, h in enumerate(headers)}
        lead_sheet.update_cell(sheet_row, col_map["TIMESTAMP"], ts)
        MAILTRACKING_WORKBOOK = "MailTracking"
        # try:
        #     sheet_name1_tab = gc.open(MAILTRACKING_WORKBOOK).worksheet(SHEET_NAME2)
        #     updated_row = lead_sheet.row_values(sheet_row)
        #     sheet_name1_tab.append_row(updated_row)
        #     logger.info(f" Mirrored row to {SHEET_NAME2}: {to}")
        # except Exception as e:
        #     logger.error(f" Failed to mirror row to {SHEET_NAME2}: {e}")

        # Advance pointers
        campaign_flag = (campaign_flag + 1) % NS
        sent += 1

    # 7) Persist updated state for next run
    state = {
    "SHEET_NAME":           SHEET_NAME,            # the key must match your header
    "campaign_row_state":   campaign_row_state,
    "campaign_flag":        campaign_flag,
    "Subject":              subject,
    "Body":                 body,
    "Footnote":             footnote,
    "start_time_ist":       start_time.strftime("%H:%M:%S"),
    "end_time_ist":         end_time.strftime("%H:%M:%S"),
    "Timezone":             time_zone,
    "Email Template":       selected_template,
    "Batch delay":          batch_delay,
    "Start Date":           date.isoformat(),      # <-- date → string
    "Open Tracking sheet":  SHEET_NAME2            # header name must match exactly
}
    logger.info(f"Saving state: {state}")
    save_state_to_sheet(SHEET_NAME, state)

    logger.info(f"📦 Batch complete: {sent} sent, next index={campaign_row_state}")

if start_btn and not st.session_state.campaign_running:
    st.session_state.scheduler.add_job(send_batch, IntervalTrigger(seconds=batch_delay*60), id="batch_job", next_run_time=datetime.now(TZ))
    st.session_state.scheduler.start()
    st.session_state.campaign_running = True
    st.success("✅ Campaign started and running in the background.")
if stop_btn:
    if st.session_state.campaign_running:
        st.session_state.scheduler.shutdown(wait=False)
        st.session_state.scheduler = BackgroundScheduler(timezone=TZ)
        st.session_state.campaign_running = False
        st.session_state.html_body = None
        st.session_state.template_saved = False
        st.warning("🛑 Campaign stopped.")
    else:
        st.info("Campaign is not currently running.")

