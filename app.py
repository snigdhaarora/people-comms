"""
People Comms — Streamlit App
========================================
Replaces the Flask + portal.html setup with a pure-Streamlit interface.

Run:
    streamlit run app.py

Slack bot one-time setup:
  1. https://api.slack.com/apps  → Create New App
  2. OAuth & Permissions → Bot Token Scopes:
       chat:write   users:read   users:read.email
       im:write     conversations:open
  3. Install to Workspace → copy the Bot Token (xoxb-...)
  4. Paste it in Settings → Slack tab

Streamlit Cloud deployment:
  Add secrets in the Streamlit Cloud dashboard (or .streamlit/secrets.toml):
    SLACK_TOKEN   = "xoxb-..."
    WA_TOKEN      = "EAA..."
    WA_PHONE_ID   = "1234567890"
    SMTP_PASSWORD = "app-password"
"""

from __future__ import annotations

import io
import json
import os
import re
import smtplib
import time
import uuid
from datetime import date
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import pandas as pd
import requests
import streamlit as st

# ── optional slack-sdk ─────────────────────────────────────────────────────
try:
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError
    _SLACK_SDK = True
except ImportError:
    _SLACK_SDK = False

# ═══════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════════

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "portal_config.json")

DEFAULT_ADMIN_EMAIL = "snigdha.arora@cars24.com"

_DEFAULT_CONFIG: dict = {
    "categories": [
        {
            "id": "high_regularization",
            "name": "High Regularization Reasons",
            "icon": "🔴",
            "type": "template",
            "template": (
                "Hi {employee_name},\n\n"
                "Basis the recent audit, we observed that you have done attendance "
                "regularizations for more than 50% of the times in the month of {month_name}.\n\n"
                "Request you to please share the reason/background for the same so that we can "
                "close the audit observation from our end.\n\nThanks."
            ),
            "variables": ["employee_name", "month_name"],
        },
        {
            "id": "missing_profile",
            "name": "Missing Profile Details",
            "icon": "📋",
            "type": "template",
            "template": (
                "Hi {employee_name},\n\n"
                "We observed that {pi_type} is currently missing/incomplete in your "
                "employee records.\n\n"
                "Request you to please share/update the required information at the earliest "
                "so that we can keep your records updated and complete the verification "
                "from our end.\n\nThanks!"
            ),
            "variables": ["employee_name", "pi_type"],
        },
        {
            "id": "policy_updates",
            "name": "Policy Updates",
            "icon": "📢",
            "type": "custom",
            "template": "",
            "variables": [],
        },
    ],
    "settings": {
        "slackToken": "",
        "waPhoneId": "",
        "waToken": "",
        "smtpFromName": "HR Team",
        "smtpFromEmail": "",
        "smtpHost": "smtp.gmail.com",
        "smtpPort": 587,
        "smtpPassword": "",
        "orgName": "Cars24",
        "emailSignature": "Thanks,\nHR Team",
        "hrSlackEmail": DEFAULT_ADMIN_EMAIL,
    },
    "admins": [
        {
            "name": "Snigdha Arora",
            "email": DEFAULT_ADMIN_EMAIL,
            "role": "admin",
            "added": str(date.today()),
        }
    ],
}

# Column alias map for auto-detection
COLUMN_ALIASES: dict[str, list[str]] = {
    "employee_name": ["employee_name", "employee name", "name", "full name", "emp name", "employee", "emp_name"],
    "email":         ["email", "email address", "e-mail", "mail", "email id", "emailid"],
    "phone":         ["phone", "mobile", "whatsapp", "contact", "phone number", "mobile number", "phonenumber", "mobilenumber"],
    "month_name":    ["month", "month_name", "month name"],
    "pi_type":       ["pi_type", "pi type", "info type", "missing info", "document type", "infotype", "documenttype"],
}


# ═══════════════════════════════════════════════════════════════════════════
# CONFIG HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _get_secret(key: str, fallback: str = "") -> str:
    """Read from st.secrets first, then os.environ, then fallback."""
    try:
        return st.secrets.get(key, "") or os.environ.get(key, fallback)
    except Exception:
        return os.environ.get(key, fallback)


def load_config() -> dict:
    """Load config from disk; seed defaults if missing."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        # Back-fill any top-level keys that might be missing in old config
        for key, val in _DEFAULT_CONFIG.items():
            cfg.setdefault(key, val)
        cfg.setdefault("settings", {})
        for k, v in _DEFAULT_CONFIG["settings"].items():
            cfg["settings"].setdefault(k, v)
    else:
        cfg = json.loads(json.dumps(_DEFAULT_CONFIG))
        save_config(cfg)

    # Inject st.secrets / env vars for credential fields that are blank
    s = cfg["settings"]
    if not s.get("slackToken"):
        s["slackToken"] = _get_secret("SLACK_TOKEN")
    if not s.get("waToken"):
        s["waToken"] = _get_secret("WA_TOKEN")
    if not s.get("waPhoneId"):
        s["waPhoneId"] = _get_secret("WA_PHONE_ID")
    if not s.get("smtpPassword"):
        s["smtpPassword"] = _get_secret("SMTP_PASSWORD")

    return cfg


def save_config(cfg: dict) -> None:
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2, ensure_ascii=False)


def get_cfg() -> dict:
    """Return config from session_state (load once per session)."""
    if "config" not in st.session_state:
        st.session_state["config"] = load_config()
    return st.session_state["config"]


def persist_cfg() -> None:
    """Save the session_state config to disk."""
    save_config(st.session_state["config"])


# ═══════════════════════════════════════════════════════════════════════════
# SEND FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def send_slack(token: str, email: str, message: str,
               attachment: tuple[str, bytes] | None = None) -> str:
    """Send a one-way Slack bot DM to the user identified by email.
    Returns "OK" on success, or an error string on failure."""
    if not token:
        return "Error: Slack token not configured"
    if not email:
        return "Error: No email address"
    if not _SLACK_SDK:
        return "Error: slack-sdk not installed (pip install slack-sdk)"
    try:
        client = WebClient(token=token)
        user_resp = client.users_lookupByEmail(email=email)
        user_id = user_resp["user"]["id"]
        channel_resp = client.conversations_open(users=user_id)
        channel_id = channel_resp["channel"]["id"]
        if attachment:
            fname, fbytes = attachment
            client.files_upload_v2(
                channel=channel_id,
                filename=fname,
                file=io.BytesIO(fbytes),
                initial_comment=message,
            )
        else:
            client.chat_postMessage(channel=channel_id, text=message)
        return "OK"
    except SlackApiError as exc:
        err = exc.response.get("error", str(exc))
        return f"Error: {err}"
    except Exception as exc:
        return f"Error: {exc}"


def send_email(settings: dict, to: str, subject: str, body: str,
               attachment: tuple[str, bytes] | None = None) -> str:
    """Send email via SMTP with STARTTLS.
    Returns "OK" on success, or an error string on failure."""
    username = settings.get("smtpFromEmail", "").strip()
    password = settings.get("smtpPassword", "").strip()
    from_name = settings.get("smtpFromName", "HR Team")
    host = settings.get("smtpHost", "smtp.gmail.com")
    port = int(settings.get("smtpPort", 587))

    if not username:
        return "Error: From email not configured — go to Settings"
    if not password:
        return "Error: SMTP password not configured — go to Settings"
    if not to:
        return "Error: No recipient email"

    try:
        msg = MIMEMultipart("mixed" if attachment else "alternative")
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{username}>"
        msg["To"] = to
        msg.attach(MIMEText(body, "plain", "utf-8"))

        if attachment:
            fname, fbytes = attachment
            part = MIMEBase("application", "octet-stream")
            part.set_payload(fbytes)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{fname}"')
            msg.attach(part)

        with smtplib.SMTP(host, port, timeout=20) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            srv.login(username, password)
            srv.sendmail(username, to, msg.as_string())
        return "OK"
    except smtplib.SMTPAuthenticationError:
        return "Error: SMTP authentication failed — check credentials"
    except smtplib.SMTPException as exc:
        return f"Error: SMTP — {exc}"
    except Exception as exc:
        return f"Error: {exc}"


def send_whatsapp(token: str, phone_id: str, phone: str, message: str) -> str:
    """Send a WhatsApp message via Meta Cloud API v19.0.
    Returns "OK" on success, or an error string on failure."""
    if not token or not phone_id:
        return "Error: WhatsApp credentials not configured"
    if not phone:
        return "Error: No phone number"

    clean_phone = "".join(c for c in phone if c.isdigit())
    if not clean_phone:
        return "Error: Invalid phone number"

    try:
        resp = requests.post(
            f"https://graph.facebook.com/v19.0/{phone_id}/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "to": clean_phone,
                "type": "text",
                "text": {"body": message},
            },
            timeout=15,
        )
        data = resp.json()
        if resp.ok and data.get("messages"):
            return "OK"
        err = data.get("error", {})
        msg = err.get("message", str(data)) if isinstance(err, dict) else str(data)
        return f"Error: {msg}"
    except requests.exceptions.Timeout:
        return "Error: Request timed out"
    except Exception as exc:
        return f"Error: {exc}"


# ═══════════════════════════════════════════════════════════════════════════
# UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def extract_variables(template: str) -> list[str]:
    """Extract {variable} placeholders from a template string."""
    return list(dict.fromkeys(re.findall(r"\{(\w+)\}", template)))


def render_message(template: str, row: dict) -> str:
    """Substitute template placeholders with row values."""
    msg = template
    for key, val in row.items():
        msg = msg.replace(f"{{{key}}}", str(val) if pd.notna(val) else "")
    return msg


def auto_detect_column(df_cols: list[str], field: str) -> str | None:
    """Return the first df column that matches any alias for the given field."""
    aliases = COLUMN_ALIASES.get(field, [field])
    lower_cols = {c.lower().strip(): c for c in df_cols}
    for alias in aliases:
        if alias.lower() in lower_cols:
            return lower_cols[alias.lower()]
    return None


def category_options(cfg: dict) -> list[str]:
    return [f"{c['icon']} {c['name']}" for c in cfg["categories"]]


def find_category(cfg: dict, display: str) -> dict | None:
    for c in cfg["categories"]:
        if f"{c['icon']} {c['name']}" == display:
            return c
    return None


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: SEND MESSAGES
# ═══════════════════════════════════════════════════════════════════════════

def page_send():
    cfg = get_cfg()
    settings = cfg["settings"]

    st.header("📤 Send Messages")

    # ── Row 1: Category + Channels ─────────────────────────────────────────
    col_cat, col_chan = st.columns([3, 2])

    with col_cat:
        st.subheader("Message Category")
        cat_labels = category_options(cfg)
        chosen_label = st.selectbox("Select category", cat_labels, key="sel_category")
        category = find_category(cfg, chosen_label)

        if category:
            st.markdown("**Template preview:**")
            if category["type"] == "template" and category.get("template"):
                st.code(category["template"], language=None)
            else:
                custom_msg = st.text_area(
                    "Custom message",
                    value=category.get("template", ""),
                    height=160,
                    key="custom_message_body",
                    placeholder="Type your message here…",
                )
                category = dict(category)  # local copy
                category["template"] = custom_msg

    with col_chan:
        st.subheader("Channels")
        use_slack = st.checkbox("💬 Slack", value=True, key="chan_slack")
        use_email = st.checkbox("📧 Email", key="chan_email")
        use_whatsapp = st.checkbox("📱 WhatsApp", key="chan_whatsapp")

        # Credential warnings
        if use_slack and not settings.get("slackToken"):
            st.warning("Slack token not set — go to ⚙️ Settings")
        if use_email and (not settings.get("smtpFromEmail") or not settings.get("smtpPassword")):
            st.warning("SMTP credentials not configured — go to ⚙️ Settings")
        if use_whatsapp and (not settings.get("waToken") or not settings.get("waPhoneId")):
            st.warning("WhatsApp credentials not configured — go to ⚙️ Settings")

    if not (use_slack or use_email or use_whatsapp):
        st.info("Select at least one channel above.")
        return

    st.divider()

    # ── Row 2: File Upload ─────────────────────────────────────────────────
    st.subheader("Employee List")
    uploaded = st.file_uploader(
        "Upload Excel or CSV file",
        type=["xlsx", "xls", "csv"],
        key="emp_file",
    )

    if not uploaded:
        st.info("Upload an employee list to continue.")
        return

    # Parse file
    try:
        if uploaded.name.endswith(".csv"):
            df = pd.read_csv(uploaded)
        else:
            df = pd.read_excel(uploaded)
        df.columns = [str(c).strip() for c in df.columns]
        df = df.dropna(how="all")
    except Exception as exc:
        st.error(f"Could not read file: {exc}")
        return

    if df.empty:
        st.error("The uploaded file is empty.")
        return

    st.success(f"Loaded {len(df)} rows · {len(df.columns)} columns")

    # ── Row 3: Column Mapping ──────────────────────────────────────────────
    st.subheader("Column Mapping")

    # Determine which fields are needed
    required_fields: list[str] = []
    if category:
        vars_in_template = extract_variables(category.get("template", ""))
        required_fields = list(dict.fromkeys(vars_in_template))

    channel_fields: list[str] = []
    if use_slack or use_email:
        channel_fields.append("email")
    if use_whatsapp:
        channel_fields.append("phone")

    all_fields = list(dict.fromkeys(channel_fields + required_fields))

    df_cols = list(df.columns)
    none_option = "— not mapped —"
    col_options = [none_option] + df_cols

    mapping: dict[str, str | None] = {}
    map_cols = st.columns(min(len(all_fields), 4)) if all_fields else []

    for i, field in enumerate(all_fields):
        auto = auto_detect_column(df_cols, field)
        default_idx = (col_options.index(auto) if auto and auto in col_options else 0)
        col_widget = map_cols[i % len(map_cols)] if map_cols else st
        sel = col_widget.selectbox(
            f"`{field}`",
            col_options,
            index=default_idx,
            key=f"map_{field}",
        )
        mapping[field] = sel if sel != none_option else None

    # Validate required mappings
    missing_maps = [f for f in channel_fields if not mapping.get(f)]
    if missing_maps:
        st.warning(f"Map these channel columns before sending: {', '.join(missing_maps)}")

    # ── Row 4: Message Preview ─────────────────────────────────────────────
    if category and category.get("template"):
        st.subheader("Message Preview")
        preview_rows = df.head(3)
        tab_labels = [f"Row {i+1}" for i in range(len(preview_rows))]
        tabs = st.tabs(tab_labels)
        for ti, (_, row) in enumerate(preview_rows.iterrows()):
            row_dict = {}
            for field, col in mapping.items():
                if col and col in df.columns:
                    row_dict[field] = row[col]
            with tabs[ti]:
                preview = render_message(category["template"], row_dict)
                st.text(preview)

    # ── Row 5: Email Subject (only if email channel) ───────────────────────
    email_subject = "HR Communication — Cars24"
    if use_email:
        email_subject = st.text_input(
            "Email subject",
            value="HR Communication — Cars24",
            key="email_subject",
        )

    # ── Row 6: Attachment (optional) ───────────────────────────────────────
    st.subheader("📎 Attachment (optional)")
    attachment_file = st.file_uploader(
        "Attach a file to send along with the message (Slack & Email only)",
        type=["pdf", "png", "jpg", "jpeg", "gif", "doc", "docx", "xlsx", "xls", "csv", "txt", "ppt", "pptx"],
        key="attachment_file",
    )
    attachment: tuple[str, bytes] | None = None
    if attachment_file:
        attachment = (attachment_file.name, attachment_file.read())
        st.caption(f"Selected: **{attachment_file.name}** ({len(attachment[1]) / 1024:.1f} KB)")
        if use_whatsapp and not (use_slack or use_email):
            st.info("Attachments are supported on Slack and Email only — WhatsApp will receive the text message.")

    st.divider()

    # ── Send Button ────────────────────────────────────────────────────────
    if st.button("🚀 Send Messages", type="primary", use_container_width=True):
        if missing_maps:
            st.error(f"Cannot send — map all required columns first: {', '.join(missing_maps)}")
            return
        if not category or not category.get("template"):
            st.error("No message template configured for this category.")
            return

        results: list[dict] = []
        progress_bar = st.progress(0, text="Sending…")
        total = len(df)

        for idx, (_, row) in enumerate(df.iterrows()):
            row_dict = {}
            for field, col in mapping.items():
                if col and col in df.columns:
                    row_dict[field] = row[col]

            message = render_message(category["template"], row_dict)
            email_addr = str(row_dict.get("email", "")).strip()
            phone_num = str(row_dict.get("phone", "")).strip()

            result_row: dict[str, Any] = {
                "name": row_dict.get("employee_name", f"Row {idx+1}"),
                "email": email_addr,
                "phone": phone_num,
            }

            # Slack
            if use_slack:
                if email_addr:
                    result_row["slack"] = send_slack(
                        settings.get("slackToken", ""), email_addr, message, attachment
                    )
                else:
                    result_row["slack"] = "Skipped: no email"

            # Email
            if use_email:
                if email_addr:
                    signature = settings.get("emailSignature", "")
                    full_body = f"{message}\n\n{signature}".strip() if signature else message
                    result_row["email_status"] = send_email(
                        settings, email_addr, email_subject, full_body, attachment
                    )
                else:
                    result_row["email_status"] = "Skipped: no email"

            # WhatsApp
            if use_whatsapp:
                if phone_num:
                    result_row["whatsapp"] = send_whatsapp(
                        settings.get("waToken", ""),
                        settings.get("waPhoneId", ""),
                        phone_num,
                        message,
                    )
                else:
                    result_row["whatsapp"] = "Skipped: no phone"

            results.append(result_row)
            progress_bar.progress((idx + 1) / total, text=f"Sending… {idx+1}/{total}")

        progress_bar.empty()

        # ── Results summary ────────────────────────────────────────────────
        results_df = pd.DataFrame(results)
        ok_count = sum(
            1 for r in results
            if all(
                v == "OK"
                for k, v in r.items()
                if k in ("slack", "email_status", "whatsapp")
            )
        )
        st.success(f"Done! {ok_count}/{total} sent successfully.")
        st.dataframe(results_df, use_container_width=True)

        # Download button
        csv_bytes = results_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download Results CSV",
            data=csv_bytes,
            file_name=f"send_results_{date.today()}.csv",
            mime="text/csv",
            use_container_width=True,
        )


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: CATEGORIES
# ═══════════════════════════════════════════════════════════════════════════

def page_categories():
    cfg = get_cfg()
    st.header("📁 Message Categories")

    # ── Edit existing categories ───────────────────────────────────────────
    st.subheader("Existing Categories")
    cats = cfg["categories"]
    to_delete: int | None = None

    for i, cat in enumerate(cats):
        with st.expander(f"{cat.get('icon','📄')} {cat['name']}", expanded=False):
            c1, c2, c3 = st.columns([1, 3, 2])
            with c1:
                new_icon = st.text_input("Icon", value=cat.get("icon", "📄"), key=f"cat_icon_{i}")
            with c2:
                new_name = st.text_input("Name", value=cat["name"], key=f"cat_name_{i}")
            with c3:
                new_type = st.selectbox(
                    "Type",
                    ["template", "custom"],
                    index=0 if cat.get("type", "template") == "template" else 1,
                    key=f"cat_type_{i}",
                )

            new_template = st.text_area(
                "Template (use {variable} for placeholders)",
                value=cat.get("template", ""),
                height=180,
                key=f"cat_template_{i}",
            )

            detected_vars = extract_variables(new_template)
            if detected_vars:
                st.caption(f"Variables detected: {', '.join(f'`{{{v}}}`' for v in detected_vars)}")

            btn_col1, btn_col2 = st.columns(2)
            with btn_col1:
                if st.button("💾 Save", key=f"cat_save_{i}", use_container_width=True):
                    cats[i]["icon"] = new_icon.strip() or "📄"
                    cats[i]["name"] = new_name.strip() or cat["name"]
                    cats[i]["type"] = new_type
                    cats[i]["template"] = new_template
                    cats[i]["variables"] = detected_vars
                    persist_cfg()
                    st.success("Category saved.")
                    st.rerun()
            with btn_col2:
                if st.button("🗑️ Delete", key=f"cat_del_{i}", use_container_width=True):
                    to_delete = i

    if to_delete is not None:
        cfg["categories"].pop(to_delete)
        persist_cfg()
        st.success("Category deleted.")
        st.rerun()

    st.divider()

    # ── Add new category ───────────────────────────────────────────────────
    st.subheader("Add New Category")
    with st.form("add_category_form", clear_on_submit=True):
        fc1, fc2, fc3 = st.columns([1, 3, 2])
        with fc1:
            new_icon = st.text_input("Icon", value="📄")
        with fc2:
            new_name = st.text_input("Name", placeholder="e.g. Onboarding Welcome")
        with fc3:
            new_type = st.selectbox("Type", ["template", "custom"])

        new_template = st.text_area(
            "Template",
            height=140,
            placeholder="Hi {employee_name},\n\nYour message here…",
        )

        submitted = st.form_submit_button("➕ Add Category", use_container_width=True)
        if submitted:
            if not new_name.strip():
                st.error("Category name is required.")
            else:
                new_cat = {
                    "id": re.sub(r"\W+", "_", new_name.lower()).strip("_") + "_" + uuid.uuid4().hex[:6],
                    "name": new_name.strip(),
                    "icon": new_icon.strip() or "📄",
                    "type": new_type,
                    "template": new_template,
                    "variables": extract_variables(new_template),
                }
                cfg["categories"].append(new_cat)
                persist_cfg()
                st.success(f"Category '{new_name}' added.")
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: SETTINGS
# ═══════════════════════════════════════════════════════════════════════════

def page_settings():
    cfg = get_cfg()
    s = cfg["settings"]

    st.header("⚙️ Settings")

    tab_slack, tab_email, tab_wa, tab_org = st.tabs(["💬 Slack", "📧 Email", "📱 WhatsApp", "🏢 Org Info"])

    # ── Slack ──────────────────────────────────────────────────────────────
    with tab_slack:
        st.subheader("Slack Configuration")
        secret_hint = " *(from st.secrets)*" if not s.get("slackToken") and _get_secret("SLACK_TOKEN") else ""
        token_val = s.get("slackToken") or _get_secret("SLACK_TOKEN")
        new_token = st.text_input(
            f"Bot User OAuth Token{secret_hint}",
            value=token_val,
            type="password",
            placeholder="xoxb-...",
        )
        new_hr_email = st.text_input(
            "HR Slack Email (optional — for bot identity)",
            value=s.get("hrSlackEmail", ""),
            placeholder=DEFAULT_ADMIN_EMAIL,
        )

        c1, c2 = st.columns(2)
        with c1:
            if st.button("💾 Save Slack Settings", use_container_width=True):
                s["slackToken"] = new_token.strip()
                s["hrSlackEmail"] = new_hr_email.strip()
                persist_cfg()
                st.success("Slack settings saved.")
        with c2:
            if st.button("🔌 Test Connection", use_container_width=True):
                if not new_token.strip():
                    st.error("Enter a Slack token first.")
                elif not _SLACK_SDK:
                    st.error("slack-sdk not installed. Run: pip install slack-sdk")
                else:
                    try:
                        client = WebClient(token=new_token.strip())
                        resp = client.auth_test()
                        st.success(f"Connected as **{resp['user']}** on **{resp['team']}**")
                    except Exception as exc:
                        st.error(f"Connection failed: {exc}")

        st.caption(
            "Required bot scopes: `chat:write` · `users:read` · `users:read.email` · "
            "`im:write` · `conversations:open`"
        )

    # ── Email ──────────────────────────────────────────────────────────────
    with tab_email:
        st.subheader("SMTP / Email Configuration")
        new_from_email = st.text_input("From Email", value=s.get("smtpFromEmail", ""), placeholder="hr@company.com")
        new_from_name = st.text_input("From Name", value=s.get("smtpFromName", "HR Team"))
        ec1, ec2 = st.columns(2)
        with ec1:
            new_host = st.text_input("SMTP Host", value=s.get("smtpHost", "smtp.gmail.com"))
        with ec2:
            new_port = st.number_input("SMTP Port", value=int(s.get("smtpPort", 587)), min_value=1, max_value=65535, step=1)

        pw_hint = " *(from st.secrets)*" if not s.get("smtpPassword") and _get_secret("SMTP_PASSWORD") else ""
        new_pw = st.text_input(
            f"App Password / SMTP Password{pw_hint}",
            value=s.get("smtpPassword") or _get_secret("SMTP_PASSWORD"),
            type="password",
            placeholder="Gmail app password",
        )
        new_sig = st.text_area("Email Signature", value=s.get("emailSignature", "Thanks,\nHR Team"), height=100)

        ec3, ec4 = st.columns(2)
        with ec3:
            if st.button("💾 Save Email Settings", use_container_width=True):
                s["smtpFromEmail"] = new_from_email.strip()
                s["smtpFromName"] = new_from_name.strip()
                s["smtpHost"] = new_host.strip()
                s["smtpPort"] = int(new_port)
                s["smtpPassword"] = new_pw.strip()
                s["emailSignature"] = new_sig
                persist_cfg()
                st.success("Email settings saved.")
        with ec4:
            if st.button("📨 Send Test Email", use_container_width=True):
                test_settings = {
                    "smtpFromEmail": new_from_email.strip(),
                    "smtpFromName": new_from_name.strip(),
                    "smtpHost": new_host.strip(),
                    "smtpPort": int(new_port),
                    "smtpPassword": new_pw.strip(),
                    "emailSignature": "",
                }
                result = send_email(
                    test_settings,
                    new_from_email.strip(),
                    "HR Portal — Test Email",
                    "This is a test email from the HR Communication Portal.",
                )
                if result == "OK":
                    st.success(f"Test email sent to {new_from_email}!")
                else:
                    st.error(result)

        st.caption("For Gmail: enable 2-Step Verification → Google Account → Security → App Passwords.")

    # ── WhatsApp ───────────────────────────────────────────────────────────
    with tab_wa:
        st.subheader("WhatsApp (Meta Cloud API)")
        wa_tok_hint = " *(from st.secrets)*" if not s.get("waToken") and _get_secret("WA_TOKEN") else ""
        wa_pid_hint = " *(from st.secrets)*" if not s.get("waPhoneId") and _get_secret("WA_PHONE_ID") else ""

        new_wa_token = st.text_input(
            f"Access Token{wa_tok_hint}",
            value=s.get("waToken") or _get_secret("WA_TOKEN"),
            type="password",
            placeholder="EAAxxxxxxxx",
        )
        new_wa_pid = st.text_input(
            f"Phone Number ID{wa_pid_hint}",
            value=s.get("waPhoneId") or _get_secret("WA_PHONE_ID"),
            placeholder="1234567890",
        )

        wc1, wc2 = st.columns(2)
        with wc1:
            if st.button("💾 Save WhatsApp Settings", use_container_width=True):
                s["waToken"] = new_wa_token.strip()
                s["waPhoneId"] = new_wa_pid.strip()
                persist_cfg()
                st.success("WhatsApp settings saved.")
        with wc2:
            if st.button("🔌 Test Connection", key="wa_test", use_container_width=True):
                tok = new_wa_token.strip()
                pid = new_wa_pid.strip()
                if not tok or not pid:
                    st.error("Enter token and phone ID first.")
                else:
                    try:
                        r = requests.get(
                            f"https://graph.facebook.com/v19.0/{pid}",
                            headers={"Authorization": f"Bearer {tok}"},
                            timeout=10,
                        )
                        d = r.json()
                        if d.get("id"):
                            st.success(f"Connected! Number: {d.get('display_phone_number', d['id'])}")
                        else:
                            err = d.get("error", {})
                            msg = err.get("message", str(d)) if isinstance(err, dict) else str(d)
                            st.error(f"Failed: {msg}")
                    except Exception as exc:
                        st.error(f"Error: {exc}")

        st.caption("Get credentials from Meta Business Suite → WhatsApp → API Setup.")

    # ── Org Info ───────────────────────────────────────────────────────────
    with tab_org:
        st.subheader("Organisation Information")
        new_org = st.text_input("Organisation Name", value=s.get("orgName", "Cars24"))

        if st.button("💾 Save Org Info", use_container_width=True):
            s["orgName"] = new_org.strip()
            persist_cfg()
            st.success("Org info saved.")


# ═══════════════════════════════════════════════════════════════════════════
# PAGE: ADMIN MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

def page_admin():
    cfg = get_cfg()
    admins = cfg.get("admins", [])

    st.header("👥 Admin Management")

    # ── Admins table ───────────────────────────────────────────────────────
    st.subheader("Current Admins")
    if admins:
        admins_df = pd.DataFrame(admins)
        st.dataframe(admins_df, use_container_width=True, hide_index=True)
    else:
        st.info("No admins configured.")

    st.divider()

    # ── Remove admin ───────────────────────────────────────────────────────
    if len(admins) > 1:
        st.subheader("Remove Admin")
        removable = [f"{a['name']} ({a['email']})" for a in admins[1:]]
        to_remove_label = st.selectbox("Select admin to remove", removable, key="remove_admin_sel")
        if st.button("🗑️ Remove Selected Admin", use_container_width=True):
            remove_idx = removable.index(to_remove_label) + 1  # offset by 1 (protect first)
            removed = cfg["admins"].pop(remove_idx)
            persist_cfg()
            st.success(f"Removed admin: {removed['name']} ({removed['email']})")
            st.rerun()
    else:
        st.info("The primary admin cannot be removed.")

    st.divider()

    # ── Add admin ──────────────────────────────────────────────────────────
    st.subheader("Add New Admin")
    with st.form("add_admin_form", clear_on_submit=True):
        ac1, ac2 = st.columns(2)
        with ac1:
            new_admin_name = st.text_input("Full Name", placeholder="Jane Doe")
        with ac2:
            new_admin_email = st.text_input("Email", placeholder="jane.doe@cars24.com")

        new_admin_role = st.selectbox("Role", ["admin", "viewer"])

        submitted = st.form_submit_button("➕ Add Admin", use_container_width=True)
        if submitted:
            if not new_admin_name.strip() or not new_admin_email.strip():
                st.error("Name and email are required.")
            elif any(a["email"].lower() == new_admin_email.strip().lower() for a in admins):
                st.warning(f"{new_admin_email} is already an admin.")
            else:
                cfg["admins"].append({
                    "name": new_admin_name.strip(),
                    "email": new_admin_email.strip().lower(),
                    "role": new_admin_role,
                    "added": str(date.today()),
                })
                persist_cfg()
                st.success(f"Admin '{new_admin_name}' added.")
                st.rerun()


# ═══════════════════════════════════════════════════════════════════════════
# APP SHELL — SIDEBAR + ROUTING
# ═══════════════════════════════════════════════════════════════════════════

def main():
    st.set_page_config(
        page_title="People Comms",
        page_icon="All Blue.png",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # ── Sidebar ────────────────────────────────────────────────────────────
    with st.sidebar:
        # Logo / branding
        st.image("All Blue.png", width=180)
        st.markdown(
            """
            <div style="text-align:center; padding: 0.1rem 0 0.5rem 0;">
                <div style="font-size:1.1rem; font-weight:700; color:#4B4BF7;">People Comms</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.divider()

        page = st.radio(
            "Navigation",
            ["📤 Send Messages", "📁 Categories", "⚙️ Settings", "👥 Admin Management"],
            label_visibility="collapsed",
        )

        st.divider()
        cfg = get_cfg()
        s = cfg["settings"]
        # Quick status indicators
        slack_ok = bool(s.get("slackToken"))
        email_ok = bool(s.get("smtpFromEmail") and s.get("smtpPassword"))
        wa_ok = bool(s.get("waToken") and s.get("waPhoneId"))

        st.markdown("**Channel Status**")
        st.markdown(
            f"{'🟢' if slack_ok else '🔴'} Slack  \n"
            f"{'🟢' if email_ok else '🔴'} Email  \n"
            f"{'🟢' if wa_ok else '🔴'} WhatsApp"
        )

    # ── Page routing ───────────────────────────────────────────────────────
    if page == "📤 Send Messages":
        page_send()
    elif page == "📁 Categories":
        page_categories()
    elif page == "⚙️ Settings":
        page_settings()
    elif page == "👥 Admin Management":
        page_admin()


if __name__ == "__main__":
    main()
