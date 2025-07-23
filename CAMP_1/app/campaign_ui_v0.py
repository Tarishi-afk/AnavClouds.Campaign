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
from datetime import datetime, timedelta, time as dt_time
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

html_body = None
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
# Streamlit UI
st.title("📧 Campaign Manager")

with st.sidebar:
    batch_size     = st.number_input("Batch Size", 1, 100, 5)
    batch_delay    = st.number_input("Batch Delay (Minutes)", 1, 600, 5)
    start_time_ist = st.time_input("Start Time (IST)", dt_time(9, 0))
    end_time_ist   = st.time_input("End Time (IST)", dt_time(18, 0))
    date           = st.date_input("Start Date")
    subject        = st.text_input("Subject", "Hello from AnavCloud 👋")
    body           = st.text_area("Email Body (bullets supported)", "- Welcome!\n- This is a demo", height=150)
    footnote       = st.text_input("Footnote", "This is an automated email.")
    time_zone      = st.text_input("Timezone (e.g., Asia/Kolkata)", "Asia/Kolkata")
    start_btn      = st.button("🚀 Start Campaign")
    stop_btn       = st.button("🛑 Stop Campaign")
    sheet_name_input = st.text_input("Google Sheet(EMAIL-status)", value=SHEET_NAME)
    sheet_name_input2 = st.text_input("Google Sheet(EMAIL-track)", value=SHEET_NAME2)
    preview         = st.button("Preview the mail")


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

SHEET_NAME = sheet_name_input
SHEET_NAME2 = sheet_name_input2
# Time Zone Setup
IST = pytz.timezone(time_zone)
sh, sm = start_time_ist.hour, start_time_ist.minute
eh, em = end_time_ist.hour, end_time_ist.minute
START = dt_time(sh, sm)
END   = dt_time(eh, em)

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S%z")
logger = logging.getLogger()
sender_config = json.loads(os.environ["SENDER_CONFIG"])
# load senders & clients

creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if not creds_json:
    raise ValueError("GOOGLE_CREDENTIALS_JSON not set in environment!")

SENDERS = sender_config
creds_dict = json.loads(creds_json)
credentials = Credentials.from_service_account_info(creds_dict)
# Read leads directly from SHEET_NAME
gc = get_gspread_client()
lead_sheet = gc.open(SHEET_NAME).sheet1
lead_df = pd.DataFrame(lead_sheet.get_all_records())

# Filter: Only leads that haven't been sent
lead_df = lead_df[~lead_df["STATUS"].str.upper().eq("SENT")]

CLIENTS = lead_df.to_dict(orient="records")
TOTAL   = len(CLIENTS)

NS         = len(SENDERS)

# Google Sheet init

sheet = gc.open(SHEET_NAME).sheet1
vals  = sheet.get_all_values()
if len(vals) <= 1:
    sheet.clear()
    sheet.append_row(["SENDER","Email_ID","STATUS","TIMESTAMP"])

# state
if "scheduler" not in st.session_state:
    st.session_state.scheduler = BackgroundScheduler(timezone=IST)

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
    

state = load_state(SHEET_NAME)

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
    now_t = datetime.now(IST).time()
    return (START <= now_t < END) if START < END else (now_t >= START or now_t < END)

def save_template_for_reference(sheet_name: str, html_body: str):
    dir_path = "sent_templates"
    os.makedirs(dir_path, exist_ok=True)  # Make folder if not exists
    file_path = os.path.join(dir_path, f"{sheet_name}_template.html")

    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html_body)

def inject_pixel(html, email, sender):
    md  = {"metadata": {"email": email, "sender": sender, "sheet":  SHEET_NAME2}}
    tok = base64.urlsafe_b64encode(json.dumps(md).encode()).decode().rstrip("=")
    img = f'<img src="{BASE_TRACK_URL}/{tok}" width="1" height="1"/>'
    return html.replace("</body>", f"{img}</body>")

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
        with open("templates/email_template_analytics.html") as f:
            tmpl = f.read()
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

today_ist = datetime.now(IST).date()
if today_ist < date:
    st.info(f"⏳ Campaign is scheduled to begin on {date.strftime('%B %d, %Y')}.")
def send_batch():
    global state
   
    campaign_row_state, campaign_flag = state["campaign_row_state"], state["campaign_flag"]
    if today_ist < date:
        logger.info("⏳ Campaign not started — waiting for start date: %s", date.strftime("%Y-%m-%d"))
        st.info(f"⏳ Campaign is scheduled to begin on {date.strftime('%B %d, %Y')}.")
        return
    if campaign_row_state >= TOTAL:
        logger.info("Done all %s clients; shutting down", TOTAL)
        st.session_state.scheduler.shutdown(wait=False)
        st.session_state.campaign_running = False
        return
    if not in_window():
        logger.info("Outside window %s-%s; skipping batch", START, END)
        return

    sent = 0
    if "html_body" not in st.session_state or st.session_state.html_body is None:
        try:
            st.session_state.html_body = generate_html_from_text(body)
        except Exception as e:
            logger.error("OpenAI generation failed: %s", e)
            st.session_state.scheduler.shutdown(wait=False)
            st.session_state.campaign_running = False
            st.error(f"OpenAI generation failed: {e}")
            return

    html_body = st.session_state.html_body

    if "template_saved" not in st.session_state or not st.session_state.template_saved:
        save_template_for_reference(SHEET_NAME, st.session_state.html_body)
        st.session_state.template_saved = True

    for _ in range(batch_size):
        if campaign_row_state >= TOTAL:
            break

        client = CLIENTS[campaign_row_state]
        sender = SENDERS[campaign_flag]
        fr, to = sender["email"], client["Email_ID"]
        name = client.get("NAME", "")
        sname = sender.get("name", "Team")

        try:
            tmpl = open("templates/email_template_analytics.html").read()
        except Exception as e:
            logger.error("Template load failed: %s", e)
            break

        
        html_full = tmpl.format(NAME=name, BODY=html_body, SENDER_NAME=sname, FOOTNOTE=footnote)
        html_trkd = adapt_html(
            html_text=inject_pixel(html_full, to, fr),
            click_tracking=False,
            open_tracking=False,
            extra_metadata={"email": to},
            base_open_tracking_url=BASE_TRACK_URL
        )

        subj = subject
        transport = sender.get("transport", "gmail").lower()
        ok = False
        status = "Failed"
        ts = datetime.now(IST).isoformat()

        try:
            # === Try SendGrid first ===
            if transport == "outlook":
                try:
                    ok = send_via_sendgrid(fr, to, subj, html_trkd)
                except Exception as sg_err:
                    logger.error("SendGrid critical error: %s — Stopping campaign", sg_err)
                    st.session_state.scheduler.shutdown(wait=False)
                    st.session_state.campaign_running = False
                    st.error(f" SendGrid failure — campaign stopped: {sg_err}")
                    try:
                        sheet.append_row([fr, to, "Failed (SendGrid Error)", ts])
                    except Exception as e:
                        logger.error("Sheet append error: %s", e)
                    return

                if not ok:
                    logger.warning("SendGrid failed, trying SMTP fallback...")

   
            try:
                ok = ok or send_via_smtp(fr, to, subj, html_trkd,
                                         sender["smtp_server"], sender["smtp_port"],
                                         fr, sender.get("password", ""))
            except smtplib.SMTPRecipientsRefused as rec_err:
                logger.warning(" Invalid recipient %s — %s", to, rec_err)
                ok = False
            except smtplib.SMTPException as smtp_err:
                logger.error(" SMTP critical error: %s — Stopping campaign", smtp_err)
                st.session_state.scheduler.shutdown(wait=False)
                st.session_state.campaign_running = False
                st.error(f" SMTP failure — campaign stopped: {smtp_err}")
                try:
                    sheet.append_row([fr, to, "Failed (SMTP Error)", ts])
                except Exception as e:
                    logger.error("Sheet append error: %s", e)
                return
            except Exception as smtp_unexpected:
                logger.error(" Unexpected SMTP error: %s — Stopping campaign", smtp_unexpected)
                st.session_state.scheduler.shutdown(wait=False)
                st.session_state.campaign_running = False
                st.error(f" Unexpected SMTP error — campaign stopped: {smtp_unexpected}")
                try:
                    sheet.append_row([fr, to, "Failed (SMTP Unknown)", ts])
                except Exception as e:
                    logger.error("Sheet append error: %s", e)
                return

        except Exception as fallback_error:
            logger.error(" Unhandled error during send logic: %s — Stopping campaign", fallback_error)
            st.session_state.scheduler.shutdown(wait=False)
            st.session_state.campaign_running = False
            st.error(f" Unhandled error occurred — campaign stopped: {fallback_error}")
            try:
                sheet.append_row([fr, to, "Failed (Fatal Error)", ts])
            except Exception as e:
                logger.error("Sheet append error: %s", e)
            return

        status = "Sent" if ok else "Failed"
        logger.info("%s | %s→%s", status, fr, to)

        try:
            for idx, row in enumerate(lead_df.to_dict(orient="records")):
                if row["Email_ID"].strip().lower() == to.strip().lower():
                    row_number = idx + 2  # Account for header row

                    # Update STATUS
                    status_col = lead_df.columns.get_loc("STATUS") + 1
                    lead_sheet.update_cell(row_number, status_col, status)

                    # Update SENDER column (create if doesn't exist)
                    if "SENDER" not in lead_df.columns:
                        lead_sheet.insert_cols([["SENDER"]], col=status_col + 1)
                        lead_df.insert(status_col, "SENDER", "")

                    sender_col = lead_df.columns.get_loc("SENDER") + 1
                    lead_sheet.update_cell(row_number, sender_col, fr)

                    # Update TIMESTAMP column (create if doesn't exist)
                    if "TIMESTAMP" not in lead_df.columns:
                        lead_sheet.insert_cols([["TIMESTAMP"]], col=sender_col + 1)
                        lead_df.insert(sender_col, "TIMESTAMP", "")

                    timestamp_col = lead_df.columns.get_loc("TIMESTAMP") + 1
                    lead_sheet.update_cell(row_number, timestamp_col, ts)

                    break
        except Exception as e:
            logger.error("Sheet update error: %s", e)

        campaign_row_state += 1
        campaign_flag = (campaign_flag + 1) % NS
        sent += 1

    state = ({"campaign_row_state": campaign_row_state, "campaign_flag": campaign_flag})
    save_state(SHEET_NAME, state)
    logger.info("Batch complete: %d emails sent, next campaign_row_state=%d", sent, campaign_row_state)

if start_btn and not st.session_state.campaign_running:
    st.session_state.scheduler.add_job(send_batch, IntervalTrigger(seconds=batch_delay*60), id="batch_job", next_run_time=datetime.now(IST))
    st.session_state.scheduler.start()
    st.session_state.campaign_running = True
    st.success("✅ Campaign started and running in the background.")
if stop_btn:
    if st.session_state.campaign_running:
        st.session_state.scheduler.shutdown(wait=False)
        st.session_state.scheduler = BackgroundScheduler(timezone=IST)  # NEW scheduler instance
        st.session_state.campaign_running = False
        st.session_state.html_body = None
        st.session_state.template_saved = False
        st.warning("🛑 Campaign stopped.")
    else:
        st.info("Campaign is not currently running.")

