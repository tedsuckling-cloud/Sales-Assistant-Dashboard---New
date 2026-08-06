import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from io import BytesIO, StringIO
from html import escape
from html.parser import HTMLParser
from datetime import date, datetime, timedelta
from pathlib import Path
import os
import uuid
import shutil
import hashlib

from flask import Flask, redirect, render_template_string, request, send_file, url_for, send_from_directory
import pandas as pd
from bs4 import BeautifulSoup
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_PATH = DATA_DIR / "sales_history.db"
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

def audit_event(event_type, detail="", trip_id=None, appointment_id=""):
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute("INSERT INTO audit_log (event_type,detail,trip_id,appointment_id,created_at) VALUES (?,?,?,?,?)",
                               (event_type, str(detail or ""), trip_id, appointment_id or "", datetime.now().isoformat(timespec="seconds")))
            connection.commit()
    except Exception:
        pass

def create_database_backup(reason="manual"):
    if not DATABASE_PATH.exists():
        return None
    stamp=datetime.now().strftime("%Y%m%d-%H%M%S")
    target=BACKUP_DIR / f"sales_history-{stamp}-{re.sub(r'[^A-Za-z0-9_-]+','_',reason)[:30]}.db"
    with sqlite3.connect(DATABASE_PATH) as source, sqlite3.connect(target) as dest:
        source.backup(dest)
    return target

def appointment_uid(trip_id, schedule_id, visit_date, visit_time, store_name):
    raw=f"{trip_id or 0}|{schedule_id or 0}|{visit_date}|{visit_time}|{str(store_name).strip().casefold()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()

def ensure_reliability_tables(connection):
    """Create Stage 9 support tables and columns before any reliability feature uses them."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS app_state (
            state_key TEXT PRIMARY KEY,
            state_value TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            trip_id INTEGER,
            appointment_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS trip_appointments (
            appointment_id TEXT PRIMARY KEY,
            trip_id INTEGER NOT NULL,
            schedule_id INTEGER NOT NULL,
            visit_date TEXT NOT NULL,
            visit_time TEXT NOT NULL DEFAULT '',
            store_name TEXT NOT NULL,
            sequence_no INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )"""
    )
    table_names = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "road_visit_notes" in table_names:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(road_visit_notes)").fetchall()}
        if "appointment_id" not in columns:
            connection.execute("ALTER TABLE road_visit_notes ADD COLUMN appointment_id TEXT NOT NULL DEFAULT ''")
    if "road_visit_attachments" in table_names:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(road_visit_attachments)").fetchall()}
        if "appointment_id" not in columns:
            connection.execute("ALTER TABLE road_visit_attachments ADD COLUMN appointment_id TEXT NOT NULL DEFAULT ''")


def set_active_trip(trip_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        ensure_reliability_tables(connection)
        connection.execute(
            "INSERT INTO app_state (state_key,state_value,updated_at) VALUES ('active_trip_id',?,?) "
            "ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at",
            (str(trip_id or ""), datetime.now().isoformat(timespec="seconds")),
        )
        connection.commit()


def get_active_trip_id():
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            ensure_reliability_tables(connection)
            row = connection.execute(
                "SELECT state_value FROM app_state WHERE state_key='active_trip_id'"
            ).fetchone()
            connection.commit()
    except sqlite3.Error:
        return None
    try:
        return int(row[0]) if row and row[0] else None
    except (TypeError, ValueError):
        return None

ROUTE_DEFAULTS = {
    'rep_start': '07:00',
    'rep_end': '18:00',
    'customer_open': '08:00',
    'customer_close': '17:00',
}


def get_route_defaults():
    values = dict(ROUTE_DEFAULTS)
    try:
        with sqlite3.connect(DATABASE_PATH) as connection:
            ensure_reliability_tables(connection)
            rows = connection.execute(
                "SELECT state_key,state_value FROM app_state WHERE state_key IN ('route_rep_start','route_rep_end','route_customer_open','route_customer_close')"
            ).fetchall()
            connection.commit()
        mapping = {
            'route_rep_start': 'rep_start',
            'route_rep_end': 'rep_end',
            'route_customer_open': 'customer_open',
            'route_customer_close': 'customer_close',
        }
        for key, value in rows:
            if key in mapping and re.fullmatch(r'\d{2}:\d{2}', value or ''):
                values[mapping[key]] = value
    except sqlite3.Error:
        pass
    return values


def save_route_defaults(rep_start, rep_end, customer_open, customer_close):
    values = {
        'route_rep_start': rep_start,
        'route_rep_end': rep_end,
        'route_customer_open': customer_open,
        'route_customer_close': customer_close,
    }
    if not all(re.fullmatch(r'\d{2}:\d{2}', value or '') for value in values.values()):
        raise ValueError('All route-default times must be valid.')
    if rep_end <= rep_start:
        raise ValueError('Rep finish time must be later than rep start time.')
    if customer_close <= customer_open:
        raise ValueError('Customer closing time must be later than opening time.')
    now = datetime.now().isoformat(timespec='seconds')
    with sqlite3.connect(DATABASE_PATH) as connection:
        ensure_reliability_tables(connection)
        for key, value in values.items():
            connection.execute(
                "INSERT INTO app_state (state_key,state_value,updated_at) VALUES (?,?,?) ON CONFLICT(state_key) DO UPDATE SET state_value=excluded.state_value,updated_at=excluded.updated_at",
                (key, value, now),
            )
        connection.commit()

ROUTE_CUSTOMER_SEED = '[{"external_id":"4354","name":"Hammer Hardware Whitianga","street":"225 South Highway","city":"WHITIANGA","zip":"3510","country":"New Zealand","customer_group":"Hammer Hardware","categories":"C","phone":"07 866 4347","email":"whitianga@hammerhardware.co.nz","call_note":"Greens opportunities, Indents","last_call":"2026-02-26","call_interval":120,"urgency":"overdue","latitude":-36.836229,"longitude":175.677105,"portatour_id":"942556f8f94845e6a1cd22933155b61b"},{"external_id":"2035","name":"Carters Whitianga","street":"Moewai Road","city":"WHITIANGA","zip":"3591","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"07 866 2489","email":"whitianga.orders@carters.co.nz","call_note":"","last_call":"2026-02-26","call_interval":180,"urgency":"due","latitude":-36.823986,"longitude":175.671813,"portatour_id":"213441f776e144ef9fd3704f580bce66"},{"external_id":"2497","name":"Mitre 10 Waihi","street":"Seddon Street","city":"WAIHI","zip":"3610","country":"New Zealand","customer_group":"Mitre 10","categories":"B","phone":"07 863 8087","email":"deborah.edwards@mitre10.co.nz","call_note":"Duratuf \'Spot\' for signage and brochures, Carts/greens opportunities","last_call":"2026-02-26","call_interval":90,"urgency":"overdue","latitude":-37.390641,"longitude":175.841702,"portatour_id":"96fa149f3a894190a371966053362b99"},{"external_id":"1981","name":"Bunnings Whangamata","street":"213 Aickin Road","city":"WHANGAMATA","zip":"3620","country":"New Zealand","customer_group":"Bunnings","categories":"B","phone":"07 865 8744","email":"whangamata@bunnings.co.nz","call_note":"Training","last_call":"2026-04-22","call_interval":90,"urgency":"due","latitude":-37.201981,"longitude":175.866447,"portatour_id":"b1db31c086704b209074de1838a67e03"},{"external_id":"2032","name":"Carters Whangamata","street":"103 Lindsay Road","city":"WHANGAMATA","zip":"3620","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"07 865 8590","email":"","call_note":"Establish Contact, Armorsteel","last_call":"2026-04-22","call_interval":180,"urgency":"not due","latitude":-37.202928,"longitude":175.865892,"portatour_id":"8037f226a37a40be9050a1bdcfbcb67d"},{"external_id":"2397","name":"ITM Pauanui","street":"27 Sheppard Avenue","city":"PAUANUI BEACH","zip":"3579","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"07 864 8579","email":"pauanui.itm@xtra.co.nz","call_note":"Greens Parts, Aromachef stock, display?:)","last_call":"2026-02-26","call_interval":180,"urgency":"due","latitude":-37.020933,"longitude":175.858711,"portatour_id":"9877c1d32c484e17b3dca961787a96f3"},{"external_id":"8595","name":"Hammer Hardware Tairua","street":"232A Main Road","city":"","zip":"3508","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"07 846 8080","email":"admin@bltairua.nz","call_note":"","last_call":"2026-04-22","call_interval":180,"urgency":"not due","latitude":-37.001268,"longitude":175.848815,"portatour_id":"9f06676798214ad09953ac61658889b9"},{"external_id":"2159","name":"Placemakers Whitianga","street":"24 Joan Gaskell Drive","city":"WHITIANGA","zip":"3510","country":"New Zealand","customer_group":"Placemakers","categories":"B","phone":"07 867 2000","email":"jessica.hamilton@placemakers.co.nz","call_note":"Displays - July","last_call":"2026-04-22","call_interval":90,"urgency":"due","latitude":-36.834437,"longitude":175.698265,"portatour_id":"d569fd9dc0704dd09ca2d717cb4e85b1"},{"external_id":"2345","name":"ITM Coromandel","street":"1170 Tiki Rd","city":"COROMANDEL","zip":"3506","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"07 866 8848","email":"coroitm@xtra.co.nz","call_note":"","last_call":"2025-10-28","call_interval":180,"urgency":"overdue","latitude":-36.76755,"longitude":175.505402,"portatour_id":"387ac68ed7d24b8cbc1c145802ffe3c5"},{"external_id":"1057","name":"Cooks Beach Timber & Hardware","street":"43 Captain Cook Road","city":"WHITIANGA","zip":"3591","country":"New Zealand","customer_group":"Other","categories":"D","phone":"07 866 4049","email":"shane@cbth.co.nz","call_note":"Follow Up Signage","last_call":"2026-04-22","call_interval":360,"urgency":"not due","latitude":-36.839233,"longitude":175.742924,"portatour_id":"cfa43b115acf49278d6ec7985246c87d"},{"external_id":"2148","name":"Placemakers Thames","street":"79 Kopu Rd","city":"THAMES","zip":"3578","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"07 868 0130","email":"graeme.saunders@placemakers.co.nz","call_note":"Make appt with Graham","last_call":"2026-02-05","call_interval":120,"urgency":"overdue","latitude":-37.188365,"longitude":175.566611,"portatour_id":"62b36a8970c84862808e279241a87591"},{"external_id":"2494","name":"Mitre 10 Thames","street":"458 Pollen Street","city":"THAMES","zip":"3500","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"07 868 7155","email":"thames@mitre10.co.nz","call_note":"Send barrow storage options","last_call":"2026-02-05","call_interval":120,"urgency":"overdue","latitude":-37.140466,"longitude":175.542989,"portatour_id":"f365be63be4b42c5b23935ebabf6944c"},{"external_id":"2025","name":"Carters Thames","street":"State Highway 26","city":"THAMES","zip":"3578","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"07 868 7047","email":"Owen.Hughes@carters.co.nz","call_note":"","last_call":"2026-04-22","call_interval":180,"urgency":"not due","latitude":-37.204746,"longitude":175.600359,"portatour_id":"55dc94edea5244ccbecc07986c7c2459"},{"external_id":"2376","name":"Dayle ITM Kopu","street":"71 Kopu Road","city":"THAMES","zip":"3578","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"07 868 9827","email":"martin@kopuitm.co.nz","call_note":"","last_call":"2026-04-22","call_interval":180,"urgency":"not due","latitude":-37.187409,"longitude":175.566285,"portatour_id":"18bccd63dd34427786ae59d7588a0c76"},{"external_id":"4337","name":"Hammer Hardware Ngatea","street":"16 Orchard Road","city":"NGATEA","zip":"3503","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"07 867 7393","email":"ngatea@hammerhardware.co.nz","call_note":"","last_call":"2026-02-05","call_interval":180,"urgency":"due","latitude":-37.276095,"longitude":175.496979,"portatour_id":"ca089e6a6e814e0eaf13c99dcf880a5f"},{"external_id":"1958","name":"Bunnings New Plymouth","street":"59 Molesworth Street","city":"NEW PLYMOUTH","zip":"4310","country":"New Zealand","customer_group":"Bunnings","categories":"B","phone":"06 758 0491","email":"admin.newplymouth@bunnings.co.nz","call_note":"","last_call":"2026-02-03","call_interval":90,"urgency":"overdue","latitude":-39.05453,"longitude":174.078394,"portatour_id":"5e647e745b754bcdbcfe77cdaeeb6b2b"},{"external_id":"2487","name":"Mitre 10 Taumarunui","street":"Taitua Street","city":"TAUMARUNUI","zip":"3920","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"07 895 8107","email":"taumarunui@mitre10.co.nz","call_note":"","last_call":"2026-07-21","call_interval":120,"urgency":"not due","latitude":-38.884535,"longitude":175.271496,"portatour_id":"e097841ec2c045d69303441346401ea0"},{"external_id":"2403","name":"ITM Standard Timber (Stratford)","street":"75 Warwick Road","city":"STRATFORD","zip":"4332","country":"New Zealand","customer_group":"ITM","categories":"C","phone":"06 765 7800","email":"mike@itmstratford.co.nz","call_note":"Displays","last_call":"2026-07-23","call_interval":120,"urgency":"not due","latitude":-39.350598,"longitude":174.289179,"portatour_id":"3225f0b4f3fc43e6a184e507167f7080"},{"external_id":"2108","name":"Placemakers Hawera","street":"Cnr Waihi Road & Fitzgerald Lane","city":"HAWERA","zip":"4610","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"06 278 6013","email":"maree.beare@placemakers.co.nz","call_note":"","last_call":"2026-07-22","call_interval":120,"urgency":"not due","latitude":-39.566388,"longitude":174.272445,"portatour_id":"21b5098f7aa74b04960a4d0eb0bdf512"},{"external_id":"2501","name":"Mitre 10 Mega Wanganui","street":"200 Taupo Quay","city":"WANGANUI","zip":"4500","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"06 349 0244","email":"brett.puklowski@mitre10.co.nz","call_note":"Training, QBR: GL75 f/u, poly carts for them and stratford","last_call":"2026-02-03","call_interval":45,"urgency":"overdue","latitude":-39.942291,"longitude":175.046628,"portatour_id":"c56d7576a9264e7ea38f43d639e9e324"},{"external_id":"2009","name":"Carters New Plymouth","street":"24 Egmont Road","city":"New Plymouth","zip":"4312","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"06 759 5460","email":"","call_note":"Meeting w/ Nigel re A/S","last_call":"2026-03-17","call_interval":180,"urgency":"due","latitude":-39.044933,"longitude":174.125639,"portatour_id":"f4ba74923d46440a83b268de6d85fe39"},{"external_id":"8637","name":"Bunnings New Plymouth Trade Centre","street":"55 Corbett Road","city":"Bell Block","zip":"4312","country":"New Zealand","customer_group":"Bunnings","categories":"D","phone":"06 755 0304","email":"admin.newplymouth@bunnings.co.nz","call_note":"Armorsteel","last_call":"2026-07-23","call_interval":180,"urgency":"not due","latitude":-39.036536,"longitude":174.162338,"portatour_id":"b3818ffac341418cbbecea5bda49760c"},{"external_id":"2466","name":"Mitre 10 Mega New Plymouth","street":"5 Vickers Rd","city":"NEW PLYMOUTH","zip":"4312","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"06 759 4399","email":"michelle.ebert@meganewplymouth.co.nz","call_note":"*Signage* Meet Les Nairn. LS500, GL75E.","last_call":"2026-07-23","call_interval":45,"urgency":"not due","latitude":-39.046189,"longitude":174.114884,"portatour_id":"54252d8550464864b5b7f585a421778f"},{"external_id":"2028","name":"Carters Wanganui","street":"95 Wilson Street","city":"WANGANUI","zip":"4500","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"06 348 1750","email":"Gwynne.Matthews@carters.co.nz","call_note":"","last_call":"2026-07-22","call_interval":180,"urgency":"not due","latitude":-39.932563,"longitude":175.044414,"portatour_id":"b86f5a4c1b7240438732ad1c10c919df"},{"external_id":"2392","name":"ITM New Plymouth Building Supplies","street":"20 Dawson Street","city":"NEW PLYMOUTH","zip":"4310","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 758 8939","email":"graeme@npbs.co.nz","call_note":"","last_call":"2026-07-23","call_interval":180,"urgency":"not due","latitude":-39.058119,"longitude":174.067635,"portatour_id":"b12279c8c3154bacb6e4e330258a7725"},{"external_id":"2420","name":"ITM Waitara","street":"Cnr Harris & Richmond Streets","city":"WAITARA","zip":"4320","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 754 8822","email":"annette@waitaraitm.co.nz","call_note":"Display Shed *CONTACTED*","last_call":"2026-03-17","call_interval":180,"urgency":"due","latitude":-38.995011,"longitude":174.242646,"portatour_id":"d8e4bb30886949e8b6824687187adb35"},{"external_id":"1944","name":"Bunnings Hawera","street":"175-179 Glover Road","city":"HAWERA","zip":"4610","country":"New Zealand","customer_group":"Bunnings","categories":"B","phone":"06 278 0130","email":"admin.hawera@bunnings.co.nz","call_note":"Armorsteel, brochure Holders","last_call":"2026-07-22","call_interval":90,"urgency":"not due","latitude":-39.577006,"longitude":174.275826,"portatour_id":"bbffc27cbb4944bc915719c848d150b8"},{"external_id":"1978","name":"Bunnings Wanganui","street":"131 Ridgway Street","city":"WANGANUI","zip":"4500","country":"New Zealand","customer_group":"Bunnings","categories":"D","phone":"06 345 0985","email":"wanganui@bunnings.co.nz","call_note":"Training, Shedbuilder tool (Emailed)","last_call":"2026-02-04","call_interval":180,"urgency":"due","latitude":-39.937123,"longitude":175.050769,"portatour_id":"132c776a6bdb4069909738a6b33c340e"},{"external_id":"2130","name":"Placemakers New Plymouth","street":"15 Egmont Road","city":"NEW PLYMOUTH","zip":"4372","country":"New Zealand","customer_group":"Placemakers","categories":"B","phone":"06 755 9040","email":"sharlene.tyler@placemakers.co.nz","call_note":"Clean the sheds, bring Greens brochure","last_call":"2026-03-17","call_interval":90,"urgency":"overdue","latitude":-39.044309,"longitude":174.125318,"portatour_id":"bb909aeb57a8419997e84ce8fa313c60"},{"external_id":"2132","name":"Placemakers Ohakune","street":"7 Burns Street","city":"OHAKUNE","zip":"4625","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"06 385 8414","email":"chrissy.cannan@placemakers.co.nz","call_note":"PA Doors","last_call":"2026-07-21","call_interval":180,"urgency":"not due","latitude":-39.416063,"longitude":175.394762,"portatour_id":"268e0fdbcd1942bbbdd18112db05eae8"},{"external_id":"2154","name":"Placemakers Wanganui","street":"Cnr Wilson & Guyton Streets","city":"WANGANUI","zip":"4500","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"06 349 1919","email":"diane.winter@placemakers.co.nz","call_note":"","last_call":"2025-05-21","call_interval":180,"urgency":"overdue","latitude":-39.93417,"longitude":175.047143,"portatour_id":"e2d0b8c1b6e14f9eb9a8862979d2bfb9"},{"external_id":"2468","name":"Mitre 10 New Plymouth","street":"305-307 St Aubyn Street","city":"NEW PLYMOUTH","zip":"4310","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"067580520","email":"J10Service.Desk@mitre10.co.nz","call_note":"WB Rack for Trade","last_call":"2026-02-03","call_interval":120,"urgency":"overdue","latitude":-39.059737,"longitude":174.058048,"portatour_id":"74b057e1e9ec4d8a90293bfe39e9ebf1"},{"external_id":"2485","name":"Mitre 10 Stratford","street":"Corner Juliet & Fenton Streets","city":"STRATFORD","zip":"4332","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"06 765 6048","email":"hayden.gibson@mitre10.co.nz","call_note":"Polycart F/U. Greens Brochure.","last_call":"2026-07-23","call_interval":120,"urgency":"not due","latitude":-39.340864,"longitude":174.286688,"portatour_id":"c5a60f4c994c428a9231a3ef6708c0f7"},{"external_id":"4329","name":"Hammer Hardware Inglewood","street":"24-26 Matai Street","city":"TARANAKI","zip":"4330","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"06 756 8009","email":"inglewood@hammerhardware.co.nz","call_note":"","last_call":"2026-07-23","call_interval":360,"urgency":"not due","latitude":-39.156818,"longitude":174.207455,"portatour_id":"df83abe4160846bb87ceb47c85a422e5"},{"external_id":"1274","name":"North Taranaki ITM Waiwhakaiho","street":"1 Katere Road","city":"NEW PLYMOUTH","zip":"4312","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 759 7435","email":"","call_note":"Try for shed signage","last_call":"2025-09-02","call_interval":42,"urgency":"not due","latitude":-39.050251,"longitude":174.114737,"portatour_id":"83bfe94429a74cdba402e1e855c23b31"},{"external_id":"","name":"Read Brothers Independent","street":"332 Pollen Street","city":"Thames","zip":"3500","country":"NZ","customer_group":"Other","categories":"","phone":"","email":"","call_note":"","last_call":"2023-12-07","call_interval":180,"urgency":"overdue","latitude":-37.142814,"longitude":175.544132,"portatour_id":"27bd56a74d724fd191fecac767ad0dac"},{"external_id":"2367","name":"ITM Havelock Building Centre","street":"6 Ngati Kuia Drive","city":"South Island","zip":"7100","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 574 1018","email":"manager@itmhavelock.co.nz","call_note":"F/up Armorsteel and Concreters","last_call":"2026-07-29","call_interval":180,"urgency":"not due","latitude":-41.279911,"longitude":173.769678,"portatour_id":"bea1d0fdc8cd4d12bc3d9a61801c75b0"},{"external_id":"1180","name":"Hammer Hardware Picton","street":"53 High Street","city":"PICTON","zip":"7220","country":"New Zealand","customer_group":"Hammer Hardware","categories":"E","phone":"03 573 6422","email":"admin@pictonhardware.co.nz","call_note":"Greens Brochure/opportunities","last_call":"2026-04-29","call_interval":360,"urgency":"not due","latitude":-41.290474,"longitude":174.006767,"portatour_id":"adea5d9d7adf4387807ef4f42116c937"},{"external_id":"4324","name":"Hammer Hardware Golden Bay","street":"7 Commercial Street","city":"TAKAKA","zip":"7110","country":"New Zealand","customer_group":"Hammer Hardware","categories":"C","phone":"03 525 7265","email":"goldenbay@hammerhardware.co.nz","call_note":"Greens brochure/opportunities","last_call":"2026-03-05","call_interval":120,"urgency":"overdue","latitude":-40.860505,"longitude":172.805916,"portatour_id":"697c80a0acd14a69aae2b4d0acce9a7f"},{"external_id":"7125","name":"Hammer Hardware Kaikoura","street":"134-136 Beach Road","city":"Kaikoura","zip":"7300","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"03 319 5013","email":"kaikoura@mitre10.co.nz","call_note":"","last_call":"2026-07-29","call_interval":180,"urgency":"not due","latitude":-42.387018,"longitude":173.679294,"portatour_id":"b25fdc5ebc91457a87cf8b397ae55f66"},{"external_id":"2465","name":"Mitre 10 Mega Nelson","street":"99 Quarantine Rd","city":"NELSON","zip":"7011","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"03 547 0747","email":"brandon.beveridge@meganelson.co.nz","call_note":"Training, Display *HV Task*","last_call":"2026-07-28","call_interval":45,"urgency":"not due","latitude":-41.299489,"longitude":173.239706,"portatour_id":"9031d53807ca4796a63aad8b906bfb35"},{"external_id":"2388","name":"ITM Motueka","street":"16-18 King Edward Street","city":"MOTUEKA","zip":"7120","country":"New Zealand","customer_group":"ITM","categories":"C","phone":"03 528 7254","email":"alistair@itmmotueka.co.nz","call_note":"","last_call":"2026-07-28","call_interval":120,"urgency":"not due","latitude":-41.124256,"longitude":173.008076,"portatour_id":"e7a24813c1ed494e957af90905d251e7"},{"external_id":"2405","name":"ITM Takaka Building Centre","street":"5 Buxton Lane","city":"TAKAKA","zip":"7110","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 525 0005","email":"manager@itmtakaka.co.nz","call_note":"Assemble Loadmaster","last_call":"2026-03-05","call_interval":180,"urgency":"due","latitude":-40.858388,"longitude":172.807279,"portatour_id":"5b62c6718c49444e9a1b89ea64bd41bc"},{"external_id":"1930","name":"Bunnings Blenheim","street":"10 Westwood Avenue","city":"BLENHEIM","zip":"7271","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"09 520 9050","email":"specialorders.blenheim@bunnings.co.nz","call_note":"Training, Armorsteel","last_call":"2026-07-29","call_interval":45,"urgency":"not due","latitude":-41.507877,"longitude":173.924581,"portatour_id":"6d460d3ed0d24038b9c14e2b5409c6e8"},{"external_id":"1956","name":"Bunnings Nelson","street":"76 Saxton Road West","city":"NELSON","zip":"7011","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"03 547 1640","email":"nelson@bunnings.co.nz","call_note":"","last_call":"2026-07-28","call_interval":45,"urgency":"not due","latitude":-41.324374,"longitude":173.215617,"portatour_id":"a63c3e4f36de4833b7278d7b6253a40e"},{"external_id":"2455","name":"Mitre 10 Mega Marlborough","street":"174-186 Alabama Road","city":"BLENHEIM","zip":"7201","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"03 520 6600","email":"purchaseorders.marlborough@mitre10.co.nz","call_note":"Address seasonal gaps, Drill into shed sales drop. *HV Tasks*","last_call":"2026-07-29","call_interval":45,"urgency":"not due","latitude":-41.527848,"longitude":173.964411,"portatour_id":"51cf78a48c4249f3ad40243b9e3f2b98"},{"external_id":"2100","name":"Placemakers Blenheim","street":"Cnr Grove & Farmar Streets","city":"BLENHEIM","zip":"7201","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"03 578 9084","email":"donna.botham@placemakers.co.nz","call_note":"PA Door pricing issue","last_call":"2026-07-29","call_interval":180,"urgency":"not due","latitude":-41.504605,"longitude":173.960208,"portatour_id":"fe7d9804295249e481462c0c2a1bc06c"},{"external_id":"1984","name":"Carters Blenheim","street":"32 New Renwick Road","city":"BLENHEIM","zip":"7201","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"03 577 5344","email":"Shaun.Thompson@carters.co.nz","call_note":"Hygiene, Greens Parts, Armorsteel checkin w/James","last_call":"2026-07-29","call_interval":180,"urgency":"not due","latitude":-41.529079,"longitude":173.938732,"portatour_id":"ccef4f3df88b4cd19e7403cd3fdad230"},{"external_id":"2008","name":"Carters Nelson","street":"70 Nayland Road","city":"NELSON","zip":"7011","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"03 547 7700","email":"","call_note":"","last_call":"2026-07-28","call_interval":180,"urgency":"not due","latitude":-41.300824,"longitude":173.237257,"portatour_id":"c34c32a16d954193b7d8f7a9e0d7c52a"},{"external_id":"2342","name":"ITM Blenheim","street":"41 Houldsworth Street","city":"BLENHEIM","zip":"7201","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 578 3049","email":"rick.may@blenheimitm.co.nz","call_note":"Greens brochure/opportunities","last_call":"2026-07-29","call_interval":180,"urgency":"not due","latitude":-41.517049,"longitude":173.963944,"portatour_id":"2b3b6c706501484f9f8ce6a3dad9ed56"},{"external_id":"2373","name":"ITM Kaikoura Building Centre","street":"218 Beach Road","city":"KAIKOURA","zip":"7300","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 319 5447","email":"richard@kbcitm.co.nz","call_note":"","last_call":"2026-07-29","call_interval":180,"urgency":"not due","latitude":-42.379462,"longitude":173.680836,"portatour_id":"022a1c1fc62a4f83ab83e572e46a37e0"},{"external_id":"2391","name":"ITM Nelson","street":"36 Pascoe Street","city":"NELSON","zip":"7011","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 548 5487","email":"rodney@itmnelson.co.nz","call_note":"Signage, Parts, LS, Flashings","last_call":"2026-04-30","call_interval":180,"urgency":"not due","latitude":-41.292597,"longitude":173.240929,"portatour_id":"bdf6a5ad6c7b4652aeb8a659c74dd5b0"},{"external_id":"2398","name":"ITM Picton","street":"5 York Street","city":"PICTON","zip":"7220","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 573 6888","email":"robyn@pictonitm.co.nz","call_note":"Greens brochure/opportunities","last_call":"2026-04-29","call_interval":180,"urgency":"not due","latitude":-41.291189,"longitude":174.001665,"portatour_id":"c002d80793834cadb85c137f40952c3d"},{"external_id":"2124","name":"Placemakers Nelson, Mot Depot","street":"84 High St","city":"MOTUEKA","zip":"7120","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"03 528 8164","email":"callum.macdonald@placemakers.co.nz","call_note":"PA doors, aiden","last_call":"2026-07-28","call_interval":180,"urgency":"not due","latitude":-41.108714,"longitude":173.011222,"portatour_id":"04e85d0d85ab442d9e48b5c67d70ffa9"},{"external_id":"2128","name":"Placemakers Nelson, Saxton Rd","street":"52 Saxton Road West","city":"NELSON","zip":"7011","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"03 547 9111","email":"rebecca.priest@placemakers.co.nz","call_note":"Check New Displays, Arrange training","last_call":"2026-07-28","call_interval":120,"urgency":"not due","latitude":-41.323593,"longitude":173.214748,"portatour_id":"f232cd3906d84c058db0e446c063121a"},{"external_id":"2137","name":"Placemakers Nelson, (St Vincent Str)","street":"103 St Vincent Street","city":"NELSON","zip":"7010","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"03 547 9111","email":"adam.haycock@placemakers.co.nz","call_note":"PA Doors","last_call":"2024-10-09","call_interval":180,"urgency":"overdue","latitude":-41.277071,"longitude":173.273145,"portatour_id":"7e51f961999d45bcaa8e2b99946454e2"},{"external_id":"2462","name":"Mitre 10 Wilkins & Field - Motueka","street":"235 High Street","city":"MOTUEKA","zip":"7120","country":"New Zealand","customer_group":"Mitre 10","categories":"B","phone":"03 528 9220","email":"dave.richards@mitre10.co.nz","call_note":"Training, LS500s, Indent","last_call":"2026-07-28","call_interval":90,"urgency":"not due","latitude":-41.113427,"longitude":173.010986,"portatour_id":"2ffdc02517d24b499dba8233e2c92e8a"},{"external_id":"","name":"Nelson Airport","street":"Trent Drive","city":"Nelson","zip":"7011","country":"New Zealand","customer_group":"Other","categories":"","phone":"","email":"","call_note":"","last_call":"","call_interval":42,"urgency":"not due","latitude":-41.300015,"longitude":173.227469,"portatour_id":"a59d23a2bf144a5cab962196f5d432dd"},{"external_id":"","name":"Blenheim Airport","street":"23 Tancred Crescent","city":"Woodbourne","zip":"7206","country":"NZ","customer_group":"Other","categories":"","phone":"","email":"","call_note":"","last_call":"2024-10-10","call_interval":42,"urgency":"not due","latitude":-41.51318795737086,"longitude":173.86714519888616,"portatour_id":"9437bff6a31c497e9977b2ec3fa826b3"},{"external_id":"1969","name":"Bunnings Silverdale","street":"18 Hibiscus Coast Highway","city":"AUCKLAND","zip":"993","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"09 426 1529","email":"silverdale@bunnings.co.nz","call_note":"Training and brochures","last_call":"2026-01-08","call_interval":45,"urgency":"overdue","latitude":-36.6393,"longitude":174.715,"portatour_id":"634aea1d1aec4804bf55b3a0e8dc627f"},{"external_id":"2423","name":"ITM Whangarei Building Supplies","street":"Cnr Port & Kioreroa Roads","city":"WHANGAREI","zip":"110","country":"New Zealand","customer_group":"ITM","categories":"C","phone":"09 437 9400","email":"andrew.j@whangareiitm.co.nz","call_note":"Lifestyle Sign for Roadside Display. Meet Daves contact","last_call":"2026-03-31","call_interval":120,"urgency":"due","latitude":-35.739042,"longitude":174.334735,"portatour_id":"61cc4d115cd94a4c9da3c07b9d91b7e0"},{"external_id":"2142","name":"Placemakers Silverdale","street":"47 Tavern Road","city":"AUCKLAND","zip":"932","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"09 424 9000","email":"Conrad.venter@placemakers.co.nz","call_note":"Display Sheds, Armorsteel","last_call":"2026-02-25","call_interval":120,"urgency":"overdue","latitude":-36.62427,"longitude":174.673598,"portatour_id":"cd92699e153e45fe8ed7569114dd075f"},{"external_id":"2033","name":"Carters Whangaparaoa","street":"35 Karepiro Drive","city":"WHANGAPARAOA","zip":"932","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 424 6300","email":"Leanda.Adams@carters.co.nz","call_note":"Reinforce A/S","last_call":"2026-01-15","call_interval":180,"urgency":"due","latitude":-36.635731,"longitude":174.738898,"portatour_id":"48e728803f354bf4b42ef481591603ea"},{"external_id":"8287","name":"Renovation Warehouse","street":"28 Kioreroa Road","city":"Whangarei","zip":"0110","country":"New Zealand","customer_group":"Other","categories":"","phone":"080 027 4438","email":"sales@renovationwarehouse.co.nz","call_note":"","last_call":"2024-04-16","call_interval":42,"urgency":"overdue","latitude":-35.741097,"longitude":174.332738,"portatour_id":"84b25cf8e52749a88c37d098993f8d02"},{"external_id":"1982","name":"Bunnings Whangarei","street":"124 Tauroa Street","city":"WHANGAREI","zip":"140","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"09 470 2100","email":"specialorders.whangarei@bunnings.co.nz","call_note":"Training. Armorsteel.","last_call":"2026-07-16","call_interval":45,"urgency":"not due","latitude":-35.75194,"longitude":174.307118,"portatour_id":"d645ebfd0e404f48a86ea190609af911"},{"external_id":"1988","name":"Carters Dargaville","street":"2 Normanby Street","city":"DARGAVILLE","zip":"310","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 439 3099","email":"Simon.Parkinson@carters.co.nz","call_note":"","last_call":"2026-02-10","call_interval":180,"urgency":"due","latitude":-35.943732,"longitude":173.866057,"portatour_id":"421c06e633184d89bc2ef9a74601bdff"},{"external_id":"2005","name":"Carters Maungaturoto","street":"186 Hurndall Street","city":"MAUNGATUROTO","zip":"0520","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 431 8575","email":"Brian.Roberts@carters.co.nz","call_note":"Meet and Greet","last_call":"2025-08-28","call_interval":180,"urgency":"overdue","latitude":-36.111712,"longitude":174.352409,"portatour_id":"8dbea90844ff450eb6999195b7310474"},{"external_id":"2029","name":"Carters Warkworth","street":"43 Auckland Rd","city":"WARKWORTH","zip":"910","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 425 7879","email":"","call_note":"PA Door Sample","last_call":"2026-02-12","call_interval":180,"urgency":"due","latitude":-36.407398,"longitude":174.65832,"portatour_id":"e5e3a65ffa094d52b10ec896b1a403db"},{"external_id":"2116","name":"Placemakers Kaitaia","street":"41 Matthews Avenue","city":"KAITAIA","zip":"410","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"09 408 9020","email":"Sharon.Norman@placemakers.co.nz","call_note":"PA Door Sample","last_call":"2026-07-14","call_interval":120,"urgency":"not due","latitude":-35.110348,"longitude":173.26301,"portatour_id":"486ddd426bdc4ed0b84bcd03e52bda91"},{"external_id":"2158","name":"Placemakers Whangarei","street":"8 Kioreroa Road","city":"WHANGAREI","zip":"110","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"09 470 3970","email":"Walter.wyatt@placemakers.co.nz","call_note":"Establish Contact, sort Display","last_call":"2026-03-31","call_interval":120,"urgency":"due","latitude":-35.740074,"longitude":174.333692,"portatour_id":"c4d6961740424d779fc1f5ef1d3ace10"},{"external_id":"2353","name":"ITM Dargaville","street":"40 - 42 Normandy Street","city":"DARGAVILLE","zip":"310","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 439 8730","email":"brett@dargavilleitm.co.nz","call_note":"","last_call":"2026-02-10","call_interval":180,"urgency":"due","latitude":-35.941591,"longitude":173.868285,"portatour_id":"b00bfab2411c44eab5d4b94b594fdfde"},{"external_id":"2384","name":"ITM Matakana Building Centre","street":"58 Matakana Valley Rd","city":"WARKWORTH","zip":"985","country":"New Zealand","customer_group":"ITM","categories":"C","phone":"09 422 7024","email":"james@matakanaitm.co.nz","call_note":"PA Door Sample","last_call":"2026-01-14","call_interval":120,"urgency":"overdue","latitude":-36.350503,"longitude":174.715394,"portatour_id":"abc0b17daadd4638bc300c4b976ca3aa"},{"external_id":"2418","name":"ITM Waipu","street":"40 Cove Road","city":"WAIPU","zip":"510","country":"New Zealand","customer_group":"ITM","categories":"C","phone":"09 432 0203","email":"nick@waipuitm.co.nz","call_note":"PA Doors, Armorsteel","last_call":"2026-04-01","call_interval":120,"urgency":"due","latitude":-35.985856,"longitude":174.452146,"portatour_id":"c43a4d9e6b9345e99ae5a07be180d611"},{"external_id":"2505","name":"Mitre 10 Mega Whangarei","street":"46 Porowini Avenue","city":"WHANGAREI","zip":"110","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"09 430 4009","email":"Dee.Field@mitre10.co.nz","call_note":"Training","last_call":"2026-07-08","call_interval":45,"urgency":"not due","latitude":-35.7322,"longitude":174.321731,"portatour_id":"d956e33976af4089bdb0888a64a5d58a"},{"external_id":"4330","name":"Hammer Hardware Kamo","street":"485 Kamo Road","city":"WHANGAREI","zip":"112","country":"New Zealand","customer_group":"Hammer Hardware","categories":"C","phone":"09 435 1342","email":"kamo@hammerhardware.co.nz","call_note":"Meet Wayne and John, get contact details","last_call":"2024-10-02","call_interval":120,"urgency":"overdue","latitude":-35.684132,"longitude":174.301343,"portatour_id":"082c2f14df0b47468edbe7b17fe74fbf"},{"external_id":"4352","name":"Hammer Hardware Waipu","street":"35 The Centre","city":"WAIPU","zip":"510","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"09 432 0292","email":"waipu.office@hammerhardware.co.nz","call_note":"Greens Brochure/Carts. Aromachef.","last_call":"2026-04-01","call_interval":180,"urgency":"not due","latitude":-35.984401,"longitude":174.445643,"portatour_id":"9ae3210399854338b98eb97dbddbafb2"},{"external_id":"8397","name":"ITM Warkworth","street":"16 State Highway 1","city":"WARKWORTH","zip":"0981","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"094251021","email":"brett@warkworthitm.co.nz","call_note":"Follow up Dan re A/S","last_call":"2026-05-13","call_interval":180,"urgency":"not due","latitude":-36.391023,"longitude":174.649543,"portatour_id":"f115f3fb72ec412d953437c8ed96dba7"},{"external_id":"9129","name":"Bay of Islands ITM Waipapa","street":"411 Waipapa Road","city":"Kerikeri","zip":"0295","country":"New Zealand","customer_group":"ITM","categories":"C","phone":"09 407 8002","email":"sales-waipapa@bayofislandsitm.co.nz","call_note":"Armorsteel","last_call":"2026-03-31","call_interval":120,"urgency":"due","latitude":-35.206202,"longitude":173.918612,"portatour_id":"da850bede2364050adc67f85bacc2b92"},{"external_id":"4315","name":"Hammer Hardware Coopers Beach","street":"241 State Highway 10","city":"MANGONUI","zip":"420","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"09 406 1088","email":"coopersbeach@hammerhardware.co.nz","call_note":"Greens Brochure","last_call":"2025-11-04","call_interval":180,"urgency":"overdue","latitude":-34.992681,"longitude":173.504551,"portatour_id":"f1163eb4e45f4a72a47b764bdeca3c1c"},{"external_id":"2451","name":"Mitre 10 McCarthny Kaitaia","street":"1 North Park Drive","city":"KAITAIA","zip":"482","country":"New Zealand","customer_group":"Mitre 10","categories":"B","phone":"09 408 1660","email":"mccarthny@mitre10.co.nz","call_note":"Confirm Display Shed","last_call":"2026-01-13","call_interval":90,"urgency":"overdue","latitude":-35.097111,"longitude":173.260993,"portatour_id":"cf24c12f00a6461f8a84692a939a5a92"},{"external_id":"8368","name":"Mitre 10 Kerikeri","street":"37 Klinac Lane","city":"Kerikeri","zip":"0295","country":"New Zealand","customer_group":"Mitre 10","categories":"B","phone":"09-407 5540","email":"shuna.watts@mitre10.co.nz","call_note":"Mark re Armorsteel, pic of card rack, Karl re Loadshifters","last_call":"2026-07-15","call_interval":90,"urgency":"not due","latitude":-35.210932,"longitude":173.917034,"portatour_id":"4b7a8953a7454e57bbd967961d6c67eb"},{"external_id":"1017","name":"Arnold Franks Ltd","street":"112-114 Cameron Street","city":"WHANGAREI","zip":"0110","country":"New Zealand","customer_group":"Other","categories":"D","phone":"09 438 1359","email":"admin@arnoldfranks.co.nz","call_note":"","last_call":"2024-10-02","call_interval":180,"urgency":"overdue","latitude":-35.727455,"longitude":174.323294,"portatour_id":"12ea8ef24ee044ed8d28da4d163208a9"},{"external_id":"1032","name":"Bream Bay Merchants","street":"Cnr Sime & Kepa Roads","city":"RUAKAKA","zip":"0151","country":"New Zealand","customer_group":"Other","categories":"D","phone":"09 433 0077","email":"info@breambaymerchants.co.nz","call_note":"Meet Dave","last_call":"2024-08-27","call_interval":180,"urgency":"overdue","latitude":-35.8732,"longitude":174.46152,"portatour_id":"8ca69bdb661746d0af5cdf1e321a2fec"},{"external_id":"1251","name":"Buildlink Head Office: Toots Timber & Hardware","street":"91 Waiotoi Rd","city":"WHANGAREI","zip":"0173","country":"New Zealand","customer_group":"Buildlink","categories":"D","phone":"09 434 3171","email":"toots@buildlink.co.nz","call_note":"","last_call":"2024-04-17","call_interval":180,"urgency":"overdue","latitude":-35.620349,"longitude":174.50359,"portatour_id":"1fc39aeb387a4557aa434a507cc9ba44"},{"external_id":"1948","name":"Bunnings Waipapa","street":"391 Waipapa Road","city":"Waipapa","zip":"0295","country":"New Zealand","customer_group":"Bunnings","categories":"C","phone":"09 407 7421","email":"kerikeri@bunnings.co.nz","call_note":"Training in lifestyles, armorsteel appt w/ Trade","last_call":"2026-07-08","call_interval":120,"urgency":"not due","latitude":-35.205816,"longitude":173.920428,"portatour_id":"f5dcdd5050b1459daacfd81e98a2bca7"},{"external_id":"1999","name":"Carters Kaitaia","street":"Dunn Street","city":"KAITAIA","zip":"410","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 408 6170","email":"Rex.Morland@carters.co.nz","call_note":"Armorsteel","last_call":"2026-01-13","call_interval":180,"urgency":"due","latitude":-35.110718,"longitude":173.265682,"portatour_id":"9cd882db03324783a5f21289de6d2b21"},{"external_id":"2001","name":"Carters Kerikeri","street":"Cnr Kahikatearoa Lane & SH 10","city":"KERIKERI","zip":"295","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 407 6609","email":"Natalia.Hill@carters.co.nz","call_note":"Meet with Michael re armorsteel","last_call":"2026-02-10","call_interval":180,"urgency":"due","latitude":-35.212067,"longitude":173.919793,"portatour_id":"51e420fc2d3941cb85259b8ac830dbc9"},{"external_id":"2003","name":"Carters Mangawhai","street":"41 Moir Street","city":"MANGAWHAI","zip":"505","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 423 1180","email":"","call_note":"Establish shed contact, workshops","last_call":"2024-10-03","call_interval":180,"urgency":"overdue","latitude":-36.125858,"longitude":174.5756,"portatour_id":"5ea934ea5f9e4b9fab186b30b917a703"},{"external_id":"2034","name":"Carters Whangarei","street":"32 Commerce Street","city":"WHANGAREI","zip":"110","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 438 3659","email":"Lio.Tua@carters.co.nz","call_note":"Catch up with Calvin Re Armorsteel","last_call":"2026-02-11","call_interval":180,"urgency":"due","latitude":-35.730406,"longitude":174.324395,"portatour_id":"d8723b9e5e5f4eb280487e550f27d947"},{"external_id":"2119","name":"Placemakers Kerikeri","street":"26 Mill Lane","city":"KERIKERI","zip":"230","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"09 407 4820","email":"kerikeri@placemakers.co.nz","call_note":"Lifestyle Signage","last_call":"2026-03-31","call_interval":120,"urgency":"due","latitude":-35.234603,"longitude":173.951542,"portatour_id":"1bc16345e3ad4e7092acfe75b4cb7e85"},{"external_id":"2341","name":"ITM Bay Of Islands","street":"2 Haruru Falls Road","city":"PAIHIA","zip":"204","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 402 7703","email":"murry.n@bayitm.co.nz","call_note":"Display shed opportunity","last_call":"2025-07-02","call_interval":180,"urgency":"overdue","latitude":-35.28018,"longitude":174.050302,"portatour_id":"b3b8882dcbee4233a1aa72e364c26fad"},{"external_id":"2358","name":"ITM Far North Mangonui","street":"22 Karamea Road","city":"NORTHLAND","zip":"442","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 406 0048","email":"danielle@fnitm.co.nz","call_note":"","last_call":"2024-08-29","call_interval":180,"urgency":"overdue","latitude":-35.0968971319237,"longitude":173.260906419169,"portatour_id":"d776d74ab3ac4a7f8feda73266cd6123"},{"external_id":"2374","name":"ITM Far North Kaitaia","street":"23 Northpark Drive","city":"KAITAIA","zip":"0410","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 408 3927","email":"danielle@fnitm.co.nz","call_note":"","last_call":"2025-10-02","call_interval":180,"urgency":"overdue","latitude":-35.1117,"longitude":173.262,"portatour_id":"16e4936988514fe0803f616cbf8f2e93"},{"external_id":"2502","name":"Mitre 10 Mega Warkworth","street":"Cnr Woodcocks & Mansell Drive","city":"WARKWORTH","zip":"910","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"09 425 8119","email":"purchasing@megawarkworth.co.nz","call_note":"Greens area in GC (Whangarei ideas?). Meet with Jordan re armorsteel","last_call":"2026-05-13","call_interval":45,"urgency":"overdue","latitude":-36.3976,"longitude":174.666,"portatour_id":"296315137af54f8db04daa940c6195cc"},{"external_id":"8395","name":"ITM Mangawhai","street":"188 Molesworth Drive","city":"","zip":"0505","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"094314963","email":"simon@warkworthitm.co.nz","call_note":"Greens opportunities","last_call":"2026-04-01","call_interval":180,"urgency":"not due","latitude":-36.102508,"longitude":174.586377,"portatour_id":"318a34b364ad43dc824ae30590a08464"},{"external_id":"2507","name":"Mitre 10 Whangaparaoa","street":"26 Karepiro Drive","city":"AUCKLAND","zip":"932","country":"New Zealand","customer_group":"Mitre 10","categories":"D","phone":"09 428 2477","email":"wgp@mitre10.co.nz","call_note":"Hygiene, Concrete, Plan for brochure holders/signage in trade.","last_call":"2026-01-15","call_interval":180,"urgency":"due","latitude":-36.635141,"longitude":174.737378,"portatour_id":"f862950926e342efa3e691b6c516cd85"},{"external_id":"2121","name":"Placemakers Mangawhai","street":"4 Moir Point Road","city":"MANGAWHAI HEADS","zip":"505","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"09 431 4236","email":"wendy.kerr@placemakers.co.nz","call_note":"displays, training.","last_call":"2026-02-12","call_interval":180,"urgency":"due","latitude":-36.101288,"longitude":174.588435,"portatour_id":"998e5063a87d400fa023b2333cdc5ddc"},{"external_id":"9328","name":"Placemakers Warkworth","street":"64 Morrison Drive","city":"WARKWORTH","zip":"0910","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"","email":"p488epo@placemakers.co.nz","call_note":"Appt with Paul Kerr *Not Confirmed*","last_call":"2026-05-13","call_interval":180,"urgency":"not due","latitude":-36.411309,"longitude":174.654119,"portatour_id":"6e030ee82d7a41429e5652c1613a6998"},{"external_id":"9095","name":"Buildlink Kaitaia","street":"374 State Highway 1","city":"","zip":"","country":"New Zealand","customer_group":"Buildlink","categories":"E","phone":"027 484 3869","email":"angela@buildlinkkaitaia.co.nz","call_note":"Lifestyle Signage","last_call":"2024-11-13","call_interval":42,"urgency":"not due","latitude":-35.09588355346075,"longitude":173.25755938450288,"portatour_id":"1bd68da47ca84ee68119d2cdd2344138"},{"external_id":"","name":"Mitre 10 Mega Silverdale","street":"35 Colin Chester Drive","city":"Silverdale","zip":"0932","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"","phone":"","email":"","call_note":"Training On Tues or Wed","last_call":"2026-05-13","call_interval":42,"urgency":"overdue","latitude":-36.611901,"longitude":174.664964,"portatour_id":"eb32e90dcf604beb96ba8dacd1b54d48"},{"external_id":"","name":"Bunnings Mangawhai","street":"75A Molesworth Drive","city":"Mangawhai","zip":"0505","country":"NZ","customer_group":"Bunnings","categories":"C","phone":"","email":"","call_note":"Tidy up Shed Stock","last_call":"2026-04-01","call_interval":120,"urgency":"due","latitude":-36.11373457695712,"longitude":174.57498796534128,"portatour_id":"0e19d894f8054b01aa0ab3ee573a5295"},{"external_id":"4316","name":"Hammer Hardware Devonport","street":"13 Clarence Street","city":"AUCKLAND","zip":"0624","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"09 445 2356","email":"devonport@hammerhardware.co.nz","call_note":"Check to see how Gardenpros and Charcoal are going","last_call":"2024-12-17","call_interval":180,"urgency":"overdue","latitude":-36.83036,"longitude":174.796549,"portatour_id":"94a7c8f039e24be1bf1ea4e1c0a3a294"},{"external_id":"9134","name":"Bunnings Westgate","street":"21 Fred Taylor Drive","city":"AUCKLAND","zip":"0814","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"","email":"Westgate@bunnings.co.nz","call_note":"Armorsteel, Brochure/pricelist swap out, solvent for sheds","last_call":"2026-04-09","call_interval":45,"urgency":"overdue","latitude":-36.821275,"longitude":174.607913,"portatour_id":"5a0fcc51708c49b3816e30be3bf6e6a6"},{"external_id":"1929","name":"Bunnings Avondale Trade Centre","street":"494 Rosebank Road","city":"AUCKLAND","zip":"1026","country":"New Zealand","customer_group":"Bunnings","categories":"D","phone":"09 828 0600","email":"trade.avondaletrade@bunnings.co.nz","call_note":"Armorsteel, Shed Sales numbers discussion","last_call":"2026-04-08","call_interval":180,"urgency":"not due","latitude":-36.879261,"longitude":174.67153,"portatour_id":"ff1e7ea055094096b2e09c3c26dfa63c"},{"external_id":"2354","name":"Dayle ITM Avondale","street":"729 Rosebank Rd","city":"AUCKLAND","zip":"1026","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 828 9791","email":"governance@dayleitm.co.nz","call_note":"Armorsteel","last_call":"2026-04-08","call_interval":180,"urgency":"not due","latitude":-36.871267,"longitude":174.671311,"portatour_id":"8514eeb3e97647cf882afbec61320bbe"},{"external_id":"1957","name":"Bunnings New Lynn","street":"Bunnings New Lynn","city":"AUCKLAND","zip":"600","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"09 826 2400","email":"newlynn@bunnings.co.nz","call_note":"Fix Armorsteel stickers, Flashing layout, A/S order forms","last_call":"2026-04-08","call_interval":45,"urgency":"overdue","latitude":-36.9137,"longitude":174.681,"portatour_id":"f222a7bdcf054c50b0f482d3e9de0f02"},{"external_id":"2467","name":"Mitre 10 Mega New Lynn","street":"5 Astley Avenue","city":"AUCKLAND","zip":"600","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"09 826 1420","email":"Cam.Caithness@mitre10.co.nz","call_note":"Wheelbarrow Stickers, Cedar protectant, Poly Cart discussion","last_call":"2026-04-08","call_interval":45,"urgency":"overdue","latitude":-36.912345,"longitude":174.688295,"portatour_id":"217eafdac7914f90aa20a1c84a00bc53"},{"external_id":"1996","name":"Carters Henderson","street":"141 Central Park Drive","city":"AUCKLAND","zip":"610","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 837 8796","email":"jamie.bonner@carters.co.nz","call_note":"Loadshifters, greens parts, and/or Carts?","last_call":"2025-02-27","call_interval":180,"urgency":"overdue","latitude":-36.853148,"longitude":174.636405,"portatour_id":"8e6ed6c55dbb4b98b94099d34f545518"},{"external_id":"2379","name":"ITM MacClures Timber","street":"256 Swanson Rd","city":"AUCKLAND","zip":"610","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 8360414","email":"mactim@xtra.co.nz","call_note":"Loadshifters, greens parts, and/or Carts?","last_call":"2025-10-29","call_interval":180,"urgency":"overdue","latitude":-36.864328,"longitude":174.614964,"portatour_id":"9e322ef839fd47218ee1a5d359ece779"},{"external_id":"2446","name":"Mitre 10 Mega Henderson","street":"186 Lincoln Rd","city":"AUCKLAND","zip":"610","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"09 836 7700","email":"alasdair.mega@mitre10.co.nz","call_note":"Clear Brochure Holder, Display F/U, Training w/ Trade","last_call":"2026-04-09","call_interval":45,"urgency":"overdue","latitude":-36.858522,"longitude":174.629736,"portatour_id":"b1aad098effd428e8589ef33712bed9a"},{"external_id":"2404","name":"ITM Western Swanson","street":"2 O\'Neills Road","city":"AUCKLAND","zip":"612","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 832 0209","email":"shaneb@westernitm.co.nz","call_note":"Armorsteel","last_call":"2026-04-08","call_interval":42,"urgency":"not due","latitude":-36.865792,"longitude":174.585926,"portatour_id":"3bc723afeafd495e89172eb39e161116"},{"external_id":"2156","name":"Placemakers Westgate","street":"547 - 557 Don Buck Road","city":"AUCKLAND","zip":"614","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"09 815 6800","email":"ruth.thornewell@placemakers.co.nz","call_note":"A/S Follow up, Loadshifters, greens parts, and/or Carts?","last_call":"2026-04-09","call_interval":120,"urgency":"due","latitude":-36.822775,"longitude":174.607558,"portatour_id":"5a2eae27847f41f0805552c4a0ab5c18"},{"external_id":"1938","name":"Bunnings Glenfield","street":"80 Wairau Road","city":"AUCKLAND","zip":"627","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"09 443 8891","email":"specialorders.glenfield@bunnings.co.nz","call_note":"New member training, Armorsteel, Sheds planogram","last_call":"2026-02-25","call_interval":45,"urgency":"overdue","latitude":-36.776805,"longitude":174.744017,"portatour_id":"8782834412c94f40bbc43eb2bcdfa6fa"},{"external_id":"1993","name":"Carters Glenfield","street":"66 Diana Drive","city":"AUCKLAND","zip":"627","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 441 2333","email":"greg.cross@carters.co.nz","call_note":"Armorsteel","last_call":"2024-09-12","call_interval":180,"urgency":"overdue","latitude":-36.773815,"longitude":174.73484,"portatour_id":"898a4de217f24e1abfaa84c1395ce51b"},{"external_id":"2440","name":"Mitre 10 Mega Glenfield","street":"15 Poland Rd","city":"AUCKLAND","zip":"627","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"09 440 5100","email":"pbritton@grove.co.nz","call_note":"Inventory Contact email, Sliding door fix, new display layout","last_call":"2026-03-10","call_interval":45,"urgency":"overdue","latitude":-36.77691,"longitude":174.740855,"portatour_id":"7003e3ed4a454d118bd660bb56af9561"},{"external_id":"2153","name":"Placemakers Wairau Park (Glenfield)","street":"3 Link Drive","city":"AUCKLAND","zip":"627","country":"New Zealand","customer_group":"Placemakers","categories":"B","phone":"09 444 5155","email":"orders.wairaupark@placemakers.co.nz","call_note":"Appt with Bob/Paul re display","last_call":"2025-07-23","call_interval":90,"urgency":"overdue","latitude":-36.768083,"longitude":174.737987,"portatour_id":"cc1a3c0e3fc943119e6f40c83365766a"},{"external_id":"1959","name":"Bunnings North Shore","street":"15 Home Place","city":"AUCKLAND","zip":"632","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"09 978 2400","email":"northshore@bunnings.co.nz","call_note":"Training, Flashing Layout","last_call":"2026-03-10","call_interval":45,"urgency":"overdue","latitude":-36.749236,"longitude":174.727416,"portatour_id":"5d0cea9560d14742ae3aaeb339e93937"},{"external_id":"1983","name":"Carters Albany","street":"10 Tawa Drive","city":"AUCKLAND","zip":"632","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"09 415 7171","email":"","call_note":"","last_call":"2024-12-17","call_interval":180,"urgency":"overdue","latitude":-36.739141,"longitude":174.716763,"portatour_id":"6f97c1a3d85a4dc6b85fe12e55d7a2f7"},{"external_id":"2336","name":"ITM Albany","street":"66 Bush Road","city":"AUCKLAND","zip":"632","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 415 6889","email":"trobinson@albanytimber.co.nz","call_note":"Recap/Feedback from David","last_call":"2025-11-20","call_interval":180,"urgency":"overdue","latitude":-36.738197,"longitude":174.703778,"portatour_id":"aa7d8678587b45fc9bb5abe18ab8e0df"},{"external_id":"2426","name":"Mitre 10 Mega Albany","street":"260 Oteha Valley Road","city":"AUCKLAND","zip":"632","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"09 442 6130","email":"ap@meganewlynn.co.nz","call_note":"Mini shed installation, signage relocation","last_call":"2026-03-10","call_interval":45,"urgency":"overdue","latitude":-36.721697,"longitude":174.706474,"portatour_id":"48fdb5b53b4b4acd8e4ed5142e16d9f5"},{"external_id":"2095","name":"Placemakers Albany","street":"24 Oteha Valley Road Ext","city":"AUCKLAND","zip":"632","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"09 414 0900","email":"sandra.teal@placemakers.co.nz","call_note":"Establish Contact, Armorsteel.","last_call":"2026-02-25","call_interval":120,"urgency":"overdue","latitude":-36.724921,"longitude":174.698086,"portatour_id":"bceaa53fde8c4f63b9807109e3caa29f"},{"external_id":"2129","name":"Placemakers New Lynn","street":"6 Crown Lynn Place","city":"AUCKLAND","zip":"640","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"09 825 0088","email":"p479epo@placemakers.co.nz","call_note":"Review WB Range with Shaun.","last_call":"2026-04-08","call_interval":120,"urgency":"due","latitude":-36.910905,"longitude":174.685005,"portatour_id":"3c014bee61b941dba9b93ce9878adec6"},{"external_id":"2369","name":"ITM Hillside Bldg Supplies 2019 (Glenfield)","street":"131 Diana Drive","city":"AUCKLAND","zip":"67","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 443 8101","email":"smcnally@hbs.co.nz","call_note":"Armorsteel","last_call":"2024-09-12","call_interval":180,"urgency":"overdue","latitude":-36.773732,"longitude":174.735121,"portatour_id":"9d54c1426db74bc4b53bd65d620b4137"},{"external_id":"2503","name":"Mitre 10 Mega Westgate","street":"Northside Drive","city":"AUCKLAND","zip":"7473","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"09 416 2400","email":"damien.clarke@mitre10.co.nz","call_note":"cedar shed to be painted.","last_call":"2026-04-09","call_interval":45,"urgency":"overdue","latitude":-36.80934806520541,"longitude":174.60491165105847,"portatour_id":"aa42568982a7444aae29f194925bd5c2"},{"external_id":"2445","name":"Mitre 10 Helensville H&T","street":"41 Mill Road","city":"AUCKLAND","zip":"800","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"09 420 8153","email":"helensville@mitre10.co.nz","call_note":"Hygiene, 2.5 and 3kg.","last_call":"2026-01-15","call_interval":120,"urgency":"overdue","latitude":-36.679152,"longitude":174.445807,"portatour_id":"03cce2207657481faecc301fb3707228"},{"external_id":"2377","name":"ITM Western Kumeu","street":"154 Main Rd","city":"AUCKLAND","zip":"810","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 412 8148","email":"wendyf@westernitm.co.nz","call_note":"Get Elvins contact","last_call":"2026-04-09","call_interval":180,"urgency":"not due","latitude":-36.774483,"longitude":174.552143,"portatour_id":"2b5ebda14164497688dfd5fe4b1f73eb"},{"external_id":"2424","name":"ITM Western Whenuapai","street":"2 Airport Road","city":"AUCKLAND","zip":"816","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"09 416 8164","email":"lyndonf@westernitm.co.nz","call_note":"Armorsteel","last_call":"2026-04-09","call_interval":180,"urgency":"not due","latitude":-36.793403,"longitude":174.617119,"portatour_id":"8445c43bbcd04c38bc51a485294c5874"},{"external_id":"2473","name":"Mitre 10 KJ Pahiatua","street":"67 Main Street","city":"PAHIATUA","zip":"4910","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"06 376 7978","email":"kj@mitre10.co.nz","call_note":"","last_call":"2026-02-19","call_interval":120,"urgency":"overdue","latitude":-40.451283,"longitude":175.842498,"portatour_id":"74a5fd0c92fa4439b4c699ef47a42273"},{"external_id":"2382","name":"ITM Central Marton","street":"40 Racecourse Ave","city":"MARTON","zip":"4710","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 327 5458","email":"rjames@centralitm.co.nz","call_note":"Signage","last_call":"2026-07-22","call_interval":180,"urgency":"not due","latitude":-40.086131,"longitude":175.393964,"portatour_id":"65f32af55e8d44bfa480576466152d5d"},{"external_id":"2381","name":"ITM Manawatu Timber & Hardware","street":"51 Joseph Street","city":"PALMERSTON NORTH","zip":"4412","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 356 9490","email":"darren@manawatuitm.co.nz","call_note":"PA Doors","last_call":"2026-07-22","call_interval":180,"urgency":"not due","latitude":-40.36288,"longitude":175.603001,"portatour_id":"b77821ca053349c28615b049f70ecf1f"},{"external_id":"2360","name":"ITM Hometown Building Supplies Foxton","street":"1 Bergin Road","city":"FOXTON","zip":"4814","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 363 8049","email":"doug@hometownitm.co.nz","call_note":"Hammer Hardware opening EOY, can call then","last_call":"2026-02-18","call_interval":180,"urgency":"due","latitude":-40.463074,"longitude":175.280547,"portatour_id":"d4dfff3b8ea94eec8a62eceb110288ff"},{"external_id":"1103","name":"Hall of Flame","street":"290 Oxford Street","city":"LEVIN","zip":"5510","country":"New Zealand","customer_group":"Other","categories":"E","phone":"06 3670760","email":"sales@hallofflame.co.nz","call_note":"","last_call":"","call_interval":42,"urgency":"overdue","latitude":-40.620116,"longitude":175.288386,"portatour_id":"b3741e528e574e02b31c792b44e15faa"},{"external_id":"2457","name":"Mitre 10 McIlwaine - Marton","street":"35 Russell Street","city":"MARTON","zip":"4710","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"06 327 7079","email":"terry@mcilwaine.co.nz","call_note":"","last_call":"2026-07-22","call_interval":120,"urgency":"not due","latitude":-40.074408,"longitude":175.371936,"portatour_id":"813a02a5133841bc9e09b4082b8c2cf7"},{"external_id":"2347","name":"ITM Crighton Levin","street":"15 - 19 Hokio Beach Rd","city":"LEVIN","zip":"5510","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 368 4057","email":"rachel@crightons.co.nz","call_note":"","last_call":"2026-02-18","call_interval":180,"urgency":"due","latitude":-40.632289,"longitude":175.27377,"portatour_id":"de7e661eb6e147359ebe818b00f175f1"},{"external_id":"1979","name":"Bunnings Wellington Central","street":"Bunnings Wellington Central","city":"WELLINGTON","zip":"6011","country":"New Zealand","customer_group":"Bunnings","categories":"D","phone":"04 805 0593","email":"specialorders.wellingtoncentral@bunnings.co.nz","call_note":"","last_call":"2026-04-16","call_interval":180,"urgency":"not due","latitude":-41.2851,"longitude":174.776001,"portatour_id":"e2abdafe4125425081dfef31327c1a5b"},{"external_id":"1284","name":"Wellington BBQ & Fire","street":"12 Te Puni Street","city":"LOWER HUTT","zip":"5012","country":"New Zealand","customer_group":"Other","categories":"C","phone":"04 5669714","email":"miramar@bbqsandfire.co.nz","call_note":"","last_call":"2023-07-25","call_interval":120,"urgency":"overdue","latitude":-41.224179,"longitude":174.869039,"portatour_id":"b293bb4f289c4f1b9e0b21030d86da36"},{"external_id":"9463","name":"Kapiti Woodfires","street":"128 Rimu Road","city":"","zip":"","country":"New Zealand","customer_group":"Other","categories":"D","phone":"042125509","email":"admin@kapitiwoodfires.co.nz","call_note":"","last_call":"2022-12-06","call_interval":180,"urgency":"overdue","latitude":-40.89446193272943,"longitude":174.98440958000415,"portatour_id":"4ecc6f2ddce84dce8308d5088d4b5fa1"},{"external_id":"2435","name":"Mitre 10 Dannevirke","street":"155-163 High Street","city":"DANNEVIRKE","zip":"4930","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"06 374 6045","email":"dannevirke@mitre10.co.nz","call_note":"Create appt with Jesse","last_call":"2026-02-19","call_interval":120,"urgency":"overdue","latitude":-40.208132,"longitude":176.101915,"portatour_id":"4076b14a41b249fc87f32d995abef328"},{"external_id":"2152","name":"Placemakers Wairarapa","street":"72 Ngaumutawa Road","city":"MASTERTON","zip":"5810","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"","email":"Kate.Ryan@placemakers.co.nz","call_note":"Display solution, Armorsteel","last_call":"2026-04-14","call_interval":120,"urgency":"due","latitude":-40.941765,"longitude":175.644471,"portatour_id":"d72f7d75553a41c089bc9291a4977ad9"},{"external_id":"2438","name":"Mitre 10 Darraghs Feilding","street":"44 Kimbolton Road","city":"FEILDING","zip":"4702","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"06 323 4054","email":"darraghs@mitre10.co.nz","call_note":"Get some sort of action on the displays","last_call":"2026-06-17","call_interval":120,"urgency":"not due","latitude":-40.226661,"longitude":175.563507,"portatour_id":"4bb23eb5295a4893ad4f41191fbe4660"},{"external_id":"2474","name":"Mitre 10 Mega Palmerston North","street":"250-270 Featherston Street","city":"PALMERSTON NORTH","zip":"4414","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"06 354 4087","email":"palmerstonnorth@mitre10.co.nz","call_note":"Establish Trade contact, Seasonal Opportunities","last_call":"2026-07-21","call_interval":45,"urgency":"not due","latitude":-40.349593,"longitude":175.60695,"portatour_id":"a66adb731997439293abef70311b9e2a"},{"external_id":"2134","name":"Placemakers Palmerston North","street":"Cnr Railway Rd & Francis Way","city":"PALMERSTON NORTH","zip":"4414","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"06 353 5777","email":"sales.palmerston@placemakers.co.nz","call_note":"PA Doors/Flashings","last_call":"2026-07-22","call_interval":180,"urgency":"not due","latitude":-40.328117,"longitude":175.627746,"portatour_id":"efceb4be3ef7423594f2ba45229ac26e"},{"external_id":"1961","name":"Bunnings Palmerston North","street":"23 Railway Road","city":"PALMERSTON NORTH","zip":"4410","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"06 355 7100","email":"palmerstonnorth@bunnings.co.nz","call_note":"Double Check Barrow stock as per last call, Armorsteel","last_call":"2026-07-22","call_interval":45,"urgency":"not due","latitude":-40.329274,"longitude":175.629068,"portatour_id":"fecc86c728b94df68c140df5fa361bad"},{"external_id":"2013","name":"Carters Palmerston North","street":"69 Railway Road","city":"PALMERSTON NORTH","zip":"4414","country":"New Zealand","customer_group":"Carters","categories":"D","phone":"06 358 9029","email":"","call_note":"","last_call":"2026-07-22","call_interval":180,"urgency":"not due","latitude":-40.326644,"longitude":175.626028,"portatour_id":"c2c92a91b75042f889617b3cf51ed38e"},{"external_id":"2359","name":"ITM Central Feilding","street":"1537 Waughs Road","city":"FEILDING","zip":"4775","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 323 3400","email":"rjames@centralitm.co.nz","call_note":"","last_call":"2026-02-04","call_interval":180,"urgency":"due","latitude":-40.236214,"longitude":175.575343,"portatour_id":"9467e7ccecfc4fe394eba3a2f773df2c"},{"external_id":"1935","name":"Bunnings Feilding","street":"15 Eyre Street","city":"FEILDING","zip":"4702","country":"New Zealand","customer_group":"Bunnings","categories":"B","phone":"06 323 6581","email":"feilding@bunnings.co.nz","call_note":"Check Steel status. Brochure holders Low Pri","last_call":"2026-01-27","call_interval":90,"urgency":"overdue","latitude":-40.227346,"longitude":175.567828,"portatour_id":"d4205e8c66ae40f7a9da197e28efeb13"},{"external_id":"2453","name":"Mitre 10 Levin","street":"521 Queen Street","city":"LEVIN","zip":"5510","country":"New Zealand","customer_group":"Mitre 10","categories":"B","phone":"06 368 2268","email":"levin@mitre10.co.nz","call_note":"Sort Display Sheds, training in trade","last_call":"2026-06-17","call_interval":90,"urgency":"not due","latitude":-40.632093,"longitude":175.279621,"portatour_id":"994e0a650e0141b091481f59d3dd8f77"},{"external_id":"2120","name":"Placemakers Levin","street":"23 South Road","city":"Levin","zip":"5510","country":"NZ","customer_group":"Placemakers","categories":"C","phone":"06 368 9189","email":"gaitchison@placemakers.co.nz","call_note":"","last_call":"2026-02-18","call_interval":120,"urgency":"overdue","latitude":-40.63498069203244,"longitude":175.27335043191087,"portatour_id":"355ddf2dc92742adbad04bf1e3f40430"},{"external_id":"4339","name":"Hammer Hardware Otaki","street":"Titoki Street","city":"OTAKI","zip":"5512","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"06 364 8389","email":"otaki@hammerhardware.co.nz","call_note":"Yard/Gardenpro, Firepits","last_call":"2026-02-18","call_interval":180,"urgency":"due","latitude":-40.760266,"longitude":175.136815,"portatour_id":"5603b7ad2bc047c4bebaaba343b75b93"},{"external_id":"2351","name":"Tumu Dannevirke","street":"198 High Street","city":"DANNEVIRKE","zip":"4930","country":"New Zealand","customer_group":"Other","categories":"D","phone":"06 374 4260","email":"jeff.sharp@tumu.co.nz","call_note":"","last_call":"2024-09-06","call_interval":180,"urgency":"overdue","latitude":-40.207105,"longitude":176.105451,"portatour_id":"7c9cb1fd754247d9af07583f8148c16d"},{"external_id":"1949","name":"Bunnings Lyall Bay","street":"24-54 Kingsford-Smith St","city":"WELLINGTON","zip":"6022","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"04 387 3550","email":"specialorders.lyallbay@bunnings.co.nz","call_note":"","last_call":"2026-04-14","call_interval":45,"urgency":"overdue","latitude":-41.325048,"longitude":174.801348,"portatour_id":"a849990316fb45c2b0f82d18a80130e8"},{"external_id":"2106","name":"Placemakers Evans Bay","street":"38 Tacy Street","city":"WELLINGTON","zip":"6022","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"04 387 8692","email":"rzonneveld@placemakers.co.nz","call_note":"Talk to John (BM) Re damaged Garrison Display.","last_call":"2026-04-14","call_interval":180,"urgency":"not due","latitude":-41.315817,"longitude":174.799442,"portatour_id":"c1a542cf71034d5d8ab6b789837b666e"},{"external_id":"2117","name":"Placemakers Kaiwharawhara","street":"188 Hutt Road","city":"WELLINGTON","zip":"6035","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"04 496 1124","email":"manoj.sharma@placemakers.co.nz","call_note":"Signage","last_call":"2026-03-24","call_interval":45,"urgency":"overdue","latitude":-41.255986,"longitude":174.795738,"portatour_id":"576dbcafc33044c9a0f9749b19cd02d9"},{"external_id":"2477","name":"Mitre 10 Mega Petone","street":"25 Bouverie St","city":"PETONE","zip":"","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"04 569 8311","email":"vivian.brandon@mitre10.co.nz","call_note":"Guardian stock, pics for case to Paul re GL75s","last_call":"2026-06-18","call_interval":45,"urgency":"due","latitude":-41.219172,"longitude":174.883749,"portatour_id":"680eef5f19aa47e7ab8fa49530b32ce2"},{"external_id":"1962","name":"Bunnings Petone","street":"47 The Esplanade","city":"LOWER HUTT","zip":"5012","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"04 570 3000","email":"petone@bunnings.co.nz","call_note":"Talk to Coord re steel display, Check deranged Tradesmen etc.","last_call":"2026-04-16","call_interval":45,"urgency":"overdue","latitude":-41.225425,"longitude":174.866418,"portatour_id":"e200ad0232ec4cca8cc367b40343434d"},{"external_id":"2349","name":"ITM Crighton Seaview","street":"123 Hutt Park Road","city":"LOWER HUTT","zip":"5010","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"04 568 3896","email":"phillip@crightonslh.co.nz","call_note":"","last_call":"2026-02-17","call_interval":180,"urgency":"due","latitude":-41.238272,"longitude":174.911814,"portatour_id":"dfe5a2331b8e439cb730236cd52e95f6"},{"external_id":"2113","name":"Placemakers Hutt City","street":"49 Seaview Road","city":"LOWER HUTT","zip":"5010","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"04 568 5042","email":"patrick.duffy@placemakers.co.nz","call_note":"","last_call":"2026-04-16","call_interval":120,"urgency":"due","latitude":-41.2408,"longitude":174.908375,"portatour_id":"157bea128e73436eb1bf0613d7724b5a"},{"external_id":"2433","name":"Mitre 10 Crofton Downs Wgtn","street":"128 Churchill Drive","city":"WELLINGTON","zip":"6035","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"04 479 8765","email":"croftondowns@mitre10.co.nz","call_note":"","last_call":"2026-04-16","call_interval":120,"urgency":"due","latitude":-41.255299,"longitude":174.765679,"portatour_id":"d4d68c99e09046d5ad4c1c5a712b6a17"},{"external_id":"2479","name":"Mitre 10 Mega Porirua","street":"The Mega Shopping Mall","city":"PORIRUA","zip":"5022","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"04 233 8009","email":"jodie.lister@mitre10.co.nz","call_note":"Look at display situation, Guardian stock, pics for case to Paul re GL75s","last_call":"2026-06-18","call_interval":45,"urgency":"due","latitude":-41.131489,"longitude":174.839994,"portatour_id":"5b2b09b4a08f43b2a2da293db9258349"},{"external_id":"2136","name":"Placemakers Porirua","street":"15 John Seddon Drive","city":"PORIRUA","zip":"5022","country":"New Zealand","customer_group":"Placemakers","categories":"D","phone":"04 237 9189","email":"bkenna@placemakers.co.nz","call_note":"Armorsteel","last_call":"2026-03-26","call_interval":360,"urgency":"not due","latitude":-41.135017,"longitude":174.8333,"portatour_id":"3b1efd67dd714592a5055ee0996bd546"},{"external_id":"1963","name":"Bunnings Porirua","street":"9 Heriot Drive","city":"PORIRUA","zip":"5022","country":"New Zealand","customer_group":"Bunnings","categories":"A","phone":"04 978 1300","email":"porirua@bunnings.co.nz","call_note":"","last_call":"2026-04-15","call_interval":45,"urgency":"overdue","latitude":-41.135312,"longitude":174.83709,"portatour_id":"c1750c4038d84d52b3de5b8e3279eef1"},{"external_id":"2408","name":"Toa Building Supplies ITM","street":"17 Prosser Street","city":"Porirua","zip":"5022","country":"New Zealand","customer_group":"ITM","categories":"","phone":"04 232 5999","email":"brian.itmtawa@xtra.co.nz","call_note":"PA Doors/Flashings","last_call":"2024-11-21","call_interval":180,"urgency":"overdue","latitude":-41.133675,"longitude":174.832518,"portatour_id":"3d5197631e6d4f39bf497c6e18d747fa"},{"external_id":"2396","name":"Toa ITM Building Centre","street":"19 Park Street","city":"WELLINGTON","zip":"5140","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"04 527 6800","email":"jmoen@parapine.co.nz","call_note":"Adam re PA Doors","last_call":"2025-11-26","call_interval":180,"urgency":"overdue","latitude":-41.124519,"longitude":175.074516,"portatour_id":"9c8f930595d545bd9c4cd19a764f3086"},{"external_id":"2496","name":"Mitre 10 Mega Upper Hutt","street":"9 Park Street","city":"WELLINGTON","zip":"5018","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"04 527 2227","email":"wayneb@megapetone.co.nz","call_note":"Guardian stock, pics for case to Paul re GL75s","last_call":"2026-06-18","call_interval":45,"urgency":"due","latitude":-41.125767,"longitude":175.074078,"portatour_id":"1f959cd660a347109f2f6865d3c7baeb"},{"external_id":"2456","name":"Mitre 10 Pain & Kershaw","street":"37 - 39 Naples Street","city":"MARTINBOROUGH","zip":"5711","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"06 306 5103","email":"painandkershaw@mitre10.co.nz","call_note":"","last_call":"2026-06-18","call_interval":120,"urgency":"not due","latitude":-41.218561,"longitude":175.457352,"portatour_id":"313c70bafed541ebaba0682fefadebe3"},{"external_id":"2458","name":"Mitre 10 Mega Masterton","street":"157-169 Ngaumutawa Road","city":"MASTERTON","zip":"5840","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"06 370 6888","email":"brent.stewart@mitre10.co.nz","call_note":"Check cart stock, ask about fish smokers, ensure they have sorted their brochure issue.","last_call":"2026-06-17","call_interval":45,"urgency":"due","latitude":-40.947243,"longitude":175.636076,"portatour_id":"ef8fbddf941b4eeaac2cf54c92eb7e6c"},{"external_id":"2383","name":"ITM Tumu Masterton","street":"337 Ngaumutawa Road","city":"MASTERTON","zip":"5810","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 370 6061","email":"corrie.james@tumu.co.nz","call_note":"Establish Contact, Armorsteel.","last_call":"2025-08-05","call_interval":180,"urgency":"overdue","latitude":-40.962079,"longitude":175.612913,"portatour_id":"0ee23c1e87494cd5ac9a08ca6fcdf6bb"},{"external_id":"1986","name":"Central ITM Carterton","street":"37-53 High Street South","city":"CARTERTON","zip":"5713","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 9297598","email":"tom.james@centralitm.co.nz","call_note":"Discuss Display w/ Chris","last_call":"2026-04-14","call_interval":180,"urgency":"not due","latitude":-41.02539,"longitude":175.5271,"portatour_id":"1bbb5ab6a37e4b75b2c87fd57c5e4b29"},{"external_id":"2346","name":"ITM Crighton Greytown","street":"201 Main Street","city":"GREYTOWN","zip":"5712","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"06 304 7193","email":"chris@crightonsgtown.co.nz","call_note":"","last_call":"2026-03-25","call_interval":180,"urgency":"due","latitude":-41.086992,"longitude":175.453091,"portatour_id":"9a4d9c50d36e44c880482ee684a96d54"},{"external_id":"2118","name":"Placemakers Kapiti","street":"16 Lodestar Place","city":"PARAPARAUMU","zip":"5032","country":"New Zealand","customer_group":"Placemakers","categories":"C","phone":"04 298 6199","email":"janne.rowe@placemakers.co.nz","call_note":"Tidy up display, suggest loadshifters","last_call":"2025-11-25","call_interval":120,"urgency":"overdue","latitude":-40.902207,"longitude":174.992882,"portatour_id":"62d0a7a633ba43f79ad771d4ee6d3caf"},{"external_id":"2452","name":"Mitre 10 Mega Kapiti","street":"135 Kapiti Road","city":"PARAPARAUMU","zip":"5032","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"A","phone":"04 296 6440","email":"Vincent.Indo@mitre10.co.nz","call_note":"Training with Garden Team","last_call":"2026-06-18","call_interval":45,"urgency":"due","latitude":-40.904835,"longitude":174.996211,"portatour_id":"c07d9a9036e94628b4a7de19f0eeecb9"},{"external_id":"","name":"Carters Wellingto Central","street":"174 Hutt Road","city":"Kaiwharawhara","zip":"6035","country":"NZ","customer_group":"Carters","categories":"","phone":"","email":"","call_note":"","last_call":"2026-02-17","call_interval":360,"urgency":"not due","latitude":-41.25677605591755,"longitude":174.7947590422853,"portatour_id":"292df3277bb4417c9f6b98d9ac32bd36"},{"external_id":"","name":"Wellington Airport","street":"Stewart Duff Drive","city":"Rongotai","zip":"6022","country":"NZ","customer_group":"Other","categories":"","phone":"","email":"","call_note":"","last_call":"2026-03-30","call_interval":730,"urgency":"not due","latitude":-41.32917996366776,"longitude":174.8120569564941,"portatour_id":"993a7d726728476d8884f3137006e1c6"},{"external_id":"","name":"Bunnings Naenae","street":"235 Cambridge Terrace","city":"Naenae","zip":"5011","country":"New Zealand","customer_group":"Bunnings","categories":"","phone":"","email":"","call_note":"","last_call":"2026-04-16","call_interval":42,"urgency":"overdue","latitude":-41.1993,"longitude":174.944726,"portatour_id":"82ba75845d084fd0a6fa7d5a39b9c9e8"},{"external_id":"","name":"Bay Of Islands ITM Kaikohe","street":"2 Mangakahia Road","city":"Kaikohe","zip":"0405","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"","email":"","call_note":"","last_call":"2024-08-28","call_interval":42,"urgency":"overdue","latitude":-35.406517,"longitude":173.803549,"portatour_id":"da72f858a80749499ad446d75be5a989"},{"external_id":"2142 | 5143","name":"Mitre 10 Wainuiomata","street":"Te Ara Raukura","city":"Wainuiomata","zip":"5014","country":"New Zealand","customer_group":"Mitre 10","categories":"B","phone":"04 564 8760","email":"","call_note":"","last_call":"2026-04-16","call_interval":42,"urgency":"overdue","latitude":-41.260741,"longitude":174.945396,"portatour_id":"786ec0a8656f436d9ca18184dad34091"},{"external_id":"552","name":"North Taranaki ITM Inglewood","street":"11 James Street","city":"Inglewood","zip":"4330","country":"New Zealand","customer_group":"ITM","categories":"","phone":"0800 248 668","email":"","call_note":"Try for shed signage","last_call":"2026-07-23","call_interval":120,"urgency":"not due","latitude":-39.153296,"longitude":174.210073,"portatour_id":"98bf6503605f42c7b50189fe11c8e60b"},{"external_id":"4349","name":"Hammer Hardware Turangi","street":"Town Centre","city":"TURANGI","zip":"3334","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"07 386 7761","email":"turangi@hammerhardware.co.nz","call_note":"","last_call":"2026-07-21","call_interval":180,"urgency":"not due","latitude":-38.989257,"longitude":175.808601,"portatour_id":"0dabcac823e945b9abbf43f52e2e982a"},{"external_id":"2416","name":"ITM Turangi","street":"65 Atirau Road","city":"TURANGI","zip":"3334","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"07 386 5736","email":"andrew@turangiitm.co.nz","call_note":"","last_call":"2026-07-21","call_interval":180,"urgency":"not due","latitude":-39.00165,"longitude":175.803594,"portatour_id":"e0152d2587104fc7859be69da7a91667"},{"external_id":"5147","name":"Dargaville Hammer Hardware","street":"30 Normanby Street","city":"Dargaville","zip":"0310","country":"New Zealand","customer_group":"Hammer Hardware","categories":"D","phone":"","email":"","call_note":"","last_call":"2026-02-10","call_interval":42,"urgency":"overdue","latitude":-35.942266,"longitude":173.867574,"portatour_id":"c121db1cc9ba41409a6fbebaef04bc66"},{"external_id":"","name":"Hammer Hardware Foxton","street":"73 Main Street","city":"Foxton","zip":"4814","country":"New Zealand","customer_group":"Hammer Hardware","categories":"C","phone":"","email":"","call_note":"","last_call":"2026-02-18","call_interval":90,"urgency":"overdue","latitude":-40.475523,"longitude":175.281587,"portatour_id":"8321d6d1ce3a4b9aa4a61da8a8fe7f55"},{"external_id":"","name":"Mitre 10 Central Wellington","street":"133 Tory Street","city":"Te Aro","zip":"6011","country":"New Zealand","customer_group":"Mitre 10","categories":"","phone":"","email":"brendan.hall@mitre10.co.nz","call_note":"Training with James and Team","last_call":"2026-03-26","call_interval":42,"urgency":"overdue","latitude":-41.296738,"longitude":174.779316,"portatour_id":"c52e1981d2a84e83a247ecee8956d765"},{"external_id":"","name":"Buildlink Head Office : Bream Bay","street":"Cnr Sime & Kepa Roads","city":"RUAKAKA","zip":"151","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"","call_interval":42,"urgency":"overdue","latitude":-35.87384,"longitude":174.463357,"portatour_id":"fd5820444f9b41ec8f40bc06f25c8064"},{"external_id":"","name":"Buildlink Head Office : Buildlink Kaitaia","street":"374 State Highway 1","city":"Kaitaia","zip":"","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"","call_interval":42,"urgency":"overdue","latitude":-35.073732,"longitude":173.257617,"portatour_id":"0c3db81f88b240089a260ce09c4db9fd"},{"external_id":"","name":"Buildlink Head Office : DJD Enterprises Ltd TA Concrete World","street":"","city":"Blenheim","zip":"","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"2025-05-28","call_interval":730,"urgency":"not due","latitude":-41.511738,"longitude":173.953003,"portatour_id":"15f9ed10d52e4a53a14ca3392ef3d5c6"},{"external_id":"","name":"Buildlink Head Office : Hautapu Rural Supplies","street":"","city":"Taihape","zip":"4720","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"2025-09-04","call_interval":42,"urgency":"overdue","latitude":-39.6779,"longitude":175.799,"portatour_id":"ed90cdabc79b4fbfb379e5d042e76607"},{"external_id":"","name":"Buildlink Head Office : Lumberland 2006 Limited","street":"","city":"Palmerston North","zip":"4410","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"Bring PA door Sample","last_call":"2026-02-04","call_interval":42,"urgency":"overdue","latitude":-40.356942,"longitude":175.609028,"portatour_id":"258a5d0db14d436590ee8832ee98b666"},{"external_id":"","name":"Buildlink Head Office : Sticks Building Supplies","street":"8 John Glenn Avenue, Rosedale, Auckland 0632","city":"Auckland","zip":"751","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"2026-02-25","call_interval":180,"urgency":"due","latitude":-36.747133,"longitude":174.695881,"portatour_id":"b7913f870f224614b5a48ffe14f415a1"},{"external_id":"","name":"Buildlink Head Office : Toots Timber & Hardware","street":"","city":"Whangarei","zip":"173","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"2026-02-11","call_interval":42,"urgency":"overdue","latitude":-35.683741,"longitude":174.334243,"portatour_id":"10b49be41d614566996528a085489ff1"},{"external_id":"","name":"Buildlink Head Office: East Taratahi Building Supplies Ltd","street":"William Wong Place","city":"Carterton","zip":"5713","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"2025-03-18","call_interval":180,"urgency":"overdue","latitude":-41.023501,"longitude":175.526406,"portatour_id":"057bdffc8925471b9859920960bc11ba"},{"external_id":"2504","name":"Mitre 10 Martin Westport","street":"47-55 Palmerston Street","city":"WESTPORT","zip":"7825","country":"New Zealand","customer_group":"Mitre 10","categories":"B","phone":"03 789 7879","email":"martin@mitre10.co.nz","call_note":"Greens parts, GX500 1MAT0SOH, Indent","last_call":"2026-07-30","call_interval":90,"urgency":"not due","latitude":-41.750017,"longitude":171.597465,"portatour_id":"6ceb450e97e345e7a478f191d0a91f64"},{"external_id":"2513","name":"ITM Greymouth Building Centre","street":"62-66 Tasman Street","city":"GREYMOUTH","zip":"7805","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 768 0441","email":"manager@itmgreymouth.co.nz","call_note":"Greens Parts, Follow up Doors","last_call":"2026-03-03","call_interval":90,"urgency":"overdue","latitude":-42.468525,"longitude":171.190193,"portatour_id":"a4fc141a5bd44d2a8101c700f413c204"},{"external_id":"2447","name":"Mitre 10 Griffen & Smith Hokitika","street":"33 Weld St","city":"HOKITIKA","zip":"7810","country":"New Zealand","customer_group":"Mitre 10","categories":"C","phone":"03 755 8254","email":"hokitika@mitre10.co.nz","call_note":"GX300 1MAT0SOH and/or Polys, Indent, Greens Parts","last_call":"2026-03-03","call_interval":90,"urgency":"overdue","latitude":-42.717847,"longitude":170.963118,"portatour_id":"3603868c48a44ccf8d4ec675fda78786"},{"external_id":"2442","name":"Mitre 10 Mega Greymouth","street":"41 Water Walk Road","city":"GREYMOUTH","zip":"7805","country":"New Zealand","customer_group":"Mitre 10 Mega","categories":"C","phone":"03 769 9036","email":"griffen.smith@mitre10.co.nz","call_note":"Displays, Kaipara Sticker, Loadshifters (300CFM), Indent","last_call":"2026-07-30","call_interval":90,"urgency":"not due","latitude":-42.459404,"longitude":171.195871,"portatour_id":"01e2cb6b11e74910b1191c4bd349cbb9"},{"external_id":"2386","name":"ITM McMullan Timber","street":"18 Kaniere Road","city":"HOKITIKA","zip":"7811","country":"New Zealand","customer_group":"ITM","categories":"D","phone":"03 755 8519","email":"timber@minidata.co.nz","call_note":"","last_call":"2026-07-30","call_interval":90,"urgency":"not due","latitude":-42.724776,"longitude":170.983883,"portatour_id":"e7040534936e4b81b138a15fb4206296"},{"external_id":"","name":"Buildlink Head Office: Reefton Hardware Buildlink","street":"37 Broadway","city":"Reefton","zip":"7830","country":"New Zealand","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"","last_call":"2025-05-27","call_interval":90,"urgency":"overdue","latitude":-42.119678,"longitude":171.866991,"portatour_id":"7add29797be34d289e6194c6ceee46fd"},{"external_id":"","name":"Carters Westport","street":"185 Derby Street","city":"Westport","zip":"7825","country":"New Zealand","customer_group":"Carters","categories":"","phone":"","email":"","call_note":"","last_call":"2026-07-30","call_interval":90,"urgency":"not due","latitude":-41.765388,"longitude":171.608433,"portatour_id":"e17d16ccd0554c11b31d1f1de296b7d5"},{"external_id":"4332","name":"Hammer Hardware Kawakawa","street":"","city":"KAWAKAWA","zip":"","country":"New Zealand","customer_group":"Hammer Hardware","categories":"","phone":"","email":"","call_note":"Establish Contact","last_call":"2026-02-11","call_interval":42,"urgency":"overdue","latitude":-35.3784,"longitude":174.071883,"portatour_id":"c45c1510f15c428ca9a31bba84d6f648"},{"external_id":"1076","name":"Buildlink Head Office: Eastown Timber & Fencing","street":"4 Holyoak Street","city":"WANGANUI","zip":"4500","country":"New Zealand","customer_group":"Buildlink","categories":"E","phone":"06 349 0040","email":"sales@eastown.co.nz","call_note":"","last_call":"2023-11-30","call_interval":120,"urgency":"overdue","latitude":-39.912497,"longitude":175.066622,"portatour_id":"8456f82ed34f46dfbe8a94b8e235bb7e"},{"external_id":"","name":"Kawakawa Farm & BuildLink","street":"16 State Highway 1","city":"Kawakawa","zip":"0210","country":"NZ","customer_group":"Buildlink","categories":"","phone":"","email":"","call_note":"Armorsteel?","last_call":"","call_interval":360,"urgency":"due","latitude":-35.37931437784353,"longitude":174.06497801480148,"portatour_id":"1a0a45df1c394c2892fbb16249330c4e"}]'

CATEGORIES = [
    "Armorsteel",
    "Greens WB",
    "NZ Lifestyle",
    "NZ WB",
    "Sheds",
    "Total",
]

BROCHURE_TYPES = [
    "Premium",
    "Lifestyle",
    "Guardian",
    "Sentry",
    "Garrison",
    "Smartstore",
    "Armorsteel",
    "Cedar Classic",
    "Cedar FJ",
]

BROCHURE_DEFAULTS = {
    "Bunnings": {"Premium": 10, "Lifestyle": 10, "Sentry": 10, "Armorsteel": 2, "Cedar Classic": 5},
    "Mitre 10 Mega": {"Premium": 10, "Lifestyle": 10, "Guardian": 10, "Armorsteel": 1, "Cedar Classic": 5},
    "Mitre 10": {"Premium": 5, "Lifestyle": 5, "Guardian": 5, "Cedar Classic": 2},
    "ITM": {"Premium": 2, "Lifestyle": 2, "Smartstore": 2, "Armorsteel": 1, "Cedar Classic": 2},
    "Placemakers": {"Premium": 2, "Lifestyle": 2, "Garrison": 2, "Armorsteel": 1, "Cedar Classic": 2},
    "Hammer Hardware": {"Premium": 2, "Lifestyle": 2, "Guardian": 2, "Cedar Classic": 2},
    "Buildlink": {"Premium": 2, "Lifestyle": 2, "Smartstore": 2, "Armorsteel": 1, "Cedar Classic": 2},
    "Carters": {"Premium": 2, "Lifestyle": 2, "Smartstore": 2, "Armorsteel": 1, "Cedar Classic": 2},
}

PAGE = r"""
<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>Riverlea Group Sales Assistant</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">

    {% if view == 'trip_workspace' %}
    <div class="workspace-shell">
        <div class="panel">
            <div class="workspace-head">
                <div>
                    <h2 style="margin-bottom:4px;">{{ workspace.trip.trip_name }}</h2>
                    <p class="small" style="margin:0;">{{ workspace.trip.destination or 'Saved trip' }} · {{ workspace.trip.date_display }}</p>
                    <div class="workspace-meta">
                        <span class="change-badge change-neutral">{{ workspace.total_calls }} calls</span>
                        <span class="change-badge change-neutral">{{ workspace.days|length }} days</span>
                        <span class="change-badge {% if workspace.completed_calls == workspace.total_calls and workspace.total_calls %}change-positive{% else %}change-neutral{% endif %}">{{ workspace.completed_calls }} complete</span>
                    </div>
                </div>
                <div class="toolbar">
                    <a class="button-link secondary" href="{{ url_for('trip_prep', trip=workspace.trip.id) }}">Trip Setup</a>
                    <a class="button-link" href="{{ url_for('on_the_road', trip_id=workspace.trip.id) }}">On the Road</a>
                </div>
            </div>
        </div>

        <div class="panel" style="padding-bottom:14px;">
            <nav class="workspace-tabs" aria-label="Trip views">
                <a href="{{ url_for('trip_workspace', trip_id=workspace.trip.id, tab='schedule') }}" class="{% if workspace_tab == 'schedule' %}active{% endif %}">Schedule</a>
                <a href="{{ url_for('trip_workspace', trip_id=workspace.trip.id, tab='map') }}" class="{% if workspace_tab == 'map' %}active{% endif %}">Map</a>
                <a href="{{ url_for('trip_workspace', trip_id=workspace.trip.id, tab='calendar') }}" class="{% if workspace_tab == 'calendar' %}active{% endif %}">Calendar</a>
                <a href="{{ url_for('trip_workspace', trip_id=workspace.trip.id, tab='closeout') }}" class="{% if workspace_tab == 'closeout' %}active{% endif %}">Close Out</a>
                <a href="{{ url_for('on_the_road', trip_id=workspace.trip.id) }}">On the Road</a>
            </nav>
        </div>

        {% if workspace_tab in ['schedule','map'] and workspace.days %}
        <div class="workspace-day-tabs" data-workspace-tabs>
            {% for day in workspace.days %}<button type="button" data-day="{{ loop.index0 }}" class="{% if loop.first %}active{% endif %}">{{ day.tab_label }}</button>{% endfor %}
        </div>
        {% endif %}

        {% if workspace_tab == 'schedule' %}
        <div class="panel">
            {% if workspace.days %}
                {% for day in workspace.days %}
                <section class="schedule-day {% if loop.first %}active{% endif %}" data-day-panel="{{ loop.index0 }}">
                    <div class="schedule-heading"><strong>{{ day.full_label }}</strong><span>{{ day.calls|length }} calls · {{ day.total_visit_display }} customer time</span></div>
                    {% if day.departure %}
                    <div class="schedule-row"><div class="schedule-time">{{ day.departure }}</div><div class="schedule-main travel"><h4>Departure</h4><div class="small">Begin the day's route</div></div><div class="schedule-side">Start</div></div>
                    {% endif %}
                    {% for item in day.timeline %}
                        {% if item.type == 'travel' %}
                        <div class="schedule-row"><div class="schedule-time">{{ item.start }}</div><div class="schedule-main travel"><h4>{{ item.title }}</h4><div class="small">{{ item.detail }}</div></div><div class="schedule-side">{{ item.duration_display }}</div></div>
                        {% elif item.type == 'break' %}
                        <div class="schedule-row"><div class="schedule-time">{{ item.start }}</div><div class="schedule-main break"><h4>Work break</h4><div class="small">{{ item.detail }}</div></div><div class="schedule-side">{{ item.duration_display }}</div></div>
                        {% else %}
                        <div class="schedule-row"><div class="schedule-time">{{ item.start }}–{{ item.end }}</div><div class="schedule-main"><h4><a class="customer-link" href="{{ item.customer_url }}">{{ item.name }}</a></h4><div class="small">{{ item.address }} · {{ item.customer_group }}</div>{% if item.desired_outcome %}<div class="schedule-note"><strong>Desired outcome:</strong> {{ item.desired_outcome }}</div>{% endif %}{% if item.preparation_notes %}<div class="schedule-note"><strong>Prepare:</strong> {{ item.preparation_notes }}</div>{% endif %}{% if item.call_note %}<div class="schedule-note">⇒ {{ item.call_note }}</div>{% endif %}{% if item.open_actions %}<div class="schedule-note"><strong>{{ item.open_actions }} open action{% if item.open_actions != 1 %}s{% endif %}</strong></div>{% endif %}</div><div class="schedule-side">{{ item.duration }} min{% if item.locked %}<br><strong>Locked</strong>{% else %}<br>Flexible{% endif %}</div></div>
                        {% endif %}
                    {% endfor %}
                </section>
                {% endfor %}
            {% else %}<div class="workspace-empty">No appointments are attached to this trip.</div>{% endif %}
        </div>
        {% elif workspace_tab == 'map' %}
        <div class="panel">
            {% if workspace.days %}<div id="workspace-map" class="workspace-map" aria-label="Trip route map"></div><p id="workspace-map-status" class="small" style="margin:10px 0 0;">Loading the road route…</p>{% else %}<div class="workspace-empty">No mapped appointments are attached to this trip.</div>{% endif %}
        </div>
        {% elif workspace_tab == 'calendar' %}
        <div class="panel">
            <div class="calendar-toolbar"><h2 style="margin:0;">{{ workspace.calendar.month_label }}</h2><span class="small">Travel, calls and breaks shown in time order</span></div>
            <div class="trip-calendar">
                {% for weekday in ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'] %}<div class="calendar-weekday">{{ weekday }}</div>{% endfor %}
                {% for cell in workspace.calendar.cells %}
                <div class="calendar-cell {% if cell.outside %}outside{% endif %} {% if cell.is_trip_day %}trip-day{% endif %}">
                    <div class="calendar-date">{{ cell.day }}</div>
                    {% for event in cell.events %}<a class="calendar-event {{ event.type }}" href="{{ url_for('trip_workspace', trip_id=workspace.trip.id, tab='schedule') }}">{{ event.time }} {{ event.title }}</a>{% endfor %}
                </div>
                {% endfor %}
            </div>
        </div>
        {% elif workspace_tab == 'closeout' %}
        <div class="panel">
            <div class="heading-row"><div><h2>Trip Close-Out</h2><p>Review outcomes, capture missed calls and finish the trip with a clean follow-up list.</p></div><a class="button-link secondary" href="{{ url_for('download_trip_closeout', trip_id=workspace.trip.id) }}">Download PDF Report</a></div>
            {% if request.args.get('success') %}<div class="success">{{ request.args.get('success') }}</div>{% endif %}
            {% if request.args.get('error') %}<div class="error">{{ request.args.get('error') }}</div>{% endif %}
            <div class="closeout-grid">
                <div class="closeout-card"><div class="label">Scheduled</div><div class="value">{{ workspace.closeout.total }}</div></div>
                <div class="closeout-card good"><div class="label">Completed</div><div class="value">{{ workspace.closeout.completed }}</div></div>
                <div class="closeout-card {% if workspace.closeout.skipped %}warn{% else %}good{% endif %}"><div class="label">Skipped</div><div class="value">{{ workspace.closeout.skipped }}</div></div>
                <div class="closeout-card {% if workspace.closeout.open %}warn{% else %}good{% endif %}"><div class="label">Not Closed</div><div class="value">{{ workspace.closeout.open }}</div></div>
            </div>
            <div class="after-trip-progress">
                <div class="after-trip-step {% if workspace.closeout.completed == workspace.closeout.total and workspace.closeout.total %}done{% else %}attention{% endif %}"><div class="number">1</div><h4>Visit outcomes</h4><p>{{ workspace.closeout.completed }} completed, {{ workspace.closeout.skipped }} skipped and {{ workspace.closeout.open }} still open.</p></div>
                <div class="after-trip-step {% if workspace.closeout.notes_count >= workspace.closeout.completed and workspace.closeout.completed %}done{% else %}attention{% endif %}"><div class="number">2</div><h4>Notes captured</h4><p>{{ workspace.closeout.notes_count }} completed visits include notes.</p></div>
                <div class="after-trip-step {% if workspace.closeout.pending_actions == 0 %}done{% endif %}"><div class="number">3</div><h4>Follow-ups</h4><p>{{ workspace.closeout.pending_actions }} open actions currently linked to these customers.</p></div>
                <div class="after-trip-step {% if workspace.trip.completed_at %}done{% endif %}"><div class="number">4</div><h4>Close trip</h4><p>{% if workspace.trip.completed_at %}Closed {{ workspace.trip.completed_at_display }}{% else %}Complete the review and close the trip when ready.{% endif %}</p></div>
            </div>
        </div>
        <div class="panel">
            <div class="heading-row"><div><h2>Visit Review</h2><p>Every planned call remains visible, including completed, skipped and untouched visits.</p></div></div>
            {% for visit in workspace.closeout.visits %}
            <div class="visit-closeout-row">
                <div><strong>{{ visit.date_display }}</strong><br><span class="small">{{ visit.visit_time }}{% if visit.actual_duration %} · {{ visit.actual_duration }} min actual{% endif %}</span></div>
                <div><strong>{{ visit.store_name }}</strong><div class="small">{{ visit.status }}</div>{% if visit.outcome %}<div class="visit-closeout-note"><strong>Outcome:</strong> {{ visit.outcome }}</div>{% endif %}{% if visit.visit_notes %}<div class="visit-closeout-note">{{ visit.visit_notes }}</div>{% endif %}{% if visit.next_step %}<div class="visit-closeout-note"><strong>Next:</strong> {{ visit.next_step }}</div>{% endif %}</div>
                <div><span class="change-badge {{ visit.badge_class }}">{{ visit.status }}</span></div>
            </div>
            {% endfor %}
        </div>
        <div class="panel">
            <h2>Finish Trip</h2>
            <form method="post" action="{{ url_for('trip_closeout_action', trip_id=workspace.trip.id) }}">
                <label><strong>Close-out notes</strong><textarea class="review-textarea" name="closeout_notes" placeholder="Overall trip result, territory observations, key wins, issues and priorities for next trip...">{{ workspace.trip.closeout_notes or '' }}</textarea></label>
                <label class="ready-row" style="margin-top:14px;"><input type="checkbox" name="create_reschedule_actions" value="1" checked>Create follow-up actions for skipped or untouched calls</label>
                <div class="button-row" style="text-align:left;">
                    {% if workspace.trip.completed_at %}<button type="submit" name="action" value="reopen" class="secondary">Reopen Trip</button>{% else %}<button type="submit" name="action" value="close">Close Trip</button>{% endif %}
                    <button type="submit" name="action" value="save_notes" class="secondary">Save Notes Only</button>
                </div>
            </form>
        </div>
        {% endif %}
    </div>

    {% if workspace_tab == 'map' and workspace.days %}
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
    <script>
    document.addEventListener('DOMContentLoaded', function(){
        const days={{ workspace.map_days_json|safe }};
        const tabs=[...document.querySelectorAll('[data-workspace-tabs] button')];
        const status=document.getElementById('workspace-map-status');
        const mapElement=document.getElementById('workspace-map');
        if(!mapElement || typeof L === 'undefined'){
            if(status) status.textContent='The map library could not load. Check the internet connection and refresh the page.';
            return;
        }
        const map=L.map(mapElement).setView([-40.3,175.5],7);
        L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; OpenStreetMap contributors'}).addTo(map);
        const layer=L.layerGroup().addTo(map);
        const colours=['#246fe5','#7f56d9','#f79009','#12b76a','#d92d20'];
        let routeController=null;
        function straightLine(points,index,message){
            L.polyline(points,{color:colours[index%colours.length],weight:4,dashArray:'8,6'}).addTo(layer);
            status.textContent=message || 'Road routing is unavailable; showing the planned stop sequence.';
        }
        async function drawDay(index){
            if(routeController) routeController.abort();
            routeController=new AbortController();
            layer.clearLayers();
            const day=days[index];
            if(!day||!day.stops.length){status.textContent='No mapped calls for this day.';return;}
            const validStops=day.stops.filter(s=>Number.isFinite(Number(s.latitude))&&Number.isFinite(Number(s.longitude)));
            const points=validStops.map(s=>[Number(s.latitude),Number(s.longitude)]);
            if(!points.length){status.textContent='No valid customer coordinates are available for this day.';return;}
            validStops.forEach((s,i)=>{const icon=L.divIcon({className:'',html:`<div class="map-stop-label">${i+1} · ${s.arrival}</div>`,iconAnchor:[20,14]});L.marker([Number(s.latitude),Number(s.longitude)],{icon}).addTo(layer).bindPopup(`<strong><a href="${s.customer_url}">${s.name}</a></strong><br>${s.address}<br>${s.arrival}–${s.departure}${s.desired_outcome?`<br><strong>Desired outcome:</strong> ${s.desired_outcome}`:''}`);});
            if(points.length===1){map.setView(points[0],12);status.textContent=`${day.full_label}: one mapped call`;return;}
            map.fitBounds(L.latLngBounds(points).pad(.15));
            setTimeout(()=>map.invalidateSize(),50);
            status.textContent='Calculating road route…';
            const timeout=setTimeout(()=>routeController.abort(),8000);
            try{
                const coords=validStops.map(s=>`${Number(s.longitude)},${Number(s.latitude)}`).join(';');
                const res=await fetch(`https://router.project-osrm.org/route/v1/driving/${coords}?overview=full&geometries=geojson`,{signal:routeController.signal});
                if(!res.ok)throw new Error('routing unavailable');
                const data=await res.json();
                if(data.routes&&data.routes[0]){
                    L.geoJSON(data.routes[0].geometry,{style:{color:colours[index%colours.length],weight:5,opacity:.85}}).addTo(layer);
                    status.textContent=`${day.full_label}: ${(data.routes[0].distance/1000).toFixed(0)} km · ${Math.round(data.routes[0].duration/60)} min driving`;
                } else throw new Error('no route');
            }catch(e){
                straightLine(points,index,e.name==='AbortError'?'Road routing timed out; showing the planned stop sequence.':'Road routing is unavailable; showing the planned stop sequence.');
            }finally{
                clearTimeout(timeout);
            }
        }
        tabs.forEach((b,i)=>b.addEventListener('click',()=>{tabs.forEach(x=>x.classList.remove('active'));b.classList.add('active');drawDay(i);}));
        drawDay(0);
        setTimeout(()=>map.invalidateSize(),150);
        window.addEventListener('resize',()=>map.invalidateSize());
    });
    </script>
    {% endif %}
    {% if workspace_tab == 'schedule' %}
    <script>document.addEventListener('DOMContentLoaded',()=>{const tabs=[...document.querySelectorAll('[data-workspace-tabs] button')],panels=[...document.querySelectorAll('[data-day-panel]')];tabs.forEach((b,i)=>b.addEventListener('click',()=>{tabs.forEach(x=>x.classList.remove('active'));panels.forEach(x=>x.classList.remove('active'));b.classList.add('active');panels[i]?.classList.add('active');}));});</script>
    {% endif %}
    {% endif %}

    {% if view == 'route_planner' %}
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css">
    {% endif %}
    <style>
        * { box-sizing: border-box; }
        body {
            margin: 0;
            font-family: Arial, sans-serif;
            background: #f3f5f8;
            color: #17202a;
        }
        .page {
            max-width: 1450px;
            margin: 0 auto;
            padding: 34px 22px 60px;
        }
        h1 { margin: 0 0 8px; font-size: 42px; }
        h2 { margin-top: 0; }
        .subtitle { margin: 0 0 24px; color: #667085; font-size: 17px; }
        .nav {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-bottom: 22px;
        }
        .nav a {
            text-decoration: none;
            color: #344054;
            background: white;
            padding: 10px 14px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }
        .nav a.active { background: #246fe5; color: white; }
        .panel {
            margin-bottom: 24px;
            padding: 24px;
            background: white;
            border-radius: 14px;
            box-shadow: 0 4px 18px rgba(0, 0, 0, 0.07);
        }
        .upload-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 20px;
        }
        .upload-box {
            padding: 24px;
            border: 2px dashed #b8c0c8;
            border-radius: 12px;
        }
        .upload-box h3 { margin: 0 0 8px; font-size: 20px; }
        .upload-box p { margin: 0 0 18px; color: #667085; font-size: 14px; }
        input[type="file"], input[type="date"], select {
            width: 100%;
            max-width: 100%;
        }
        .date-box {
            max-width: 420px;
            margin: 22px auto 0;
            padding: 18px;
            background: #f8fafc;
            border-radius: 10px;
        }
        .date-box label { display: block; margin-bottom: 8px; font-weight: bold; }
        .date-box p { margin: 8px 0 0; color: #667085; font-size: 13px; }
        input[type="date"], select {
            padding: 11px;
            border: 1px solid #d0d5dd;
            border-radius: 8px;
            font-size: 16px;
            background: white;
        }
        button, .button-link {
            display: inline-block;
            border: none;
            border-radius: 9px;
            background: #246fe5;
            color: white;
            padding: 13px 22px;
            font-size: 16px;
            font-weight: bold;
            cursor: pointer;
            text-decoration: none;
        }
        button:hover, .button-link:hover { background: #1958b8; }
        .button-row { margin-top: 22px; text-align: center; }
        .secondary { background: #475467; }
        .danger { background: #b42318; }
        .error, .success {
            margin-top: 18px;
            padding: 14px;
            border-radius: 8px;
        }
        .error { background: #fee4e2; color: #b42318; }
        .success { background: #ecfdf3; color: #027a48; }
        .heading-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 20px;
            flex-wrap: wrap;
        }
        .heading-row h2 { margin: 0 0 5px; font-size: 28px; }
        .heading-row p { margin: 4px 0; color: #667085; }
        .summary-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 18px;
            margin-top: 22px;
        }
        .summary-card {
            min-height: 135px;
            padding: 22px;
            border-radius: 12px;
            background: #101828;
            color: white;
        }
        .summary-card .label { margin-bottom: 8px; color: #d0d5dd; font-size: 14px; }
        .summary-card .value { font-size: 29px; font-weight: bold; }
        .summary-card .note { margin-top: 7px; color: #d0d5dd; font-size: 13px; }
        .summary-card.positive { background: #087443; }
        .summary-card.negative { background: #b42318; }
        .summary-card.neutral { background: #475467; }
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px;
        }
        .metric {
            padding: 18px;
            border: 1px solid #e4e7ec;
            border-radius: 10px;
            background: #f8fafc;
        }
        .metric .label { margin-bottom: 7px; color: #667085; font-size: 14px; }
        .metric .value { font-size: 23px; font-weight: bold; }
        .progress-track {
            height: 18px;
            margin-top: 20px;
            overflow: hidden;
            border-radius: 999px;
            background: #eaecf0;
        }
        .progress-fill { height: 100%; border-radius: 999px; background: #246fe5; }
        .progress-fill.positive { background: #12b76a; }
        .progress-fill.negative { background: #f04438; }
        .progress-fill.neutral { background: #98a2b3; }
        .progress-text {
            display: flex;
            justify-content: space-between;
            gap: 15px;
            margin-top: 8px;
            color: #667085;
            font-size: 14px;
        }
        .comparison-table, .history-table {
            width: 100%;
            border-collapse: collapse;
        }
        .comparison-table th, .comparison-table td,
        .history-table th, .history-table td {
            padding: 14px 12px;
            text-align: right;
            border-bottom: 1px solid #e4e7ec;
        }
        .comparison-table th:first-child, .comparison-table td:first-child,
        .history-table th:first-child, .history-table td:first-child {
            text-align: left;
        }
        .comparison-table th, .history-table th {
            background: #f8fafc;
            color: #475467;
            font-size: 14px;
        }
        .comparison-table .total-row { background: #f8fafc; font-weight: bold; }
        .change-badge {
            display: inline-block;
            min-width: 86px;
            padding: 7px 10px;
            border-radius: 999px;
            text-align: center;
            font-weight: bold;
        }
        .change-positive { background: #dcfae6; color: #087443; }
        .change-negative { background: #fee4e2; color: #b42318; }
        .change-neutral { background: #eaecf0; color: #475467; }
        .file-details {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 18px;
            margin-top: 20px;
        }
        .file-detail { padding: 16px; border-radius: 10px; background: #f8fafc; }
        .file-detail strong { display: block; margin-bottom: 5px; }
        .file-detail span { color: #667085; overflow-wrap: anywhere; }
        .month-link {
            color: #155eef;
            text-decoration: none;
            font-weight: 700;
        }
        .month-link:hover { text-decoration: underline; }
        .view-link {
            display: inline-block;
            padding: 10px 14px;
            border-radius: 8px;
            background: #246fe5;
            color: white;
            text-decoration: none;
            font-weight: 700;
            white-space: nowrap;
        }
        .view-link:hover { background: #1958b8; }
        .action-cell {
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 10px;
        }
        .empty { color: #667085; text-align: center; padding: 30px; }
        .small { color: #667085; font-size: 13px; }
        .chart-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 20px;
        }
        .chart-card {
            padding: 20px;
            border: 1px solid #e4e7ec;
            border-radius: 12px;
            background: #ffffff;
        }
        .chart-card.full { grid-column: 1 / -1; }
        .chart-card h3 { margin: 0 0 5px; font-size: 19px; }
        .chart-card p { margin: 0 0 16px; color: #667085; font-size: 13px; }
        .chart-wrap { position: relative; width: 100%; height: 330px; }
        .chart-wrap.tall { height: 380px; }
        .chart-wrap canvas { width: 100%; height: 100%; display: block; }
        .chart-controls {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            flex-wrap: wrap;
            margin-bottom: 12px;
        }
        .chart-controls select { width: auto; min-width: 190px; }
        .chart-legend {
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
            color: #475467;
            font-size: 13px;
            margin-bottom: 10px;
        }
        .legend-item { display: inline-flex; align-items: center; gap: 7px; }
        .legend-swatch { width: 13px; height: 13px; border-radius: 3px; display: inline-block; }
        .swatch-actual { background: #246fe5; }
        .swatch-target { background: #98a2b3; }
        .swatch-positive { background: #12b76a; }
        .swatch-negative { background: #f04438; }
        .opportunity-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 16px;
            margin-top: 20px;
        }
        .opportunity-card {
            padding: 20px;
            border-radius: 12px;
            background: #101828;
            color: white;
        }
        .opportunity-card .label { color: #d0d5dd; font-size: 13px; margin-bottom: 7px; }
        .opportunity-card .value { font-size: 29px; font-weight: 700; }
        .opportunity-card.urgent { background: #b42318; }
        .opportunity-card.warning { background: #b54708; }
        .opportunity-card.positive { background: #087443; }
        .finder-help { color: #667085; line-height: 1.55; }
        .finder-table { width: 100%; border-collapse: collapse; }
        .finder-table th, .finder-table td {
            padding: 13px 11px;
            border-bottom: 1px solid #e4e7ec;
            text-align: left;
            vertical-align: top;
        }
        .finder-table th { background: #f8fafc; color: #475467; font-size: 13px; }
        .severity { display: inline-block; padding: 6px 9px; border-radius: 999px; font-weight: 700; font-size: 12px; }
        .severity-critical { background: #fee4e2; color: #b42318; }
        .severity-high { background: #fef0c7; color: #b54708; }
        .severity-medium { background: #eaf2ff; color: #175cd3; }
        .severity-positive { background: #dcfae6; color: #087443; }
        .detected-columns { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .column-pill { background: #f2f4f7; color: #344054; padding: 7px 10px; border-radius: 999px; font-size: 12px; }

        .four-upload-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 18px;
        }
        .report-tag {
            display: inline-block;
            padding: 5px 9px;
            border-radius: 999px;
            background: #eef4ff;
            color: #175cd3;
            font-size: 12px;
            font-weight: 700;
            margin-bottom: 9px;
        }
        .insight-list {
            display: grid;
            gap: 14px;
        }
        .insight-card {
            padding: 18px;
            border: 1px solid #e4e7ec;
            border-left: 5px solid #246fe5;
            border-radius: 10px;
            background: #fff;
        }
        .insight-card.critical { border-left-color: #d92d20; background: #fff7f6; }
        .insight-card.high { border-left-color: #f79009; background: #fffcf5; }
        .insight-card.growth { border-left-color: #12b76a; background: #f6fef9; }
        .insight-card h3 { margin: 0 0 6px; font-size: 18px; }
        .insight-card p { margin: 4px 0; color: #475467; line-height: 1.45; }
        .section-note { color: #667085; margin-top: -8px; margin-bottom: 18px; }
        .mini-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 16px;
        }
        .mini-card {
            padding: 18px;
            border-radius: 10px;
            background: #f8fafc;
            border: 1px solid #e4e7ec;
        }
        .mini-card .label { color: #667085; font-size: 13px; margin-bottom: 7px; }
        .mini-card .value { font-size: 24px; font-weight: 700; }
        .toolbar { display: flex; gap: 10px; justify-content: flex-end; flex-wrap: wrap; }
        .secondary-button {
            display: inline-block;
            padding: 10px 16px;
            border: 1px solid #d0d5dd;
            border-radius: 8px;
            background: white;
            color: #344054;
            font-weight: 700;
            cursor: pointer;
        }
        .followup-textarea, .review-textarea, .review-input {
            width: 100%;
            border: 1px solid #d0d5dd;
            border-radius: 9px;
            padding: 12px;
            font: inherit;
            background: white;
        }
        .followup-textarea { min-height: 300px; resize: vertical; line-height: 1.5; }
        .review-textarea { min-height: 100px; resize: vertical; line-height: 1.45; }
        .review-input { min-height: 44px; }
        .followup-card {
            padding: 20px;
            border: 1px solid #e4e7ec;
            border-radius: 12px;
            background: #fff;
            margin-bottom: 16px;
        }
        .followup-card.armoursteel { border-left: 5px solid #7f56d9; }
        .followup-card .check-row { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
        .followup-card .check-row input { width:20px; height:20px; }
        .followup-grid { display:grid; grid-template-columns: 1fr 1fr 180px 180px; gap:14px; }
        .followup-grid label, .draft-box label { display:block; font-weight:700; font-size:13px; color:#475467; }
        .followup-grid input, .followup-grid select, .draft-box textarea { margin-top:6px; }
        .draft-box { margin-top:14px; padding:14px; border-radius:10px; background:#f8fafc; }
        .tag-purple { display:inline-block; padding:5px 9px; border-radius:999px; background:#f4ebff; color:#6941c6; font-size:12px; font-weight:700; }
        .copy-button { padding:8px 12px; font-size:13px; margin-top:8px; background:#475467; }
        @media (max-width: 900px) { .followup-grid { grid-template-columns: 1fr 1fr; } }
        @media (max-width: 600px) { .followup-grid { grid-template-columns: 1fr; } }
        .table-wrap { overflow-x: auto; }
        .number-positive { color: #087443; font-weight: 700; }
        .number-negative { color: #b42318; font-weight: 700; }
        .muted { color: #667085; }

        .route-layout { display:grid; grid-template-columns:minmax(0,1.65fr) minmax(320px,.75fr); gap:18px; align-items:start; }
        #route-map { height:680px; width:100%; position:relative; overflow:hidden; border-radius:12px; border:1px solid #d0d5dd; background:#eef2f6; isolation:isolate; }

        /* Leaflet fallback layout. These rules keep map tiles and controls contained
           if the external Leaflet stylesheet is delayed or blocked. */
        .leaflet-container { overflow:hidden; -webkit-tap-highlight-color:transparent; }
        .leaflet-pane, .leaflet-tile, .leaflet-marker-icon, .leaflet-marker-shadow,
        .leaflet-tile-container, .leaflet-pane > svg, .leaflet-pane > canvas,
        .leaflet-zoom-box { position:absolute; left:0; top:0; }
        .leaflet-map-pane, .leaflet-tile-pane, .leaflet-overlay-pane,
        .leaflet-shadow-pane, .leaflet-marker-pane, .leaflet-tooltip-pane,
        .leaflet-popup-pane { position:absolute; left:0; top:0; }
        .leaflet-tile { width:256px !important; height:256px !important; max-width:none !important; max-height:none !important; padding:0 !important; margin:0 !important; user-select:none; -webkit-user-drag:none; }
        .leaflet-container img.leaflet-image-layer, .leaflet-container .leaflet-tile { max-width:none !important; max-height:none !important; }
        .leaflet-control-container { position:absolute; inset:0; pointer-events:none; z-index:800; }
        .leaflet-control { position:relative; z-index:800; pointer-events:auto; float:left; clear:both; }
        .leaflet-top, .leaflet-bottom { position:absolute; z-index:1000; pointer-events:none; }
        .leaflet-top { top:0; } .leaflet-right { right:0; } .leaflet-bottom { bottom:0; } .leaflet-left { left:0; }
        .leaflet-top .leaflet-control { margin-top:10px; } .leaflet-bottom .leaflet-control { margin-bottom:10px; }
        .leaflet-left .leaflet-control { margin-left:10px; } .leaflet-right .leaflet-control { margin-right:10px; float:right; }
        .leaflet-zoom-animated { transform-origin:0 0; }
        .leaflet-marker-icon, .leaflet-marker-shadow { display:block; }
        .route-toolbar { display:grid; grid-template-columns:2fr 1fr 1fr auto; gap:10px; margin-bottom:14px; }
        .route-toolbar input, .route-toolbar select { width:100%; padding:10px; border:1px solid #d0d5dd; border-radius:8px; background:white; font:inherit; }
        .route-side { position:sticky; top:14px; }
        .route-summary { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; margin-bottom:14px; }
        .route-mini { padding:12px; background:#f8fafc; border:1px solid #e4e7ec; border-radius:9px; }
        .route-mini strong { display:block; font-size:22px; margin-top:3px; }
        .selected-list { max-height:430px; overflow:auto; border:1px solid #e4e7ec; border-radius:10px; }
        .selected-customer { padding:11px 12px; border-bottom:1px solid #e4e7ec; display:flex; justify-content:space-between; gap:10px; align-items:flex-start; }
        .selected-customer:last-child { border-bottom:0; }
        .selected-customer button { padding:5px 9px; font-size:12px; background:#b42318; }
        .route-actions { display:flex; gap:9px; flex-wrap:wrap; margin-top:12px; }
        .route-actions button { padding:10px 14px; font-size:14px; }
        .route-import { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
        .route-import input[type=file] { max-width:420px; }
        .map-legend { display:flex; flex-wrap:wrap; gap:12px; margin-top:10px; color:#667085; font-size:13px; }
        .map-legend span { display:inline-flex; align-items:center; gap:6px; }
        .legend-pin { width:12px; height:12px; border-radius:50%; display:inline-block; border:2px solid white; box-shadow:0 0 0 1px #667085; }
        .pin-overdue { background:#d92d20; }.pin-due { background:#f79009; }.pin-not-due { background:#12b76a; }.pin-other { background:#667085; }.pin-selected { background:#246fe5; }
        .route-save-grid { display:grid; grid-template-columns:1fr 130px 130px; gap:10px; margin-top:14px; }
        .route-save-grid input { width:100%; padding:10px; border:1px solid #d0d5dd; border-radius:8px; }
        .saved-route-row { display:flex; justify-content:space-between; gap:12px; align-items:center; padding:10px 0; border-bottom:1px solid #e4e7ec; }
        .saved-route-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
        .saved-route-actions form { margin:0; }
        .compact-danger { padding:9px 12px; font-size:13px; background:#b42318; }
        .compact-danger:hover { background:#912018; }
        .route-planning-panel { margin-top:18px; padding:16px; border:1px solid #d0d5dd; border-radius:12px; background:#f8fafc; }
        .route-planning-panel h3 { margin:0 0 12px; }
        .route-plan-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
        .route-plan-grid label { display:block; font-size:12px; font-weight:700; color:#475467; }
        .route-plan-grid input, .route-plan-grid select { width:100%; margin-top:5px; padding:9px; border:1px solid #d0d5dd; border-radius:8px; background:white; font:inherit; }
        .route-location-row { display:grid; grid-template-columns:1fr auto; gap:8px; align-items:end; }
        .route-location-row button { padding:9px 11px; font-size:12px; }
        .route-check { display:flex !important; align-items:center; gap:8px; min-height:42px; padding-top:19px; }
        .route-check input { width:18px !important; height:18px; margin:0 !important; }
        .route-status { margin-top:12px; padding:11px 12px; border-radius:9px; background:#eef4ff; color:#194185; font-size:13px; }
        .route-status.error-state { background:#fee4e2; color:#b42318; }
        .route-result { margin-top:18px; }
        .route-result-summary { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; margin-bottom:14px; }
        .route-result-summary .route-mini { background:white; }
        .route-day { margin-top:12px; border:1px solid #d0d5dd; border-radius:11px; overflow:hidden; background:white; }
        .route-day-header { display:flex; justify-content:space-between; gap:12px; align-items:center; padding:13px 14px; background:#101828; color:white; }
        .route-day-header strong { font-size:17px; }
        .route-day-header a { color:white; font-size:12px; font-weight:700; }
        .route-stop { display:grid; grid-template-columns:40px 1fr auto; gap:10px; align-items:center; padding:11px 12px; border-top:1px solid #e4e7ec; }
        .route-stop:first-child { border-top:0; }
        .route-stop-number { width:30px; height:30px; border-radius:50%; display:flex; align-items:center; justify-content:center; background:#eef4ff; color:#175cd3; font-weight:800; }
        .route-stop-time { text-align:right; color:#667085; font-size:12px; white-space:nowrap; }
        .route-reorder { display:flex; gap:5px; margin-top:5px; }
        .route-reorder button { padding:4px 7px; font-size:11px; background:#475467; }
        .map-mode-banner { display:none; margin:0 0 10px; padding:10px 12px; border-radius:8px; background:#fef0c7; color:#92400e; font-weight:700; }
        .map-mode-banner.active { display:block; }
        .leaflet-interactive.route-line { stroke-linecap:round; stroke-linejoin:round; }
        @media(max-width:1050px){ .route-layout{grid-template-columns:1fr;} .route-side{position:static;} #route-map{height:580px;} }
        @media(max-width:900px){ .route-plan-grid{grid-template-columns:1fr 1fr;} .route-result-summary{grid-template-columns:1fr 1fr;} }
        @media(max-width:700px){ .route-toolbar,.route-save-grid,.route-plan-grid{grid-template-columns:1fr;} #route-map{height:500px;} .route-summary,.route-result-summary{grid-template-columns:1fr 1fr;} .route-stop{grid-template-columns:36px 1fr;} .route-stop-time{grid-column:2;text-align:left;} }

        .road-dashboard { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin:16px 0 20px; }
        .road-kpi { padding:15px; border:1px solid #e4e7ec; border-radius:11px; background:#f8fafc; }
        .road-kpi .label { color:#667085; font-size:12px; margin-bottom:5px; }
        .road-kpi .value { font-size:23px; font-weight:800; }
        .road-card.active { border-color:#246fe5; box-shadow:0 0 0 2px #dbeafe; }
        .road-card.skipped { border-color:#fdba74; background:#fff7ed; }
        .road-status-line { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin:8px 0 3px; }
        .road-status-chip { display:inline-block; padding:6px 9px; border-radius:999px; font-size:12px; font-weight:800; background:#f2f4f7; color:#475467; }
        .road-status-chip.active { background:#eef4ff; color:#175cd3; }
        .road-status-chip.complete { background:#dcfae6; color:#087443; }
        .road-status-chip.skipped { background:#fef0c7; color:#b54708; }
        .road-live-actions { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:9px; margin:14px 0; }
        .road-live-actions form { margin:0; }
        .road-live-actions button, .road-live-actions a { width:100%; text-align:center; padding:11px 9px; font-size:14px; }
        .road-fields { display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-top:12px; }
        .road-fields label { display:block; font-size:13px; font-weight:700; color:#475467; }
        .road-fields input, .road-fields select { width:100%; margin-top:5px; padding:10px; border:1px solid #d0d5dd; border-radius:8px; background:white; }
        .road-attachment { display:flex; justify-content:space-between; gap:10px; align-items:center; padding:8px 0; border-bottom:1px solid #e4e7ec; }
        .road-attachment:last-child { border-bottom:0; }
        @media(max-width:800px){ .road-dashboard{grid-template-columns:1fr 1fr;} .road-live-actions{grid-template-columns:1fr 1fr;} }
        @media(max-width:520px){ .road-dashboard,.road-fields{grid-template-columns:1fr;} .road-card{padding:16px;} .road-store{font-size:24px;} .road-live-actions{grid-template-columns:1fr 1fr;} .road-live-actions button,.road-live-actions a{font-size:15px;padding:13px 8px;} }


        .daily-hours-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; }
        .daily-hours-card { display:grid; grid-template-columns:64px 1fr 1fr; gap:10px; align-items:end; padding:12px; border:1px solid #d0d5dd; border-radius:10px; background:#fff; }
        .daily-hours-card strong { align-self:center; white-space:nowrap; }
        .daily-hours-card label { min-width:0; }
        .daily-hours-card input { width:100%; min-width:0; }
        @media print {
            .nav, .upload-panel, .toolbar, button { display: none !important; }
            body { background: white; }
            .page { max-width: none; padding: 0; }
            .panel { box-shadow: none; border: 1px solid #d0d5dd; break-inside: avoid; }
        }

        .priority-summary {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 16px;
            margin-top: 18px;
        }
        .priority-score {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-width: 58px;
            padding: 8px 10px;
            border-radius: 999px;
            background: #101828;
            color: white;
            font-weight: 800;
        }
        .priority-class {
            display: inline-block;
            padding: 7px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: 800;
        }
        .priority-protect { background:#dcfae6; color:#087443; }
        .priority-grow { background:#eef4ff; color:#175cd3; }
        .priority-maintain { background:#f2f4f7; color:#344054; }
        .priority-low { background:#fef0c7; color:#b54708; }
        .priority-form-grid {
            display:grid;
            grid-template-columns: 1.4fr 120px 150px 160px 180px 1fr auto;
            gap:10px;
            align-items:end;
        }
        .priority-form-grid label { font-size:12px; font-weight:700; color:#475467; }
        .priority-form-grid input, .priority-form-grid select {
            margin-top:5px; width:100%; padding:9px; border:1px solid #d0d5dd; border-radius:7px; background:white;
        }
        .priority-form-grid button { margin:0; padding:10px 14px; }
        .score-breakdown { color:#667085; font-size:12px; line-height:1.45; }
        @media (max-width: 1100px) { .priority-form-grid { grid-template-columns: 1fr 1fr 1fr; } }
        @media (max-width: 700px) { .priority-form-grid, .priority-summary { grid-template-columns: 1fr; } }


        .workflow-nav {
            display:grid; grid-template-columns:repeat(8,minmax(130px,1fr)); gap:10px; margin-bottom:24px;
        }
        .workflow-nav a {
            display:grid; grid-template-columns:34px 1fr; grid-template-rows:auto auto; column-gap:9px;
            padding:12px; border-radius:12px; background:white; color:#344054; text-decoration:none;
            border:1px solid #e4e7ec; box-shadow:0 2px 8px rgba(0,0,0,.04);
        }
        .workflow-nav a > span { grid-row:1 / 3; width:30px; height:30px; border-radius:50%; display:flex; align-items:center; justify-content:center; background:#eef4ff; color:#175cd3; font-weight:800; }
        .workflow-nav strong { font-size:14px; line-height:18px; }
        .workflow-nav small { color:#667085; font-size:11px; line-height:15px; }
        .workflow-nav a.active { background:#175cd3; color:white; border-color:#175cd3; }
        .workflow-nav a.active span { background:white; color:#175cd3; }
        .workflow-nav a.active small { color:#dbe7ff; }
        .year-planner-toolbar { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:14px; }
        .year-planner-toolbar .year-nav { display:flex; align-items:center; gap:10px; }
        .year-planner-toolbar .year-title { font-size:24px; font-weight:800; min-width:80px; text-align:center; }
        .year-planner-wrap { overflow-x:auto; padding-bottom:10px; }
        .year-planner { display:grid; grid-template-columns:repeat(12, minmax(205px, 1fr)); gap:8px; min-width:2520px; }
        .year-month { border:1px solid #d0d5dd; border-radius:10px; overflow:hidden; background:white; }
        .year-month-header { padding:9px 10px; background:#101828; color:white; font-weight:800; text-align:center; letter-spacing:.02em; }
        .year-day { min-height:27px; display:grid; grid-template-columns:25px 31px 1fr; align-items:stretch; border-top:1px dotted #d0d5dd; font-size:11px; }
        .year-day.weekend { background:#f2f4f7; }
        .year-day.today { outline:2px solid #246fe5; outline-offset:-2px; }
        .year-day-number, .year-day-name { padding:6px 3px; text-align:center; color:#475467; }
        .year-day-name { border-right:1px solid #e4e7ec; }
        .year-day-events { min-width:0; display:flex; flex-direction:column; }
        .calendar-trip { display:block; flex:1; padding:5px 6px; text-decoration:none; color:#17202a; font-weight:700; line-height:1.25; border-left:4px solid #98a2b3; background:#f2f4f7; overflow:hidden; text-overflow:ellipsis; }
        .calendar-trip:hover { filter:brightness(.97); }
        .calendar-trip.status-ready { background:#dcfae6; border-left-color:#12b76a; color:#087443; }
        .calendar-trip.status-progress { background:#eef4ff; border-left-color:#246fe5; color:#175cd3; }
        .calendar-trip.status-attention { background:#fef0c7; border-left-color:#f79009; color:#b54708; }
        .calendar-trip.status-complete { background:#e4e7ec; border-left-color:#667085; color:#475467; opacity:.78; }
        .calendar-trip.selected { box-shadow:inset 0 0 0 2px #101828; }
        .planner-legend { display:flex; gap:14px; flex-wrap:wrap; margin:10px 0 0; color:#667085; font-size:12px; }
        .planner-legend span { display:inline-flex; align-items:center; gap:6px; }
        .planner-dot { width:11px; height:11px; border-radius:3px; display:inline-block; }
        .planner-dot.grey { background:#98a2b3; }.planner-dot.orange { background:#f79009; }.planner-dot.blue { background:#246fe5; }.planner-dot.green { background:#12b76a; }
        @media (max-width: 750px) { .year-planner { min-width:2280px; grid-template-columns:repeat(12, 185px); } }

        .trip-steps { display:flex; gap:8px; flex-wrap:wrap; margin:18px 0 24px; }
        .trip-step { flex:1; min-width:145px; padding:12px 14px; border-radius:10px; background:#f2f4f7; color:#475467; font-weight:700; font-size:13px; }
        .trip-step.current { background:#eef4ff; color:#175cd3; border:1px solid #b2ccff; }
        .trip-step.complete { background:#ecfdf3; color:#087443; }
        .road-card { padding:18px; border:1px solid #e4e7ec; border-radius:12px; background:white; margin-bottom:12px; }
        .road-card.complete { border-color:#86efac; background:#f0fdf4; }
        .road-time { font-size:13px; color:#667085; font-weight:700; }
        .road-store { font-size:21px; font-weight:800; margin:4px 0; }
        .road-actions { display:flex; gap:9px; flex-wrap:wrap; margin-top:13px; }
        .road-actions .view-link { padding:9px 12px; font-size:13px; }
        .road-note-form { margin-top:14px; padding-top:14px; border-top:1px solid #e4e7ec; }
        .road-note-form textarea { width:100%; min-height:82px; padding:10px; border:1px solid #d0d5dd; border-radius:8px; font:inherit; resize:vertical; }
        .road-note-toolbar { display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; margin-top:9px; }
        .road-note-toolbar label { display:flex; align-items:center; gap:7px; font-weight:700; color:#344054; }
        .road-note-toolbar input[type='checkbox'] { width:19px; height:19px; }
        .road-day-heading { margin:24px 0 10px; padding:10px 13px; border-radius:9px; background:#eef4ff; color:#194185; font-weight:800; }
        .after-trip-progress { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-top:18px; }
        .after-trip-step { padding:16px; border:1px solid #e4e7ec; border-radius:11px; background:#f8fafc; }
        .after-trip-step.done { border-color:#86efac; background:#f0fdf4; }
        .after-trip-step.attention { border-color:#fdba74; background:#fff7ed; }
        .after-trip-step .number { width:28px; height:28px; display:inline-flex; align-items:center; justify-content:center; border-radius:50%; background:#246fe5; color:white; font-weight:800; margin-bottom:9px; }
        .after-trip-step h4 { margin:0 0 5px; }
        .after-trip-step p { margin:0; color:#667085; font-size:13px; line-height:1.4; }
        .closeout-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:14px; margin-top:18px; }
        .closeout-card { padding:18px; border-radius:11px; background:#101828; color:white; }
        .closeout-card.good { background:#087443; } .closeout-card.warn { background:#b54708; }
        .closeout-card .label { color:#d0d5dd; font-size:13px; } .closeout-card .value { font-size:28px; font-weight:800; margin-top:6px; }
        .visit-closeout-row { display:grid; grid-template-columns:150px 1fr 120px; gap:14px; padding:14px 0; border-bottom:1px solid #e4e7ec; align-items:start; }
        .visit-closeout-row:last-child { border-bottom:none; }
        .visit-closeout-note { color:#475467; white-space:pre-wrap; line-height:1.45; }
        @media (max-width:850px) { .after-trip-progress,.closeout-grid { grid-template-columns:1fr 1fr; } .visit-closeout-row { grid-template-columns:1fr; } }
        @media (max-width:520px) { .after-trip-progress,.closeout-grid { grid-template-columns:1fr; } }
        @media(max-width:1150px){ .workflow-nav{grid-template-columns:repeat(4,1fr);} }
        @media(max-width:700px){ .workflow-nav{grid-template-columns:1fr 1fr;} }

        @media (max-width: 1050px) {
            .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .metrics-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .opportunity-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .mini-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }
        @media (max-width: 750px) {
            .upload-grid, .four-upload-grid, .summary-grid, .metrics-grid, .file-details, .opportunity-grid, .mini-grid {
                grid-template-columns: 1fr;
            }
            h1 { font-size: 34px; }
            .chart-grid { grid-template-columns: 1fr; }
            .chart-card.full { grid-column: auto; }
            .chart-wrap, .chart-wrap.tall { height: 300px; }
            .comparison-table, .history-table {
                display: block;
                overflow-x: auto;
                white-space: nowrap;
            }
        }


        .review-toolbar {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
            margin: 18px 0;
        }

        .review-toolbar button {
            margin: 0;
        }

        .store-review-heading {
            margin: 26px 0 12px;
            padding: 12px 16px;
            border-radius: 10px;
            background: #eef4ff;
            color: #194185;
            font-size: 18px;
            font-weight: bold;
        }

        .duplicate-warning {
            margin-top: 10px;
            padding: 10px 12px;
            border-radius: 8px;
            background: #fff7ed;
            color: #9a3412;
            font-size: 13px;
        }

        .followup-card.duplicate {
            border-color: #fdba74;
            background: #fffbeb;
        }

        .control-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }
        .control-card {
            padding: 20px;
            border-radius: 12px;
            background: #101828;
            color: white;
        }
        .control-card.alert { background: #b42318; }
        .control-card.warn { background: #b54708; }
        .control-card.good { background: #087443; }
        .control-card .label { color: #d0d5dd; font-size: 14px; margin-bottom: 8px; }
        .control-card .value { font-size: 30px; font-weight: bold; }
        .control-card .note { color: #eaecf0; font-size: 13px; margin-top: 7px; }
        .checklist-list { display: grid; gap: 10px; }
        .checklist-row {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 14px 16px;
            border: 1px solid #e4e7ec;
            border-radius: 10px;
            background: #f8fafc;
        }
        .checklist-row.done { opacity: .65; text-decoration: line-through; }
        .checklist-row input { width: 20px; height: 20px; }
        .briefing-card {
            padding: 18px;
            border: 1px solid #e4e7ec;
            border-radius: 12px;
            margin-bottom: 12px;
            background: #fff;
        }
        .briefing-top { display:flex; justify-content:space-between; gap:16px; flex-wrap:wrap; }
        .briefing-store { font-size: 19px; font-weight: bold; }
        .briefing-meta { color:#667085; font-size:14px; }
        .briefing-note { margin-top:10px; padding:10px 12px; background:#f8fafc; border-radius:8px; }
        .mini-tags { display:flex; flex-wrap:wrap; gap:8px; margin-top:10px; }
        .mini-tag { padding:5px 9px; border-radius:999px; background:#eef4ff; color:#194185; font-size:12px; font-weight:bold; }
        @media (max-width: 900px) { .control-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
        @media (max-width: 600px) { .control-grid { grid-template-columns: 1fr; } }


        .prep-table { width:100%; border-collapse:collapse; min-width:1050px; }
        .prep-table th, .prep-table td { padding:10px 8px; border-bottom:1px solid #e4e7ec; text-align:center; }
        .prep-table th:first-child, .prep-table td:first-child { text-align:left; position:sticky; left:0; background:white; z-index:1; }
        .prep-table input[type="number"] { width:68px; padding:8px; border:1px solid #d0d5dd; border-radius:7px; text-align:center; }
        .prep-store { font-weight:bold; }
        .prep-meta { color:#667085; font-size:12px; margin-top:3px; }
        .packing-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(210px,1fr)); gap:14px; }
        .packing-item { display:flex; align-items:center; justify-content:space-between; gap:12px; padding:15px; border:1px solid #e4e7ec; border-radius:10px; background:#f8fafc; }
        .packing-item .qty { font-size:23px; font-weight:bold; }
        .special-list { margin:8px 0 0; padding-left:20px; color:#475467; }
        .print-only { display:none; }
        .travel-form-grid {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 14px;
        }
        .travel-form-grid label, .travel-notes-label {
            display: block;
            color: #475467;
            font-size: 13px;
            font-weight: 700;
        }
        .travel-form-grid input, .travel-form-grid select, .travel-notes-label textarea {
            width: 100%;
            margin-top: 6px;
            padding: 10px;
            border: 1px solid #d0d5dd;
            border-radius: 8px;
            background: white;
            font: inherit;
        }
        .travel-cost-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(0, 1fr));
            gap: 12px;
            margin-top: 16px;
        }
        .travel-cost-box {
            padding: 14px;
            border-radius: 10px;
            background: #f8fafc;
            border: 1px solid #e4e7ec;
        }
        .travel-cost-box h4 { margin: 0 0 10px; font-size: 14px; }
        .travel-cost-box label { display:block; color:#667085; font-size:12px; margin-top:8px; }
        .travel-cost-box input { width:100%; margin-top:4px; padding:9px; border:1px solid #d0d5dd; border-radius:7px; }
        .travel-status { display:inline-block; padding:6px 9px; border-radius:999px; font-size:12px; font-weight:700; }
        .travel-status-planned { background:#eef4ff; color:#175cd3; }
        .travel-status-booked { background:#fef0c7; color:#b54708; }
        .travel-status-completed { background:#dcfae6; color:#087443; }
        .travel-status-cancelled { background:#eaecf0; color:#475467; }
        .variance-positive { color:#087443; font-weight:700; }
        .variance-negative { color:#b42318; font-weight:700; }
        @media (max-width: 1050px) {
            .travel-form-grid { grid-template-columns: repeat(2, minmax(0,1fr)); }
            .travel-cost-grid { grid-template-columns: repeat(2, minmax(0,1fr)); }
        }
        @media (max-width: 650px) {
            .travel-form-grid, .travel-cost-grid { grid-template-columns: 1fr; }
        }

        .customer-hero {
            display:grid;
            grid-template-columns: 1.5fr repeat(3, minmax(150px, .7fr));
            gap:16px;
            align-items:stretch;
        }
        .customer-hero-main { padding:22px; border-radius:12px; background:#101828; color:white; }
        .customer-hero-main h2 { margin:0 0 8px; font-size:30px; }
        .customer-hero-main p { margin:4px 0; color:#d0d5dd; }
        .customer-stat { padding:18px; border:1px solid #e4e7ec; border-radius:12px; background:#f8fafc; }
        .customer-stat .label { color:#667085; font-size:12px; margin-bottom:8px; }
        .customer-stat .value { font-size:24px; font-weight:800; }
        .customer-section-grid { display:grid; grid-template-columns: 1.2fr .8fr; gap:20px; }
        .customer-action-card { padding:14px 16px; border:1px solid #e4e7ec; border-radius:10px; margin-bottom:10px; background:white; }
        .customer-action-top { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
        .customer-action-card h4 { margin:0 0 5px; }
        .customer-link { color:#155eef; text-decoration:none; font-weight:800; }
        .customer-link:hover { text-decoration:underline; }
        @media (max-width: 950px) { .customer-hero { grid-template-columns:1fr 1fr; } .customer-hero-main { grid-column:1 / -1; } .customer-section-grid { grid-template-columns:1fr; } }
        @media (max-width: 600px) { .customer-hero { grid-template-columns:1fr; } .customer-hero-main { grid-column:auto; } }

        .plan-grid { display:grid; grid-template-columns:1.1fr .9fr; gap:20px; margin-top:20px; }
        .plan-card { padding:20px; border:1px solid #e4e7ec; border-radius:12px; background:#fff; }
        .plan-card h3 { margin:0 0 6px; }
        .plan-form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; }
        .plan-form-grid label, .ready-list label { display:block; font-size:13px; font-weight:700; color:#475467; }
        .plan-form-grid input, .plan-form-grid select, .plan-form-grid textarea { width:100%; margin-top:6px; padding:10px; border:1px solid #d0d5dd; border-radius:8px; font:inherit; background:white; }
        .ready-list { display:grid; gap:10px; margin-top:12px; }
        .ready-row { display:flex !important; align-items:center; gap:12px; padding:13px 14px; border:1px solid #e4e7ec; border-radius:9px; background:#f8fafc; }
        .ready-row input { width:19px; height:19px; margin:0; }
        .readiness-banner { padding:18px; border-radius:12px; color:white; background:#b54708; }
        .readiness-banner.ready { background:#087443; }
        .readiness-banner .big { font-size:26px; font-weight:800; margin-bottom:4px; }
        .appointment-mini { padding:13px 0; border-bottom:1px solid #e4e7ec; }
        .appointment-mini:last-child { border-bottom:0; }
        .step-anchor { scroll-margin-top:20px; }
        @media (max-width:900px) { .plan-grid,.plan-form-grid { grid-template-columns:1fr; } }

        .route-workflow-form { margin-top:18px; padding:18px; border:1px solid #b2ccff; border-radius:12px; background:#f5f8ff; }
        .route-workflow-grid { display:grid; grid-template-columns:1.4fr 1fr 1fr auto; gap:12px; align-items:end; }
        .route-workflow-grid label { display:block; font-size:13px; font-weight:700; color:#475467; }
        .route-workflow-grid input { width:100%; margin-top:6px; padding:10px; border:1px solid #d0d5dd; border-radius:8px; font:inherit; background:white; }
        .route-context { margin-top:5px; display:flex; gap:6px; flex-wrap:wrap; }
        .route-context span { padding:3px 7px; border-radius:999px; background:#eef4ff; color:#175cd3; font-size:11px; font-weight:700; }
        @media(max-width:850px){.route-workflow-grid{grid-template-columns:1fr 1fr}.route-workflow-grid button{grid-column:1/-1}}
        @media(max-width:520px){.route-workflow-grid{grid-template-columns:1fr}}

        .settings-section-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:16px; }
        .settings-category { border:1px solid #d0d5dd; border-radius:10px; overflow:hidden; background:white; }
        .settings-category h3 { margin:0; padding:11px 14px; background:#e7aa0b; color:white; font-size:14px; text-transform:uppercase; letter-spacing:.03em; }
        .settings-category a { display:block; padding:11px 14px; border-top:1px solid #e4e7ec; color:#175cd3; text-decoration:none; font-size:14px; }
        .settings-category a:hover { background:#f8fafc; }
        .customer-settings-toolbar { display:grid; grid-template-columns:1.5fr repeat(3,minmax(145px,.7fr)) auto; gap:10px; align-items:end; }
        .customer-settings-toolbar label, .bulk-edit-grid label { display:block; font-size:12px; font-weight:700; color:#475467; }
        .customer-settings-toolbar input, .customer-settings-toolbar select, .bulk-edit-grid input, .bulk-edit-grid select { width:100%; margin-top:5px; padding:9px; border:1px solid #d0d5dd; border-radius:8px; background:white; }
        .bulk-edit-grid { display:grid; grid-template-columns:repeat(5,minmax(135px,1fr)) auto; gap:10px; align-items:end; }
        .customer-settings-table { width:100%; border-collapse:separate; border-spacing:0; min-width:1900px; }
        .customer-settings-table th { position:sticky; top:0; z-index:3; padding:10px 8px; background:#101828; color:white; font-size:12px; text-align:left; white-space:nowrap; }
        .customer-settings-table td { padding:7px 6px; border-bottom:1px solid #e4e7ec; vertical-align:top; background:white; }
        .customer-settings-table tr:nth-child(even) td { background:#f8fafc; }
        .customer-settings-table input, .customer-settings-table select, .customer-settings-table textarea { width:100%; min-width:90px; padding:8px; border:1px solid #d0d5dd; border-radius:7px; background:white; font:inherit; font-size:13px; }
        .customer-settings-table textarea { min-width:230px; min-height:38px; resize:vertical; }
        .customer-settings-table .name-field { min-width:230px; font-weight:700; }
        .customer-settings-table .address-field { min-width:210px; }
        .customer-settings-table .city-field { min-width:125px; }
        .customer-settings-table .number-field { min-width:76px; }
        .customer-settings-table .checkbox-cell { text-align:center; width:42px; }
        .customer-settings-table .checkbox-cell input { width:18px; min-width:18px; height:18px; }
        .settings-table-wrap { max-height:680px; overflow:auto; border:1px solid #d0d5dd; border-radius:10px; }
        .unsaved-note { color:#b54708; font-size:12px; font-weight:700; display:none; }
        @media(max-width:1000px){.settings-section-grid{grid-template-columns:1fr 1fr}.customer-settings-toolbar{grid-template-columns:1fr 1fr}.bulk-edit-grid{grid-template-columns:1fr 1fr}.customer-settings-toolbar button,.bulk-edit-grid button{grid-column:1/-1}}
        @media(max-width:600px){.settings-section-grid,.customer-settings-toolbar,.bulk-edit-grid{grid-template-columns:1fr}}

        @media print {
            .nav, .button-row, .subtitle, .no-print { display:none !important; }
            .page { max-width:none; padding:0; }
            .panel { box-shadow:none; border:1px solid #ddd; break-inside:avoid; }
            .print-only { display:block; }
        }


        /* Stage 6: unified trip workspace */
        .workspace-shell { display:grid; gap:16px; }
        .workspace-head { display:flex; justify-content:space-between; gap:18px; align-items:flex-start; flex-wrap:wrap; }
        .workspace-meta { display:flex; gap:8px; flex-wrap:wrap; margin-top:10px; }
        .workspace-tabs { display:flex; gap:7px; flex-wrap:wrap; border-bottom:1px solid #d0d5dd; padding-bottom:10px; }
        .workspace-tabs a { padding:10px 15px; border-radius:8px 8px 0 0; text-decoration:none; color:#344054; font-weight:800; background:#f2f4f7; }
        .workspace-tabs a.active { background:#246fe5; color:white; }
        .workspace-day-tabs { display:flex; justify-content:center; gap:7px; flex-wrap:wrap; margin:2px 0 14px; }
        .workspace-day-tabs button { padding:8px 13px; background:#f2f4f7; color:#344054; }
        .workspace-day-tabs button.active { background:#eaaa08; color:#101828; }
        .schedule-day { display:none; border:1px solid #d0d5dd; border-radius:10px; overflow:hidden; background:white; }
        .schedule-day.active { display:block; }
        .schedule-heading { padding:12px 16px; background:#101828; color:white; display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap; }
        .schedule-row { display:grid; grid-template-columns:112px 1fr 115px; align-items:start; border-top:1px solid #d0d5dd; min-height:58px; }
        .schedule-row:first-child { border-top:0; }
        .schedule-time { padding:12px 13px; font-weight:800; color:#175cd3; }
        .schedule-main { padding:11px 13px; border-left:5px solid #2e90fa; }
        .schedule-main.travel { border-left-color:#98a2b3; background:#f2f4f7; }
        .schedule-main.break { border-left-color:#eaaa08; background:#fffaeb; }
        .schedule-main h4 { margin:0 0 3px; font-size:16px; }
        .schedule-side { padding:11px 13px; text-align:right; color:#667085; font-size:13px; }
        .schedule-note { margin-top:6px; color:#475467; font-size:13px; }
        .workspace-map { height:680px; border:1px solid #d0d5dd; border-radius:10px; overflow:hidden; }
        .workspace-map-day { display:none; }
        .workspace-map-day.active { display:block; }
        .map-stop-label { background:white; border:2px solid #d92d20; border-radius:5px; color:#101828; font-weight:800; padding:2px 5px; box-shadow:0 2px 7px rgba(0,0,0,.25); white-space:nowrap; }
        .calendar-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; }
        .trip-calendar { display:grid; grid-template-columns:repeat(7,minmax(0,1fr)); border-left:1px solid #d0d5dd; border-top:1px solid #d0d5dd; }
        .calendar-weekday { padding:8px; text-align:center; background:#f2f4f7; font-weight:800; border-right:1px solid #d0d5dd; border-bottom:1px solid #d0d5dd; }
        .calendar-cell { min-height:145px; padding:7px; border-right:1px solid #d0d5dd; border-bottom:1px solid #d0d5dd; background:white; }
        .calendar-cell.outside { background:#f9fafb; color:#98a2b3; }
        .calendar-cell.trip-day { background:#fffdf0; }
        .calendar-date { font-weight:800; margin-bottom:5px; }
        .calendar-event { display:block; margin:3px 0; padding:5px 6px; border-radius:4px; background:#3498bd; color:white; text-decoration:none; font-size:11px; line-height:1.3; overflow:hidden; }
        .calendar-event.travel { background:#0b658a; }
        .calendar-event.break { background:#eaaa08; color:#101828; }
        .workspace-empty { padding:40px 20px; text-align:center; color:#667085; }
        @media(max-width:800px){ .schedule-row{grid-template-columns:85px 1fr}.schedule-side{grid-column:2;text-align:left;padding-top:0}.workspace-map{height:520px}.trip-calendar{grid-template-columns:1fr}.calendar-weekday{display:none}.calendar-cell{min-height:auto}.calendar-cell.outside{display:none} }


        /* Stage 7: simplified workflow shell */
        body { background:#f5f7fa; color:#182230; }
        .page { max-width:1580px; padding:20px 24px 64px; }
        .app-shell-header { display:flex; justify-content:space-between; align-items:center; gap:20px; padding:6px 2px 18px; }
        .brand-block { display:flex; align-items:center; gap:14px; }
        .brand-mark { width:44px; height:44px; display:flex; align-items:center; justify-content:center; border-radius:12px; background:#175cd3; color:white; font-weight:800; letter-spacing:.03em; }
        .app-shell-header h1 { margin:0; font-size:27px; line-height:1.1; }
        .app-shell-header p { margin:5px 0 0; color:#667085; font-size:14px; }
        .header-link { display:inline-block; padding:9px 13px; border-radius:8px; color:#344054; text-decoration:none; font-weight:700; border:1px solid #d0d5dd; background:white; }
        .header-link.active { color:#175cd3; border-color:#84adff; background:#eff4ff; }
        .workflow-shell { overflow:hidden; margin-bottom:16px; border:1px solid #dfe4ea; border-radius:14px; background:white; box-shadow:0 2px 8px rgba(16,24,40,.04); }
        .workflow-label { padding:9px 14px 7px; color:#667085; font-size:11px; font-weight:800; letter-spacing:.12em; }
        .workflow-primary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); border-top:1px solid #eef1f4; }
        .workflow-item { min-width:0; display:flex; align-items:center; gap:10px; padding:14px 15px; color:#344054; text-decoration:none; border-right:1px solid #eef1f4; }
        .workflow-item:last-child { border-right:0; }
        .workflow-item > span { flex:0 0 30px; width:30px; height:30px; display:flex; align-items:center; justify-content:center; border-radius:50%; background:#f2f4f7; color:#475467; font-weight:800; }
        .workflow-item div { min-width:0; }
        .workflow-item strong,.workflow-item small { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .workflow-item strong { font-size:14px; }
        .workflow-item small { margin-top:2px; color:#667085; font-size:11px; }
        .workflow-item.active { background:#eff4ff; color:#175cd3; box-shadow:inset 0 -3px 0 #175cd3; }
        .workflow-item.active > span { background:#175cd3; color:white; }
        .workflow-item.active small { color:#475467; }
        .workspace-nav { display:flex; align-items:center; flex-wrap:wrap; gap:5px; padding:9px 12px; border-top:1px solid #e4e7ec; background:#fafbfc; }
        .workspace-label { margin-right:4px; color:#98a2b3; font-size:10px; font-weight:800; letter-spacing:.12em; }
        .workspace-nav a { padding:7px 10px; border-radius:7px; color:#475467; text-decoration:none; font-size:13px; font-weight:700; }
        .workspace-nav a:hover,.workspace-nav a.active { color:#175cd3; background:#eaf2ff; }
        .workspace-nav b { display:inline-flex; min-width:20px; height:20px; align-items:center; justify-content:center; padding:0 5px; border-radius:999px; background:#d92d20; color:white; font-size:11px; }
        .page-context { display:flex; justify-content:space-between; align-items:center; gap:20px; margin-bottom:18px; padding:18px 20px; border:1px solid #dfe4ea; border-radius:14px; background:#101828; color:white; }
        .page-context h2 { margin:3px 0 4px; font-size:25px; }
        .page-context p { margin:0; color:#d0d5dd; font-size:14px; }
        .eyebrow { color:#84adff; font-size:11px; font-weight:800; letter-spacing:.1em; }
        .context-next { flex:0 0 auto; }
        .context-next .button-link { white-space:nowrap; }
        .panel { margin-bottom:16px; padding:20px; border:1px solid #dfe4ea; border-radius:12px; box-shadow:0 2px 7px rgba(16,24,40,.035); }
        .panel > .heading-row:first-child { margin-bottom:4px; }
        .heading-row h2 { font-size:23px; }
        .plan-card,.chart-card,.mini-card,.metric,.file-detail,.settings-category,.road-card,.briefing-card,.customer-action-card { box-shadow:none; }
        .button-row { margin-top:16px; }
        button,.button-link,.view-link { transition:background .15s ease, transform .05s ease; }
        button:active,.button-link:active,.view-link:active { transform:translateY(1px); }
        .trip-steps { position:sticky; top:8px; z-index:20; padding:8px; border:1px solid #dfe4ea; border-radius:12px; background:rgba(255,255,255,.96); box-shadow:0 3px 12px rgba(16,24,40,.08); }
        .trip-step { text-decoration:none; text-align:center; }
        .workspace-tabs { position:sticky; top:8px; z-index:20; background:white; padding-top:4px; }
        @media(max-width:1050px){ .workflow-primary{grid-template-columns:repeat(5,minmax(120px,1fr));overflow-x:auto}.workflow-item{min-width:145px}.page-context{align-items:flex-start}.context-next{margin-top:4px} }
        @media(max-width:700px){ .page{padding:14px 12px 50px}.app-shell-header{align-items:flex-start}.app-shell-header p{display:none}.workflow-primary{display:flex;overflow-x:auto}.workflow-item{min-width:150px}.workspace-nav{overflow-x:auto;flex-wrap:nowrap}.workspace-nav a{white-space:nowrap}.page-context{display:block}.context-next{margin-top:14px}.context-next .button-link{width:100%;text-align:center}.panel{padding:16px} }


        /* Riverlea Group brand layer — public Riverlea logo and website-inspired palette */
        :root {
            --riverlea-blue:#43A9DF;
            --riverlea-blue-dark:#147DAF;
            --riverlea-charcoal:#28333D;
            --riverlea-ink:#15212B;
            --riverlea-silver:#D9DEE3;
            --riverlea-mist:#F3F5F6;
            --riverlea-white:#FFFFFF;
        }
        body { background:var(--riverlea-mist); color:var(--riverlea-ink); }
        .page { max-width:1580px; }
        .app-shell-header {
            margin-bottom:14px; padding:16px 20px; border-radius:14px;
            background:var(--riverlea-charcoal); color:white;
            box-shadow:0 5px 18px rgba(21,33,43,.14);
        }
        .brand-block { gap:22px; min-width:0; }
        .riverlea-logo { display:flex; align-items:center; gap:10px; flex:0 0 auto; }
        .riverlea-symbol { width:48px; height:48px; display:block; }
        .riverlea-wordmark { position:relative; display:flex; align-items:flex-end; min-width:184px; line-height:1; }
        .riverlea-name { color:white; font-size:35px; font-weight:700; letter-spacing:-1.5px; }
        .riverlea-group { margin:0 0 1px 5px; color:var(--riverlea-blue); font-size:13px; font-weight:500; }
        .product-title { padding-left:20px; border-left:1px solid rgba(255,255,255,.22); min-width:0; }
        .app-shell-header .product-title h1 { color:white; font-size:24px; }
        .app-shell-header .product-title p { color:#D9E0E5; }
        .header-link { color:white; border-color:rgba(255,255,255,.28); background:rgba(255,255,255,.08); }
        .header-link:hover,.header-link.active { color:var(--riverlea-ink); border-color:var(--riverlea-blue); background:var(--riverlea-blue); }
        .workflow-shell { border-color:#D4DADE; }
        .workflow-label,.workspace-label { color:#6E7B85; }
        .workflow-item.active { background:#EAF7FD; color:var(--riverlea-blue-dark); box-shadow:inset 0 -3px 0 var(--riverlea-blue); }
        .workflow-item.active > span { background:var(--riverlea-blue); color:white; }
        .workflow-item:hover { background:#F5FBFE; }
        .workspace-nav a:hover,.workspace-nav a.active { color:var(--riverlea-blue-dark); background:#EAF7FD; }
        .page-context { border-color:var(--riverlea-charcoal); background:var(--riverlea-charcoal); }
        .eyebrow { color:#73C4EA; }
        button,.button-link,.view-link { background:var(--riverlea-blue-dark); }
        button:hover,.button-link:hover,.view-link:hover { background:#0E6791; }
        button.secondary,.button-link.secondary,.secondary-button { background:#52616D; color:white; }
        button.secondary:hover,.button-link.secondary:hover,.secondary-button:hover { background:var(--riverlea-charcoal); }
        .nav a.active,.progress-fill,.trip-step.current { background:var(--riverlea-blue-dark); }
        .month-link,.customer-link { color:var(--riverlea-blue-dark); }
        .panel { border-color:#D8DEE2; }
        .summary-card { background:var(--riverlea-charcoal); }
        .control-card { background:var(--riverlea-charcoal); }
        .road-day-heading,.store-review-heading { background:#EAF7FD; color:#155D7E; }
        .mini-tag,.report-tag,.column-pill { background:#EAF7FD; color:#155D7E; }
        .schedule-time { color:var(--riverlea-blue-dark); }
        .schedule-main { border-left-color:var(--riverlea-blue); }
        .calendar-event { background:var(--riverlea-blue-dark); }
        .calendar-event.travel { background:var(--riverlea-charcoal); }
        .workspace-tabs a.active { color:var(--riverlea-blue-dark); border-color:var(--riverlea-blue); }
        input:focus,select:focus,textarea:focus { outline:2px solid rgba(67,169,223,.35); border-color:var(--riverlea-blue); }
        @media(max-width:760px){
            .app-shell-header{padding:14px;align-items:flex-start}.brand-block{gap:12px}.riverlea-symbol{width:40px;height:40px}
            .riverlea-name{font-size:29px}.riverlea-wordmark{min-width:154px}.product-title{display:none}
        }

    </style>
</head>
<body>
<div class="page">
    <header class="app-shell-header">
        <div class="brand-block">
            <div class="riverlea-logo" aria-label="Riverlea Group">
                <svg class="riverlea-symbol" viewBox="0 0 64 64" role="img" aria-hidden="true">
                    <circle cx="32" cy="32" r="29" fill="#ffffff"/>
                    <path d="M5 27C9 12 21 4 35 4c10 0 18 4 24 11-10-2-19 0-27 5-8 5-16 8-27 7Z" fill="#43A9DF"/>
                    <path d="M59 37C54 51 44 60 29 60 18 60 9 55 4 46c10 2 19 0 27-5 8-5 17-7 28-4Z" fill="#D9DEE3"/>
                    <path d="M4 45c11 2 20 0 28-5 8-5 17-7 27-4-5 7-12 12-20 15-13 5-25 3-35-6Z" fill="#28333D"/>
                </svg>
                <div class="riverlea-wordmark">
                    <span class="riverlea-name">Riverlea</span>
                    <span class="riverlea-group">Group</span>
                </div>
            </div>
            <div class="product-title">
                <h1>Sales Assistant</h1>
                <p>Plan the week, build efficient trips, complete calls, and close out every follow-up.</p>
            </div>
        </div>
        <div class="header-actions">
            <a href="{{ url_for('settings') }}" class="header-link {% if view == 'settings' %}active{% endif %}">Settings</a>
        </div>
    </header>

    <section class="workflow-shell" aria-label="Main sales workflow">
        <div class="workflow-label">WORKFLOW</div>
        <nav class="workflow-primary">
            <a href="{{ url_for('control_centre') }}" class="workflow-item {% if view == 'control_centre' %}active{% endif %}"><span>1</span><div><strong>Prepare</strong><small>Weekly priorities</small></div></a>
            <a href="{{ url_for('route_planner') }}" class="workflow-item {% if view == 'route_planner' %}active{% endif %}"><span>2</span><div><strong>Build Route</strong><small>Select &amp; optimise</small></div></a>
            <a href="{{ url_for('trip_prep') }}" class="workflow-item {% if view in ['trip_prep','trip_workspace'] %}active{% endif %}"><span>3</span><div><strong>Finalise Trip</strong><small>Schedule &amp; materials</small></div></a>
            <a href="{{ url_for('on_the_road') }}" class="workflow-item {% if view == 'on_the_road' %}active{% endif %}"><span>4</span><div><strong>On the Road</strong><small>Run today’s calls</small></div></a>
            <a href="{{ url_for('follow_up') }}" class="workflow-item {% if view == 'follow_up' %}active{% endif %}"><span>5</span><div><strong>Close Out</strong><small>Notes &amp; follow-ups</small></div></a>
        </nav>
        <nav class="workspace-nav" aria-label="Sales tools">
            <span class="workspace-label">TOOLS</span>
            <a href="{{ url_for('customer_priorities') }}" class="{% if view in ['customer_priorities','customer_detail'] %}active{% endif %}">Customers</a>
            <a href="{{ url_for('home') }}" class="{% if view in ['dashboard','history','saved_month','opportunities','opportunity_library','saved_opportunity','visit_brief','qtr_bonus'] %}active{% endif %}">Performance</a>
            <a href="{{ url_for('actions') }}" class="{% if view in ['actions','action_form'] %}active{% endif %}">Actions{% if open_action_count %} <b>{{ open_action_count }}</b>{% endif %}</a>
            <a href="{{ url_for('travel_expenses') }}" class="{% if view == 'travel_expenses' %}active{% endif %}">Travel &amp; Expenses</a>
            <a href="{{ url_for('settings') }}" class="{% if view == 'settings' %}active{% endif %}">Settings</a>
        </nav>
    </section>

    <section class="page-context">
        <div>
            {% if view == 'control_centre' %}<span class="eyebrow">STEP 1 · PREPARE</span><h2>Weekly Control Centre</h2><p>See what needs attention before you plan the next trip.</p>
            {% elif view == 'route_planner' %}<span class="eyebrow">STEP 2 · BUILD ROUTE</span><h2>Customer Route Planner</h2><p>Select the territory, apply real-world constraints, and create a workable route.</p>
            {% elif view == 'trip_prep' %}<span class="eyebrow">STEP 3 · FINALISE TRIP</span><h2>Trip Planning</h2><p>Confirm dates, appointments, brochures, travel and final readiness.</p>
            {% elif view == 'trip_workspace' %}<span class="eyebrow">TRIP WORKSPACE</span><h2>Saved Trip</h2><p>Review the same trip as a schedule, map or calendar.</p>
            {% elif view == 'on_the_road' %}<span class="eyebrow">STEP 4 · ON THE ROAD</span><h2>Today’s Calls</h2><p>Navigate, record visits, capture evidence and create follow-ups.</p>
            {% elif view == 'follow_up' %}<span class="eyebrow">STEP 5 · CLOSE OUT</span><h2>After Trip</h2><p>Finish notes, assign actions and clear outstanding work.</p>
            {% elif view in ['customer_priorities','customer_detail'] %}<span class="eyebrow">CUSTOMER HUB</span><h2>Customers</h2><p>Manage customer value, visit priority, history and open actions.</p>
            {% elif view == 'settings' %}<span class="eyebrow">ADMINISTRATION</span><h2>Settings</h2><p>Manage customer records, route defaults and bulk updates.</p>
            {% elif view == 'travel_expenses' %}<span class="eyebrow">TRAVEL</span><h2>Travel &amp; Expenses</h2><p>Track bookings, forecasts and actual trip costs.</p>
            {% elif view in ['actions','action_form'] %}<span class="eyebrow">FOLLOW-UP</span><h2>Action Tracker</h2><p>Prioritise and complete work created from customer calls.</p>
            {% else %}<span class="eyebrow">SALES PERFORMANCE</span><h2>Performance &amp; Opportunities</h2><p>Review results, trends, bonuses and store opportunities.</p>{% endif %}
        </div>
        <div class="context-next">
            {% if view == 'control_centre' %}<a class="button-link" href="{{ url_for('route_planner') }}">Next: Build Route →</a>
            {% elif view == 'route_planner' %}<a class="button-link" href="{{ url_for('trip_prep') }}">Next: Finalise Trip →</a>
            {% elif view == 'trip_workspace' %}<a class="button-link" href="{{ url_for('on_the_road', trip_id=workspace.trip.id) }}">Next: On the Road →</a>{% elif view == 'trip_prep' %}<a class="button-link" href="{{ url_for('on_the_road') }}">Next: On the Road →</a>
            {% elif view == 'on_the_road' %}<a class="button-link" href="{{ url_for('follow_up', trip_id=active_trip.id) if active_trip else url_for('follow_up') }}">Next: Close Out →</a>
            {% elif view == 'follow_up' %}<a class="button-link secondary" href="{{ url_for('control_centre') }}">Return Home</a>{% endif %}
        </div>
    </section>



    {% if view == 'settings' %}
    <div class="panel">
        <div class="heading-row"><div><h2>Reliability & Data Protection</h2><p>Keep trip data linked correctly, check database health, and manage automatic backups.</p></div></div>
        <div class="mini-grid">
            <div class="mini-card"><div class="label">Active trip</div><div class="value" style="font-size:18px;">{{ reliability.active_trip_name or 'None selected' }}</div><div class="small">Used by On the Road and Close Out</div></div>
            <div class="mini-card"><div class="label">Data health</div><div class="value">{{ reliability.issue_count }}</div><div class="small">issue{% if reliability.issue_count != 1 %}s{% endif %} detected</div></div>
            <div class="mini-card"><div class="label">Backups</div><div class="value">{{ reliability.backup_count }}</div><div class="small">Latest: {{ reliability.latest_backup or 'None yet' }}</div></div>
        </div>
        {% if reliability.issues %}<div class="error" style="margin-top:16px;"><strong>Data health findings</strong><ul>{% for issue in reliability.issues %}<li>{{ issue }}</li>{% endfor %}</ul></div>{% else %}<div class="success" style="margin-top:16px;">No structural data problems detected.</div>{% endif %}
        <form method="post" style="display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-top:16px;">
            <input type="hidden" name="action" value="set_active_trip">
            <label style="min-width:280px;">Active trip across the workflow<select name="active_trip_id"><option value="">None selected</option>{% for trip in reliability.trips %}<option value="{{ trip.id }}" {% if trip.id == reliability.active_trip_id %}selected{% endif %}>{{ trip.trip_name }} · {{ trip.start_date }}</option>{% endfor %}</select></label>
            <button type="submit">Set Active Trip</button>
        </form>
        <div class="button-row" style="text-align:left;">
            <form method="post" style="display:inline;"><input type="hidden" name="action" value="create_backup"><button type="submit">Create Backup Now</button></form>
            <a class="button-link secondary" href="{{ url_for('data_health_report') }}">Download Health Report</a>
        </div>
        {% if reliability.backups %}<div class="table-wrap" style="margin-top:16px;"><table class="history-table"><thead><tr><th>Backup</th><th>Size</th><th></th></tr></thead><tbody>{% for backup in reliability.backups %}<tr><td>{{ backup.name }}</td><td>{{ backup.size }}</td><td><form method="post" action="{{ url_for('restore_backup', filename=backup.name) }}" onsubmit="return confirm('Restore this backup? The current database will be backed up first.');"><button class="danger" type="submit">Restore</button></form></td></tr>{% endfor %}</tbody></table></div>{% endif %}
        {% if reliability.recent_audit %}<h3 style="margin-top:24px;">Recent Activity</h3><div class="table-wrap"><table class="history-table"><thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead><tbody>{% for item in reliability.recent_audit %}<tr><td>{{ item.created_at }}</td><td>{{ item.event_type|replace('_',' ')|title }}</td><td>{{ item.detail }}</td></tr>{% endfor %}</tbody></table></div>{% endif %}
    </div>
    <div class="panel">
        <div class="heading-row">
            <div><h2>Settings</h2><p>Manage customers and route-planning defaults from one place.</p></div>
            <span class="change-badge change-neutral">{{ settings_customers|length }} customers</span>
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <div class="settings-section-grid" style="margin-top:18px;">
            <div class="settings-category"><h3>Customer Data</h3><a href="#customer-management">Edit individual customers</a><a href="#bulk-edit">Bulk update customers</a><a href="{{ url_for('route_planner') }}">Import Portatour export</a></div>
            <div class="settings-category"><h3>Route Defaults</h3><a href="#route-defaults">Rep and customer hours</a><a href="{{ url_for('route_planner') }}">Trip start and finish locations</a><a href="#customer-management">Call duration, cycle and priority</a></div>
            <div class="settings-category"><h3>Related Tools</h3><a href="{{ url_for('customer_priorities') }}">Customer value and priorities</a><a href="{{ url_for('actions') }}">Action tracker</a><a href="{{ url_for('travel_expenses') }}">Travel and expense settings</a></div>
        </div>
    </div>

    <div class="panel" id="route-defaults">
        <div class="heading-row"><div><h2>General Route Defaults</h2><p>These hours apply automatically to every new route. Individual customer trading hours and day-specific rep overrides can still take precedence.</p></div></div>
        <form method="post">
            <input type="hidden" name="action" value="save_route_defaults">
            <div class="route-plan-grid">
                <label>Rep working day starts<input type="time" name="route_rep_start" value="{{ route_defaults.rep_start }}" required></label>
                <label>Rep working day finishes<input type="time" name="route_rep_end" value="{{ route_defaults.rep_end }}" required></label>
                <label>Default customer opening<input type="time" name="route_customer_open" value="{{ route_defaults.customer_open }}" required></label>
                <label>Default customer closing<input type="time" name="route_customer_close" value="{{ route_defaults.customer_close }}" required></label>
            </div>
            <p class="small" style="margin-top:12px;">A 30-minute lunch break is scheduled automatically at the most practical point between 11:00 am and 1:00 pm.</p>
            <div class="button-row" style="text-align:left;"><button type="submit">Save Route Defaults</button></div>
        </form>
    </div>

    <div class="panel" id="bulk-edit">
        <div class="heading-row"><div><h2>Bulk Update Customers</h2><p>Tick customers in the table, then apply one or more values to all selected rows.</p></div><span id="selected-settings-count" class="change-badge change-neutral">0 selected</span></div>
        <form method="post" id="bulk-settings-form">
            <input type="hidden" name="action" value="bulk_update_customers">
            <input type="hidden" name="selected_customer_ids" id="selected-customer-ids">
            <div class="bulk-edit-grid">
                <label>Call duration (minutes)<input type="number" name="bulk_call_minutes" min="10" max="240" step="5" placeholder="Leave unchanged"></label>
                <label>Priority tier<select name="bulk_priority_tier"><option value="">Leave unchanged</option>{% for tier in ['A','B','C','D'] %}<option value="{{ tier }}">Tier {{ tier }}</option>{% endfor %}</select></label>
                <label>Call cycle (days)<input type="number" name="bulk_call_interval" min="1" max="365" placeholder="Leave unchanged"></label>
                <label>Customer group<select name="bulk_customer_group"><option value="">Leave unchanged</option>{% for group in settings_groups %}<option value="{{ group }}">{{ group }}</option>{% endfor %}</select></label>
                <label>Visit status<select name="bulk_urgency"><option value="">Leave unchanged</option><option value="overdue">Overdue</option><option value="due">Due</option><option value="not due">Not due</option></select></label>
                <label>Customer opens<input type="time" name="bulk_customer_open"></label>
                <label>Customer closes<input type="time" name="bulk_customer_close"></label>
                <button type="submit">Apply to Selected</button>
            </div>
        </form>
    </div>

    <div class="panel" id="customer-management">
        <div class="heading-row"><div><h2>Customer Management</h2><p>Edit names, addresses, visit durations, priority, call cycles and contact details. Save all visible changes together.</p></div><span id="unsaved-settings-note" class="unsaved-note">Unsaved changes</span></div>
        <div class="customer-settings-toolbar">
            <label>Search<input id="settings-search" type="search" placeholder="Customer, city, address or group"></label>
            <label>Customer group<select id="settings-group-filter"><option value="">All groups</option>{% for group in settings_groups %}<option value="{{ group }}">{{ group }}</option>{% endfor %}</select></label>
            <label>Priority tier<select id="settings-tier-filter"><option value="">All tiers</option>{% for tier in ['A','B','C','D'] %}<option value="{{ tier }}">Tier {{ tier }}</option>{% endfor %}</select></label>
            <label>Visit status<select id="settings-status-filter"><option value="">All statuses</option><option value="overdue">Overdue</option><option value="due">Due</option><option value="not due">Not due</option></select></label>
            <button id="select-visible-settings" type="button" class="secondary">Select Visible</button>
        </div>
        <form method="post" id="customer-settings-form" style="margin-top:16px;">
            <input type="hidden" name="action" value="save_customer_table">
            <div class="settings-table-wrap"><table class="customer-settings-table"><thead><tr><th class="checkbox-cell"><input id="settings-select-all" type="checkbox" aria-label="Select all visible customers"></th><th>Customer name</th><th>Street address</th><th>City</th><th>Postcode</th><th>Group</th><th>Tier</th><th>Call duration</th><th>Customer opens</th><th>Customer closes</th><th>Call cycle</th><th>Last call</th><th>Status</th><th>Phone</th><th>Email</th><th>Latitude</th><th>Longitude</th><th>Call note</th></tr></thead>
            <tbody>{% for c in settings_customers %}<tr class="settings-customer-row" data-id="{{ c.id }}" data-search="{{ (c.name ~ ' ' ~ c.street ~ ' ' ~ c.city ~ ' ' ~ c.customer_group)|lower }}" data-group="{{ c.customer_group }}" data-tier="{{ c.priority_tier }}" data-status="{{ c.urgency }}">
                <td class="checkbox-cell"><input class="settings-row-check" type="checkbox" value="{{ c.id }}"></td>
                <td><input class="name-field" name="name_{{ c.id }}" value="{{ c.name }}" required></td>
                <td><input class="address-field" name="street_{{ c.id }}" value="{{ c.street }}"></td>
                <td><input class="city-field" name="city_{{ c.id }}" value="{{ c.city }}"></td>
                <td><input name="zip_{{ c.id }}" value="{{ c.zip }}"></td>
                <td><select name="customer_group_{{ c.id }}">{% for group in settings_groups %}<option value="{{ group }}" {% if group == c.customer_group %}selected{% endif %}>{{ group }}</option>{% endfor %}</select></td>
                <td><select name="priority_tier_{{ c.id }}">{% for tier in ['A','B','C','D'] %}<option value="{{ tier }}" {% if tier == c.priority_tier %}selected{% endif %}>{{ tier }}</option>{% endfor %}</select></td>
                <td><input class="number-field" type="number" name="ideal_call_minutes_{{ c.id }}" min="10" max="240" step="5" value="{{ c.ideal_call_minutes }}"></td>
                <td><input type="time" name="customer_open_time_{{ c.id }}" value="{{ c.customer_open_time or '08:00' }}"></td>
                <td><input type="time" name="customer_close_time_{{ c.id }}" value="{{ c.customer_close_time or '17:00' }}"></td>
                <td><input class="number-field" type="number" name="call_interval_{{ c.id }}" min="1" max="365" value="{{ c.call_interval }}"></td>
                <td><input type="date" name="last_call_{{ c.id }}" value="{{ c.last_call_date }}"></td>
                <td><select name="urgency_{{ c.id }}"><option value="overdue" {% if c.urgency == 'overdue' %}selected{% endif %}>Overdue</option><option value="due" {% if c.urgency == 'due' %}selected{% endif %}>Due</option><option value="not due" {% if c.urgency == 'not due' %}selected{% endif %}>Not due</option></select></td>
                <td><input name="phone_{{ c.id }}" value="{{ c.phone }}"></td>
                <td><input name="email_{{ c.id }}" value="{{ c.email }}"></td>
                <td><input class="number-field" type="number" step="0.000001" name="latitude_{{ c.id }}" value="{{ c.latitude }}"></td>
                <td><input class="number-field" type="number" step="0.000001" name="longitude_{{ c.id }}" value="{{ c.longitude }}"></td>
                <td><textarea name="call_note_{{ c.id }}">{{ c.call_note }}</textarea></td>
            </tr>{% endfor %}</tbody></table></div>
            <div class="button-row"><button type="submit">Save All Customer Changes</button></div>
        </form>
    </div>
    <script>
    (() => {
        const rows = Array.from(document.querySelectorAll('.settings-customer-row'));
        const search = document.getElementById('settings-search');
        const group = document.getElementById('settings-group-filter');
        const tier = document.getElementById('settings-tier-filter');
        const status = document.getElementById('settings-status-filter');
        const allCheck = document.getElementById('settings-select-all');
        const count = document.getElementById('selected-settings-count');
        const idsInput = document.getElementById('selected-customer-ids');
        const bulkForm = document.getElementById('bulk-settings-form');
        const mainForm = document.getElementById('customer-settings-form');
        const unsaved = document.getElementById('unsaved-settings-note');
        function applyFilters(){
            const q=(search.value||'').trim().toLowerCase();
            rows.forEach(row => {
                const visible=(!q || row.dataset.search.includes(q)) && (!group.value || row.dataset.group===group.value) && (!tier.value || row.dataset.tier===tier.value) && (!status.value || row.dataset.status===status.value);
                row.style.display=visible?'':'none';
            });
            updateSelection();
        }
        function updateSelection(){
            const selected=rows.filter(r => r.querySelector('.settings-row-check').checked).map(r => r.dataset.id);
            count.textContent=`${selected.length} selected`;
            idsInput.value=selected.join(',');
            const visible=rows.filter(r=>r.style.display!=='none');
            allCheck.checked=visible.length>0 && visible.every(r=>r.querySelector('.settings-row-check').checked);
            allCheck.indeterminate=visible.some(r=>r.querySelector('.settings-row-check').checked) && !allCheck.checked;
        }
        [search,group,tier,status].forEach(el=>el.addEventListener(el===search?'input':'change',applyFilters));
        rows.forEach(row=>row.querySelector('.settings-row-check').addEventListener('change',updateSelection));
        allCheck.addEventListener('change',()=>{ rows.filter(r=>r.style.display!=='none').forEach(r=>r.querySelector('.settings-row-check').checked=allCheck.checked); updateSelection(); });
        document.getElementById('select-visible-settings').addEventListener('click',()=>{ rows.filter(r=>r.style.display!=='none').forEach(r=>r.querySelector('.settings-row-check').checked=true); updateSelection(); });
        bulkForm.addEventListener('submit',e=>{ updateSelection(); if(!idsInput.value){e.preventDefault(); alert('Select at least one customer first.');} });
        mainForm.addEventListener('input',()=>{unsaved.style.display='inline-block';});
        mainForm.addEventListener('change',()=>{unsaved.style.display='inline-block';});
        applyFilters();
    })();
    </script>
    {% endif %}

    {% if view == 'route_planner' %}
    <div class="panel">
        <div class="heading-row">
            <div><h2>Customer Route Planner</h2><p>Select a territory, optimise the call order, and divide the route into practical working days.</p></div>
            <span class="change-badge change-neutral">{{ map_customers|length }} mapped customers</span>
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="post" enctype="multipart/form-data" class="route-import">
            <input type="hidden" name="action" value="import_customers">
            <input type="file" name="customer_export" accept=".xlsx" required>
            <button type="submit">Update from Portatour Export</button>
            <span class="small">Existing customers are updated by Portatour ID or customer name.</span>
        </form>
    </div>

    <div class="route-layout">
        <div class="panel">
            <div class="route-toolbar">
                <input id="route-search" type="search" placeholder="Search customer, city or address">
                <select id="route-group"><option value="">All customer groups</option>{% for group in route_groups %}<option value="{{ group }}">{{ group }}</option>{% endfor %}</select>
                <select id="route-urgency"><option value="">All visit statuses</option><option value="overdue">Overdue</option><option value="due">Due</option><option value="not due">Not due</option></select>
                <button id="fit-visible" type="button" class="secondary">Fit Visible</button>
            </div>
            <div id="map-mode-banner" class="map-mode-banner">Click the map to set the location.</div>
            <div id="route-map" aria-label="Interactive customer map"></div>
            <div class="map-legend"><span><i class="legend-pin pin-overdue"></i>Overdue</span><span><i class="legend-pin pin-due"></i>Due</span><span><i class="legend-pin pin-not-due"></i>Not due</span><span><i class="legend-pin pin-selected"></i>Selected</span></div>
            <p class="small" style="margin-top:10px;">Draw around an area or click individual pins. The optimiser uses free road-routing when available and automatically falls back to a local distance calculation.</p>

            <div class="route-planning-panel">
                <h3>Build the Route</h3>
                <div class="route-plan-grid">
                    <label>Start location
                        <div class="route-location-row"><input id="start-label" value="Hamilton" placeholder="Start location"><button id="set-start" type="button" class="secondary">Set on map</button></div>
                    </label>
                    <label>End location
                        <div class="route-location-row"><input id="end-label" value="Hamilton" placeholder="End location"><button id="set-end" type="button" class="secondary">Set on map</button></div>
                    </label>
                    <label>Number of days<select id="route-days"><option>1</option><option selected>2</option><option>3</option><option>4</option><option>5</option></select></label>
                    <label class="route-check"><input id="same-end" type="checkbox" checked> Finish each day at start</label>
                    <div class="route-default-summary" style="grid-column:1 / -1;display:flex;gap:18px;flex-wrap:wrap;padding:12px 14px;border:1px solid #d0d5dd;border-radius:10px;background:white;">
                        <span><strong>Rep hours:</strong> {{ route_defaults.rep_start }}–{{ route_defaults.rep_end }}</span>
                        <span><strong>Customer hours:</strong> {{ route_defaults.customer_open }}–{{ route_defaults.customer_close }}</span>
                        <span><strong>Lunch:</strong> automatic 30 minutes between 11:00–13:00</span>
                        <a href="{{ url_for('settings') }}#route-defaults">Change defaults</a>
                    </div>
                    <input id="rep-start" type="hidden" value="{{ route_defaults.rep_start }}">
                    <input id="rep-end" type="hidden" value="{{ route_defaults.rep_end }}">
                    <input id="customer-open" type="hidden" value="{{ route_defaults.customer_open }}">
                    <input id="customer-close" type="hidden" value="{{ route_defaults.customer_close }}">
                    <label class="route-check"><input id="overnight-mode" type="checkbox"> Stay overnight and continue from the previous day's final call</label>
                    <label>Default call duration<input id="default-visit-minutes" type="number" min="10" max="240" step="5" value="45"><span class="small">Only used where a customer has no saved duration.</span></label>
                    <label>Route preference<select id="route-preference"><option value="fastest">Fastest practical order</option><option value="priority">Overdue customers first</option></select></label>
                </div>
                <div class="route-day-settings" style="margin-top:18px;">
                    <h3 style="margin-bottom:8px;">Daily rep hours</h3>
                    <p class="small">Override the rep's working window only where a particular day differs from the general Settings defaults.</p>
                    <div id="daily-hours-grid" class="daily-hours-grid"></div>
                </div>
                <div class="route-lock-panel" style="margin-top:18px;padding:16px;border:1px solid #e4e7ec;border-radius:10px;background:#f8fafc;">
                    <div class="heading-row"><div><h3 style="margin:0;">Locked &amp; preferred appointments</h3><p class="small" style="margin:4px 0 0;">Fix confirmed calls to a day and time, or mark a preferred time that the optimiser will try to honour.</p></div><button id="add-lock" type="button" class="secondary">Add Appointment</button></div>
                    <div id="locked-appointments" style="margin-top:12px;"></div>
                </div>
                <div class="route-actions">
                    <button id="optimise-route" type="button">Optimise &amp; Build Days</button>
                    <button id="clear-route" type="button" class="secondary">Clear Route Lines</button>
                </div>
                <div id="route-status" class="route-status">Select customers, confirm the start point, then build the route.</div>
                <div id="route-result" class="route-result"></div>
                <form id="route-workflow-form" method="post" class="route-workflow-form" style="display:none;">
                    <input type="hidden" name="action" value="create_trip_from_route">
                    <input type="hidden" name="route_payload" id="route-payload">
                    <input type="hidden" name="route_destination" id="route-destination">
                    <div class="route-workflow-grid">
                        <label>Trip name<input type="text" name="trip_name" id="workflow-trip-name" placeholder="e.g. Lower North Island trip" required></label>
                        <label>First day<input type="date" name="trip_start_date" id="workflow-start-date" required></label>
                        <label>Final day<input type="date" name="trip_end_date" id="workflow-end-date" readonly></label>
                        <button type="submit">Send to Plan Trip</button>
                    </div>
                    <p class="small" style="margin:10px 0 0;">Creates the trip, appointments, brochure list, customer briefs and On the Road schedule from this route.</p>
                </form>
            </div>
        </div>

        <div class="panel route-side">
            <h2 style="margin-bottom:12px;">Selected Calls</h2>
            <div class="route-summary">
                <div class="route-mini"><span class="small">Selected</span><strong id="selected-count">0</strong></div>
                <div class="route-mini"><span class="small">Overdue</span><strong id="selected-overdue">0</strong></div>
                <div class="route-mini"><span class="small">Groups</span><strong id="selected-groups">0</strong></div>
            </div>
            <div id="selected-list" class="selected-list"><div class="empty">Draw around customers or click pins to select them.</div></div>
            <div class="route-actions"><button id="select-visible" type="button">Select Visible</button><button id="clear-selection" type="button" class="secondary">Clear</button></div>
            <form method="post" id="save-route-form">
                <input type="hidden" name="action" value="save_selection">
                <input type="hidden" name="selected_ids" id="selected-ids">
                <div class="route-save-grid">
                    <input type="text" name="selection_name" placeholder="e.g. Coromandel trip" required>
                    <input type="date" name="start_date">
                    <input type="date" name="end_date">
                </div>
                <div class="button-row" style="text-align:left;margin-top:10px;"><button type="submit">Save Selection</button></div>
            </form>
            {% if saved_route_selections %}
            <h3 style="margin:20px 0 5px;">Saved Selections</h3>
            {% for item in saved_route_selections %}
            <div class="saved-route-row">
                <div><strong>{{ item.name }}</strong><br><span class="small">{{ item.count }} customers{% if item.date_display %} · {{ item.date_display }}{% endif %}</span></div>
                <div class="saved-route-actions">
                    <button type="button" class="secondary load-route" data-ids="{{ item.customer_ids }}">Load</button>
                    <form method="post" action="{{ url_for('delete_route_selection', selection_id=item.id) }}" onsubmit="return confirm('Delete this saved route selection?');">
                        <button type="submit" class="compact-danger">Delete</button>
                    </form>
                </div>
            </div>
            {% endfor %}
            {% endif %}
        </div>
    </div>

    <div class="panel">
        <div class="heading-row"><div><h2>Customer Route Settings</h2><p>Set the normal on-site time for each customer. The optimiser uses this saved duration every time that customer is planned.</p></div></div>
        <form method="post" class="plan-form-grid" style="max-width:900px;">
            <input type="hidden" name="action" value="save_customer_route_settings">
            <label>Customer<select name="customer_id" id="settings-customer" required><option value="">Choose customer</option>{% for customer in map_customers %}<option value="{{ customer.id }}" data-duration="{{ customer.ideal_call_minutes or 45 }}">{{ customer.name }}{% if customer.city %} — {{ customer.city }}{% endif %}</option>{% endfor %}</select></label>
            <label>Standard call duration (minutes)<input type="number" name="ideal_call_minutes" id="settings-duration" min="10" max="240" step="5" value="45" required></label>
            <div class="button-row" style="text-align:left;align-self:end;"><button type="submit">Save Customer Duration</button></div>
        </form>
    </div>

    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
    <script>
    (() => {
        const customers = {{ map_customers|tojson }};
        const customerById = new Map(customers.map(c => [Number(c.id), c]));
        const map = L.map('route-map', { preferCanvas: true }).setView([-38.2, 175.7], 7);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
        const drawnItems = new L.FeatureGroup().addTo(map);
        const routeLayers = new L.FeatureGroup().addTo(map);
        map.addControl(new L.Control.Draw({ draw:{polyline:false,rectangle:true,circle:false,circlemarker:false,marker:false,polygon:{allowIntersection:false,showArea:true}}, edit:{featureGroup:drawnItems,remove:true} }));
        const selected = new Set();
        let routeOrder = [];
        const markerById = new Map();
        const visibleIds = new Set();
        const search = document.getElementById('route-search');
        const group = document.getElementById('route-group');
        const urgency = document.getElementById('route-urgency');
        const list = document.getElementById('selected-list');
        const idsInput = document.getElementById('selected-ids');
        const statusBox = document.getElementById('route-status');
        const resultBox = document.getElementById('route-result');
        const modeBanner = document.getElementById('map-mode-banner');
        const dayColours = ['#246fe5','#7f56d9','#f79009','#12b76a','#d92d20'];
        let mapPickMode = '';
        let startPoint = {lat:-37.7870,lng:175.2793,label:'Hamilton'};
        let endPoint = {...startPoint};
        let startMarker = null;
        let endMarker = null;
        let currentDays = [];
        let currentUnassigned = [];

        function esc(value){ return String(value ?? '').replace(/[&<>'"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch])); }
        function markerColour(c){ if(selected.has(Number(c.id))) return '#246fe5'; if(c.urgency==='overdue') return '#d92d20'; if(c.urgency==='due') return '#f79009'; if(c.urgency==='not due') return '#12b76a'; return '#667085'; }
        function icon(c){ return L.divIcon({className:'',html:`<div style="width:16px;height:16px;border-radius:50%;background:${markerColour(c)};border:2px solid white;box-shadow:0 0 0 1px #475467;"></div>`,iconSize:[16,16],iconAnchor:[8,8]}); }
        function matches(c){ const q=search.value.trim().toLowerCase(); return (!q || `${c.name} ${c.city} ${c.street}`.toLowerCase().includes(q)) && (!group.value || c.customer_group===group.value) && (!urgency.value || c.urgency===urgency.value); }
        function popup(c){ const address=[c.street,c.city,c.zip].filter(Boolean).join(', '); return `<strong>${esc(c.name)}</strong><br>${esc(c.customer_group)}<br>${esc(address)}<br><span style="text-transform:capitalize">${esc(c.urgency||'No status')}</span>${c.last_call?` · Last call ${esc(c.last_call)}`:''}${c.call_note?`<br><em>${esc(c.call_note)}</em>`:''}`; }
        function renderMarkers(){ visibleIds.clear(); customers.forEach(c=>{ const m=markerById.get(Number(c.id)); if(matches(c)){ if(!map.hasLayer(m)) m.addTo(map); visibleIds.add(Number(c.id)); } else if(map.hasLayer(m)) map.removeLayer(m); m.setIcon(icon(c)); }); }
        function chosenInDisplayOrder(){ const ids = routeOrder.length ? routeOrder.filter(id=>selected.has(id)) : [...selected]; const missing=[...selected].filter(id=>!ids.includes(id)); return [...ids,...missing].map(id=>customerById.get(id)).filter(Boolean); }
        function updateSelection(){ const chosen=chosenInDisplayOrder(); routeOrder=chosen.map(c=>Number(c.id)); idsInput.value=routeOrder.join(','); document.getElementById('selected-count').textContent=chosen.length; document.getElementById('selected-overdue').textContent=chosen.filter(c=>c.urgency==='overdue').length; document.getElementById('selected-groups').textContent=new Set(chosen.map(c=>c.customer_group)).size; if(!chosen.length){list.innerHTML='<div class="empty">Draw around customers or click pins to select them.</div>';} else { list.innerHTML=chosen.map((c,i)=>`<div class="selected-customer"><div><strong>${i+1}. ${esc(c.name)}</strong><br><span class="small">${esc(c.city||'')} · ${esc(c.customer_group)} · ${esc(c.urgency||'no status')} · ${visitDuration(c)} min standard call</span><div class="route-reorder"><button type="button" data-up="${c.id}" ${i===0?'disabled':''}>↑</button><button type="button" data-down="${c.id}" ${i===chosen.length-1?'disabled':''}>↓</button></div></div><button type="button" data-remove="${c.id}">Remove</button></div>`).join(''); } document.querySelectorAll('.lock-customer').forEach(sel=>{const v=sel.value;sel.innerHTML=lockCustomerOptions();sel.value=v;}); renderMarkers(); }
        function toggle(id){ id=Number(id); selected.has(id)?selected.delete(id):selected.add(id); routeOrder=routeOrder.filter(x=>x!==id); if(selected.has(id)) routeOrder.push(id); clearRouteDisplay(); updateSelection(); }
        customers.forEach(c=>{ const id=Number(c.id); const m=L.marker([c.latitude,c.longitude],{icon:icon(c),title:c.name}); m.bindPopup(popup(c)); m.on('click',()=>toggle(id)); markerById.set(id,m); });
        renderMarkers();

        function setStatus(message,isError=false){ statusBox.textContent=message; statusBox.classList.toggle('error-state',isError); }
        function formatMinutes(total){ total=Math.max(0,Math.round(total)); const h=Math.floor(total/60),m=total%60; return h?`${h}h ${m}m`:`${m}m`; }
        function formatDistance(km){ return km>=100?`${Math.round(km)} km`:`${km.toFixed(1)} km`; }
        function timeToMinutes(value){ const [h,m]=value.split(':').map(Number); return h*60+m; }
        function clock(minutes){ minutes=Math.round(minutes); const h=Math.floor(minutes/60)%24,m=minutes%60; return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`; }
        function visitDuration(c){ const fallback=Math.max(10,Number(document.getElementById('default-visit-minutes').value)||45); return Math.max(10,Number(c.ideal_call_minutes)||fallback); }
        function customerWindow(c,defaultOpen,defaultClose){
            const open=c.customer_open_time?timeToMinutes(c.customer_open_time):defaultOpen;
            const close=c.customer_close_time?timeToMinutes(c.customer_close_time):defaultClose;
            return {open,close};
        }
        function scheduleFromLegs(stops,legs,config,locksById){
            const lunchWindowStart=11*60, lunchLatestStart=12*60+30, lunchDuration=30;
            let cursor=config.repStart,totalVisit=0,totalWait=0,lunchTaken=false,lunchAt=null; const timings=[];
            for(let i=0;i<stops.length;i++){
                const drive=legs?.[i]?.duration?legs[i].duration/60:0; cursor+=drive;
                const c=stops[i], win=customerWindow(c,config.customerOpen,config.customerClose), lock=locksById.get(Number(c.id));
                let arrival=Math.max(cursor,win.open), wait=Math.max(0,win.open-cursor), locked=false;
                const duration=visitDuration(c);
                if(lock){
                    const target=lock.timeMinutes;
                    if(lock.kind==='locked' && cursor>target+0.5)return {fits:false,timings,finish:cursor,totalVisit,totalWait,reason:`Cannot reach ${c.name} by ${clock(target)}`};
                    if(lock.kind==='locked'){arrival=target;locked=true;wait=Math.max(0,target-cursor);}
                    else if(cursor<=target) {arrival=Math.max(arrival,target);wait=Math.max(0,arrival-cursor);}
                }
                if(!lunchTaken){
                    const naturalLunchStart=Math.max(cursor,lunchWindowStart);
                    if(naturalLunchStart<=lunchLatestStart && naturalLunchStart+lunchDuration<=arrival){
                        lunchTaken=true;lunchAt=naturalLunchStart;
                    }else if(arrival>lunchLatestStart || arrival+duration>13*60){
                        const forcedLunchStart=Math.max(cursor,lunchWindowStart);
                        if(forcedLunchStart<=lunchLatestStart){
                            lunchTaken=true;lunchAt=forcedLunchStart;
                            arrival=Math.max(arrival,forcedLunchStart+lunchDuration);
                            wait=Math.max(0,arrival-cursor-lunchDuration);
                        }
                    }
                }
                const depart=arrival+duration;
                if(arrival<win.open || depart>win.close)return {fits:false,timings,finish:depart,totalVisit,totalWait,reason:`${c.name} falls outside its ${clock(win.open)}–${clock(win.close)} customer hours`};
                if(depart>config.repEnd)return {fits:false,timings,finish:depart,totalVisit,totalWait,reason:`Rep hours exceeded at ${c.name}`};
                timings.push({arrival,depart,duration,drive,wait,locked,kind:lock?.kind||''}); cursor=depart; totalVisit+=duration; totalWait+=wait;
            }
            if(!lunchTaken && cursor>=lunchWindowStart){
                const finalLunchStart=Math.max(lunchWindowStart,Math.min(cursor,lunchLatestStart));
                if(finalLunchStart<=lunchLatestStart){cursor=Math.max(cursor,finalLunchStart)+lunchDuration;lunchTaken=true;lunchAt=finalLunchStart;}
            }
            const returnDrive=config.skipReturn?0:(legs?.[stops.length]?.duration?legs[stops.length].duration/60:0); cursor+=returnDrive;
            return {fits:cursor<=config.repEnd,timings,finish:cursor,totalVisit,totalWait,returnDrive,lunchAt,reason:cursor<=config.repEnd?'':'Return drive exceeds rep finish time'};
        }
        function haversine(a,b){ const R=6371,rad=x=>x*Math.PI/180; const dLat=rad(b.lat-a.lat),dLon=rad(b.lng-a.lng); const q=Math.sin(dLat/2)**2+Math.cos(rad(a.lat))*Math.cos(rad(b.lat))*Math.sin(dLon/2)**2; return 2*R*Math.asin(Math.sqrt(q)); }
        function nearestNeighbour(items,start,priorityFirst){ const remaining=[...items],ordered=[]; let current=start; while(remaining.length){ remaining.sort((a,b)=>{ if(priorityFirst){const rank=x=>x.urgency==='overdue'?0:x.urgency==='due'?1:2; const d=rank(a)-rank(b); if(d) return d;} return haversine(current,{lat:a.latitude,lng:a.longitude})-haversine(current,{lat:b.latitude,lng:b.longitude}); }); const next=remaining.shift(); ordered.push(next); current={lat:next.latitude,lng:next.longitude}; } return ordered; }
        function clearRouteDisplay(){ routeLayers.clearLayers(); resultBox.innerHTML=''; currentDays=[]; currentUnassigned=[]; }
        function setEndpoint(kind,latlng,label){ const point={lat:latlng.lat,lng:latlng.lng,label:label||`${latlng.lat.toFixed(5)}, ${latlng.lng.toFixed(5)}`}; if(kind==='start'){startPoint=point;document.getElementById('start-label').value=point.label;if(startMarker)map.removeLayer(startMarker);startMarker=L.marker(latlng,{title:'Route start'}).addTo(map).bindPopup('<strong>Route start</strong>'); if(document.getElementById('same-end').checked){endPoint={...point};document.getElementById('end-label').value=point.label;updateEndMarker();}} else {endPoint=point;document.getElementById('end-label').value=point.label;updateEndMarker();} }
        function updateEndMarker(){ if(endMarker)map.removeLayer(endMarker); if(!document.getElementById('same-end').checked){endMarker=L.marker([endPoint.lat,endPoint.lng],{title:'Route finish'}).addTo(map).bindPopup('<strong>Route finish</strong>');} }
        setEndpoint('start',L.latLng(startPoint.lat,startPoint.lng),'Hamilton');

        document.getElementById('set-start').addEventListener('click',()=>{mapPickMode='start';modeBanner.textContent='Click the map to set the route start.';modeBanner.classList.add('active');});
        document.getElementById('set-end').addEventListener('click',()=>{document.getElementById('same-end').checked=false;mapPickMode='end';modeBanner.textContent='Click the map to set the route finish.';modeBanner.classList.add('active');});
        document.getElementById('same-end').addEventListener('change',e=>{if(e.target.checked){endPoint={...startPoint};document.getElementById('end-label').value=document.getElementById('start-label').value;}updateEndMarker();});
        map.on('click',e=>{if(!mapPickMode)return;setEndpoint(mapPickMode,e.latlng);mapPickMode='';modeBanner.classList.remove('active');});

        async function osrmTrip(chosen){
            const coords=[startPoint,...chosen.map(c=>({lat:c.latitude,lng:c.longitude})),endPoint];
            const coordText=coords.map(p=>`${p.lng},${p.lat}`).join(';');
            const url=`https://router.project-osrm.org/trip/v1/driving/${coordText}?source=first&destination=last&roundtrip=false&overview=false&steps=false`;
            const response=await fetch(url); if(!response.ok)throw new Error(`Routing service returned ${response.status}`); const data=await response.json(); if(data.code!=='Ok'||!data.trips?.length)throw new Error(data.message||'No route found');
            const sequence=data.waypoints.map((w,inputIndex)=>({inputIndex,waypointIndex:w.waypoint_index})).sort((a,b)=>a.waypointIndex-b.waypointIndex);
            const ordered=sequence.filter(x=>x.inputIndex>0&&x.inputIndex<coords.length-1).map(x=>chosen[x.inputIndex-1]);
            return {ordered, legs:data.trips[0].legs||[]};
        }
        function urgencyRank(c){ return c.urgency==='overdue'?0:c.urgency==='due'?1:2; }
        function dayConfig(dayIndex){
            const row=document.querySelector(`[data-day-config="${dayIndex}"]`);
            const defaultStart=timeToMinutes(document.getElementById('rep-start').value), defaultEnd=timeToMinutes(document.getElementById('rep-end').value);
            return {repStart:timeToMinutes(row?.querySelector('.day-rep-start').value||document.getElementById('rep-start').value),repEnd:timeToMinutes(row?.querySelector('.day-rep-end').value||document.getElementById('rep-end').value),customerOpen:timeToMinutes(document.getElementById('customer-open').value),customerClose:timeToMinutes(document.getElementById('customer-close').value),skipReturn:document.getElementById('overnight-mode').checked && dayIndex<Number(document.getElementById('route-days').value)-1,defaultStart,defaultEnd};
        }
        function readLocks(){
            const locks=[];
            document.querySelectorAll('.locked-row').forEach(row=>{const customerId=Number(row.querySelector('.lock-customer').value),dayIndex=Number(row.querySelector('.lock-day').value)-1,time=row.querySelector('.lock-time').value,kind=row.querySelector('.lock-kind').value;if(customerId&&time)locks.push({customerId,dayIndex,time,timeMinutes:timeToMinutes(time),kind});});
            return locks;
        }
        function estimatedLegs(stops,start,finish){const points=[start,...stops.map(c=>({lat:c.latitude,lng:c.longitude})),finish];return points.slice(1).map((p,i)=>{const km=haversine(points[i],p)*1.28;return {distance:km*1000,duration:(km/67)*3600};});}
        function estimatedSchedule(stops,config,locksById,start,finish){return scheduleFromLegs(stops,estimatedLegs(stops,start,finish),config,locksById);}
        function bestInsertion(remaining,dayStops,config,locksById,start,finish,priorityFirst){
            let best=null;
            const candidates=[...remaining].sort((a,b)=>priorityFirst?urgencyRank(a)-urgencyRank(b):0);
            for(const candidate of candidates){
                for(let pos=0;pos<=dayStops.length;pos++){
                    const proposed=[...dayStops.slice(0,pos),candidate,...dayStops.slice(pos)];
                    const sch=estimatedSchedule(proposed,config,locksById,start,finish);
                    if(sch.fits && (!best || sch.finish<best.finish))best={candidate,pos,finish:sch.finish};
                }
            }
            return best;
        }
        async function buildPracticalDays(chosen,maxDays,priorityFirst){
            const allLocks=readLocks(), lockCustomerIds=new Set(allLocks.map(x=>x.customerId));
            const invalidLocks=allLocks.filter(x=>x.dayIndex<0||x.dayIndex>=maxDays||!chosen.some(c=>Number(c.id)===x.customerId));
            if(invalidLocks.length)throw new Error('A locked appointment refers to an unselected customer or a day outside this trip.');
            const remaining=chosen.filter(c=>!lockCustomerIds.has(Number(c.id))),built=[];
            let dayStart={...startPoint};
            for(let dayIndex=0;dayIndex<maxDays;dayIndex++){
                const config=dayConfig(dayIndex), dayLocks=allLocks.filter(x=>x.dayIndex===dayIndex), locksById=new Map(dayLocks.map(x=>[x.customerId,x]));
                let dayStops=dayLocks.sort((a,b)=>a.timeMinutes-b.timeMinutes).map(x=>chosen.find(c=>Number(c.id)===x.customerId)).filter(Boolean);
                const finish=config.skipReturn?(dayStops.length?{lat:dayStops[dayStops.length-1].latitude,lng:dayStops[dayStops.length-1].longitude}:dayStart):(document.getElementById('same-end').checked?startPoint:endPoint);
                while(remaining.length){const best=bestInsertion(remaining,dayStops,config,locksById,dayStart,finish,priorityFirst);if(!best)break;dayStops.splice(best.pos,0,best.candidate);remaining.splice(remaining.indexOf(best.candidate),1);}
                if(!dayStops.length)continue;
                let route=await routeDay(dayStops,dayStart,finish), schedule=scheduleFromLegs(dayStops,route.legs,config,locksById);
                while(dayStops.length&&!schedule.fits){
                    let removeIndex=-1;for(let i=dayStops.length-1;i>=0;i--){if(!locksById.has(Number(dayStops[i].id))){removeIndex=i;break;}}
                    if(removeIndex<0)throw new Error(schedule.reason||`Locked appointments on Day ${dayIndex+1} cannot fit.`);
                    const [removed]=dayStops.splice(removeIndex,1);remaining.unshift(removed);route=await routeDay(dayStops,dayStart,finish);schedule=scheduleFromLegs(dayStops,route.legs,config,locksById);
                }
                if(dayStops.length){built.push({stops:dayStops,...route,schedule,config,locksById,startPoint:{...dayStart},finishPoint:{...finish}});if(config.skipReturn){const last=dayStops[dayStops.length-1];dayStart={lat:last.latitude,lng:last.longitude,label:last.name};}}
            }
            return {days:built,unassigned:remaining};
        }
        async function routeDay(stops,start=startPoint,finish=(document.getElementById('same-end').checked?startPoint:endPoint)){ const points=[start,...stops.map(c=>({lat:c.latitude,lng:c.longitude})),finish]; const coordText=points.map(p=>`${p.lng},${p.lat}`).join(';'); try{ const response=await fetch(`https://router.project-osrm.org/route/v1/driving/${coordText}?overview=full&geometries=geojson&steps=false`); if(!response.ok)throw new Error(); const data=await response.json(); if(data.code!=='Ok'||!data.routes?.length)throw new Error(); const r=data.routes[0]; return {distanceKm:r.distance/1000,driveMinutes:r.duration/60,geometry:r.geometry,legs:r.legs||[],source:'road'}; }catch(error){ let km=0;const legs=[];for(let i=1;i<points.length;i++){const legKm=haversine(points[i-1],points[i])*1.28;km+=legKm;legs.push({distance:legKm*1000,duration:(legKm/67)*3600});}return {distanceKm:km,driveMinutes:legs.reduce((n,l)=>n+l.duration/60,0),geometry:{coordinates:points.map(p=>[p.lng,p.lat])},legs,source:'estimate'};} }
        function googleMapsUrl(stops){ const destination=document.getElementById('same-end').checked?startPoint:endPoint; const waypoints=stops.map(c=>`${c.latitude},${c.longitude}`).join('|'); return `https://www.google.com/maps/dir/?api=1&origin=${startPoint.lat},${startPoint.lng}&destination=${destination.lat},${destination.lng}&travelmode=driving&waypoints=${encodeURIComponent(waypoints)}`; }
        async function buildRoute(){
            clearRouteDisplay(); const chosen=[...selected].map(id=>customerById.get(id)).filter(Boolean); if(!chosen.length){setStatus('Select at least one customer first.',true);return;} if(chosen.length>40){setStatus('Please optimise no more than 40 calls at once. Split a very large territory into separate trips.',true);return;}
            const repStart=timeToMinutes(document.getElementById('rep-start').value),repEnd=timeToMinutes(document.getElementById('rep-end').value),customerOpen=timeToMinutes(document.getElementById('customer-open').value),customerClose=timeToMinutes(document.getElementById('customer-close').value);
            if(repEnd<=repStart){setStatus('The default rep finish time must be later than the start time.',true);return;} if(customerClose<=customerOpen){setStatus('Default customer closing time must be later than opening time.',true);return;}
            const days=Math.max(1,Number(document.getElementById('route-days').value)||1); const priorityFirst=document.getElementById('route-preference').value==='priority';
            setStatus('Optimising road order, customer call lengths, trading hours and rep driving hours…'); document.getElementById('optimise-route').disabled=true;
            let seedOrder,fallback=false;
            try{ const trip=await osrmTrip(chosen); seedOrder=trip.ordered; }catch(error){seedOrder=nearestNeighbour(chosen,startPoint,priorityFirst);fallback=true;}
            if(priorityFirst)seedOrder=nearestNeighbour(seedOrder,startPoint,true);
            try{
                const practical=await buildPracticalDays(seedOrder,days,priorityFirst);
                currentDays=practical.days; currentUnassigned=practical.unassigned; routeOrder=[...currentDays.flatMap(d=>d.stops),...currentUnassigned].map(c=>Number(c.id));updateSelection();
                renderRouteResults(repStart,repEnd,customerOpen,customerClose,fallback);
            }catch(error){setStatus(error.message||'Could not build the route.',true);}
            document.getElementById('optimise-route').disabled=false;
        }
        function renderRouteResults(repStart,repEnd,customerOpen,customerClose,fallback){
            routeLayers.clearLayers(); let totalKm=0,totalDrive=0,totalVisit=0; const bounds=[[startPoint.lat,startPoint.lng]];
            currentDays.forEach((day,index)=>{totalKm+=day.distanceKm;totalDrive+=day.driveMinutes;totalVisit+=day.schedule.totalVisit;const coords=day.geometry.coordinates.map(p=>[p[1],p[0]]);const line=L.polyline(coords,{color:dayColours[index%dayColours.length],weight:5,opacity:.82,className:'route-line'}).addTo(routeLayers);bounds.push(...coords);line.bindTooltip(`Day ${index+1}: ${formatDistance(day.distanceKm)}`);});
            if(bounds.length>1)map.fitBounds(bounds,{padding:[30,30]});
            let html=`<div class="route-result-summary"><div class="route-mini"><span class="small">Planned days</span><strong>${currentDays.length}</strong></div><div class="route-mini"><span class="small">Calls</span><strong>${currentDays.reduce((n,d)=>n+d.stops.length,0)}</strong></div><div class="route-mini"><span class="small">Distance</span><strong>${formatDistance(totalKm)}</strong></div><div class="route-mini"><span class="small">Drive / visits</span><strong>${formatMinutes(totalDrive)} / ${formatMinutes(totalVisit)}</strong></div></div>`;
            currentDays.forEach((day,dayIndex)=>{html+=`<div class="route-day"><div class="route-day-header"><div><strong>Day ${dayIndex+1}</strong><br><span class="small">${day.stops.length} calls · ${formatDistance(day.distanceKm)} · ${formatMinutes(day.driveMinutes)} driving · ${formatMinutes(day.schedule.totalVisit)} in calls · rep ${clock(day.config.repStart)}–${clock(day.config.repEnd)} · lunch ${day.schedule.lunchAt!==null?clock(day.schedule.lunchAt):'not required'} · finish about ${clock(day.schedule.finish)}</span></div><a href="${googleMapsUrl(day.stops)}" target="_blank" rel="noopener">Open in Google Maps</a></div>`;day.stops.forEach((c,i)=>{const t=day.schedule.timings[i];html+=`<div class="route-stop"><div class="route-stop-number">${i+1}</div><div><strong>${esc(c.name)}</strong><br><span class="small">${esc(c.city||'')} · ${esc(c.customer_group)} · ${esc(c.urgency||'no status')}</span><div class="route-context">${c.priority_classification?`<span>${esc(c.priority_classification)}</span>`:''}${Number(c.open_action_count||0)?`<span>${c.open_action_count} open action${Number(c.open_action_count)===1?'':'s'}</span>`:''}${c.call_note?`<span>Call note available</span>`:''}</div></div><div class="route-stop-time">${clock(t.arrival)}–${clock(t.depart)}<br>${t.duration} min visit${t.locked?' · LOCKED':t.kind==='preferred'?' · preferred':''}${t.wait?` · ${Math.round(t.wait)} min wait`:''}</div></div>`;});html+='</div>';});
            if(currentUnassigned.length){html+=`<div class="route-day" style="border-color:#f79009;"><div class="route-day-header" style="background:#b54708;"><div><strong>Unscheduled calls</strong><br><span class="small">${currentUnassigned.length} calls cannot fit inside ${currentDays.length} day${currentDays.length===1?'':'s'} while respecting rep hours, customer hours, driving and saved call durations.</span></div></div>${currentUnassigned.map((c,i)=>`<div class="route-stop"><div class="route-stop-number">${i+1}</div><div><strong>${esc(c.name)}</strong><br><span class="small">${esc(c.city||'')} · ${visitDuration(c)} min standard call</span></div><div class="route-stop-time">Not scheduled</div></div>`).join('')}</div>`;}
            resultBox.innerHTML=html;
            const workflowForm=document.getElementById('route-workflow-form');
            if(currentDays.length){
                const payload={start_point:startPoint,end_point:document.getElementById('same-end').checked?startPoint:endPoint,rep_start:clock(repStart),rep_end:clock(repEnd),customer_open:clock(customerOpen),customer_close:clock(customerClose),overnight_mode:document.getElementById('overnight-mode').checked,locked_appointments:readLocks(),days:currentDays.map((day,dayIndex)=>({day_index:dayIndex,rep_start:clock(day.config.repStart),rep_end:clock(day.config.repEnd),start_point:day.startPoint,finish_point:day.finishPoint,stops:day.stops.map((c,i)=>({customer_id:Number(c.id),name:c.name,city:c.city||'',customer_group:c.customer_group||'',arrival:clock(day.schedule.timings[i].arrival),departure:clock(day.schedule.timings[i].depart),duration:Number(day.schedule.timings[i].duration),locked:Boolean(day.schedule.timings[i].locked),appointment_kind:day.schedule.timings[i].kind||'',call_note:c.call_note||''}))}))};
                document.getElementById('route-payload').value=JSON.stringify(payload);
                document.getElementById('route-destination').value=[...new Set(currentDays.flatMap(d=>d.stops.map(c=>c.city).filter(Boolean)))].slice(0,5).join(', ');
                workflowForm.style.display='block';
            } else workflowForm.style.display='none';
            const estimated=currentDays.some(d=>d.source==='estimate')||fallback;if(currentUnassigned.length)setStatus(`${currentDays.reduce((n,d)=>n+d.stops.length,0)} calls fit; ${currentUnassigned.length} remain unscheduled.`,true);else setStatus(estimated?'Route fits the time rules. Some road times are estimates because the free routing service was unavailable.':'Route fits rep hours, customer trading hours and each customer’s saved call duration.');
        }

        const workflowStart=document.getElementById('workflow-start-date');
        const workflowEnd=document.getElementById('workflow-end-date');
        const todayIso=new Date().toISOString().slice(0,10); workflowStart.value=todayIso;
        function updateWorkflowEnd(){const d=new Date(`${workflowStart.value}T12:00:00`);d.setDate(d.getDate()+Math.max(0,currentDays.length-1));workflowEnd.value=d.toISOString().slice(0,10);}
        workflowStart.addEventListener('change',updateWorkflowEnd);
        document.getElementById('route-workflow-form').addEventListener('submit',e=>{if(!currentDays.length||!document.getElementById('route-payload').value){e.preventDefault();alert('Build the route before sending it to Plan Trip.');return;}updateWorkflowEnd();});

        function renderDailyHours(){
            const count=Math.max(1,Number(document.getElementById('route-days').value)||1),grid=document.getElementById('daily-hours-grid');
            const old={};grid.querySelectorAll('[data-day-config]').forEach(r=>old[r.dataset.dayConfig]={start:r.querySelector('.day-rep-start').value,end:r.querySelector('.day-rep-end').value});
            grid.innerHTML=Array.from({length:count},(_,i)=>`<div data-day-config="${i}" class="daily-hours-card"><strong>Day ${i+1}</strong><label>Start<input class="day-rep-start" type="time" value="${old[i]?.start||document.getElementById('rep-start').value}"></label><label>Finish<input class="day-rep-end" type="time" value="${old[i]?.end||document.getElementById('rep-end').value}"></label></div>`).join('');
            refreshLockDayOptions();
        }
        function lockCustomerOptions(){return `<option value="">Choose selected customer</option>${chosenInDisplayOrder().map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('')}`;}
        function refreshLockDayOptions(){const count=Math.max(1,Number(document.getElementById('route-days').value)||1);document.querySelectorAll('.lock-day').forEach(sel=>{const v=sel.value;sel.innerHTML=Array.from({length:count},(_,i)=>`<option value="${i+1}">Day ${i+1}</option>`).join('');sel.value=Math.min(Number(v)||1,count);});}
        function addLockedRow(values={}){const wrap=document.getElementById('locked-appointments'),row=document.createElement('div');row.className='locked-row';row.style.cssText='display:grid;grid-template-columns:2fr 100px 130px 130px auto;gap:8px;align-items:end;margin-bottom:8px;';row.innerHTML=`<label>Customer<select class="lock-customer">${lockCustomerOptions()}</select></label><label>Day<select class="lock-day"></select></label><label>Time<input class="lock-time" type="time" value="${values.time||'10:00'}"></label><label>Type<select class="lock-kind"><option value="locked">Confirmed / locked</option><option value="preferred">Preferred</option></select></label><button type="button" class="danger remove-lock">Remove</button>`;wrap.appendChild(row);refreshLockDayOptions();row.querySelector('.lock-customer').value=values.customerId||'';row.querySelector('.lock-day').value=values.day||1;row.querySelector('.lock-kind').value=values.kind||'locked';row.querySelector('.remove-lock').addEventListener('click',()=>row.remove());}
        document.getElementById('add-lock').addEventListener('click',()=>addLockedRow());
        document.getElementById('route-days').addEventListener('change',renderDailyHours);
        document.getElementById('rep-start').addEventListener('change',renderDailyHours);
        document.getElementById('rep-end').addEventListener('change',renderDailyHours);
        renderDailyHours();

        const settingsCustomer=document.getElementById('settings-customer');
        const settingsDuration=document.getElementById('settings-duration');
        settingsCustomer.addEventListener('change',()=>{const option=settingsCustomer.options[settingsCustomer.selectedIndex];settingsDuration.value=option?.dataset.duration||45;});

        requestAnimationFrame(() => { map.invalidateSize(true); const allBounds=L.latLngBounds(customers.map(c=>[c.latitude,c.longitude])); if(allBounds.isValid()) map.fitBounds(allBounds.pad(.05)); });
        window.addEventListener('resize', () => map.invalidateSize(false));
        [search,group,urgency].forEach(el=>el.addEventListener(el.tagName==='INPUT'?'input':'change',renderMarkers));
        document.getElementById('fit-visible').addEventListener('click',()=>{const pts=customers.filter(c=>visibleIds.has(Number(c.id))).map(c=>[c.latitude,c.longitude]);if(pts.length)map.fitBounds(pts,{padding:[20,20]});});
        document.getElementById('select-visible').addEventListener('click',()=>{visibleIds.forEach(id=>{selected.add(id);if(!routeOrder.includes(id))routeOrder.push(id);});clearRouteDisplay();updateSelection();});
        document.getElementById('clear-selection').addEventListener('click',()=>{selected.clear();routeOrder=[];drawnItems.clearLayers();clearRouteDisplay();updateSelection();});
        document.getElementById('clear-route').addEventListener('click',()=>{clearRouteDisplay();setStatus('Route lines cleared. Your customer selection is unchanged.');});
        document.getElementById('optimise-route').addEventListener('click',buildRoute);
        list.addEventListener('click',e=>{const remove=Number(e.target.dataset.remove),up=Number(e.target.dataset.up),down=Number(e.target.dataset.down);if(remove){selected.delete(remove);routeOrder=routeOrder.filter(x=>x!==remove);clearRouteDisplay();updateSelection();return;}const id=up||down;if(id){const i=routeOrder.indexOf(id),j=up?i-1:i+1;if(i>=0&&j>=0&&j<routeOrder.length){[routeOrder[i],routeOrder[j]]=[routeOrder[j],routeOrder[i]];clearRouteDisplay();updateSelection();setStatus('Manual order updated. Click Optimise & Build Days to recalculate travel times.');}}});
        document.querySelectorAll('.load-route').forEach(btn=>btn.addEventListener('click',()=>{selected.clear();routeOrder=[];btn.dataset.ids.split(',').filter(Boolean).forEach(x=>{const id=Number(x);selected.add(id);routeOrder.push(id);});clearRouteDisplay();updateSelection();const pts=routeOrder.map(id=>customerById.get(id)).filter(Boolean).map(c=>[c.latitude,c.longitude]);if(pts.length)map.fitBounds(pts,{padding:[30,30]});}));
        map.on(L.Draw.Event.CREATED,e=>{drawnItems.clearLayers();drawnItems.addLayer(e.layer);const bounds=e.layer.getBounds();customers.forEach(c=>{const id=Number(c.id);if(matches(c)&&bounds.contains([c.latitude,c.longitude])){if(e.layer instanceof L.Polygon){const p=L.latLng(c.latitude,c.longitude),vs=e.layer.getLatLngs()[0];let inside=false;for(let i=0,j=vs.length-1;i<vs.length;j=i++){const xi=vs[i].lng,yi=vs[i].lat,xj=vs[j].lng,yj=vs[j].lat;const hit=((yi>p.lat)!=(yj>p.lat))&&(p.lng<(xj-xi)*(p.lat-yi)/(yj-yi)+xi);if(hit)inside=!inside;}if(inside){selected.add(id);if(!routeOrder.includes(id))routeOrder.push(id);}}else{selected.add(id);if(!routeOrder.includes(id))routeOrder.push(id);}}});clearRouteDisplay();updateSelection();});
        document.getElementById('save-route-form').addEventListener('submit',e=>{if(!selected.size){e.preventDefault();alert('Select at least one customer before saving.');}});
    })();
    </script>
    {% endif %}

    {% if view == 'travel_expenses' %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>Travel &amp; Expense Assistant</h2>
                <p>Track upcoming trips, booking progress, forecast costs and actual spend.</p>
            </div>
            {% if editing_trip %}<a class="button-link secondary" href="{{ url_for('travel_expenses', year=selected_year, quarter=selected_quarter) }}">Cancel Editing</a>{% endif %}
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}

        <form method="post" action="{{ url_for('travel_expenses') }}">
            <input type="hidden" name="trip_id" value="{{ trip_form.id }}">
            <div class="travel-form-grid">
                <label>Trip name<input type="text" name="trip_name" value="{{ trip_form.trip_name }}" placeholder="Northland territory trip" required></label>
                <label>Destination / territory<input type="text" name="destination" value="{{ trip_form.destination }}" placeholder="Kaitaia, Kerikeri, Whangarei"></label>
                <label>Start date<input type="date" name="start_date" value="{{ trip_form.start_date }}" required></label>
                <label>End date<input type="date" name="end_date" value="{{ trip_form.end_date }}" required></label>
                <label>Trip status<select name="status">{% for option in trip_statuses %}<option value="{{ option }}" {% if option == trip_form.status %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                <label>Flights<select name="flight_status">{% for option in booking_statuses %}<option value="{{ option }}" {% if option == trip_form.flight_status %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                <label>Rental car<select name="rental_status">{% for option in booking_statuses %}<option value="{{ option }}" {% if option == trip_form.rental_status %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                <label>Accommodation<select name="accommodation_status">{% for option in booking_statuses %}<option value="{{ option }}" {% if option == trip_form.accommodation_status %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
            </div>

            <div class="travel-cost-grid">
                {% for item in cost_items %}
                <div class="travel-cost-box">
                    <h4>{{ item.label }}</h4>
                    <label>Forecast ($)<input type="number" min="0" step="0.01" name="forecast_{{ item.key }}" value="{{ trip_form['forecast_' + item.key] }}"></label>
                    <label>Actual ($)<input type="number" min="0" step="0.01" name="actual_{{ item.key }}" value="{{ trip_form['actual_' + item.key] }}"></label>
                </div>
                {% endfor %}
            </div>

            <label class="travel-notes-label" style="margin-top:16px;">Notes<textarea name="notes" rows="3" placeholder="Booking references, preferred flights, special requirements...">{{ trip_form.notes }}</textarea></label>
            <div class="button-row"><button type="submit">{% if editing_trip %}Save Trip Changes{% else %}Add Trip{% endif %}</button></div>
        </form>
    </div>

    <div class="panel">
        <div class="heading-row">
            <div><h2>Travel Spend Overview</h2><p>Filter the dashboard by calendar year and quarter.</p></div>
            <form method="get" action="{{ url_for('travel_expenses') }}" style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
                <select name="year">{% for year in travel_years %}<option value="{{ year }}" {% if year == selected_year %}selected{% endif %}>{{ year }}</option>{% endfor %}</select>
                <select name="quarter"><option value="0" {% if selected_quarter == 0 %}selected{% endif %}>Full year</option>{% for q in [1,2,3,4] %}<option value="{{ q }}" {% if q == selected_quarter %}selected{% endif %}>Quarter {{ q }}</option>{% endfor %}</select>
                <button type="submit">Apply</button>
            </form>
        </div>
        <div class="summary-grid">
            <div class="summary-card"><div class="label">Trips</div><div class="value">{{ travel_summary.trip_count }}</div><div class="note">Matching the selected period</div></div>
            <div class="summary-card"><div class="label">Forecast Spend</div><div class="value">{{ travel_summary.forecast_display }}</div><div class="note">Expected total travel cost</div></div>
            <div class="summary-card"><div class="label">Actual Spend</div><div class="value">{{ travel_summary.actual_display }}</div><div class="note">Costs entered so far</div></div>
            <div class="summary-card {{ travel_summary.variance_class }}"><div class="label">Actual vs Forecast</div><div class="value">{{ travel_summary.variance_display }}</div><div class="note">Negative means under budget</div></div>
        </div>
    </div>

    <div class="panel">
        <h2>Trips</h2>
        {% if travel_rows %}
        <div class="table-wrap"><table class="history-table">
            <thead><tr><th>Trip</th><th>Dates</th><th>Status</th><th>Bookings</th><th>Forecast</th><th>Actual</th><th>Variance</th><th></th></tr></thead>
            <tbody>{% for row in travel_rows %}<tr>
                <td><strong>{{ row.trip_name }}</strong>{% if row.destination %}<br><span class="small">{{ row.destination }}</span>{% endif %}{% if row.notes %}<br><span class="small">{{ row.notes }}</span>{% endif %}</td>
                <td>{{ row.date_display }}</td>
                <td><span class="travel-status {{ row.status_class }}">{{ row.status }}</span></td>
                <td><span class="small">Flight: {{ row.flight_status }}<br>Car: {{ row.rental_status }}<br>Stay: {{ row.accommodation_status }}</span></td>
                <td>{{ row.forecast_display }}</td><td>{{ row.actual_display }}</td>
                <td class="{{ row.variance_text_class }}">{{ row.variance_display }}</td>
                <td class="action-cell"><a class="view-link" href="{{ url_for('travel_expenses', edit=row.id, year=selected_year, quarter=selected_quarter) }}">Edit</a><form method="post" action="{{ url_for('delete_travel_trip', trip_id=row.id) }}" onsubmit="return confirm('Delete this trip?');"><button type="submit" class="danger">Delete</button></form></td>
            </tr>{% endfor %}</tbody>
        </table></div>
        {% else %}<div class="empty">No trips have been recorded for the selected period.</div>{% endif %}
    </div>
    {% endif %}



    {% if view == 'on_the_road' %}
    <div class="panel">
        <div class="heading-row">
            <div><h2>On the Road{% if active_trip %} — {{ active_trip.trip_name }}{% endif %}</h2><p>Navigate, track live call times, capture notes and create follow-ups from your phone.</p></div>
            {% if active_trip %}<a class="button-link secondary" href="{{ url_for('trip_workspace', trip_id=active_trip.id, tab='schedule') }}">Back to Trip Workspace</a>{% else %}<a class="button-link secondary" href="{{ url_for('trip_prep') }}">Back to Plan Trip</a>{% endif %}
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <div class="road-dashboard">
            <div class="road-kpi"><div class="label">Calls shown</div><div class="value">{{ road_summary.total }}</div></div>
            <div class="road-kpi"><div class="label">Completed</div><div class="value">{{ road_summary.completed }}</div></div>
            <div class="road-kpi"><div class="label">In progress</div><div class="value">{{ road_summary.active }}</div></div>
            <div class="road-kpi"><div class="label">Remaining</div><div class="value">{{ road_summary.remaining }}</div></div>
        </div>
        {% if road_rows %}
            {% set ns = namespace(day='') %}
            {% for row in road_rows %}
                {% if row.date_display != ns.day %}<div class="road-day-heading">{{ row.day_label }}</div>{% set ns.day = row.date_display %}{% endif %}
                <div class="road-card {% if row.completed %}complete{% elif row.skipped %}skipped{% elif row.started_at %}active{% endif %}">
                    <div class="road-time">Planned {{ row.time }}{% if row.actual_time_text %} · {{ row.actual_time_text }}{% endif %}</div>
                    <div class="road-store">{{ row.store }}</div>
                    <div class="road-status-line">
                        <span class="road-status-chip {% if row.completed %}complete{% elif row.skipped %}skipped{% elif row.started_at %}active{% endif %}">{{ row.status_label }}</span>
                        <span class="mini-tag">{{ row.customer_type }}</span>
                        {% if row.open_action_count %}<span class="mini-tag">{{ row.open_action_count }} open action{% if row.open_action_count != 1 %}s{% endif %}</span>{% endif %}
                        {% if row.priority_classification %}<span class="mini-tag">{{ row.priority_classification }}</span>{% endif %}
                    </div>
                    {% if row.address %}<div class="small" style="margin-top:7px;">{{ row.address }}</div>{% endif %}
                    {% if row.desired_outcome %}<div class="briefing-note"><strong>Desired outcome:</strong> {{ row.desired_outcome }}</div>{% endif %}
                    {% if row.preparation_notes %}<div class="briefing-note"><strong>Preparation:</strong> {{ row.preparation_notes }}</div>{% endif %}
                    {% if row.call_note %}<div class="briefing-note"><strong>Call note:</strong> {{ row.call_note }}</div>{% endif %}
                    {% if row.open_action_titles %}<div class="briefing-note"><strong>Outstanding:</strong> {{ row.open_action_titles|join('; ') }}</div>{% endif %}

                    <div class="road-live-actions">
                        {% if row.navigation_url %}<a class="button-link" target="_blank" rel="noopener" href="{{ row.navigation_url }}">Navigate</a>{% endif %}
                        {% if not row.started_at and not row.completed %}<form method="post" action="{{ url_for('road_visit_event') }}"><input type="hidden" name="event" value="start">{{ row.hidden_fields|safe }}<button type="submit">Start Visit</button></form>{% endif %}
                        {% if not row.completed %}<form method="post" action="{{ url_for('road_visit_event') }}"><input type="hidden" name="event" value="skip">{{ row.hidden_fields|safe }}<button type="submit" class="secondary">{% if row.skipped %}Undo Skip{% else %}Skip Call{% endif %}</button></form>{% endif %}
                        {% if row.customer_exists %}<a class="button-link secondary" href="{{ url_for('customer_detail', store_name=row.store) }}">Customer Brief</a>{% endif %}
                    </div>

                    <form class="road-note-form" method="post" enctype="multipart/form-data" action="{{ url_for('update_road_visit') }}">
                        <input type="hidden" name="schedule_id" value="{{ row.schedule_id }}">
                        <input type="hidden" name="visit_date" value="{{ row.date_iso }}">
                        <input type="hidden" name="visit_time" value="{{ row.time }}">
                        <input type="hidden" name="store_name" value="{{ row.store }}">
                        {% if active_trip %}<input type="hidden" name="trip_id" value="{{ active_trip.id }}">{% endif %}
                        <label><strong>Visit notes</strong></label>
                        <textarea name="visit_notes" placeholder="Key discussion, order agreed, display issue, follow-up needed...">{{ row.visit_notes }}</textarea>
                        <div class="road-fields">
                            <label>Outcome<select name="outcome"><option value="">Select outcome</option>{% for option in ['Successful visit','Order / commitment','Follow-up required','No decision','Customer unavailable','Cancelled'] %}<option value="{{ option }}" {% if row.outcome == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                            <label>Next step<input type="text" name="next_step" value="{{ row.next_step }}" placeholder="What happens next?"></label>
                            <label>Create follow-up action<input type="text" name="followup_title" placeholder="Optional action title"></label>
                            <label>Follow-up due<input type="date" name="followup_due"></label>
                        </div>
                        <label style="display:block;margin-top:12px;"><strong>Add photo or file</strong><input style="display:block;margin-top:6px;" type="file" name="attachment" accept="image/*,.pdf,.doc,.docx,.xls,.xlsx"></label>
                        {% if row.attachments %}<div style="margin-top:12px;"><strong>Attachments</strong>{% for file in row.attachments %}<div class="road-attachment"><a href="{{ file.url }}" target="_blank">{{ file.original_name }}</a><span class="small">{{ file.uploaded_display }}</span></div>{% endfor %}</div>{% endif %}
                        <div class="road-note-toolbar">
                            <span class="small">{% if row.actual_duration %}Actual duration: {{ row.actual_duration }} minutes{% else %}Start and finish the visit to track actual duration.{% endif %}</span>
                            <span style="display:flex;gap:9px;flex-wrap:wrap;justify-content:flex-end;">
                                <button type="submit" name="action" value="save">Save Visit Details</button>
                                {% if row.started_at and not row.completed %}<button type="submit" name="action" value="finish">Save &amp; Finish Visit</button>{% endif %}
                            </span>
                        </div>
                    </form>
                </div>
            {% endfor %}
        {% else %}<div class="empty">No upcoming appointments were found. Send an optimised route to Plan Trip first.</div>{% endif %}
    </div>
    {% endif %}

    {% if view == 'control_centre' %}
    <div class="control-grid">
        <div class="control-card {% if control_counts.overdue %}alert{% else %}good{% endif %}">
            <div class="label">Overdue Actions</div><div class="value">{{ control_counts.overdue }}</div><div class="note">Incomplete actions past their due date</div>
        </div>
        <div class="control-card {% if control_counts.high %}warn{% else %}good{% endif %}">
            <div class="label">High Priority</div><div class="value">{{ control_counts.high }}</div><div class="note">Open or in-progress high-priority items</div>
        </div>
        <div class="control-card">
            <div class="label">Due in 7 Days</div><div class="value">{{ control_counts.due_soon }}</div><div class="note">Actions requiring attention this week</div>
        </div>
        <div class="control-card {% if control_counts.armoursteel %}warn{% else %}good{% endif %}">
            <div class="label">Armorsteel Follow-Ups</div><div class="value">{{ control_counts.armoursteel }}</div><div class="note">Open crossover opportunities</div>
        </div>
    </div>

    <div class="panel">
        <div class="heading-row"><div><h2>Friday Follow-Up</h2><p>Recent and urgent actions to clear after your trip.</p></div><a class="button-link secondary" href="{{ url_for('actions') }}">Open Full Action Tracker</a></div>
        {% if friday_actions %}
        <div class="table-wrap"><table class="history-table">
            <thead><tr><th>Priority</th><th>Store</th><th>Action</th><th>Due</th><th>Status</th><th></th></tr></thead>
            <tbody>{% for row in friday_actions %}<tr>
                <td><span class="severity {{ row.priority_class }}">{{ row.priority }}</span></td>
                <td><strong>{{ row.store_name }}</strong></td>
                <td>{{ row.title }}{% if row.detail %}<br><span class="small">{{ row.detail }}</span>{% endif %}</td>
                <td>{{ row.due_display }}</td>
                <td><form method="post" action="{{ url_for('update_action', action_id=row.id) }}"><select name="status" onchange="this.form.submit()">{% for status in action_statuses %}<option value="{{ status }}" {% if status == row.status %}selected{% endif %}>{{ status }}</option>{% endfor %}</select></form></td>
                <td><a class="view-link" href="{{ url_for('edit_action', action_id=row.id) }}">Edit</a></td>
            </tr>{% endfor %}</tbody>
        </table></div>
        {% else %}<div class="empty">No urgent or recently created follow-ups are waiting.</div>{% endif %}
    </div>

    <div class="panel">
        <div class="heading-row"><div><h2>Monday Preparation Checklist</h2><p>Week commencing {{ checklist_week_display }}. Your ticks are saved automatically.</p></div><span class="change-badge {% if checklist_complete == checklist_total %}change-positive{% else %}change-neutral{% endif %}">{{ checklist_complete }}/{{ checklist_total }} complete</span></div>
        <form method="post" action="{{ url_for('update_weekly_checklist') }}" class="checklist-list">
            <input type="hidden" name="week_key" value="{{ checklist_week_key }}">
            {% for item in checklist_items %}
            <label class="checklist-row {% if item.done %}done{% endif %}"><input type="checkbox" name="item_{{ item.item_key }}" value="1" {% if item.done %}checked{% endif %} onchange="this.form.submit()"><span>{{ item.label }}</span></label>
            {% endfor %}
        </form>
    </div>

    <div class="panel">
        <div class="heading-row"><div><h2>Upcoming Trip Briefing</h2><p>Appointments read from the latest Portatour HTML report, enriched with open actions and saved store analysis.</p></div><a class="button-link secondary" href="{{ url_for('follow_up') }}">Upload Latest Portatour Report</a></div>
        {% if upcoming_briefings %}
            {% for item in upcoming_briefings %}<div class="briefing-card">
                <div class="briefing-top"><div><div class="briefing-store">{{ item.store }}</div><div class="briefing-meta">{{ item.date_display }} · {{ item.time }}</div></div><div class="mini-tags">{% if item.open_action_count %}<span class="mini-tag">{{ item.open_action_count }} open action{% if item.open_action_count != 1 %}s{% endif %}</span>{% endif %}{% if item.has_analysis %}<span class="mini-tag">Store analysis saved</span>{% endif %}{% if item.armoursteel %}<span class="mini-tag">Armorsteel</span>{% endif %}</div></div>
                {% if item.call_note %}<div class="briefing-note"><strong>Portatour note:</strong> {{ item.call_note }}</div>{% endif %}
                {% if item.open_action_titles %}<div class="briefing-note"><strong>Outstanding:</strong> {{ item.open_action_titles|join('; ') }}</div>{% endif %}
            </div>{% endfor %}
        {% else %}<div class="empty">No upcoming appointments were found. Upload a Portatour HTML report in the Follow-Up Assistant to populate this section.</div>{% endif %}
    </div>
    {% endif %}

    {% if view == 'trip_prep' %}
    <div class="panel">
        <div class="year-planner-toolbar">
            <div>
                <h2 style="margin-bottom:4px;">Annual Trip Planner</h2>
                <p class="small" style="margin:0;">Click a trip to open its booking, brochure and readiness details below.</p>
            </div>
            <div class="year-nav">
                <a class="view-link" href="{{ url_for('trip_prep', year=planner_year-1) }}">&larr;</a>
                <div class="year-title">{{ planner_year }}</div>
                <a class="view-link" href="{{ url_for('trip_prep', year=planner_year+1) }}">&rarr;</a>
                <a class="button-link" href="#trip-details">Add Trip</a>
            </div>
        </div>
        <div class="year-planner-wrap">
            <div class="year-planner">
                {% for month in planner_months %}
                <div class="year-month">
                    <div class="year-month-header">{{ month.name|upper }}</div>
                    {% for day in month.days %}
                    <div class="year-day {% if day.weekend %}weekend{% endif %} {% if day.today %}today{% endif %}">
                        <div class="year-day-number">{{ day.day }}</div>
                        <div class="year-day-name">{{ day.weekday }}</div>
                        <div class="year-day-events">
                            {% for trip in day.trips %}
                            <a href="{{ url_for('trip_prep', trip=trip.id, year=planner_year) }}#trip-details" title="{{ trip.trip_name }} — {{ trip.status_label }}" class="calendar-trip status-{{ trip.status_key }} {% if trip.id == trip_plan.id %}selected{% endif %}">{{ trip.trip_name }}</a>
                            {% endfor %}
                        </div>
                    </div>
                    {% endfor %}
                </div>
                {% endfor %}
            </div>
        </div>
        <div class="planner-legend">
            <span><i class="planner-dot grey"></i>Not started</span>
            <span><i class="planner-dot orange"></i>Needs attention</span>
            <span><i class="planner-dot blue"></i>Planning underway</span>
            <span><i class="planner-dot green"></i>Trip ready</span>
        </div>
    </div>

    <div class="panel">
        <div class="heading-row">
            <div><h2>Plan Trip</h2><p>Work through the trip in order, from dates and appointments to packing, travel and final readiness.</p></div>
            {% if trip_plan.id %}
            <div class="toolbar">
                <span class="change-badge {% if readiness.complete %}change-positive{% else %}change-neutral{% endif %}">{{ readiness.done }}/{{ readiness.total }} ready</span>
                <a class="button-link" href="{{ url_for('trip_workspace', trip_id=trip_plan.id) }}">Open Trip Workspace</a>
                <form method="post" action="{{ url_for('delete_trip_plan', trip_id=trip_plan.id) }}" onsubmit="return confirm('Delete this planned trip? This will remove the trip and its route-generated schedule, but will not delete customers, actions, customer settings, or general visit history.');">
                    <button type="submit" class="danger">Delete Trip</button>
                </form>
            </div>
            {% endif %}
        </div>
        <div class="trip-steps">
            <a class="trip-step {% if trip_plan.id %}complete{% else %}current{% endif %}" href="#trip-details">1. Trip details</a>
            <a class="trip-step {% if appointments %}complete{% elif trip_plan.id %}current{% endif %}" href="#appointments">2. Appointments</a>
            <a class="trip-step {% if prep_rows %}complete{% elif appointments %}current{% endif %}" href="#brochures">3. Brochures</a>
            <a class="trip-step {% if trip_plan.travel_confirmed %}complete{% elif prep_rows %}current{% endif %}" href="#travel">4. Travel</a>
            <a class="trip-step {% if readiness.complete %}complete{% elif trip_plan.id %}current{% endif %}" href="#ready">5. Ready</a>
        </div>
    </div>

    <div class="plan-grid">
        <div class="plan-card step-anchor" id="trip-details">
            <h3>1. Trip details</h3><p class="small">Create or update the trip you are preparing.</p>
            <form method="post">
                <input type="hidden" name="action" value="save_trip_plan">
                <input type="hidden" name="trip_plan_id" value="{{ trip_plan.id or '' }}">
                <div class="plan-form-grid">
                    <label>Trip name<input type="text" name="trip_name" value="{{ trip_plan.trip_name }}" placeholder="Northland trip" required></label>
                    <label>Territory / destination<input type="text" name="destination" value="{{ trip_plan.destination }}" placeholder="Kerikeri, Kaitaia, Whangarei"></label>
                    <label>Start date<input type="date" name="start_date" value="{{ trip_plan.start_date }}" required></label>
                    <label>End date<input type="date" name="end_date" value="{{ trip_plan.end_date }}" required></label>
                    <label>Travel method<select name="travel_method">{% for option in ['Drive','Fly','Mixed'] %}<option value="{{ option }}" {% if trip_plan.travel_method == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                    <label>General notes<textarea name="notes" rows="2">{{ trip_plan.notes }}</textarea></label>
                </div>
                <div class="button-row" style="text-align:left;"><button type="submit">Save Trip Details</button></div>
            </form>
        </div>

        <div class="plan-card step-anchor" id="appointments">
            <h3>2. Appointments</h3>
            {% if latest_schedule_filename %}<p class="small">Latest schedule: <strong>{{ latest_schedule_filename }}</strong>{% if latest_schedule_uploaded %} · {{ latest_schedule_uploaded }}{% endif %}</p>{% endif %}
            <form method="post" enctype="multipart/form-data">
                <input type="file" name="schedule_report" accept=".html,.htm">
                <div class="button-row" style="text-align:left;">
                    <button type="submit" name="action" value="upload_generate">Upload Schedule</button>
                    {% if latest_schedule_filename %}<button type="submit" name="action" value="generate" class="secondary">Use Latest Schedule</button>{% endif %}
                </div>
            </form>
            {% if appointments %}
            <div style="margin-top:14px;display:grid;gap:12px;">
                {% for item in appointments %}
                <form method="post" class="appointment-mini" style="padding:14px;border:1px solid #d9e2ec;border-radius:10px;">
                    <input type="hidden" name="action" value="save_appointment_objective">
                    <input type="hidden" name="trip_plan_id" value="{{ trip_plan.id or 0 }}">
                    <input type="hidden" name="appointment_id" value="{{ item.appointment_id }}">
                    <input type="hidden" name="schedule_id" value="{{ item.schedule_id }}">
                    <input type="hidden" name="visit_date" value="{{ item.date_iso }}">
                    <input type="hidden" name="visit_time" value="{{ item.time }}">
                    <input type="hidden" name="store_name" value="{{ item.store }}">
                    <div class="heading-row"><div><strong><a class="customer-link" href="{{ url_for('customer_detail', store_name=item.store) }}">{{ item.store }}</a></strong><br><span class="small">{{ item.date_display }} · {{ item.time }} · {{ item.customer_type }}</span></div><button type="submit">Save Objective</button></div>
                    <label style="display:block;margin-top:10px;font-weight:700;">Desired outcome<textarea class="review-textarea" name="desired_outcome" placeholder="What should this visit achieve?">{{ item.desired_outcome }}</textarea></label>
                    <label style="display:block;margin-top:10px;font-weight:700;">Preparation notes<textarea class="review-textarea" name="preparation_notes" placeholder="Products, questions, evidence or people to prepare for the call">{{ item.preparation_notes }}</textarea></label>
                </form>
                {% endfor %}
            </div>
            {% else %}<div class="empty">No appointments loaded yet.</div>{% endif %}
        </div>
    </div>

    {% if trip_prep_error %}<div class="error">{{ trip_prep_error }}</div>{% endif %}
    {% if trip_prep_success %}<div class="success">{{ trip_prep_success }}</div>{% endif %}

    <div class="panel step-anchor" id="brochures">
        <div class="heading-row"><div><h2>3. Brochures</h2><p>Adjust quantities by customer, then download the packing sheet.</p></div></div>
        {% if prep_rows %}
        <form method="post">
            <div class="table-wrap"><table class="prep-table">
                <thead><tr><th>Appointment</th>{% for item in brochure_types %}<th>{{ item }}</th>{% endfor %}<th>Total</th></tr></thead>
                <tbody>{% for row in prep_rows %}<tr>
                    <td><div class="prep-store">{{ row.store }}</div><div class="prep-meta">{{ row.date_display }} · {{ row.time }} · {{ row.customer_type }}</div></td>
                    {% for item in brochure_types %}<td><input type="number" min="0" step="1" name="qty_{{ row.row_index }}_{{ item|replace(' ', '_') }}" value="{{ row.quantities[item] }}"></td>{% endfor %}
                    <td><strong>{{ row.total }}</strong></td>
                </tr>{% endfor %}</tbody>
            </table></div>
            <div class="button-row no-print"><button type="submit" name="action" value="recalculate">Recalculate</button><button type="submit" name="action" value="download_pdf" class="secondary">Download Packing Sheet PDF</button></div>
        </form>
        {% else %}<div class="empty">Load a Portatour schedule to build the brochure list.</div>{% endif %}
    </div>

    <div class="plan-grid">
        <div class="plan-card step-anchor" id="travel">
            <h3>4. Travel</h3><p class="small">Confirm the travel arrangements for this trip.</p>
            <form method="post">
                <input type="hidden" name="action" value="save_trip_travel"><input type="hidden" name="trip_plan_id" value="{{ trip_plan.id or '' }}">
                <div class="plan-form-grid">
                    <label>Flights<select name="flight_status">{% for option in booking_statuses %}<option value="{{ option }}" {% if trip_plan.flight_status == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                    <label>Rental car<select name="rental_status">{% for option in booking_statuses %}<option value="{{ option }}" {% if trip_plan.rental_status == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                    <label>Accommodation<select name="accommodation_status">{% for option in booking_statuses %}<option value="{{ option }}" {% if trip_plan.accommodation_status == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                    <label>Travel notes<textarea name="travel_notes" rows="2">{{ trip_plan.travel_notes }}</textarea></label>
                </div>
                <label class="ready-row" style="margin-top:14px;"><input type="checkbox" name="travel_confirmed" value="1" {% if trip_plan.travel_confirmed %}checked{% endif %}>Travel arrangements checked and confirmed</label>
                <div class="button-row" style="text-align:left;"><button type="submit" {% if not trip_plan.id %}disabled title="Save trip details first"{% endif %}>Save Travel</button></div>
            </form>
        </div>

        <div class="plan-card step-anchor" id="ready">
            <h3>5. Final readiness</h3>
            {% if trip_plan.id %}
            <div class="readiness-banner {% if readiness.complete %}ready{% endif %}"><div class="big">{% if readiness.complete %}Trip ready{% else %}{{ readiness.remaining }} item{% if readiness.remaining != 1 %}s{% endif %} still need attention{% endif %}</div><div>{{ readiness.done }} of {{ readiness.total }} checks completed</div></div>
            <form method="post" class="ready-list">
                <input type="hidden" name="action" value="save_trip_readiness"><input type="hidden" name="trip_plan_id" value="{{ trip_plan.id }}">
                {% for item in readiness["items"] %}<label class="ready-row"><input type="checkbox" name="ready_{{ item.key }}" value="1" {% if item.done %}checked{% endif %}>{{ item.label }}</label>{% endfor %}
                <div class="button-row" style="text-align:left;"><button type="submit">Save Readiness</button></div>
            </form>
            {% else %}<div class="empty">Save the trip details first to activate the readiness checklist.</div>{% endif %}
        </div>
    </div>
    {% endif %}

    {% if view == 'customer_detail' %}
    <div class="panel">
        <div class="customer-hero">
            <div class="customer-hero-main">
                <h2>{{ customer.store_name }}</h2>
                <p>{{ customer.customer_type }} · {{ customer.classification }}</p>
                <p>{{ customer.visit_status }} — {{ customer.visit_detail }}</p>
                {% if customer.strategic_notes %}<p style="margin-top:12px;"><strong>Strategic note:</strong> {{ customer.strategic_notes }}</p>{% endif %}
            </div>
            <div class="customer-stat"><div class="label">Priority score</div><div class="value">{{ customer.score }}</div><div class="small">{{ customer.score_breakdown }}</div></div>
            <div class="customer-stat"><div class="label">Sales value</div><div class="value">{{ customer.sales_display }}</div><div class="small">{{ customer.yoy_display }} year on year</div></div>
            <div class="customer-stat"><div class="label">Open actions</div><div class="value">{{ customer.open_actions }}</div><div class="small">{{ customer.high_actions }} high priority</div></div>
        </div>
        {% if customer.is_mapped_customer %}
        <div class="file-details" style="margin-top:18px;">
            <div class="file-detail"><strong>Address</strong><span>{{ customer.address_display or 'Not supplied' }}</span></div>
            <div class="file-detail"><strong>Customer group</strong><span>{{ customer.customer_group }}</span></div>
            <div class="file-detail"><strong>Phone</strong><span>{% if customer.phone %}<a href="tel:{{ customer.phone }}">{{ customer.phone }}</a>{% else %}Not supplied{% endif %}</span></div>
            <div class="file-detail"><strong>Email</strong><span>{% if customer.email %}<a href="mailto:{{ customer.email }}">{{ customer.email }}</a>{% else %}Not supplied{% endif %}</span></div>
            <div class="file-detail"><strong>Portatour call note</strong><span>{{ customer.call_note or 'No imported call note' }}</span></div>
            <div class="file-detail"><strong>Imported planning</strong><span>Tier {{ customer.priority_tier }} · {{ customer.ideal_call_minutes }} min calls · {{ customer.customer_open_time }}–{{ customer.customer_close_time }}</span></div>
            <div class="file-detail"><strong>Categories</strong><span>{{ customer.categories or 'Not supplied' }}</span></div>
            <div class="file-detail"><strong>Map coordinates</strong><span>{% if customer.latitude is not none and customer.longitude is not none %}{{ customer.latitude }}, {{ customer.longitude }}{% else %}Not supplied{% endif %}</span></div>
        </div>
        {% endif %}
        <div class="button-row" style="text-align:left;">
            <a class="button-link secondary" href="{{ url_for('customer_priorities') }}">Back to Customers</a>
            <a class="button-link" href="{{ url_for('new_action', store=customer.store_name, source='Customer Hub') }}">Add Action</a>
            {% if analysis_id %}<a class="button-link secondary" href="{{ url_for('saved_opportunity', analysis_id=analysis_id) }}">Open Full Store Analysis</a>{% endif %}
        </div>
    </div>

    <div class="customer-section-grid">
        <div>
            <div class="panel">
                <div class="heading-row"><div><h2>Open Actions</h2><p>Everything still requiring follow-up for this customer.</p></div></div>
                {% if customer_actions %}
                    {% for action in customer_actions %}
                    <div class="customer-action-card">
                        <div class="customer-action-top"><div><h4>{{ action.title }}</h4><div class="small">{{ action.detail }}</div></div><span class="severity {{ action.priority_class }}">{{ action.priority }}</span></div>
                        <div class="small" style="margin-top:9px;">Due: {{ action.due_display }} · Status: {{ action.status }} · Source: {{ action.source or 'Manual' }}</div>
                        <div style="margin-top:10px;"><a class="view-link" href="{{ url_for('edit_action', action_id=action.id) }}">Edit Action</a></div>
                    </div>
                    {% endfor %}
                {% else %}<div class="empty">No open actions for this customer.</div>{% endif %}
            </div>

            <div class="panel">
                <h2>Store Opportunity Snapshot</h2>
                {% if analysis %}
                <div class="mini-grid">
                    <div class="mini-card"><div class="label">Ranking opportunity</div><div class="value">{{ analysis.total_opp_gap }}</div></div>
                    <div class="mini-card"><div class="label">Immediate stock risks</div><div class="value">{{ analysis.urgent_stock_count }}</div></div>
                    <div class="mini-card"><div class="label">Core range gaps</div><div class="value">{{ analysis.core_gap_count }}</div></div>
                </div>
                {% if analysis.conversation_plan %}
                <h3 style="margin-top:22px;">Best discussion points</h3>
                <div class="insight-list">{% for item in analysis.conversation_plan[:3] %}<div class="insight-card {{ item.card_class }}"><h3>{{ item.title }}</h3><p>{{ item.evidence }}</p><p><strong>Next discussion:</strong> {{ item.action }}</p></div>{% endfor %}</div>
                {% endif %}
                {% else %}<div class="empty">No saved Mitre 10 store analysis is attached to this customer yet.</div>{% endif %}
            </div>
        </div>

        <div>
            <div class="panel">
                <div class="heading-row"><div><h2>Contacts</h2><p>People associated with this customer.</p></div></div>
                {% if customer_contacts %}
                    {% for contact in customer_contacts %}
                    <div class="customer-action-card"><div class="customer-action-top"><div><h4>{{ contact.contact_name }}</h4><div class="small">{{ contact.role or 'Contact' }}</div></div><form method="post" action="{{ url_for('delete_customer_contact', contact_id=contact.id) }}" onsubmit="return confirm('Delete this contact?');"><input type="hidden" name="store_name" value="{{ customer.store_name }}"><button class="danger" type="submit">Delete</button></form></div>{% if contact.phone %}<div class="small">Phone: <a href="tel:{{ contact.phone }}">{{ contact.phone }}</a></div>{% endif %}{% if contact.email %}<div class="small">Email: <a href="mailto:{{ contact.email }}">{{ contact.email }}</a></div>{% endif %}{% if contact.notes %}<div class="briefing-note">{{ contact.notes }}</div>{% endif %}</div>
                    {% endfor %}
                {% else %}<div class="empty">No contacts recorded yet.</div>{% endif %}
                <form method="post" action="{{ url_for('add_customer_contact') }}" style="margin-top:14px;">
                    <input type="hidden" name="store_name" value="{{ customer.store_name }}">
                    <div class="plan-form-grid"><label>Name<input type="text" name="contact_name" required></label><label>Role<input type="text" name="role"></label><label>Phone<input type="text" name="phone"></label><label>Email<input type="email" name="email"></label></div>
                    <label style="display:block;margin-top:10px;">Notes<textarea class="review-textarea" name="notes"></textarea></label><div class="button-row" style="text-align:left;"><button type="submit">Add Contact</button></div>
                </form>
            </div>
            <div class="panel">
                <h2>Upcoming Visits</h2>
                {% if upcoming_customer_visits %}{% for visit in upcoming_customer_visits %}<div class="customer-action-card"><strong>{{ visit.date_display }} · {{ visit.visit_time }}</strong><div class="small">{{ visit.trip_name }}</div>{% if visit.desired_outcome %}<div class="briefing-note"><strong>Desired outcome:</strong> {{ visit.desired_outcome }}</div>{% endif %}</div>{% endfor %}{% else %}<div class="empty">No upcoming planned visits.</div>{% endif %}
            </div>
            <div class="panel">
                <h2>Customer Settings</h2>
                <form method="post" action="{{ url_for('customer_priorities') }}">
                    <input type="hidden" name="mode" value="save">
                    <input type="hidden" name="store_name" value="{{ customer.store_name }}">
                    <label>Growth potential<select name="growth_potential">{% for n in range(1,6) %}<option value="{{ n }}" {% if n == customer.growth_potential %}selected{% endif %}>{{ n }}/5</option>{% endfor %}</select></label>
                    <label style="display:block;margin-top:12px;">Visit cycle in days<input class="review-input" type="number" min="1" name="visit_frequency_days" value="{{ customer.visit_frequency_days }}"></label>
                    <label style="display:block;margin-top:12px;">Last visit<input class="review-input" type="date" name="last_visit_date" value="{{ customer.last_visit_date }}"></label>
                    <label style="display:block;margin-top:12px;">Manual sales value<input class="review-input" type="number" min="0" step="0.01" name="manual_sales_value" value="{{ customer.manual_sales_value }}"></label>
                    <label style="display:block;margin-top:12px;">Strategic notes<textarea class="review-textarea" name="strategic_notes">{{ customer.strategic_notes }}</textarea></label>
                    <div class="button-row"><button type="submit">Save Customer Settings</button></div>
                </form>
            </div>
            <div class="panel">
                <h2>Visit Planning</h2>
                <div class="file-detail"><strong>Last visit</strong><span>{{ customer.last_visit_date or 'Not recorded' }}</span></div>
                <div class="file-detail" style="margin-top:10px;"><strong>Normal cycle</strong><span>Every {{ customer.visit_frequency_days }} days</span></div>
                <div class="file-detail" style="margin-top:10px;"><strong>Current status</strong><span>{{ customer.visit_status }} — {{ customer.visit_detail }}</span></div>
            </div>
        </div>
    </div>
    <div class="panel">
        <div class="heading-row"><div><h2>Call History</h2><p>Completed and recorded visits, newest first.</p></div></div>
        {% if customer_call_history %}
            {% for visit in customer_call_history %}
            <div class="visit-closeout-row">
                <div><strong>{{ visit.date_display }}</strong><br><span class="small">{{ visit.visit_time }}{% if visit.actual_duration_minutes %} · {{ visit.actual_duration_minutes }} min{% endif %}</span></div>
                <div><strong>{{ visit.status }}</strong>{% if visit.trip_name %}<div class="small">{{ visit.trip_name }}</div>{% endif %}{% if visit.desired_outcome %}<div class="visit-closeout-note"><strong>Desired outcome:</strong> {{ visit.desired_outcome }}</div>{% endif %}{% if visit.outcome %}<div class="visit-closeout-note"><strong>Outcome:</strong> {{ visit.outcome }}</div>{% endif %}{% if visit.visit_notes %}<div class="visit-closeout-note">{{ visit.visit_notes }}</div>{% endif %}{% if visit.next_step %}<div class="visit-closeout-note"><strong>Next:</strong> {{ visit.next_step }}</div>{% endif %}{% if visit.attachments %}<div class="mini-tags">{% for file in visit.attachments %}<a class="mini-tag" href="{{ file.url }}" target="_blank">{{ file.original_name }}</a>{% endfor %}</div>{% endif %}</div>
                <div><span class="change-badge {{ visit.badge_class }}">{{ visit.status }}</span></div>
            </div>
            {% endfor %}
        {% else %}<div class="empty">No call history has been recorded yet.</div>{% endif %}
    </div>
    {% endif %}

    {% if view == 'customer_priorities' %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>Customer Value & Visit Priority</h2>
                <p>Ranks customers using saved sales opportunities, open actions, commercial potential and visit timing.</p>
            </div>
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <div class="priority-summary">
            <div class="summary-card positive"><div class="label">Protect</div><div class="value">{{ priority_counts.protect }}</div><div class="note">High-value accounts to defend</div></div>
            <div class="summary-card"><div class="label">Grow</div><div class="value">{{ priority_counts.grow }}</div><div class="note">Best expansion opportunities</div></div>
            <div class="summary-card neutral"><div class="label">Maintain</div><div class="value">{{ priority_counts.maintain }}</div><div class="note">Normal call-cycle coverage</div></div>
            <div class="summary-card warning"><div class="label">Low Priority</div><div class="value">{{ priority_counts.low }}</div><div class="note">Lower immediate return</div></div>
        </div>
    </div>

    <div class="panel">
        <h2>Add a Customer</h2>
        <p class="section-note">Use this for accounts that do not yet appear in a saved opportunity analysis or the Action Tracker.</p>
        <form method="post" class="priority-form-grid">
            <input type="hidden" name="mode" value="save">
            <label>Customer<input type="text" name="store_name" required></label>
            <label>Growth potential<select name="growth_potential">{% for n in range(1,6) %}<option value="{{ n }}" {% if n == 3 %}selected{% endif %}>{{ n }}/5</option>{% endfor %}</select></label>
            <label>Visit every<input type="number" min="1" name="visit_frequency_days" value="42"></label>
            <label>Last visit<input type="date" name="last_visit_date"></label>
            <label>Sales value if known<input type="number" min="0" step="0.01" name="manual_sales_value" value="0"></label>
            <label>Notes<input type="text" name="strategic_notes"></label>
            <button type="submit">Add</button>
        </form>
    </div>

    <div class="panel">
        <div class="heading-row"><div><h2>Ranked Customer List</h2><p>Higher scores indicate a stronger reason to invest time now.</p></div></div>
        {% if priority_rows %}
        <div class="table-wrap"><table class="finder-table">
            <thead><tr><th>Rank</th><th>Customer</th><th>Priority</th><th>Score</th><th>Commercial picture</th><th>Visit status</th><th>Settings</th></tr></thead>
            <tbody>
            {% for row in priority_rows %}
            <tr>
                <td><strong>{{ loop.index }}</strong></td>
                <td><a class="customer-link" href="{{ url_for('customer_detail', store_name=row.store_name) }}">{{ row.store_name }}</a><br><span class="small">{{ row.customer_group }}{% if row.address_display %} · {{ row.address_display }}{% endif %}</span>{% if row.strategic_notes %}<br><span class="small">{{ row.strategic_notes }}</span>{% elif row.call_note %}<br><span class="small">{{ row.call_note }}</span>{% endif %}</td>
                <td><span class="priority-class priority-{{ row.class_key }}">{{ row.classification }}</span></td>
                <td><span class="priority-score">{{ row.score }}</span><div class="score-breakdown">{{ row.score_breakdown }}</div></td>
                <td>
                    <strong>{{ row.sales_display }}</strong> sales
                    <br><span class="small">{{ row.yoy_display }} YoY · {{ row.opportunity_display }} opportunity</span>
                    <br><span class="small">{{ row.open_actions }} open action{% if row.open_actions != 1 %}s{% endif %}, {{ row.high_actions }} high priority</span>
                </td>
                <td><strong>{{ row.visit_status }}</strong><br><span class="small">{{ row.visit_detail }}</span></td>
                <td>
                    <form method="post" class="priority-form-grid" style="grid-template-columns:110px 125px 145px 165px 1fr auto; min-width:760px;">
                        <input type="hidden" name="mode" value="save">
                        <input type="hidden" name="store_name" value="{{ row.store_name }}">
                        <label>Potential<select name="growth_potential">{% for n in range(1,6) %}<option value="{{ n }}" {% if n == row.growth_potential %}selected{% endif %}>{{ n }}/5</option>{% endfor %}</select></label>
                        <label>Cycle days<input type="number" min="1" name="visit_frequency_days" value="{{ row.visit_frequency_days }}"></label>
                        <label>Last visit<input type="date" name="last_visit_date" value="{{ row.last_visit_date }}"></label>
                        <label>Manual sales<input type="number" min="0" step="0.01" name="manual_sales_value" value="{{ row.manual_sales_value }}"></label>
                        <label>Notes<input type="text" name="strategic_notes" value="{{ row.strategic_notes }}"></label>
                        <button type="submit">Save</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
            </tbody>
        </table></div>
        {% else %}<div class="empty">No customers are available yet. Save a store analysis, create a store action, or add a customer above.</div>{% endif %}
    </div>
    {% endif %}

    {% if view in ['dashboard', 'history', 'saved_month', 'opportunities', 'opportunity_library', 'saved_opportunity', 'visit_brief', 'actions', 'action_form', 'qtr_bonus'] %}
    <div class="nav" style="margin-top:-10px;">
        <a href="{{ url_for('home') }}" class="{% if view == 'dashboard' %}active{% endif %}">Upload & Compare</a>
        <a href="{{ url_for('history') }}" class="{% if view in ['history', 'saved_month'] %}active{% endif %}">Saved Months & Trends</a>
        <a href="{{ url_for('opportunities') }}" class="{% if view == 'opportunities' %}active{% endif %}">Mitre 10 Store Analyser</a>
        <a href="{{ url_for('opportunity_library') }}" class="{% if view in ['opportunity_library', 'saved_opportunity', 'visit_brief'] %}active{% endif %}">Saved Store Analyses</a>
        <a href="{{ url_for('qtr_bonus_tracker') }}" class="{% if view == 'qtr_bonus' %}active{% endif %}">QTR Bonus Tracker</a>
        <a href="{{ url_for('actions') }}" class="{% if view in ['actions', 'action_form'] %}active{% endif %}">Actions{% if open_action_count %} ({{ open_action_count }}){% endif %}</a>
    </div>
    {% endif %}

    {% if view == 'dashboard' and show_upload %}
    <div class="panel">
        <form method="post" enctype="multipart/form-data">
            <div class="upload-grid">
                <div class="upload-box">
                    <h3>This Year</h3>
                    <p>Upload the latest report for the month being measured.</p>
                    <input type="file" name="current_report" accept=".xls,.xlsx,.xml" required>
                </div>
                <div class="upload-box">
                    <h3>Last Year</h3>
                    <p>Upload the completed report for the matching month last year.</p>
                    <input type="file" name="previous_report" accept=".xls,.xlsx,.xml" required>
                </div>
            </div>

            <div class="date-box">
                <label for="as_of_date">This-year sales are current as at:</label>
                <input id="as_of_date" type="date" name="as_of_date" value="{{ selected_date }}" required>
                <p>Choose the date on which you downloaded the current report.</p>
            </div>

            <div class="button-row">
                <button type="submit">Build Dashboard</button>
            </div>
        </form>

        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
    </div>
    {% endif %}

    {% if view == 'qtr_bonus' %}
    <div class="panel upload-panel">
        <div class="heading-row">
            <div>
                <h2>QTR Bonus Tracker</h2>
                <p>Upload a raw sales transactions export (Date, Document Number, Name, Sum of Amount) to track quarterly growth and bonus against last year.</p>
            </div>
        </div>
        <form method="post" enctype="multipart/form-data">
            <div class="upload-grid">
                <div class="upload-box">
                    <h3>Transactions Report</h3>
                    <p>Upload the rep's transaction-level export. Re-uploading an updated file for the same rep will refresh existing rows rather than duplicate them.</p>
                    <input type="file" name="transactions_report" accept=".xls,.xlsx,.xml" required>
                </div>
                <div class="upload-box">
                    <h3>Salesperson</h3>
                    <p>Enter the rep's name exactly as you'd like it saved (the report itself doesn't contain a name column).</p>
                    <input type="text" name="salesperson" placeholder="e.g. Ted Suckling" list="known_salespeople" value="{{ selected_salesperson }}" required>
                    <datalist id="known_salespeople">
                        {% for name in salesperson_options %}<option value="{{ name }}">{% endfor %}
                    </datalist>
                </div>
            </div>
            <div class="button-row">
                <button type="submit">Upload Transactions</button>
            </div>
        </form>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
    </div>

    {% if salesperson_options %}
    <div class="panel">
        <form method="get">
            <div class="file-details">
                <div class="file-detail"><strong>Salesperson</strong><select name="salesperson">{% for name in salesperson_options %}<option value="{{ name }}" {% if name == selected_salesperson %}selected{% endif %}>{{ name }}</option>{% endfor %}</select></div>
                <div class="file-detail"><strong>Growth target %</strong><input type="number" step="0.1" name="growth_target" value="{{ growth_target_percent }}"></div>
                <div class="file-detail"><strong>Bonus rate %</strong><input type="number" step="0.1" name="bonus_rate" value="{{ bonus_rate_percent }}"></div>
            </div>
            <div class="button-row"><button type="submit">Update</button></div>
        </form>
    </div>
    {% endif %}

    {% if bonus_dashboard %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>{{ selected_salesperson }}</h2>
                <p>{{ bonus_dashboard.quarter_label }} &middot; {{ bonus_dashboard.quarter_range_display }}</p>
                <p>Progress calculated as at {{ bonus_dashboard.today_display }}</p>
            </div>
        </div>

        <h3 style="margin-top:0;">Vs Last Year - Same Point in Quarter</h3>
        <div class="summary-grid">
            <div class="summary-card neutral">
                <div class="label">Last Year (to date)</div>
                <div class="value">{{ bonus_dashboard.qtd_last_year_display }}</div>
                <div class="note">{{ bonus_dashboard.qtd_last_year_range_display }}</div>
            </div>
            <div class="summary-card neutral">
                <div class="label">Target (+{{ growth_target_percent }}%)</div>
                <div class="value">{{ bonus_dashboard.target_qtd_display }}</div>
                <div class="note">{{ growth_target_percent }}% growth on last year</div>
            </div>
            <div class="summary-card neutral">
                <div class="label">This Quarter (to date)</div>
                <div class="value">{{ bonus_dashboard.qtd_this_year_display }}</div>
                <div class="note">{{ bonus_dashboard.qtd_this_year_range_display }}</div>
            </div>
            <div class="summary-card {{ bonus_dashboard.vs_target_qtd_class }}">
                <div class="label">Vs Target</div>
                <div class="value">{{ bonus_dashboard.vs_target_qtd_display }}</div>
                <div class="note"><span class="change-badge {{ bonus_dashboard.growth_pct_qtd_class }}">{{ bonus_dashboard.growth_pct_qtd_display }}</span> vs last year</div>
            </div>
        </div>
        <div class="summary-grid" style="margin-top:14px;">
            <div class="summary-card {% if bonus_dashboard.bonus_qtd_earned %}positive{% else %}neutral{% endif %}" style="grid-column: 1 / -1;">
                <div class="label">Bonus Earned (vs same point last year)</div>
                <div class="value">{{ bonus_dashboard.bonus_qtd_display }}</div>
                <div class="note">
                    {% if bonus_dashboard.bonus_qtd_earned %}
                        {{ bonus_dashboard.above_target_qtd_display }} above target &times; {{ bonus_rate_percent }}%
                    {% else %}
                        Target not yet reached on this basis
                    {% endif %}
                </div>
            </div>
        </div>

        <h3>Vs Last Year - Full Quarter Total</h3>
        <p style="color:#667085; font-size:14px; margin-top:-8px;">Compares your running quarter-to-date total against last year's completed quarter total &mdash; a stricter benchmark while the quarter is still in progress.</p>
        <div class="summary-grid">
            <div class="summary-card neutral">
                <div class="label">Last Year (full quarter)</div>
                <div class="value">{{ bonus_dashboard.full_last_year_display }}</div>
                <div class="note">{{ bonus_dashboard.full_last_year_range_display }}</div>
            </div>
            <div class="summary-card neutral">
                <div class="label">Target (+{{ growth_target_percent }}%)</div>
                <div class="value">{{ bonus_dashboard.target_full_display }}</div>
                <div class="note">{{ growth_target_percent }}% growth on last year</div>
            </div>
            <div class="summary-card neutral">
                <div class="label">This Quarter (to date)</div>
                <div class="value">{{ bonus_dashboard.qtd_this_year_display }}</div>
                <div class="note">{{ bonus_dashboard.qtd_this_year_range_display }}</div>
            </div>
            <div class="summary-card {{ bonus_dashboard.vs_target_full_class }}">
                <div class="label">Vs Target</div>
                <div class="value">{{ bonus_dashboard.vs_target_full_display }}</div>
                <div class="note"><span class="change-badge {{ bonus_dashboard.growth_pct_full_class }}">{{ bonus_dashboard.growth_pct_full_display }}</span> vs last year</div>
            </div>
        </div>
        <div class="summary-grid" style="margin-top:14px;">
            <div class="summary-card {% if bonus_dashboard.bonus_full_earned %}positive{% else %}neutral{% endif %}" style="grid-column: 1 / -1;">
                <div class="label">Bonus Earned (vs last year's full quarter)</div>
                <div class="value">{{ bonus_dashboard.bonus_full_display }}</div>
                <div class="note">
                    {% if bonus_dashboard.bonus_full_earned %}
                        {{ bonus_dashboard.above_target_full_display }} above target &times; {{ bonus_rate_percent }}%
                    {% else %}
                        Target not yet reached on this basis
                    {% endif %}
                </div>
            </div>
        </div>
    </div>

    <div class="panel">
        <h2>Quarter Progress</h2>
        <div class="metrics-grid">
            <div class="metric"><div class="label">Quarter</div><div class="value">{{ bonus_dashboard.quarter_label }}</div></div>
            <div class="metric"><div class="label">Days Elapsed</div><div class="value">{{ bonus_dashboard.days_elapsed }} / {{ bonus_dashboard.days_total }}</div></div>
            <div class="metric"><div class="label">Days Remaining</div><div class="value">{{ bonus_dashboard.days_remaining }}</div></div>
        </div>
        <div class="progress-track">
            <div class="progress-fill" style="width: {{ bonus_dashboard.quarter_complete_percent }}%;"></div>
        </div>
        <div class="progress-text">
            <span>{{ bonus_dashboard.quarter_complete_percent }}% of quarter elapsed</span>
            <span>{{ bonus_dashboard.transaction_count }} transactions loaded for {{ selected_salesperson }}</span>
        </div>
    </div>
    {% elif bonus_dashboard_empty %}
    <div class="panel"><div class="empty">No transactions loaded yet for {{ selected_salesperson }}. Upload a report above to build the dashboard.</div></div>
    {% elif not salesperson_options %}
    <div class="panel"><div class="empty">No transaction reports uploaded yet. Upload one above to get started.</div></div>
    {% endif %}
    {% endif %}

    {% if view in ['dashboard', 'saved_month'] and comparison_rows %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>{{ current_report.salesperson }}</h2>
                <p>{{ current_report.report_period }}</p>
                <p>Progress calculated as at {{ as_of_display }}</p>
            </div>
            {% if view == 'dashboard' %}
            <form method="post" action="{{ url_for('save_month') }}">
                {% for key, value in save_payload.items() %}
                    <input type="hidden" name="{{ key }}" value="{{ value }}">
                {% endfor %}
                <button type="submit">Save Month</button>
            </form>
            {% else %}
            <a class="button-link secondary" href="{{ url_for('history', year=current_year) }}">Back to Saved History</a>
            {% endif %}
        </div>

        <div class="summary-grid">
            <div class="summary-card">
                <div class="label">MTD Actual</div>
                <div class="value">{{ current_total_display }}</div>
                <div class="note">Sales loaded from the current report</div>
            </div>
            <div class="summary-card">
                <div class="label">Full-Month Target</div>
                <div class="value">{{ monthly_target_display }}</div>
                <div class="note">Based on last year's matching month</div>
            </div>
            <div class="summary-card">
                <div class="label">MTD Target</div>
                <div class="value">{{ mtd_target_display }}</div>
                <div class="note">Based on elapsed working days</div>
            </div>
            <div class="summary-card {{ performance_summary_class }}">
                <div class="label">Actual Versus MTD Target</div>
                <div class="value">{{ performance_percentage_display }}</div>
                <div class="note">{{ performance_difference_display }}</div>
            </div>
        </div>

        <div class="progress-track">
            <div class="progress-fill {{ performance_summary_class }}" style="width: {{ progress_width }}%;"></div>
        </div>
        <div class="progress-text">
            <span>{{ current_total_display }} achieved</span>
            <span>{{ monthly_progress_display }} of monthly target</span>
        </div>
    </div>

    <div class="panel">
        <h2>Working-Day Targets</h2>
        <div class="metrics-grid">
            <div class="metric"><div class="label">Working Days in Month</div><div class="value">{{ total_workdays }}</div></div>
            <div class="metric"><div class="label">Working Days Elapsed</div><div class="value">{{ elapsed_workdays }}</div></div>
            <div class="metric"><div class="label">Working Days Remaining</div><div class="value">{{ remaining_workdays }}</div></div>
            <div class="metric"><div class="label">Target Per Working Day</div><div class="value">{{ daily_target_display }}</div></div>
            <div class="metric"><div class="label">Required Per Remaining Day</div><div class="value">{{ required_daily_display }}</div></div>
            <div class="metric"><div class="label">Remaining to Reach Target</div><div class="value">{{ remaining_target_display }}</div></div>
        </div>
    </div>

    <div class="panel">
        <h2>Category Comparison</h2>
        <table class="comparison-table">
            <thead>
                <tr>
                    <th>Category</th>
                    <th>Last Year / Target</th>
                    <th>This Year Actual</th>
                    <th>Dollar Change</th>
                    <th>Percentage Change</th>
                </tr>
            </thead>
            <tbody>
                {% for row in comparison_rows %}
                <tr class="{% if row.category == 'Total' %}total-row{% endif %}">
                    <td>{{ row.category }}</td>
                    <td>{{ row.previous_display }}</td>
                    <td>{{ row.current_display }}</td>
                    <td>{{ row.difference_display }}</td>
                    <td><span class="change-badge {{ row.change_class }}">{{ row.percentage_display }}</span></td>
                </tr>
                {% endfor %}
            </tbody>
        </table>

        <div class="file-details">
            <div class="file-detail"><strong>This-year report</strong><span>{{ current_report.filename }}</span></div>
            <div class="file-detail"><strong>Last-year report</strong><span>{{ previous_report.filename }}</span></div>
        </div>
    </div>
    {% endif %}

    {% if view in ['opportunities', 'saved_opportunity', 'visit_brief'] %}
    {% if view == 'opportunities' %}
    <div class="panel upload-panel">
        <div class="heading-row">
            <div>
                <h2>Mitre 10 Store Comparison</h2>
                <p>Upload the four reports for a Mitre 10 store to compare performance, stock, ranking and core compliance in one clear dashboard.</p>
            </div>
            <a class="button-link secondary" href="{{ url_for('opportunity_library') }}">Saved Store Library</a>
        </div>

        <form method="post" enctype="multipart/form-data">
            <div class="four-upload-grid">
                <div class="upload-box">
                    <span class="report-tag">1 of 4</span>
                    <h3>Monthly Dashboard</h3>
                    <p>Fineline performance for this year versus last year.</p>
                    <input type="file" name="monthly_report" accept=".csv" required>
                </div>
                <div class="upload-box">
                    <span class="report-tag">2 of 4</span>
                    <h3>Weeks of Cover</h3>
                    <p>Stock on hand, on order, sales rate and weeks of cover.</p>
                    <input type="file" name="woc_report" accept=".csv" required>
                </div>
                <div class="upload-box">
                    <span class="report-tag">3 of 4</span>
                    <h3>Ranking Report</h3>
                    <p>Store performance and opportunity gaps against other stores.</p>
                    <input type="file" name="ranking_report" accept=".csv" required>
                </div>
                <div class="upload-box">
                    <span class="report-tag">4 of 4</span>
                    <h3>Core For Me</h3>
                    <p>Core range status, compliance, stock and recent sales.</p>
                    <input type="file" name="cfm_report" accept=".csv" required>
                </div>
            </div>
            <div class="button-row"><button type="submit">Build Store Opportunity Dashboard</button></div>
        </form>

        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
    </div>
    {% endif %}

    {% if opportunity_results and view != 'visit_brief' %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>{{ store_name }}</h2>
                <p>Mitre 10 opportunity dashboard · Reports exported {{ export_date }}</p>
            </div>
            <div class="toolbar">
                {% if view == 'opportunities' %}
                <form method="post" action="{{ url_for('save_opportunity') }}" style="display:inline;">
                    <textarea name="analysis_json" hidden>{{ opportunity_payload }}</textarea>
                    <button type="submit">Save Store Analysis</button>
                </form>
                {% else %}
                <a class="button-link secondary" href="{{ url_for('opportunity_library') }}">Back to Store Library</a>
                {% endif %}
                {% if analysis_id %}<a class="button-link" href="{{ url_for('visit_brief', analysis_id=analysis_id) }}">Visit Brief</a>{% endif %}
                <button type="button" class="secondary-button" onclick="window.print()">Print / Save as PDF</button>
            </div>
        </div>

        <div class="opportunity-grid">
            <div class="opportunity-card"><div class="label">Sales this year</div><div class="value">{{ total_sales_ty }}</div></div>
            <div class="opportunity-card {% if total_change_raw >= 0 %}positive{% else %}urgent{% endif %}"><div class="label">Year-on-year performance</div><div class="value">{{ total_change_pct }}</div></div>
            <div class="opportunity-card urgent"><div class="label">Immediate stock risks</div><div class="value">{{ urgent_stock_count }}</div></div>
            <div class="opportunity-card warning"><div class="label">Core range gaps</div><div class="value">{{ core_gap_count }}</div></div>
        </div>

        <div class="mini-grid" style="margin-top:16px;">
            <div class="mini-card"><div class="label">Positive ranking opportunity</div><div class="value">{{ total_opp_gap }}</div></div>
            <div class="mini-card"><div class="label">Low-cover lines</div><div class="value">{{ low_cover_count }}</div></div>
            <div class="mini-card"><div class="label">High-cover lines to review</div><div class="value">{{ overstock_count }}</div></div>
        </div>

        <div class="mini-grid" style="margin-top:16px;">
            <div class="mini-card"><div class="label">National sales rank</div><div class="value">{{ overall_rank_display }}</div>{% if overall_percentile_display %}<div class="note" style="margin-top:6px; color:#667085; font-size:13px;">{{ overall_percentile_display }} of stores in this report</div>{% endif %}</div>
            {% if best_category %}
            <div class="mini-card"><div class="label">Strongest category</div><div class="value">{{ best_category.fineline }}</div><div class="note" style="margin-top:6px; color:#667085; font-size:13px;">Ranked {{ best_category.rank_display }} ({{ best_category.percentile_display }})</div></div>
            {% endif %}
            {% if worst_category %}
            <div class="mini-card"><div class="label">Weakest category</div><div class="value">{{ worst_category.fineline }}</div><div class="note" style="margin-top:6px; color:#667085; font-size:13px;">Ranked {{ worst_category.rank_display }} ({{ worst_category.percentile_display }})</div></div>
            {% endif %}
        </div>
    </div>

    <div class="panel">
        <h2>Store Conversation Plan</h2>
        <p class="section-note">The highest-value points to take into the store, ranked by urgency and commercial impact.</p>
        <div class="insight-list">
            {% for item in conversation_plan %}
            <div class="insight-card {{ item.card_class }}">
                <h3>{{ item.title }}</h3>
                <p><strong>What the data says:</strong> {{ item.evidence }}</p>
                <p><strong>Store discussion:</strong> {{ item.action }}</p>
            </div>
            {% endfor %}
        </div>
    </div>

    <div class="panel">
        <h2>Fineline Performance</h2>
        <p class="section-note">Use this table to show where the store is growing, declining and leaving measurable ranking opportunity on the table.</p>
        <div class="table-wrap">
            <table class="finder-table">
                <thead><tr><th>Fineline</th><th>Sales TY</th><th>Sales LY</th><th>YoY</th><th>Ranking opportunity</th><th>Stock risks</th><th>Core gaps</th></tr></thead>
                <tbody>
                {% for row in fineline_rows %}
                    <tr>
                        <td><strong>{{ row.fineline }}</strong></td>
                        <td>{{ row.sales_ty }}</td>
                        <td>{{ row.sales_ly }}</td>
                        <td class="{% if row.change_raw >= 0 %}number-positive{% else %}number-negative{% endif %}">{{ row.change_pct }}</td>
                        <td>{{ row.opp_gap }}</td>
                        <td>{{ row.stock_risks }}</td>
                        <td>{{ row.core_gaps }}</td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="panel">
        <h2>Category Rankings — This Store vs Rest of the Country</h2>
        <p class="section-note">Where this store sits nationally within each fineline it sells, based on Store Sales Value TY from the ranking report.</p>
        <div class="table-wrap">
            <table class="finder-table">
                <thead><tr><th>Fineline</th><th>National Rank</th><th>Percentile</th><th>Sales TY</th></tr></thead>
                <tbody>
                {% for row in category_ranks %}
                    <tr>
                        <td><strong>{{ row.fineline }}</strong></td>
                        <td>{{ row.rank_display }}</td>
                        <td>{{ row.percentile_display }}</td>
                        <td>{{ row.sales_ty_display }}</td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="panel">
        <h2>Fix Now — Stock and Core Compliance</h2>
        <p class="section-note">Items with no stock and demonstrated demand, plus core products that are not currently available.</p>
        <div class="table-wrap">
            <table class="finder-table">
                <thead><tr><th>Priority</th><th>Product</th><th>Code</th><th>Fineline</th><th>SOH</th><th>On order</th><th>Demand evidence</th><th>Recommended action</th></tr></thead>
                <tbody>
                {% for row in fix_now_rows %}
                    <tr>
                        <td><span class="severity {{ row.severity_class }}">{{ row.priority }}</span></td>
                        <td><strong>{{ row.product }}</strong></td>
                        <td>{{ row.code }}</td>
                        <td>{{ row.fineline }}</td>
                        <td>{{ row.soh }}</td>
                        <td>{{ row.on_order }}</td>
                        <td>{{ row.demand }}</td>
                        <td>{{ row.action }}</td>
                        <td><a class="view-link" href="{{ url_for('new_action', store=store_name, title=row.product, detail=row.action, priority=row.priority, source='Stock / Core Gap') }}">Create Action</a></td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="panel">
        <h2>Grow — Ranking Opportunities</h2>
        <p class="section-note">Positive opportunity gaps from the ranking report, ordered by estimated sales value.</p>
        <div class="table-wrap">
            <table class="finder-table">
                <thead><tr><th>Fineline</th><th>Store sales</th><th>Last year</th><th>Opportunity value</th><th>Opportunity units</th><th>Suggested discussion</th><th></th></tr></thead>
                <tbody>
                {% for row in ranking_rows %}
                    <tr><td><strong>{{ row.fineline }}</strong></td><td>{{ row.sales_ty }}</td><td>{{ row.sales_ly }}</td><td class="number-positive">{{ row.opp_value }}</td><td>{{ row.opp_units }}</td><td>{{ row.action }}</td><td><a class="view-link" href="{{ url_for('new_action', store=store_name, title=row.fineline, detail=row.action, priority='Medium', source='Ranking Opportunity') }}">Create Action</a></td></tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="panel">
        <h2>Protect and Review</h2>
        <div class="four-upload-grid">
            <div>
                <h3>Protect strong growth</h3>
                <table class="finder-table">
                    <thead><tr><th>Fineline</th><th>YoY</th><th>Action</th></tr></thead>
                    <tbody>{% for row in growth_rows %}<tr><td><strong>{{ row.fineline }}</strong></td><td class="number-positive">{{ row.change_pct }}</td><td>{{ row.action }}</td></tr>{% endfor %}</tbody>
                </table>
            </div>
            <div>
                <h3>Review excess cover</h3>
                <table class="finder-table">
                    <thead><tr><th>Product</th><th>SOH</th><th>WOC</th><th>Action</th></tr></thead>
                    <tbody>{% for row in overstock_rows %}<tr><td><strong>{{ row.product }}</strong><br><span class="muted">{{ row.code }}</span></td><td>{{ row.soh }}</td><td>{{ row.woc }}</td><td>{{ row.action }}</td></tr>{% endfor %}</tbody>
                </table>
            </div>
        </div>
    </div>

    <div class="panel">
        <h2>Reports Used</h2>
        <div class="file-details">
            {% for file in uploaded_files %}<div class="file-detail"><strong>{{ file.label }}</strong><span>{{ file.name }}</span></div>{% endfor %}
        </div>
    </div>
    {% endif %}
    {% endif %}


    {% if view == 'visit_brief' %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>{{ store_name }} — Store Visit Brief</h2>
                <p>Customer-friendly talking points · Reports exported {{ export_date }}</p>
            </div>
            <div class="toolbar">
                <a class="button-link secondary" href="{{ url_for('saved_opportunity', analysis_id=analysis_id) }}">Full Analysis</a>
                <a class="button-link secondary" href="{{ url_for('opportunity_library') }}">Store Library</a>
                <button type="button" class="secondary-button" onclick="window.print()">Print / Save as PDF</button>
            </div>
        </div>
        <div class="opportunity-grid">
            <div class="opportunity-card"><div class="label">Sales this year</div><div class="value">{{ total_sales_ty }}</div></div>
            <div class="opportunity-card {% if total_change_raw >= 0 %}positive{% else %}urgent{% endif %}"><div class="label">Year-on-year</div><div class="value">{{ total_change_pct }}</div></div>
            <div class="opportunity-card urgent"><div class="label">Immediate stock risks</div><div class="value">{{ urgent_stock_count }}</div></div>
            <div class="opportunity-card warning"><div class="label">Core gaps</div><div class="value">{{ core_gap_count }}</div></div>
        </div>
    </div>

    <div class="panel">
        <h2>Top Store Discussion Points</h2>
        <div class="insight-list">
            {% for item in conversation_plan[:3] %}
            <div class="insight-card {{ item.card_class }}">
                <h3>{{ loop.index }}. {{ item.title }}</h3>
                <p><strong>What the data says:</strong> {{ item.evidence }}</p>
                <p><strong>Suggested action:</strong> {{ item.action }}</p>
                <a class="view-link" href="{{ url_for('new_action', store=store_name, title=item.title, detail=item.action, priority='High' if item.card_class == 'urgent' else 'Medium') }}">Create Action</a>
            </div>
            {% endfor %}
        </div>
    </div>

    <div class="panel">
        <h2>Priority Products to Fix</h2>
        <p class="section-note">The highest-priority stock and core-range issues to discuss today.</p>
        <div class="table-wrap">
            <table class="finder-table">
                <thead><tr><th>Priority</th><th>Product</th><th>Fineline</th><th>SOH</th><th>On order</th><th>Evidence</th><th>Action</th></tr></thead>
                <tbody>
                {% for row in fix_now_rows[:10] %}
                    <tr><td><span class="severity {{ row.severity_class }}">{{ row.priority }}</span></td><td><strong>{{ row.product }}</strong></td><td>{{ row.fineline }}</td><td>{{ row.soh }}</td><td>{{ row.on_order }}</td><td>{{ row.demand }}</td><td>{{ row.action }}</td></tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </div>

    <div class="panel">
        <h2>Best Growth Opportunities</h2>
        <div class="table-wrap">
            <table class="finder-table">
                <thead><tr><th>Fineline</th><th>Current sales</th><th>Opportunity value</th><th>Suggested discussion</th></tr></thead>
                <tbody>{% for row in ranking_rows[:5] %}<tr><td><strong>{{ row.fineline }}</strong></td><td>{{ row.sales_ty }}</td><td class="number-positive">{{ row.opp_value }}</td><td>{{ row.action }}</td></tr>{% endfor %}</tbody>
            </table>
        </div>
    </div>
    {% endif %}

    {% if view == 'opportunity_library' %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>Saved Store Opportunities</h2>
                <p>Reopen full analyses, print visit briefs and replace a store's analysis whenever new reports are available.</p>
            </div>
            <a class="button-link" href="{{ url_for('opportunities') }}">Analyse Another Store</a>
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
    </div>
    {% if store_rows %}
    <div class="panel">
        <table class="history-table">
            <thead><tr><th>Store</th><th>Last analysed</th><th>Sales TY</th><th>YoY</th><th>Urgent stock</th><th>Core gaps</th><th>Opportunity</th><th>Actions</th></tr></thead>
            <tbody>
            {% for row in store_rows %}
                <tr>
                    <td><a class="month-link" href="{{ url_for('saved_opportunity', analysis_id=row.id) }}">{{ row.store_name }}</a></td>
                    <td class="small">{{ row.saved_display }}</td>
                    <td>{{ row.total_sales_ty }}</td>
                    <td><span class="change-badge {% if row.total_change_raw >= 0 %}change-positive{% else %}change-negative{% endif %}">{{ row.total_change_pct }}</span></td>
                    <td>{{ row.urgent_stock_count }}</td><td>{{ row.core_gap_count }}</td><td>{{ row.total_opp_gap }}</td>
                    <td class="action-cell">
                        <a class="view-link" href="{{ url_for('saved_opportunity', analysis_id=row.id) }}">Full Dashboard</a>
                        <a class="view-link" href="{{ url_for('visit_brief', analysis_id=row.id) }}">Visit Brief</a>
                        <form method="post" action="{{ url_for('delete_opportunity', analysis_id=row.id) }}" onsubmit="return confirm('Delete this saved store analysis?');"><button class="danger" type="submit">Delete</button></form>
                    </td>
                </tr>
            {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}<div class="panel empty">No store opportunity analyses have been saved yet.</div>{% endif %}
    {% endif %}

    {% if view == 'actions' %}
    <div class="panel">
        <div class="heading-row">
            <div><h2>Store Action Tracker</h2><p>Track opportunities from discovery through to completion.</p></div>
            <a class="button-link" href="{{ url_for('new_action') }}">Create Manual Action</a>
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <div class="summary-grid">
            <div class="summary-card urgent"><div class="label">Open</div><div class="value">{{ action_counts.open }}</div></div>
            <div class="summary-card warning"><div class="label">In Progress</div><div class="value">{{ action_counts.in_progress }}</div></div>
            <div class="summary-card"><div class="label">Waiting on Store</div><div class="value">{{ action_counts.waiting }}</div></div>
            <div class="summary-card positive"><div class="label">Completed</div><div class="value">{{ action_counts.completed }}</div></div>
        </div>
    </div>
    <div class="panel">
        <form method="get" action="{{ url_for('actions') }}" class="file-details">
            <div class="file-detail"><strong>Store</strong><select name="store"><option value="">All stores</option>{% for store in action_stores %}<option value="{{ store }}" {% if store == selected_store %}selected{% endif %}>{{ store }}</option>{% endfor %}</select></div>
            <div class="file-detail"><strong>Status</strong><select name="status"><option value="">All statuses</option>{% for status in action_statuses %}<option value="{{ status }}" {% if status == selected_status %}selected{% endif %}>{{ status }}</option>{% endfor %}</select></div>
            <div class="button-row"><button type="submit">Apply Filters</button></div>
        </form>
    </div>
    {% if action_rows %}
    <div class="panel"><div class="table-wrap"><table class="history-table">
        <thead><tr><th>Priority</th><th>Store</th><th>Action</th><th>Source</th><th>Due</th><th>Status</th><th>Notes / Outcome</th><th></th></tr></thead>
        <tbody>{% for row in action_rows %}<tr>
            <td><span class="severity {{ row.priority_class }}">{{ row.priority }}</span></td><td><strong>{{ row.store_name }}</strong></td><td><strong>{{ row.title }}</strong><br><span class="small">{{ row.detail }}</span></td><td>{{ row.source }}</td><td>{{ row.due_display }}</td>
            <td><form method="post" action="{{ url_for('update_action', action_id=row.id) }}"><select name="status" onchange="this.form.submit()">{% for status in action_statuses %}<option value="{{ status }}" {% if status == row.status %}selected{% endif %}>{{ status }}</option>{% endfor %}</select></form></td>
            <td>{{ row.notes or '—' }}</td><td class="action-cell"><a class="view-link" href="{{ url_for('edit_action', action_id=row.id) }}">Edit</a><form method="post" action="{{ url_for('delete_action', action_id=row.id) }}" onsubmit="return confirm('Delete this action?');"><button class="danger" type="submit">Delete</button></form></td>
        </tr>{% endfor %}</tbody>
    </table></div></div>
    {% else %}<div class="panel empty">No actions match the selected filters.</div>{% endif %}
    {% endif %}

    {% if view == 'action_form' %}
    <div class="panel">
        <div class="heading-row"><div><h2>{{ form_heading }}</h2><p>Create a follow-up from a store opportunity or add one manually.</p></div><a class="button-link secondary" href="{{ url_for('actions') }}">Back to Actions</a></div>
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="post" class="action-entry-form">
            <div class="form-grid">
                <label>Store<input type="text" name="store_name" value="{{ action_form.store_name }}" required></label>
                <label>Priority<select name="priority">{% for option in ['High','Medium','Low'] %}<option value="{{ option }}" {% if action_form.priority == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                <label>Action title<input type="text" name="title" value="{{ action_form.title }}" required></label>
                <label>Source<input type="text" name="source" value="{{ action_form.source }}"></label>
                <label>Due date<input type="date" name="due_date" value="{{ action_form.due_date }}"></label>
                <label>Status<select name="status">{% for status in action_statuses %}<option value="{{ status }}" {% if action_form.status == status %}selected{% endif %}>{{ status }}</option>{% endfor %}</select></label>
            </div>
            <label>Details<textarea name="detail" rows="4">{{ action_form.detail }}</textarea></label>
            <label>Notes / outcome<textarea name="notes" rows="4">{{ action_form.notes }}</textarea></label>
            <div class="button-row"><button type="submit">{{ submit_label }}</button></div>
        </form>
    </div>
    {% endif %}

    {% if view == 'follow_up' %}
    <div class="panel">
        <div class="heading-row">
            <div><h2>After Trip Close-Out{% if active_trip %} — {{ active_trip.trip_name }}{% endif %}</h2><p>Finish the trip in order: confirm visits, bring in notes, create follow-ups, then clear the remaining work.</p></div>
            <a class="button-link secondary" href="{{ url_for('on_the_road', trip_id=active_trip_id) if active_trip_id else url_for('on_the_road') }}">Review Visit Notes</a>
        </div>
        <div class="after-trip-progress">
            <div class="after-trip-step {% if after_trip_summary.completed_visits %}done{% else %}attention{% endif %}"><div class="number">1</div><h4>Confirm visits</h4><p>{{ after_trip_summary.completed_visits }} of {{ after_trip_summary.total_visits }} scheduled visits marked complete.</p></div>
            <div class="after-trip-step {% if after_trip_summary.notes_captured %}done{% else %}attention{% endif %}"><div class="number">2</div><h4>Collect notes</h4><p>{{ after_trip_summary.notes_captured }} scheduled visits have notes captured.</p></div>
            <div class="after-trip-step {% if parsed_notes or after_trip_summary.actions_today %}done{% else %}{% endif %}"><div class="number">3</div><h4>Create follow-ups</h4><p>Upload the completed Portatour report or load your road notes below.</p></div>
            <div class="after-trip-step {% if after_trip_summary.pending_visits == 0 and after_trip_summary.total_visits > 0 %}done{% endif %}"><div class="number">4</div><h4>Close out</h4><p>{{ after_trip_summary.pending_visits }} scheduled visits still need completion.</p></div>
        </div>
        <div class="closeout-grid">
            <div class="closeout-card"><div class="label">Scheduled Visits</div><div class="value">{{ after_trip_summary.total_visits }}</div></div>
            <div class="closeout-card good"><div class="label">Completed</div><div class="value">{{ after_trip_summary.completed_visits }}</div></div>
            <div class="closeout-card {% if after_trip_summary.pending_visits %}warn{% else %}good{% endif %}"><div class="label">Still Open</div><div class="value">{{ after_trip_summary.pending_visits }}</div></div>
            <div class="closeout-card"><div class="label">Actions Added Today</div><div class="value">{{ after_trip_summary.actions_today }}</div></div>
        </div>
    </div>

    {% if after_trip_visits %}
    <div class="panel">
        <div class="heading-row"><div><h2>Captured Visit Notes</h2><p>Calls and notes saved against the selected trip.</p></div><a class="button-link" href="{{ url_for('follow_up', load_road_notes=1, trip_id=active_trip_id) if active_trip_id else url_for('follow_up', load_road_notes=1) }}">Load Road Notes Below</a></div>
        {% for visit in after_trip_visits %}<div class="visit-closeout-row">
            <div><strong>{{ visit.date_display }}</strong><br><span class="small">{{ visit.visit_time }}</span></div>
            <div><strong>{{ visit.store_name }}</strong>{% if visit.visit_notes %}<div class="visit-closeout-note">{{ visit.visit_notes }}</div>{% else %}<div class="small">No notes captured.</div>{% endif %}</div>
            <div><span class="change-badge {% if visit.completed %}change-positive{% else %}change-neutral{% endif %}">{% if visit.completed %}Complete{% else %}Open{% endif %}</span></div>
        </div>{% endfor %}
    </div>
    {% endif %}

    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>Customer Follow-Up Assistant</h2>
                <p>Paste call notes from one or several customer visits. The assistant separates notes, suggests actions and prepares editable email drafts.</p>
            </div>
            <a class="button-link secondary" href="{{ url_for('actions') }}">Open Action Tracker</a>
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if error %}<div class="error">{{ error }}</div>{% endif %}
        <form method="post" action="{{ url_for('follow_up', trip_id=active_trip_id) if active_trip_id else url_for('follow_up') }}" enctype="multipart/form-data">
            {% if active_trip_id %}<input type="hidden" name="trip_id" value="{{ active_trip_id }}">{% endif %}
            <div class="upload-box" style="margin-bottom: 22px;">
                <h3>Upload a Portatour call report</h3>
                <p class="small">Upload the HTML report saved from Portatour. The assistant reads the Customer call reports section and pulls out only the dash items under Actions:. Items marked DONE, EMAILED, SENT or COMPLETED are skipped; DNT items remain outstanding.</p>
                <input type="file" name="portatour_report" accept=".html,.htm,text/html">
            </div>
            <label for="call_notes"><strong>Or paste call notes</strong></label>
            <p class="small">You can use either option, or both together. For multiple pasted visits, separate each customer with a blank line or a line containing <strong>---</strong>.</p>
            <textarea id="call_notes" class="followup-textarea" name="call_notes" placeholder="Store: Mitre 10 Hamilton\n\nActions:\n- Send Nigel the PA door brochure and pricing\n- Follow up on the Armorsteel quote next Friday\n\nNotes:\nGood discussion with the trade team...">{{ raw_notes or '' }}</textarea>
            <div class="button-row"><button type="submit">Extract Follow-Ups</button></div>
        </form>
    </div>

    {% if followup_items %}
    <div class="panel">
        <div class="heading-row">
            <div><h2>Review Suggested Actions</h2><p>Edit anything below, untick items you do not want, then save the approved actions.</p></div>
            <span class="change-badge change-positive">{{ followup_items|length }} suggested</span>
        </div>
        <form method="post" action="{{ url_for('save_followups') }}">
            {% if active_trip_id %}<input type="hidden" name="trip_id" value="{{ active_trip_id }}">{% endif %}
            <input type="hidden" name="raw_notes" value="{{ raw_notes }}">
            <input type="hidden" name="item_count" value="{{ followup_items|length }}">
            <div class="review-toolbar">
                <button type="button" class="secondary" onclick="setAllFollowups(true)">Select all new actions</button>
                <button type="button" class="secondary" onclick="setAllFollowups(false)">Clear all</button>
                <span class="small">Possible duplicates are highlighted and left unticked.</span>
            </div>
            {% set ns = namespace(last_store='') %}
            {% for item in followup_items %}
            {% if item.store != ns.last_store %}
                <div class="store-review-heading">{{ item.store }}</div>
                {% set ns.last_store = item.store %}
            {% endif %}
            <div class="followup-card {% if item.armoursteel %}armoursteel{% endif %} {% if item.is_duplicate %}duplicate{% endif %}">
                <div class="check-row">
                    <input class="followup-selector" type="checkbox" id="selected_{{ loop.index0 }}" name="selected_{{ loop.index0 }}" {% if not item.is_duplicate %}checked{% endif %}>
                    <label for="selected_{{ loop.index0 }}"><strong>Save this action</strong></label>
                    {% if item.armoursteel %}<span class="tag-purple">Armorsteel opportunity</span>{% endif %}
                    {% if item.is_duplicate %}<span class="change-badge change-neutral">Possible duplicate</span>{% endif %}
                </div>
                {% if item.is_duplicate %}<div class="duplicate-warning">An incomplete action with the same store and title already exists in the Action Tracker. Tick it only if you intentionally want another copy.</div>{% endif %}
                <div class="followup-grid">
                    <label>Store / customer<input class="review-input" type="text" name="store_{{ loop.index0 }}" value="{{ item.store }}" required></label>
                    <label>Action title<input class="review-input" type="text" name="title_{{ loop.index0 }}" value="{{ item.title }}" required></label>
                    <label>Priority<select name="priority_{{ loop.index0 }}">{% for option in ['High','Medium','Low'] %}<option value="{{ option }}" {% if item.priority == option %}selected{% endif %}>{{ option }}</option>{% endfor %}</select></label>
                    <label>Due date<input class="review-input" type="date" name="due_{{ loop.index0 }}" value="{{ item.due_date }}"></label>
                </div>
                <label><strong>Action detail</strong><textarea class="review-textarea" name="detail_{{ loop.index0 }}">{{ item.detail }}</textarea></label>
                <input type="hidden" name="armoursteel_{{ loop.index0 }}" value="{% if item.armoursteel %}1{% else %}0{% endif %}">
                <input type="hidden" name="source_{{ loop.index0 }}" value="{{ item.source or 'Follow-Up Assistant' }}">
                {% if item.contact_email %}<p class="small"><strong>Suggested contact:</strong> {{ item.contact_email }}</p>{% endif %}
                {% if item.email_draft %}
                <div class="draft-box">
                    <label>Email draft<textarea class="review-textarea" id="draft_{{ loop.index0 }}" name="draft_{{ loop.index0 }}">{{ item.email_draft }}</textarea></label>
                    <button type="button" class="copy-button" onclick="copyDraft('draft_{{ loop.index0 }}', this)">Copy Draft</button>
                </div>
                {% endif %}
            </div>
            {% endfor %}
            <div class="button-row"><button type="submit">Save Approved Actions</button></div>
        </form>
    </div>
    <script>
    function setAllFollowups(selected) {
        document.querySelectorAll('.followup-selector').forEach((box) => {
            if (selected) {
                const duplicateCard = box.closest('.followup-card').classList.contains('duplicate');
                box.checked = !duplicateCard;
            } else {
                box.checked = false;
            }
        });
    }

    function copyDraft(id, button) {
        const field = document.getElementById(id);
        navigator.clipboard.writeText(field.value).then(() => {
            const oldText = button.textContent;
            button.textContent = 'Copied';
            setTimeout(() => button.textContent = oldText, 1200);
        });
    }
    </script>
    {% elif parsed_notes %}
    <div class="panel empty">No clear follow-up actions were found. Add an <strong>Actions:</strong> section or use action phrases such as “send”, “follow up”, “check”, “arrange” or “contact”.</div>
    {% endif %}
    {% endif %}

    {% if view == 'history' %}
    <div class="panel">
        <div class="heading-row">
            <div>
                <h2>Saved Monthly History</h2>
                <p>Your saved figures remain in <strong>sales_history.db</strong> beside app.py.</p>
            </div>
            <form method="get" action="{{ url_for('history') }}">
                <select name="year" onchange="this.form.submit()">
                    {% for year_option in year_options %}
                        <option value="{{ year_option }}" {% if year_option == selected_year %}selected{% endif %}>{{ year_option }}</option>
                    {% endfor %}
                </select>
            </form>
        </div>
    </div>

    {% if history_rows %}
    <div class="panel">
        <h2>{{ selected_year }} Year-to-Date</h2>
        <div class="summary-grid">
            <div class="summary-card"><div class="label">YTD Actual</div><div class="value">{{ ytd_actual_display }}</div></div>
            <div class="summary-card"><div class="label">YTD Last Year / Target</div><div class="value">{{ ytd_target_display }}</div></div>
            <div class="summary-card {{ ytd_class }}"><div class="label">YTD Difference</div><div class="value">{{ ytd_difference_display }}</div></div>
            <div class="summary-card {{ ytd_class }}"><div class="label">YTD Percentage</div><div class="value">{{ ytd_percentage_display }}</div></div>
        </div>
    </div>

    <div class="panel">
        <h2>Monthly Trends</h2>
        <p class="small">Charts update automatically whenever you save or replace a month.</p>

        <div class="chart-grid">
            <div class="chart-card">
                <h3>Monthly Actual vs Target</h3>
                <p>Compares each saved month with the matching prior-year result.</p>
                <div class="chart-legend">
                    <span class="legend-item"><span class="legend-swatch swatch-actual"></span>Actual</span>
                    <span class="legend-item"><span class="legend-swatch swatch-target"></span>Target / last year</span>
                </div>
                <div class="chart-wrap"><canvas id="monthlyChart"></canvas></div>
            </div>

            <div class="chart-card">
                <h3>Cumulative Year-to-Date</h3>
                <p>Shows whether cumulative sales are pulling ahead of or behind target.</p>
                <div class="chart-legend">
                    <span class="legend-item"><span class="legend-swatch swatch-actual"></span>YTD actual</span>
                    <span class="legend-item"><span class="legend-swatch swatch-target"></span>YTD target</span>
                </div>
                <div class="chart-wrap"><canvas id="cumulativeChart"></canvas></div>
            </div>

            <div class="chart-card full">
                <div class="chart-controls">
                    <div>
                        <h3>Category Trend</h3>
                        <p>Choose a product category to compare its monthly sales with last year.</p>
                    </div>
                    <select id="categorySelect">
                        {% for category in category_options %}
                            <option value="{{ category }}">{{ category }}</option>
                        {% endfor %}
                    </select>
                </div>
                <div class="chart-legend">
                    <span class="legend-item"><span class="legend-swatch swatch-actual"></span>This year</span>
                    <span class="legend-item"><span class="legend-swatch swatch-target"></span>Last year</span>
                </div>
                <div class="chart-wrap tall"><canvas id="categoryChart"></canvas></div>
            </div>
        </div>
    </div>

    <script>
    (() => {
        const labels = {{ chart_labels | tojson }};
        const actual = {{ chart_actual | tojson }};
        const target = {{ chart_target | tojson }};
        const cumulativeActual = {{ chart_cumulative_actual | tojson }};
        const cumulativeTarget = {{ chart_cumulative_target | tojson }};
        const categoryData = {{ category_chart_data | tojson }};

        const money = value => new Intl.NumberFormat('en-NZ', {
            style: 'currency', currency: 'NZD', maximumFractionDigits: 0
        }).format(value);

        function setupCanvas(canvas) {
            const rect = canvas.getBoundingClientRect();
            const dpr = window.devicePixelRatio || 1;
            canvas.width = Math.max(1, Math.floor(rect.width * dpr));
            canvas.height = Math.max(1, Math.floor(rect.height * dpr));
            const ctx = canvas.getContext('2d');
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            return {ctx, width: rect.width, height: rect.height};
        }

        function drawAxes(ctx, width, height, maxValue, padding) {
            ctx.clearRect(0, 0, width, height);
            ctx.font = '12px Arial';
            ctx.fillStyle = '#667085';
            ctx.strokeStyle = '#e4e7ec';
            ctx.lineWidth = 1;
            const steps = 4;
            for (let i = 0; i <= steps; i++) {
                const y = padding.top + (height - padding.top - padding.bottom) * i / steps;
                const value = maxValue * (1 - i / steps);
                ctx.beginPath();
                ctx.moveTo(padding.left, y);
                ctx.lineTo(width - padding.right, y);
                ctx.stroke();
                ctx.textAlign = 'right';
                ctx.textBaseline = 'middle';
                ctx.fillText(money(value), padding.left - 8, y);
            }
        }

        function drawBars(canvas, dataA, dataB) {
            if (!canvas || !labels.length) return;
            const {ctx, width, height} = setupCanvas(canvas);
            const padding = {left: 82, right: 18, top: 15, bottom: 44};
            const maxValue = Math.max(...dataA, ...dataB, 1) * 1.12;
            drawAxes(ctx, width, height, maxValue, padding);
            const plotWidth = width - padding.left - padding.right;
            const plotHeight = height - padding.top - padding.bottom;
            const groupWidth = plotWidth / labels.length;
            const barWidth = Math.min(34, groupWidth * 0.30);
            labels.forEach((label, index) => {
                const centre = padding.left + groupWidth * (index + 0.5);
                const values = [dataA[index] || 0, dataB[index] || 0];
                const colours = ['#246fe5', '#98a2b3'];
                values.forEach((value, j) => {
                    const barHeight = value / maxValue * plotHeight;
                    const x = centre + (j === 0 ? -barWidth - 2 : 2);
                    const y = padding.top + plotHeight - barHeight;
                    ctx.fillStyle = colours[j];
                    ctx.fillRect(x, y, barWidth, barHeight);
                });
                ctx.fillStyle = '#475467';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'top';
                ctx.fillText(label.slice(0, 3), centre, height - padding.bottom + 12);
            });
        }

        function drawLines(canvas, dataA, dataB) {
            if (!canvas || !labels.length) return;
            const {ctx, width, height} = setupCanvas(canvas);
            const padding = {left: 82, right: 22, top: 18, bottom: 44};
            const maxValue = Math.max(...dataA, ...dataB, 1) * 1.10;
            drawAxes(ctx, width, height, maxValue, padding);
            const plotWidth = width - padding.left - padding.right;
            const plotHeight = height - padding.top - padding.bottom;
            const xFor = i => labels.length === 1
                ? padding.left + plotWidth / 2
                : padding.left + plotWidth * i / (labels.length - 1);
            const yFor = value => padding.top + plotHeight - (value / maxValue * plotHeight);
            const series = [
                {data: dataA, colour: '#246fe5'},
                {data: dataB, colour: '#98a2b3'}
            ];
            series.forEach(item => {
                ctx.beginPath();
                item.data.forEach((value, i) => {
                    const x = xFor(i);
                    const y = yFor(value || 0);
                    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
                });
                ctx.strokeStyle = item.colour;
                ctx.lineWidth = 3;
                ctx.stroke();
                item.data.forEach((value, i) => {
                    ctx.beginPath();
                    ctx.arc(xFor(i), yFor(value || 0), 4, 0, Math.PI * 2);
                    ctx.fillStyle = item.colour;
                    ctx.fill();
                });
            });
            labels.forEach((label, i) => {
                ctx.fillStyle = '#475467';
                ctx.textAlign = 'center';
                ctx.textBaseline = 'top';
                ctx.fillText(label.slice(0, 3), xFor(i), height - padding.bottom + 12);
            });
        }

        function renderAll() {
            drawBars(document.getElementById('monthlyChart'), actual, target);
            drawLines(document.getElementById('cumulativeChart'), cumulativeActual, cumulativeTarget);
            const selected = document.getElementById('categorySelect').value;
            const data = categoryData[selected] || {current: [], previous: []};
            drawLines(document.getElementById('categoryChart'), data.current, data.previous);
        }

        document.getElementById('categorySelect').addEventListener('change', renderAll);
        window.addEventListener('resize', renderAll);
        renderAll();
    })();
    </script>

    <div class="panel">
        <table class="history-table">
            <thead>
                <tr>
                    <th>Month</th>
                    <th>Actual</th>
                    <th>Last Year / Target</th>
                    <th>Difference</th>
                    <th>Change</th>
                    <th>Saved</th>
                    <th></th>
                </tr>
            </thead>
            <tbody>
                {% for row in history_rows %}
                <tr>
                    <td><a class="month-link" href="{{ url_for('saved_month', record_id=row.id) }}">{{ row.month_name }}</a></td>
                    <td>{{ row.actual_display }}</td>
                    <td>{{ row.target_display }}</td>
                    <td>{{ row.difference_display }}</td>
                    <td><span class="change-badge {{ row.change_class }}">{{ row.percentage_display }}</span></td>
                    <td class="small">{{ row.saved_display }}</td>
                    <td class="action-cell">
                        <a class="view-link" href="{{ url_for('saved_month', record_id=row.id) }}">View Dashboard</a>
                        <form method="post" action="{{ url_for('delete_month', record_id=row.id) }}" onsubmit="return confirm('Delete this saved month?');">
                            <button type="submit" class="danger">Delete</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
    {% else %}
    <div class="panel empty">No saved months found for {{ selected_year }}.</div>
    {% endif %}
    {% endif %}
</div>
</body>
</html>
"""



WEEKLY_CHECKLIST_ITEMS = [
    ("sales_meeting", "Update sales-meeting figures: visits, training sessions and key activity"),
    ("todos", "Review and clear outstanding to-dos"),
    ("issues", "Log issues and discussion points for the sales meeting"),
    ("inbox", "Bring the inbox up to date"),
    ("appointments", "Confirm this week's customer appointments and travel plan"),
    ("materials", "Confirm brochures, signage, samples and tools are packed"),
    ("store_briefs", "Review open actions and opportunity briefs for each planned store"),
]


def monday_for(value):
    return value - timedelta(days=value.weekday())


def parse_schedule_appointments(html_text, from_date=None):
    if not html_text or "<html" not in html_text.lower():
        return []
    soup = BeautifulSoup(html_text, "html.parser")
    results = []
    threshold = from_date or date.today()
    for day_div in soup.select("div.scheduleday"):
        class_date = next((c for c in day_div.get("class", []) if c.startswith("scheduleday_") and c != "scheduleday"), "")
        try:
            appointment_date = datetime.strptime(class_date.replace("scheduleday_", ""), "%Y%m%d").date()
        except ValueError:
            continue
        if appointment_date < threshold:
            continue
        for row in day_div.select("tr.stop"):
            reservation = row.select_one("td.reservation")
            if not reservation or "appointment" not in reservation.get_text(" ", strip=True).lower():
                continue
            display = row.select_one("td.displayname")
            if not display:
                continue
            direct_divs = display.find_all("div", recursive=False)
            store = direct_divs[0].get_text(" ", strip=True) if direct_divs else display.get_text(" ", strip=True)
            note_el = display.select_one("div.callnote")
            note = note_el.get_text(" ", strip=True).lstrip("⇒").strip() if note_el else ""
            time_el = row.select_one("td.time")
            time_text = time_el.get_text(" ", strip=True) if time_el else ""
            results.append({"date": appointment_date, "store": store, "time": time_text, "call_note": note})
    results.sort(key=lambda x: (x["date"], x["time"], x["store"].lower()))
    return results



def classify_customer(store_name):
    lowered = (store_name or "").lower()
    if "bunnings" in lowered:
        return "Bunnings"
    if "mitre 10 mega" in lowered or "mitre10 mega" in lowered:
        return "Mitre 10 Mega"
    if "mitre 10" in lowered or "mitre10" in lowered:
        return "Mitre 10"
    if re.search(r"\bitm\b", lowered):
        return "ITM"
    if "placemakers" in lowered or "place makers" in lowered:
        return "Placemakers"
    if "hammer hardware" in lowered:
        return "Hammer Hardware"
    if "buildlink" in lowered or "build link" in lowered:
        return "Buildlink"
    if "carters" in lowered:
        return "Carters"
    return "Other"


def default_brochure_quantities(store_name):
    customer_type = classify_customer(store_name)
    defaults = BROCHURE_DEFAULTS.get(customer_type, {})
    return {item: int(defaults.get(item, 0)) for item in BROCHURE_TYPES}


def infer_special_packing_items(call_note):
    note = (call_note or "").lower()
    items = []
    if "pa door" in note:
        items.append("PA Door sample, brochure and pricing")
    if "training" in note:
        items.append("Training materials, cards and relevant product samples")
    if "sign" in note or "signage" in note:
        items.append("Requested signage")
    if "greens brochure" in note:
        items.append("Greens brochures or carts information")
    if "aromachef" in note or "charcoal" in note:
        items.append("Aromachef brochure and product information")
    if "display shed" in note or "shed display" in note:
        items.append("Display shed information and current display options")
    if "sample" in note and "pa door" not in note:
        items.append("Requested product sample")
    return items


def _pdf_text(value):
    text = str(value or "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_packing_pdf(prep_rows, schedule_filename):
    """Generate a landscape PDF that mirrors the on-screen customer table."""
    buffer = BytesIO()
    page_size = landscape(A4)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        rightMargin=8 * mm,
        leftMargin=8 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="Trip Packing Sheet by Customer",
        author="Sales Assistant",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PackingTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=21,
        textColor=colors.HexColor("#17202A"),
        alignment=TA_LEFT,
        spaceAfter=2 * mm,
    )
    sub_style = ParagraphStyle(
        "PackingSub",
        parent=styles["Normal"],
        fontSize=7.5,
        leading=9.5,
        textColor=colors.HexColor("#667085"),
        spaceAfter=4 * mm,
    )
    store_style = ParagraphStyle(
        "PackingStore",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8.5,
        leading=10.5,
        textColor=colors.HexColor("#17202A"),
        spaceAfter=1,
    )
    meta_style = ParagraphStyle(
        "PackingMeta",
        parent=styles["BodyText"],
        fontSize=6.5,
        leading=8,
        textColor=colors.HexColor("#667085"),
    )
    note_style = ParagraphStyle(
        "PackingNote",
        parent=styles["BodyText"],
        fontSize=6.5,
        leading=8,
        textColor=colors.HexColor("#475467"),
        leftIndent=0,
        spaceBefore=1,
    )
    header_style = ParagraphStyle(
        "PackingHeader",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=6.5,
        leading=7.5,
        textColor=colors.HexColor("#17202A"),
        alignment=TA_CENTER,
    )
    number_style = ParagraphStyle(
        "PackingNumber",
        parent=styles["BodyText"],
        fontName="Helvetica-Bold",
        fontSize=8,
        leading=9,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17202A"),
    )

    generated = datetime.now()
    story = [
        Paragraph("Trip Packing Sheet by Customer", title_style),
        Paragraph(
            f"Generated {generated.day} {generated.strftime('%B %Y at %H:%M')}"
            + (f" | Portatour schedule: {_pdf_text(schedule_filename)}" if schedule_filename else ""),
            sub_style,
        ),
    ]

    table_data = [[
        Paragraph("Appointment", header_style),
        *[Paragraph(_pdf_text(item).replace(" ", "<br/>") if item in {"Cedar Classic", "Cedar FJ"} else _pdf_text(item), header_style) for item in BROCHURE_TYPES],
        Paragraph("Total", header_style),
    ]]

    for row in prep_rows:
        appointment_parts = [
            Paragraph(_pdf_text(row.get("store", "")), store_style),
            Paragraph(
                f"{_pdf_text(row.get('date_display', ''))} | {_pdf_text(row.get('time', ''))} | {_pdf_text(row.get('customer_type', ''))}",
                meta_style,
            ),
        ]
        appointment_cell = Table(
            [[part] for part in appointment_parts],
            colWidths=[73 * mm],
            style=TableStyle([
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]),
        )

        quantity_cells = [
            Paragraph(str(int(row.get("quantities", {}).get(item, 0))), number_style)
            for item in BROCHURE_TYPES
        ]
        total = int(row.get("total", sum(int(row.get("quantities", {}).get(item, 0)) for item in BROCHURE_TYPES)))
        table_data.append([
            appointment_cell,
            *quantity_cells,
            Paragraph(str(total), number_style),
        ])

    available_width = page_size[0] - 16 * mm
    appointment_width = 76 * mm
    total_width = 13 * mm
    qty_width = (available_width - appointment_width - total_width) / len(BROCHURE_TYPES)
    column_widths = [appointment_width] + [qty_width] * len(BROCHURE_TYPES) + [total_width]

    packing_table = Table(
        table_data,
        colWidths=column_widths,
        repeatRows=1,
        hAlign="LEFT",
    )
    packing_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F8FAFC")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#17202A")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("ALIGN", (1, 0), (-1, 0), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#D0D5DD")),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#98A2B3")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#FCFCFD")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 6),
    ]))
    story.append(packing_table)

    def add_page_number(canvas, document):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#667085"))
        canvas.drawString(8 * mm, 5.5 * mm, "Sales Assistant - Trip Preparation")
        canvas.drawRightString(page_size[0] - 8 * mm, 5.5 * mm, f"Page {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=add_page_number, onLaterPages=add_page_number)
    buffer.seek(0)
    filename = f"trip-packing-by-customer-{generated.strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )

def action_display_row(record):
    row = dict(record)
    row["due_display"] = datetime.strptime(row["due_date"], "%Y-%m-%d").strftime("%d %b %Y") if row.get("due_date") else "No date"
    row["priority_class"] = "critical" if row["priority"] == "High" else ("warning" if row["priority"] == "Medium" else "low")
    return row

def init_database():
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                salesperson TEXT NOT NULL,
                year INTEGER NOT NULL,
                month INTEGER NOT NULL,
                as_of_date TEXT NOT NULL,
                current_filename TEXT NOT NULL,
                previous_filename TEXT NOT NULL,
                current_total REAL NOT NULL,
                previous_total REAL NOT NULL,
                armorsteel_current REAL NOT NULL,
                armorsteel_previous REAL NOT NULL,
                greens_current REAL NOT NULL,
                greens_previous REAL NOT NULL,
                lifestyle_current REAL NOT NULL,
                lifestyle_previous REAL NOT NULL,
                nzwb_current REAL NOT NULL,
                nzwb_previous REAL NOT NULL,
                sheds_current REAL NOT NULL,
                sheds_previous REAL NOT NULL,
                saved_at TEXT NOT NULL,
                UNIQUE(salesperson, year, month)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS opportunity_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT NOT NULL UNIQUE,
                export_date TEXT NOT NULL,
                analysis_json TEXT NOT NULL,
                saved_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS follow_up_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                raw_notes TEXT NOT NULL,
                extracted_json TEXT NOT NULL,
                saved_action_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trip_schedules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                raw_html TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS store_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                priority TEXT NOT NULL DEFAULT 'Medium',
                due_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'Open',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_priorities (
                store_name TEXT PRIMARY KEY,
                growth_potential INTEGER NOT NULL DEFAULT 3,
                visit_frequency_days INTEGER NOT NULL DEFAULT 42,
                last_visit_date TEXT NOT NULL DEFAULT '',
                manual_sales_value REAL NOT NULL DEFAULT 0,
                strategic_notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS travel_trips (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_name TEXT NOT NULL,
                destination TEXT NOT NULL DEFAULT '',
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Planned',
                flight_status TEXT NOT NULL DEFAULT 'Not Required',
                rental_status TEXT NOT NULL DEFAULT 'Not Required',
                accommodation_status TEXT NOT NULL DEFAULT 'Not Required',
                forecast_flights REAL NOT NULL DEFAULT 0,
                actual_flights REAL NOT NULL DEFAULT 0,
                forecast_rental REAL NOT NULL DEFAULT 0,
                actual_rental REAL NOT NULL DEFAULT 0,
                forecast_fuel REAL NOT NULL DEFAULT 0,
                actual_fuel REAL NOT NULL DEFAULT 0,
                forecast_accommodation REAL NOT NULL DEFAULT 0,
                actual_accommodation REAL NOT NULL DEFAULT 0,
                forecast_other REAL NOT NULL DEFAULT 0,
                actual_other REAL NOT NULL DEFAULT 0,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS road_visit_notes (
                schedule_id INTEGER NOT NULL,
                visit_date TEXT NOT NULL,
                visit_time TEXT NOT NULL DEFAULT '',
                store_name TEXT NOT NULL,
                visit_notes TEXT NOT NULL DEFAULT '',
                completed INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (schedule_id, visit_date, visit_time, store_name)
            )
            """
        )
        road_note_columns = {row[1] for row in connection.execute("PRAGMA table_info(road_visit_notes)").fetchall()}
        for column_name, column_sql in [
            ("started_at", "TEXT NOT NULL DEFAULT ''"),
            ("finished_at", "TEXT NOT NULL DEFAULT ''"),
            ("actual_duration_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ("outcome", "TEXT NOT NULL DEFAULT ''"),
            ("next_step", "TEXT NOT NULL DEFAULT ''"),
            ("skipped", "INTEGER NOT NULL DEFAULT 0")
        ]:
            if column_name not in road_note_columns:
                connection.execute(f"ALTER TABLE road_visit_notes ADD COLUMN {column_name} {column_sql}")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS road_visit_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id INTEGER NOT NULL,
                visit_date TEXT NOT NULL,
                visit_time TEXT NOT NULL DEFAULT '',
                store_name TEXT NOT NULL,
                stored_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS trip_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trip_name TEXT NOT NULL,
                destination TEXT NOT NULL DEFAULT '',
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                travel_method TEXT NOT NULL DEFAULT 'Drive',
                notes TEXT NOT NULL DEFAULT '',
                flight_status TEXT NOT NULL DEFAULT 'Not Required',
                rental_status TEXT NOT NULL DEFAULT 'Not Required',
                accommodation_status TEXT NOT NULL DEFAULT 'Not Required',
                travel_notes TEXT NOT NULL DEFAULT '',
                travel_confirmed INTEGER NOT NULL DEFAULT 0,
                appointments_confirmed INTEGER NOT NULL DEFAULT 0,
                brochures_packed INTEGER NOT NULL DEFAULT 0,
                samples_packed INTEGER NOT NULL DEFAULT 0,
                briefs_reviewed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_checklist (
                week_key TEXT NOT NULL,
                item_key TEXT NOT NULL,
                is_done INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (week_key, item_key)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rep_bonus_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                salesperson TEXT NOT NULL,
                doc_number TEXT NOT NULL,
                txn_date TEXT NOT NULL,
                amount REAL NOT NULL,
                source_filename TEXT NOT NULL,
                uploaded_at TEXT NOT NULL,
                UNIQUE(salesperson, doc_number)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS appointment_objectives (
                appointment_id TEXT PRIMARY KEY,
                trip_plan_id INTEGER NOT NULL DEFAULT 0,
                schedule_id INTEGER NOT NULL DEFAULT 0,
                visit_date TEXT NOT NULL DEFAULT '',
                visit_time TEXT NOT NULL DEFAULT '',
                store_name TEXT NOT NULL DEFAULT '',
                desired_outcome TEXT NOT NULL DEFAULT '',
                preparation_notes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_name TEXT NOT NULL,
                contact_name TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS route_customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_id TEXT NOT NULL DEFAULT '',
                portatour_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL UNIQUE,
                street TEXT NOT NULL DEFAULT '', city TEXT NOT NULL DEFAULT '', zip TEXT NOT NULL DEFAULT '', country TEXT NOT NULL DEFAULT '',
                customer_group TEXT NOT NULL DEFAULT 'Other', categories TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
                call_note TEXT NOT NULL DEFAULT '', last_call TEXT NOT NULL DEFAULT '', call_interval INTEGER NOT NULL DEFAULT 42, urgency TEXT NOT NULL DEFAULT '',
                ideal_call_minutes INTEGER NOT NULL DEFAULT 45,
                priority_tier TEXT NOT NULL DEFAULT 'C',
                customer_open_time TEXT NOT NULL DEFAULT '08:00',
                customer_close_time TEXT NOT NULL DEFAULT '17:00',
                latitude REAL NOT NULL, longitude REAL NOT NULL, updated_at TEXT NOT NULL
            )
            """
        )
        trip_schedule_columns = {row[1] for row in connection.execute("PRAGMA table_info(trip_schedules)").fetchall()}
        if "trip_plan_id" not in trip_schedule_columns:
            connection.execute("ALTER TABLE trip_schedules ADD COLUMN trip_plan_id INTEGER")
        trip_plan_columns = {row[1] for row in connection.execute("PRAGMA table_info(trip_plans)").fetchall()}
        if "completed_at" not in trip_plan_columns:
            connection.execute("ALTER TABLE trip_plans ADD COLUMN completed_at TEXT NOT NULL DEFAULT ''")
        if "closeout_notes" not in trip_plan_columns:
            connection.execute("ALTER TABLE trip_plans ADD COLUMN closeout_notes TEXT NOT NULL DEFAULT ''")
        route_customer_columns = {row[1] for row in connection.execute("PRAGMA table_info(route_customers)").fetchall()}
        if "ideal_call_minutes" not in route_customer_columns:
            connection.execute("ALTER TABLE route_customers ADD COLUMN ideal_call_minutes INTEGER NOT NULL DEFAULT 45")
        if "priority_tier" not in route_customer_columns:
            connection.execute("ALTER TABLE route_customers ADD COLUMN priority_tier TEXT NOT NULL DEFAULT 'C'")
        if "customer_open_time" not in route_customer_columns:
            connection.execute("ALTER TABLE route_customers ADD COLUMN customer_open_time TEXT NOT NULL DEFAULT '08:00'")
        if "customer_close_time" not in route_customer_columns:
            connection.execute("ALTER TABLE route_customers ADD COLUMN customer_close_time TEXT NOT NULL DEFAULT '17:00'")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS route_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, start_date TEXT NOT NULL DEFAULT '', end_date TEXT NOT NULL DEFAULT '',
                customer_ids TEXT NOT NULL, created_at TEXT NOT NULL
            )
            """
        )
        ensure_reliability_tables(connection)
        existing_route_customers = connection.execute("SELECT COUNT(*) FROM route_customers").fetchone()[0]
        if existing_route_customers == 0:
            for item in json.loads(ROUTE_CUSTOMER_SEED):
                connection.execute(
                    """INSERT INTO route_customers (external_id,portatour_id,name,street,city,zip,country,customer_group,categories,phone,email,call_note,last_call,call_interval,urgency,ideal_call_minutes,priority_tier,latitude,longitude,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item['external_id'],item['portatour_id'],item['name'],item['street'],item['city'],item['zip'],item['country'],item['customer_group'],item['categories'],item['phone'],item['email'],item['call_note'],item['last_call'],item['call_interval'],item['urgency'],45,'C',item['latitude'],item['longitude'],datetime.now().isoformat(timespec='seconds'))
                )
        connection.commit()


def money(value):
    return f"${value:,.2f}"


def signed_money(value):
    if value > 0:
        return f"+${value:,.2f}"
    if value < 0:
        return f"-${abs(value):,.2f}"
    return "$0.00"


def percentage_change(current_value, previous_value):
    if previous_value == 0:
        return None if current_value != 0 else 0.0
    return ((current_value - previous_value) / previous_value) * 100


def percentage_display(value):
    if value is None:
        return "New"
    if value > 0:
        return f"+{value:.1f}%"
    return f"{value:.1f}%"


def change_class(value):
    if value is None or value > 0:
        return "change-positive"
    if value < 0:
        return "change-negative"
    return "change-neutral"


def summary_class(value):
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def parse_report_dates(report_period):
    pattern = (
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})"
        r"\s+-\s+"
        r"(\d{1,2}\s+[A-Za-z]+\s+\d{4})"
    )
    match = re.search(pattern, report_period)
    if not match:
        raise ValueError(
            "The current report's date range could not be identified. "
            f"Report text found: {report_period}"
        )
    start_date = datetime.strptime(match.group(1), "%d %B %Y").date()
    end_date = datetime.strptime(match.group(2), "%d %B %Y").date()
    return start_date, end_date


def count_weekdays(start_date, end_date):
    if end_date < start_date:
        return 0
    total = 0
    current_date = start_date
    while current_date <= end_date:
        if current_date.weekday() < 5:
            total += 1
        current_date += timedelta(days=1)
    return total


def calendar_quarter_bounds(year, quarter):
    start_month = (quarter - 1) * 3 + 1
    start = date(year, start_month, 1)
    end_month = start_month + 2
    end_year = year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    if end_month == 12:
        end = date(end_year, 12, 31)
    else:
        end = date(end_year, end_month + 1, 1) - timedelta(days=1)
    return start, end


def quarter_of_date(target_date):
    return (target_date.month - 1) // 3 + 1


def same_day_last_year(target_date):
    try:
        return target_date.replace(year=target_date.year - 1)
    except ValueError:
        # 29 Feb has no equivalent in a non-leap year; fall back a day.
        return target_date.replace(year=target_date.year - 1, day=28)


def read_rep_transactions_report(uploaded_file):
    filename = uploaded_file.filename
    file_bytes = uploaded_file.read()
    if not file_bytes:
        raise ValueError(f"{filename} is empty.")

    namespace = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    try:
        root = ET.fromstring(file_bytes)
    except ET.ParseError as exc:
        raise ValueError(
            f"{filename} could not be read as a NetSuite XML spreadsheet."
        ) from exc

    spreadsheet_index_name = "{urn:schemas-microsoft-com:office:spreadsheet}Index"
    worksheet_rows = root.findall(".//ss:Worksheet/ss:Table/ss:Row", namespace)
    if not worksheet_rows:
        raise ValueError(f"No spreadsheet rows were found in {filename}.")

    rows = []
    for row in worksheet_rows:
        row_values = []
        for cell in row.findall("ss:Cell", namespace):
            index_value = cell.get(spreadsheet_index_name)
            if index_value:
                target_index = int(index_value) - 1
                while len(row_values) < target_index:
                    row_values.append("")
            data = cell.find("ss:Data", namespace)
            value = "" if data is None or data.text is None else data.text.strip()
            row_values.append(value)
        rows.append(row_values)

    header_row_number = None
    for row_number, row in enumerate(rows):
        if row and str(row[0]).strip() == "Date":
            header_row_number = row_number
            break
    if header_row_number is None:
        raise ValueError(f'The transaction "Date" heading could not be found in {filename}.')

    headers = [str(value).strip() for value in rows[header_row_number]]
    try:
        date_index = headers.index("Date")
        doc_index = headers.index("Document Number")
        name_index = headers.index("Name")
        amount_index = headers.index("Sum of Amount")
    except ValueError as exc:
        raise ValueError(
            f"Expected columns (Date, Document Number, Name, Sum of Amount) were not found in {filename}."
        ) from exc

    transactions = []
    for row in rows[header_row_number + 1:]:
        if not row or not str(row[0]).strip():
            continue
        first_value = str(row[0]).strip()
        if first_value.startswith("Overall Total") or first_value.startswith("Total"):
            continue
        while len(row) < len(headers):
            row.append("")
        raw_date = row[date_index]
        try:
            txn_date = datetime.strptime(raw_date[:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        try:
            amount = float(row[amount_index]) if row[amount_index] not in ("", None) else 0.0
        except (TypeError, ValueError):
            amount = 0.0
        transactions.append({
            "doc_number": str(row[doc_index]).strip(),
            "name": str(row[name_index]).strip(),
            "date": txn_date,
            "amount": amount,
        })

    if not transactions:
        raise ValueError(f"No transaction rows could be read from {filename}.")
    return transactions


def read_sales_report(uploaded_file):
    filename = uploaded_file.filename
    file_bytes = uploaded_file.read()
    if not file_bytes:
        raise ValueError(f"{filename} is empty.")

    namespace = {"ss": "urn:schemas-microsoft-com:office:spreadsheet"}
    try:
        root = ET.fromstring(file_bytes)
    except ET.ParseError as exc:
        raise ValueError(
            f"{filename} could not be read as a NetSuite XML spreadsheet."
        ) from exc

    spreadsheet_index_name = "{urn:schemas-microsoft-com:office:spreadsheet}Index"
    worksheet_rows = root.findall(".//ss:Worksheet/ss:Table/ss:Row", namespace)
    if not worksheet_rows:
        raise ValueError(f"No spreadsheet rows were found in {filename}.")

    rows = []
    for row in worksheet_rows:
        row_values = []
        for cell in row.findall("ss:Cell", namespace):
            index_value = cell.get(spreadsheet_index_name)
            if index_value:
                target_index = int(index_value) - 1
                while len(row_values) < target_index:
                    row_values.append("")
            data = cell.find("ss:Data", namespace)
            value = "" if data is None or data.text is None else data.text.strip()
            row_values.append(value)
        rows.append(row_values)

    month_names = [
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    ]
    report_period = ""
    for row in rows:
        if row:
            first_value = str(row[0]).strip()
            if any(month in first_value for month in month_names):
                report_period = first_value
                break

    header_row_number = None
    for row_number, row in enumerate(rows):
        if row and str(row[0]).strip() == "Customer":
            header_row_number = row_number
            break
    if header_row_number is None:
        raise ValueError(f'The "Customer" heading could not be found in {filename}.')

    headers = [str(value).strip() for value in rows[header_row_number]]
    total_row = None
    for row in rows[header_row_number + 1:]:
        if row and str(row[0]).strip().startswith("Total - "):
            total_row = row
    if total_row is None:
        raise ValueError(f"The salesperson total row could not be found in {filename}.")

    while len(total_row) < len(headers):
        total_row.append("")
    record = dict(zip(headers, total_row))
    salesperson = str(record.get("Customer", "")).replace("Total - ", "", 1).strip()

    sales = {}
    for category in CATEGORIES:
        raw_value = record.get(category, 0)
        try:
            sales[category] = float(raw_value)
        except (TypeError, ValueError):
            sales[category] = 0.0

    return {
        "filename": filename,
        "salesperson": salesperson,
        "report_period": report_period,
        "sales": sales,
    }


def build_comparison(current_report, previous_report):
    rows = []
    for category in CATEGORIES:
        current_value = current_report["sales"].get(category, 0.0)
        previous_value = previous_report["sales"].get(category, 0.0)
        difference = current_value - previous_value
        percentage = percentage_change(current_value, previous_value)
        rows.append({
            "category": category,
            "current_display": money(current_value),
            "previous_display": money(previous_value),
            "difference_display": signed_money(difference),
            "percentage_display": percentage_display(percentage),
            "change_class": change_class(percentage),
        })
    return rows


def month_payload(current_report, previous_report, as_of_date):
    month_start, _ = parse_report_dates(current_report["report_period"])
    return {
        "salesperson": current_report["salesperson"],
        "year": month_start.year,
        "month": month_start.month,
        "as_of_date": as_of_date.isoformat(),
        "current_filename": current_report["filename"],
        "previous_filename": previous_report["filename"],
        "current_total": current_report["sales"]["Total"],
        "previous_total": previous_report["sales"]["Total"],
        "armorsteel_current": current_report["sales"]["Armorsteel"],
        "armorsteel_previous": previous_report["sales"]["Armorsteel"],
        "greens_current": current_report["sales"]["Greens WB"],
        "greens_previous": previous_report["sales"]["Greens WB"],
        "lifestyle_current": current_report["sales"]["NZ Lifestyle"],
        "lifestyle_previous": previous_report["sales"]["NZ Lifestyle"],
        "nzwb_current": current_report["sales"]["NZ WB"],
        "nzwb_previous": previous_report["sales"]["NZ WB"],
        "sheds_current": current_report["sales"]["Sheds"],
        "sheds_previous": previous_report["sales"]["Sheds"],
    }



@app.route("/")
def landing():
    return redirect(url_for("control_centre"))

@app.route("/sales-performance", methods=["GET", "POST"])
def home():
    selected_date = date.today().isoformat()
    template_data = {
        "view": "dashboard",
        "comparison_rows": None,
        "current_report": None,
        "previous_report": None,
        "error": request.args.get("error"),
        "success": request.args.get("success"),
        "selected_date": selected_date,
        "show_upload": True,
    }

    if request.method == "POST":
        current_file = request.files.get("current_report")
        previous_file = request.files.get("previous_report")
        selected_date = request.form.get("as_of_date", "")
        template_data["selected_date"] = selected_date

        if not current_file or not current_file.filename:
            template_data["error"] = "Please choose the this-year report."
        elif not previous_file or not previous_file.filename:
            template_data["error"] = "Please choose the matching last-year report."
        elif not selected_date:
            template_data["error"] = "Please choose the report's as-at date."
        else:
            try:
                current_report = read_sales_report(current_file)
                previous_report = read_sales_report(previous_file)
                if current_report["salesperson"] != previous_report["salesperson"]:
                    raise ValueError("The reports appear to belong to different salespeople.")

                as_of_date = datetime.strptime(selected_date, "%Y-%m-%d").date()
                month_start, month_end = parse_report_dates(current_report["report_period"])
                if as_of_date < month_start or as_of_date > month_end:
                    raise ValueError("The as-at date must fall within the current report month.")

                total_workdays = count_weekdays(month_start, month_end)
                elapsed_workdays = count_weekdays(month_start, as_of_date)
                remaining_workdays = count_weekdays(as_of_date + timedelta(days=1), month_end)
                current_total = current_report["sales"]["Total"]
                monthly_target = previous_report["sales"]["Total"]
                daily_target = monthly_target / total_workdays if total_workdays else 0.0
                mtd_target = daily_target * elapsed_workdays
                remaining_target = max(monthly_target - current_total, 0.0)
                required_daily = remaining_target / remaining_workdays if remaining_workdays else remaining_target
                performance_difference = current_total - mtd_target
                performance_percentage = percentage_change(current_total, mtd_target)
                monthly_progress = (current_total / monthly_target * 100) if monthly_target else 0.0
                progress_width = min(max(monthly_progress, 0.0), 100.0)
                as_of_display = f"{as_of_date.day} {as_of_date.strftime('%B %Y')}"
                payload = month_payload(current_report, previous_report, as_of_date)

                template_data.update({
                    "comparison_rows": build_comparison(current_report, previous_report),
                    "current_report": current_report,
                    "previous_report": previous_report,
                    "success": "Reports uploaded and dashboard calculated successfully.",
                    "as_of_display": as_of_display,
                    "current_total_display": money(current_total),
                    "monthly_target_display": money(monthly_target),
                    "mtd_target_display": money(mtd_target),
                    "performance_percentage_display": percentage_display(performance_percentage),
                    "performance_difference_display": signed_money(performance_difference),
                    "performance_summary_class": summary_class(performance_difference),
                    "monthly_progress_display": f"{monthly_progress:.1f}%",
                    "progress_width": f"{progress_width:.1f}",
                    "total_workdays": total_workdays,
                    "elapsed_workdays": elapsed_workdays,
                    "remaining_workdays": remaining_workdays,
                    "daily_target_display": money(daily_target),
                    "required_daily_display": money(required_daily),
                    "remaining_target_display": money(remaining_target),
                    "save_payload": payload,
                })
            except Exception as exc:
                template_data["error"] = f"The dashboard could not be calculated. Details: {exc}"

    return render_template_string(PAGE, **template_data)


@app.route("/save-month", methods=["POST"])
def save_month():
    required_fields = [
        "salesperson", "year", "month", "as_of_date", "current_filename",
        "previous_filename", "current_total", "previous_total",
        "armorsteel_current", "armorsteel_previous", "greens_current",
        "greens_previous", "lifestyle_current", "lifestyle_previous",
        "nzwb_current", "nzwb_previous", "sheds_current", "sheds_previous",
    ]
    missing = [field for field in required_fields if field not in request.form]
    if missing:
        return redirect(url_for("home", error="The month could not be saved because some data was missing."))

    values = {field: request.form[field] for field in required_fields}
    saved_at = datetime.now().isoformat(timespec="seconds")

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO monthly_sales (
                salesperson, year, month, as_of_date, current_filename,
                previous_filename, current_total, previous_total,
                armorsteel_current, armorsteel_previous, greens_current,
                greens_previous, lifestyle_current, lifestyle_previous,
                nzwb_current, nzwb_previous, sheds_current, sheds_previous,
                saved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(salesperson, year, month) DO UPDATE SET
                as_of_date=excluded.as_of_date,
                current_filename=excluded.current_filename,
                previous_filename=excluded.previous_filename,
                current_total=excluded.current_total,
                previous_total=excluded.previous_total,
                armorsteel_current=excluded.armorsteel_current,
                armorsteel_previous=excluded.armorsteel_previous,
                greens_current=excluded.greens_current,
                greens_previous=excluded.greens_previous,
                lifestyle_current=excluded.lifestyle_current,
                lifestyle_previous=excluded.lifestyle_previous,
                nzwb_current=excluded.nzwb_current,
                nzwb_previous=excluded.nzwb_previous,
                sheds_current=excluded.sheds_current,
                sheds_previous=excluded.sheds_previous,
                saved_at=excluded.saved_at
            """,
            (
                values["salesperson"], int(values["year"]), int(values["month"]),
                values["as_of_date"], values["current_filename"], values["previous_filename"],
                float(values["current_total"]), float(values["previous_total"]),
                float(values["armorsteel_current"]), float(values["armorsteel_previous"]),
                float(values["greens_current"]), float(values["greens_previous"]),
                float(values["lifestyle_current"]), float(values["lifestyle_previous"]),
                float(values["nzwb_current"]), float(values["nzwb_previous"]),
                float(values["sheds_current"]), float(values["sheds_previous"]),
                saved_at,
            ),
        )
        connection.commit()

    return redirect(url_for("history", year=values["year"], success="Month saved successfully."))


@app.route("/qtr-bonus-tracker", methods=["GET", "POST"])
def qtr_bonus_tracker():
    error = request.args.get("error")
    success = request.args.get("success")
    selected_salesperson = request.args.get("salesperson", "").strip()

    if request.method == "POST":
        uploaded_file = request.files.get("transactions_report")
        form_salesperson = request.form.get("salesperson", "").strip()
        if not uploaded_file or not uploaded_file.filename:
            error = "Please choose a transactions report file."
        elif not form_salesperson:
            error = "Please enter the salesperson's name for this report."
        else:
            try:
                transactions = read_rep_transactions_report(uploaded_file)
                uploaded_at = datetime.now().isoformat(timespec="seconds")
                with sqlite3.connect(DATABASE_PATH) as connection:
                    for txn in transactions:
                        connection.execute(
                            """
                            INSERT INTO rep_bonus_transactions (
                                salesperson, doc_number, txn_date, amount, source_filename, uploaded_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(salesperson, doc_number) DO UPDATE SET
                                txn_date=excluded.txn_date,
                                amount=excluded.amount,
                                source_filename=excluded.source_filename,
                                uploaded_at=excluded.uploaded_at
                            """,
                            (
                                form_salesperson, txn["doc_number"], txn["date"].isoformat(),
                                txn["amount"], uploaded_file.filename, uploaded_at,
                            ),
                        )
                    connection.commit()
                selected_salesperson = form_salesperson
                success = f"Loaded {len(transactions)} transactions for {form_salesperson}."
            except Exception as exc:
                error = f"The report could not be processed. Details: {exc}"

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        salesperson_options = [
            row["salesperson"] for row in connection.execute(
                "SELECT DISTINCT salesperson FROM rep_bonus_transactions ORDER BY salesperson"
            ).fetchall()
        ]

    if not selected_salesperson and salesperson_options:
        selected_salesperson = salesperson_options[0]
    elif selected_salesperson and salesperson_options and selected_salesperson not in salesperson_options:
        selected_salesperson = salesperson_options[0]

    try:
        growth_target_percent = float(request.args.get("growth_target", 30))
    except (TypeError, ValueError):
        growth_target_percent = 30.0
    try:
        bonus_rate_percent = float(request.args.get("bonus_rate", 4))
    except (TypeError, ValueError):
        bonus_rate_percent = 4.0

    template_data = {
        "view": "qtr_bonus",
        "error": error,
        "success": success,
        "salesperson_options": salesperson_options,
        "selected_salesperson": selected_salesperson,
        "growth_target_percent": growth_target_percent,
        "bonus_rate_percent": bonus_rate_percent,
        "bonus_dashboard": None,
    }

    if selected_salesperson:
        with sqlite3.connect(DATABASE_PATH) as connection:
            rows = connection.execute(
                "SELECT txn_date, amount FROM rep_bonus_transactions WHERE salesperson = ?",
                (selected_salesperson,),
            ).fetchall()
        transactions = [(date.fromisoformat(row[0]), row[1]) for row in rows]

        if transactions:
            today = date.today()
            quarter_number = quarter_of_date(today)
            year = today.year
            current_start, current_end = calendar_quarter_bounds(year, quarter_number)
            last_year_start, last_year_end = calendar_quarter_bounds(year - 1, quarter_number)
            last_year_to_date = same_day_last_year(today)

            qtd_this_year = sum(
                amount for txn_date, amount in transactions
                if current_start <= txn_date <= today
            )
            qtd_last_year = sum(
                amount for txn_date, amount in transactions
                if last_year_start <= txn_date <= last_year_to_date
            )
            full_last_year_quarter = sum(
                amount for txn_date, amount in transactions
                if last_year_start <= txn_date <= last_year_end
            )

            growth_multiplier = 1 + (growth_target_percent / 100)
            bonus_multiplier = bonus_rate_percent / 100

            target_qtd = qtd_last_year * growth_multiplier
            above_target_qtd = max(0.0, qtd_this_year - target_qtd)
            bonus_qtd = above_target_qtd * bonus_multiplier
            growth_pct_qtd = percentage_change(qtd_this_year, qtd_last_year)

            target_full = full_last_year_quarter * growth_multiplier
            above_target_full = max(0.0, qtd_this_year - target_full)
            bonus_full = above_target_full * bonus_multiplier
            growth_pct_full = percentage_change(qtd_this_year, full_last_year_quarter)

            days_elapsed = (today - current_start).days + 1
            days_total = (current_end - current_start).days + 1
            days_remaining = days_total - days_elapsed
            quarter_complete_percent = min(max((days_elapsed / days_total) * 100 if days_total else 0.0, 0.0), 100.0)

            template_data["bonus_dashboard"] = {
                "quarter_label": f"Q{quarter_number} {year}",
                "quarter_range_display": f"{current_start.strftime('%d %b %Y')} - {current_end.strftime('%d %b %Y')}",
                "today_display": today.strftime("%d %B %Y"),
                "days_elapsed": days_elapsed,
                "days_total": days_total,
                "days_remaining": days_remaining,
                "quarter_complete_percent": f"{quarter_complete_percent:.1f}",

                "qtd_last_year_display": money(qtd_last_year),
                "qtd_last_year_range_display": f"{last_year_start.strftime('%d %b %Y')} - {last_year_to_date.strftime('%d %b %Y')}",
                "target_qtd_display": money(target_qtd),
                "qtd_this_year_display": money(qtd_this_year),
                "qtd_this_year_range_display": f"{current_start.strftime('%d %b %Y')} - {today.strftime('%d %b %Y')}",
                "vs_target_qtd_display": signed_money(qtd_this_year - target_qtd),
                "vs_target_qtd_class": summary_class(qtd_this_year - target_qtd),
                "growth_pct_qtd_display": percentage_display(growth_pct_qtd),
                "growth_pct_qtd_class": change_class(growth_pct_qtd),
                "bonus_qtd_display": money(bonus_qtd),
                "bonus_qtd_earned": bonus_qtd > 0,
                "above_target_qtd_display": money(above_target_qtd),

                "full_last_year_display": money(full_last_year_quarter),
                "full_last_year_range_display": f"{last_year_start.strftime('%d %b %Y')} - {last_year_end.strftime('%d %b %Y')}",
                "target_full_display": money(target_full),
                "vs_target_full_display": signed_money(qtd_this_year - target_full),
                "vs_target_full_class": summary_class(qtd_this_year - target_full),
                "growth_pct_full_display": percentage_display(growth_pct_full),
                "growth_pct_full_class": change_class(growth_pct_full),
                "bonus_full_display": money(bonus_full),
                "bonus_full_earned": bonus_full > 0,
                "above_target_full_display": money(above_target_full),

                "transaction_count": len(transactions),
            }
        else:
            template_data["bonus_dashboard_empty"] = True

    return render_template_string(PAGE, **template_data)


@app.route("/history")
def history():
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        year_rows = connection.execute(
            "SELECT DISTINCT year FROM monthly_sales ORDER BY year DESC"
        ).fetchall()

        year_options = [row["year"] for row in year_rows]
        if not year_options:
            year_options = [date.today().year]

        try:
            selected_year = int(request.args.get("year", year_options[0]))
        except ValueError:
            selected_year = year_options[0]

        records = connection.execute(
            """
            SELECT * FROM monthly_sales
            WHERE year = ?
            ORDER BY month
            """,
            (selected_year,),
        ).fetchall()

    history_rows = []
    ytd_actual = 0.0
    ytd_target = 0.0
    for record in records:
        actual = record["current_total"]
        target = record["previous_total"]
        difference = actual - target
        percentage = percentage_change(actual, target)
        ytd_actual += actual
        ytd_target += target
        saved_date = datetime.fromisoformat(record["saved_at"])
        history_rows.append({
            "id": record["id"],
            "month_name": date(2000, record["month"], 1).strftime("%B"),
            "actual_display": money(actual),
            "target_display": money(target),
            "difference_display": signed_money(difference),
            "percentage_display": percentage_display(percentage),
            "change_class": change_class(percentage),
            "saved_display": saved_date.strftime("%d %b %Y %H:%M"),
        })

    chart_labels = []
    chart_actual = []
    chart_target = []
    chart_cumulative_actual = []
    chart_cumulative_target = []
    category_options = [category for category in CATEGORIES if category != "Total"]
    category_chart_data = {
        category: {"current": [], "previous": []}
        for category in category_options
    }
    cumulative_actual = 0.0
    cumulative_target = 0.0

    category_fields = {
        "Armorsteel": ("armorsteel_current", "armorsteel_previous"),
        "Greens WB": ("greens_current", "greens_previous"),
        "NZ Lifestyle": ("lifestyle_current", "lifestyle_previous"),
        "NZ WB": ("nzwb_current", "nzwb_previous"),
        "Sheds": ("sheds_current", "sheds_previous"),
    }

    for record in records:
        actual = float(record["current_total"])
        target = float(record["previous_total"])
        chart_labels.append(date(2000, record["month"], 1).strftime("%B"))
        chart_actual.append(actual)
        chart_target.append(target)
        cumulative_actual += actual
        cumulative_target += target
        chart_cumulative_actual.append(cumulative_actual)
        chart_cumulative_target.append(cumulative_target)

        for category, (current_field, previous_field) in category_fields.items():
            category_chart_data[category]["current"].append(float(record[current_field]))
            category_chart_data[category]["previous"].append(float(record[previous_field]))

    ytd_difference = ytd_actual - ytd_target
    ytd_percentage = percentage_change(ytd_actual, ytd_target)

    return render_template_string(
        PAGE,
        view="history",
        year_options=year_options,
        selected_year=selected_year,
        history_rows=history_rows,
        ytd_actual_display=money(ytd_actual),
        ytd_target_display=money(ytd_target),
        ytd_difference_display=signed_money(ytd_difference),
        ytd_percentage_display=percentage_display(ytd_percentage),
        ytd_class=summary_class(ytd_difference),
        chart_labels=chart_labels,
        chart_actual=chart_actual,
        chart_target=chart_target,
        chart_cumulative_actual=chart_cumulative_actual,
        chart_cumulative_target=chart_cumulative_target,
        category_options=category_options,
        category_chart_data=category_chart_data,
        success=request.args.get("success"),
        error=request.args.get("error"),
    )


@app.route("/month/<int:record_id>")
def saved_month(record_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        record = connection.execute(
            "SELECT * FROM monthly_sales WHERE id = ?",
            (record_id,),
        ).fetchone()

    if record is None:
        return redirect(url_for("history", error="That saved month could not be found."))

    year = int(record["year"])
    month = int(record["month"])
    month_start = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    month_end = next_month - timedelta(days=1)
    as_of_date = datetime.strptime(record["as_of_date"], "%Y-%m-%d").date()

    current_sales = {
        "Armorsteel": float(record["armorsteel_current"]),
        "Greens WB": float(record["greens_current"]),
        "NZ Lifestyle": float(record["lifestyle_current"]),
        "NZ WB": float(record["nzwb_current"]),
        "Sheds": float(record["sheds_current"]),
        "Total": float(record["current_total"]),
    }
    previous_sales = {
        "Armorsteel": float(record["armorsteel_previous"]),
        "Greens WB": float(record["greens_previous"]),
        "NZ Lifestyle": float(record["lifestyle_previous"]),
        "NZ WB": float(record["nzwb_previous"]),
        "Sheds": float(record["sheds_previous"]),
        "Total": float(record["previous_total"]),
    }

    report_period = (
        f"{month_start.day} {month_start.strftime('%B %Y')} - "
        f"{month_end.day} {month_end.strftime('%B %Y')}"
    )
    current_report = {
        "filename": record["current_filename"],
        "salesperson": record["salesperson"],
        "report_period": report_period,
        "sales": current_sales,
    }
    previous_report = {
        "filename": record["previous_filename"],
        "salesperson": record["salesperson"],
        "report_period": "Saved matching prior-year report",
        "sales": previous_sales,
    }

    total_workdays = count_weekdays(month_start, month_end)
    elapsed_workdays = count_weekdays(month_start, as_of_date)
    remaining_workdays = count_weekdays(as_of_date + timedelta(days=1), month_end)
    current_total = current_sales["Total"]
    monthly_target = previous_sales["Total"]
    daily_target = monthly_target / total_workdays if total_workdays else 0.0
    mtd_target = daily_target * elapsed_workdays
    remaining_target = max(monthly_target - current_total, 0.0)
    required_daily = remaining_target / remaining_workdays if remaining_workdays else remaining_target
    performance_difference = current_total - mtd_target
    performance_percentage = percentage_change(current_total, mtd_target)
    monthly_progress = (current_total / monthly_target * 100) if monthly_target else 0.0
    progress_width = min(max(monthly_progress, 0.0), 100.0)

    return render_template_string(
        PAGE,
        view="saved_month",
        show_upload=False,
        comparison_rows=build_comparison(current_report, previous_report),
        current_report=current_report,
        previous_report=previous_report,
        current_year=year,
        as_of_display=f"{as_of_date.day} {as_of_date.strftime('%B %Y')}",
        current_total_display=money(current_total),
        monthly_target_display=money(monthly_target),
        mtd_target_display=money(mtd_target),
        performance_percentage_display=percentage_display(performance_percentage),
        performance_difference_display=signed_money(performance_difference),
        performance_summary_class=summary_class(performance_difference),
        monthly_progress_display=f"{monthly_progress:.1f}%",
        progress_width=f"{progress_width:.1f}",
        total_workdays=total_workdays,
        elapsed_workdays=elapsed_workdays,
        remaining_workdays=remaining_workdays,
        daily_target_display=money(daily_target),
        required_daily_display=money(required_daily),
        remaining_target_display=money(remaining_target),
        save_payload={},
        error=None,
        success=None,
        selected_date=record["as_of_date"],
    )



MITRE10_REQUIRED_COLUMNS = {
    "monthly": ["Fineline", "$ Value Excl GST This Year", "$ Value Excl GST Last Year", "Change %"],
    "woc": ["Article Name", "Supplier Item Code", "Fineline", "Store", "SOH Units", "On Order", "52W Sales", "Avg Weekly Sales", "Weeks of Cover"],
    "ranking": ["Store", "Fineline", "Store Sales Value TY", "Store Sales Value LY", "Opp Gap Value", "Opp Gap Units"],
    "cfm": ["Article Name", "Supplier Item Code", "Fineline", "Store", "Core For Me", "Status", "Stock on Hand", "On Order", "Sales 4 Weeks", "Sales 13 Weeks"],
}


def read_csv_upload(uploaded_file, report_key):
    filename = uploaded_file.filename
    file_bytes = uploaded_file.read()
    if not file_bytes:
        raise ValueError(f"{filename} is empty.")
    try:
        text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = file_bytes.decode("cp1252")
    dataframe = pd.read_csv(StringIO(text)).dropna(how="all")
    dataframe.columns = [str(column).strip() for column in dataframe.columns]
    missing = [column for column in MITRE10_REQUIRED_COLUMNS[report_key] if column not in dataframe.columns]
    if missing:
        raise ValueError(f"{filename} is missing required columns: {', '.join(missing)}")
    return filename, dataframe


def safe_number(value):
    """Convert exported numeric values safely, including currency, commas and percentages."""
    if pd.isna(value):
        return 0.0
    text = str(value).strip()
    if not text:
        return 0.0
    negative = text.startswith("(") and text.endswith(")")
    text = text.replace("$", "").replace(",", "").replace("%", "").strip("() ")
    try:
        number = float(text)
        return -number if negative else number
    except (TypeError, ValueError):
        return 0.0


def normalise_fineline(value):
    """Create a strict stable fineline key so formatting differences cannot duplicate rows."""
    if pd.isna(value):
        return ""
    text = str(value).replace("\u00a0", " ").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    # Ignore punctuation and separator differences such as &, /, hyphens and repeated spaces.
    return re.sub(r"[^a-z0-9]+", "", text)


def clean_monthly_finelines(monthly):
    """Return one validated row per real fineline, excluding totals and duplicate rows."""
    monthly = monthly.copy()
    monthly["Fineline"] = monthly["Fineline"].fillna("").astype(str).str.strip()
    monthly["_fineline_key"] = monthly["Fineline"].map(normalise_fineline)

    excluded_labels = {
        "", "total", "grand total", "overall total", "subtotal", "sub total",
        "all", "all finelines", "all products", "report total"
    }
    excluded_keys = {normalise_fineline(value) for value in excluded_labels}
    monthly = monthly[~monthly["_fineline_key"].isin(excluded_keys)].copy()

    for column in ["$ Value Excl GST This Year", "$ Value Excl GST Last Year"]:
        monthly[column] = monthly[column].map(safe_number)

    # Remove completely empty/non-data rows, then consolidate duplicate finelines.
    monthly = monthly[
        (monthly["$ Value Excl GST This Year"].abs() > 0.000001)
        | (monthly["$ Value Excl GST Last Year"].abs() > 0.000001)
    ].copy()
    if monthly.empty:
        raise ValueError("The Monthly Dashboard report contains no usable fineline sales rows.")

    display_names = (
        monthly.groupby("_fineline_key", as_index=False)["Fineline"]
        .first()
    )
    totals = (
        monthly.groupby("_fineline_key", as_index=False)[
            ["$ Value Excl GST This Year", "$ Value Excl GST Last Year"]
        ].sum()
    )
    cleaned = totals.merge(display_names, on="_fineline_key", how="left")
    cleaned["Change %"] = cleaned.apply(
        lambda row: ((row["$ Value Excl GST This Year"] - row["$ Value Excl GST Last Year"])
                     / row["$ Value Excl GST Last Year"] * 100)
        if row["$ Value Excl GST Last Year"] else (100.0 if row["$ Value Excl GST This Year"] else 0.0),
        axis=1,
    )
    return cleaned


def extract_store_name(woc, cfm):
    woc_stores = [str(value).strip() for value in woc["Store"].dropna().unique() if str(value).strip()]
    cfm_stores = [str(value).strip() for value in cfm["Store"].dropna().unique() if str(value).strip()]
    if len(woc_stores) != 1:
        raise ValueError("The Weeks of Cover report must contain exactly one store.")
    if len(cfm_stores) != 1:
        raise ValueError("The Core For Me report must contain exactly one store.")
    if woc_stores[0] != cfm_stores[0]:
        raise ValueError("The Weeks of Cover and Core For Me reports belong to different stores.")
    return woc_stores[0]


def fmt_percent(value):
    return f"{value:+.1f}%" if value else "0.0%"


def analyse_mitre10_reports(monthly, woc, ranking, cfm, store_name):
    monthly = monthly.copy()
    woc = woc.copy()
    ranking = ranking.copy()
    cfm = cfm.copy()

    monthly = clean_monthly_finelines(monthly)
    for column in ["SOH Units", "On Order", "52W Sales", "Avg Weekly Sales", "Weeks of Cover"]:
        woc[column] = pd.to_numeric(woc[column], errors="coerce").fillna(0.0)
    for column in ["Store Sales Value TY", "Store Sales Value LY", "Opp Gap Value", "Opp Gap Units"]:
        ranking[column] = pd.to_numeric(ranking[column], errors="coerce").fillna(0.0)
    for column in ["Stock on Hand", "On Order", "Sales 4 Weeks", "Sales 13 Weeks"]:
        cfm[column] = pd.to_numeric(cfm[column], errors="coerce").fillna(0.0)

    for frame in [monthly, woc, ranking, cfm]:
        frame["_fineline_key"] = frame["Fineline"].map(normalise_fineline)

    store_key = str(store_name).strip().casefold()
    store_ranking = ranking[ranking["Store"].astype(str).str.strip().str.casefold().eq(store_key)].copy()
    if store_ranking.empty:
        raise ValueError(f"The ranking report does not contain {store_name}.")

    total_ty = float(monthly["$ Value Excl GST This Year"].sum())
    total_ly = float(monthly["$ Value Excl GST Last Year"].sum())
    total_change = ((total_ty - total_ly) / total_ly * 100) if total_ly else 0.0

    urgent_stock = woc[(woc["SOH Units"] <= 0) & (woc["52W Sales"] > 0) & (woc["On Order"] <= 0)].copy()
    low_cover = woc[(woc["SOH Units"] > 0) & (woc["Avg Weekly Sales"] > 0) & (woc["Weeks of Cover"] < 2) & (woc["On Order"] <= 0)].copy()
    overstock = woc[(woc["SOH Units"] > 0) & (woc["Weeks of Cover"] > 12)].copy()
    core_gaps = cfm[(cfm["Core For Me"].astype(str).str.strip().str.lower() == "yes") & (cfm["Status"].astype(str).str.strip().str.lower() == "active") & (cfm["Stock on Hand"] <= 0)].copy()
    core_demand_gaps = core_gaps[core_gaps["Sales 13 Weeks"] > 0].copy()
    ranking_positive = store_ranking[store_ranking["Opp Gap Value"] > 0].sort_values("Opp Gap Value", ascending=False)
    total_opp_gap = float(ranking_positive["Opp Gap Value"].sum())

    def rank_within(frame, store_key):
        totals = frame.groupby("Store", as_index=False)["Store Sales Value TY"].sum()
        totals = totals.sort_values("Store Sales Value TY", ascending=False).reset_index(drop=True)
        totals["_store_key"] = totals["Store"].astype(str).str.strip().str.casefold()
        total_count = len(totals)
        match = totals.index[totals["_store_key"] == store_key]
        if len(match) == 0 or total_count == 0:
            return None, total_count, 0.0
        position = int(match[0]) + 1
        value = float(totals.loc[match[0], "Store Sales Value TY"])
        return position, total_count, value

    overall_rank, overall_rank_total, overall_rank_value = rank_within(ranking, store_key)
    overall_percentile_display = (
        f"Top {(overall_rank / overall_rank_total * 100):.0f}%"
        if overall_rank and overall_rank_total else ""
    )

    category_ranks = []
    for fineline_key in store_ranking["_fineline_key"].dropna().unique():
        fineline_display = str(
            store_ranking.loc[store_ranking["_fineline_key"] == fineline_key, "Fineline"].iloc[0]
        ).strip()
        category_frame = ranking[ranking["_fineline_key"] == fineline_key]
        cat_rank, cat_total, cat_value = rank_within(category_frame, store_key)
        category_ranks.append({
            "fineline": fineline_display.title(),
            "rank": cat_rank,
            "total_stores": cat_total,
            "rank_display": f"{cat_rank} of {cat_total}" if cat_rank else "Not ranked",
            "percentile_display": f"Top {(cat_rank / cat_total * 100):.0f}%" if cat_rank and cat_total else "",
            "sales_ty_display": money(cat_value),
        })
    category_ranks.sort(key=lambda item: (item["rank"] is None, item["rank"]))
    ranked_categories = [item for item in category_ranks if item["rank"] is not None]
    best_category = ranked_categories[0] if ranked_categories else None
    worst_category = ranked_categories[-1] if len(ranked_categories) > 1 else None

    stock_risk_counts = pd.concat([
        urgent_stock[["_fineline_key"]],
        low_cover[["_fineline_key"]],
    ]).groupby("_fineline_key").size().to_dict()
    core_gap_counts = core_gaps.groupby("_fineline_key").size().to_dict()
    ranking_gap_map = store_ranking.groupby("_fineline_key")["Opp Gap Value"].sum().to_dict()

    # Force a second aggregation immediately before display. This makes the visible table
    # safe even if a source export contains one row per product rather than one row per fineline.
    visible_totals = {}
    for _, row in monthly.iterrows():
        fineline = str(row.get("Fineline", "")).strip()
        fineline_key = normalise_fineline(fineline)
        if not fineline_key:
            continue
        bucket = visible_totals.setdefault(fineline_key, {
            "fineline": fineline.title(), "ty": 0.0, "ly": 0.0
        })
        bucket["ty"] += safe_number(row.get("$ Value Excl GST This Year", 0))
        bucket["ly"] += safe_number(row.get("$ Value Excl GST Last Year", 0))

    fineline_rows = []
    for fineline_key, bucket in visible_totals.items():
        ty = bucket["ty"]
        ly = bucket["ly"]
        change = ((ty - ly) / ly * 100) if ly else (100.0 if ty else 0.0)
        fineline_rows.append({
            "fineline": bucket["fineline"],
            "sales_ty": money(ty),
            "sales_ly": money(ly),
            "change_pct": fmt_percent(change),
            "change_raw": change,
            "opp_gap": money(max(safe_number(ranking_gap_map.get(fineline_key, 0)), 0)),
            "stock_risks": int(stock_risk_counts.get(fineline_key, 0)),
            "core_gaps": int(core_gap_counts.get(fineline_key, 0)),
        })
    fineline_rows.sort(key=lambda row: safe_number(row["sales_ty"]), reverse=True)

    if len(fineline_rows) != len({normalise_fineline(row["fineline"]) for row in fineline_rows}):
        raise ValueError("Fineline aggregation failed: duplicate finelines remain after consolidation.")

    fix_rows = []
    seen = set()
    for _, row in core_demand_gaps.sort_values("Sales 13 Weeks", ascending=False).iterrows():
        key = str(row["Supplier Item Code"])
        seen.add(key)
        fix_rows.append({
            "priority": "Critical", "severity_class": "severity-critical",
            "product": str(row["Article Name"]).strip(), "code": key,
            "fineline": str(row["Fineline"]).title(), "soh": f'{safe_number(row["Stock on Hand"]):,.0f}',
            "on_order": f'{safe_number(row["On Order"]):,.0f}',
            "demand": f'${safe_number(row["Sales 13 Weeks"]):,.0f} sales in 13 weeks',
            "action": "Agree an immediate order and restore core compliance.",
        })
    for _, row in urgent_stock.sort_values("52W Sales", ascending=False).iterrows():
        key = str(row["Supplier Item Code"])
        if key in seen:
            continue
        seen.add(key)
        fix_rows.append({
            "priority": "High", "severity_class": "severity-high",
            "product": str(row["Article Name"]).strip(), "code": key,
            "fineline": str(row["Fineline"]).title(), "soh": f'{safe_number(row["SOH Units"]):,.0f}',
            "on_order": f'{safe_number(row["On Order"]):,.0f}',
            "demand": f'{safe_number(row["52W Sales"]):,.0f} units sold in 52 weeks',
            "action": "Confirm replenishment and discuss likely lost sales while out of stock.",
        })
    for _, row in low_cover.sort_values("Weeks of Cover").iterrows():
        key = str(row["Supplier Item Code"])
        if key in seen:
            continue
        seen.add(key)
        fix_rows.append({
            "priority": "High", "severity_class": "severity-high",
            "product": str(row["Article Name"]).strip(), "code": key,
            "fineline": str(row["Fineline"]).title(), "soh": f'{safe_number(row["SOH Units"]):,.0f}',
            "on_order": f'{safe_number(row["On Order"]):,.0f}',
            "demand": f'{safe_number(row["Weeks of Cover"]):.1f} weeks of cover',
            "action": "Top up before the next stockout.",
        })

    ranking_rows = []
    for _, row in ranking_positive.head(10).iterrows():
        fineline = str(row["Fineline"]).strip()
        ranking_rows.append({
            "fineline": fineline.title(),
            "sales_ty": money(safe_number(row["Store Sales Value TY"])),
            "sales_ly": money(safe_number(row["Store Sales Value LY"])),
            "opp_value": money(safe_number(row["Opp Gap Value"])),
            "opp_units": f'{safe_number(row["Opp Gap Units"]):,.0f}',
            "action": "Review range, stock depth, display space and promotional support.",
        })

    growth_rows = []
    for _, row in monthly[monthly["Change %"] >= 15].sort_values("Change %", ascending=False).iterrows():
        growth_rows.append({
            "fineline": str(row["Fineline"]).title(),
            "change_pct": fmt_percent(safe_number(row["Change %"])),
            "action": "Protect availability and consider expanding range or facings.",
        })

    overstock_rows = []
    for _, row in overstock.sort_values("Weeks of Cover", ascending=False).head(12).iterrows():
        overstock_rows.append({
            "product": str(row["Article Name"]).strip(),
            "code": str(row["Supplier Item Code"]).strip(),
            "soh": f'{safe_number(row["SOH Units"]):,.0f}',
            "woc": f'{safe_number(row["Weeks of Cover"]):.1f}',
            "action": "Review upcoming orders, promotion, transfer or range position.",
        })

    conversation_plan = []
    top_declines = monthly[monthly["Change %"] <= -10].sort_values("Change %")
    for _, row in top_declines.head(3).iterrows():
        fineline = str(row["Fineline"]).strip()
        gap = max(safe_number(ranking_gap_map.get(row["_fineline_key"], 0)), 0)
        evidence = f'{fmt_percent(safe_number(row["Change %"]))} year on year, with {money(gap)} ranking opportunity' if gap else f'{fmt_percent(safe_number(row["Change %"]))} year on year'
        conversation_plan.append({
            "card_class": "high", "title": f'{fineline.title()} — sales recovery opportunity',
            "evidence": evidence,
            "action": "Review ranging, shelf/display position, stock availability and the next order.",
            "score": 80 + gap / 1000,
        })
    if len(core_demand_gaps):
        conversation_plan.append({
            "card_class": "critical", "title": "Core products are unavailable despite recent demand",
            "evidence": f'{len(core_demand_gaps)} core items have no stock and recorded sales in the last 13 weeks.',
            "action": "Show the product list and agree corrective orders before leaving the store.",
            "score": 100,
        })
    if len(urgent_stock):
        conversation_plan.append({
            "card_class": "critical", "title": "Out-of-stocks are creating lost-sales risk",
            "evidence": f'{len(urgent_stock)} lines have no stock, no order and sales history.',
            "action": "Prioritise the highest-selling lines and confirm replenishment dates.",
            "score": 95,
        })
    if len(growth_rows):
        best = growth_rows[0]
        conversation_plan.append({
            "card_class": "growth", "title": f'{best["fineline"]} — protect the momentum',
            "evidence": f'{best["change_pct"]} year-on-year growth.',
            "action": "Check stock depth and discuss whether additional range, display or promotion can extend the growth.",
            "score": 50,
        })
    conversation_plan = sorted(conversation_plan, key=lambda item: item["score"], reverse=True)[:6]

    export_candidates = []
    if "Export_Date" in monthly.columns and not monthly["Export_Date"].dropna().empty:
        export_candidates.append(str(monthly["Export_Date"].dropna().iloc[0]))
    export_date = export_candidates[0] if export_candidates else "date not supplied"

    return {
        "store_name": store_name,
        "export_date": export_date,
        "total_sales_ty": money(total_ty),
        "total_sales_ly": money(total_ly),
        "total_change_pct": fmt_percent(total_change),
        "total_change_raw": total_change,
        "urgent_stock_count": len(urgent_stock),
        "core_gap_count": len(core_gaps),
        "low_cover_count": len(low_cover),
        "overstock_count": len(overstock),
        "total_opp_gap": money(total_opp_gap),
        "overall_rank": overall_rank,
        "overall_rank_total": overall_rank_total,
        "overall_rank_display": f"{overall_rank} of {overall_rank_total}" if overall_rank else "Not ranked",
        "overall_percentile_display": overall_percentile_display,
        "category_ranks": category_ranks,
        "best_category": best_category,
        "worst_category": worst_category,
        "conversation_plan": conversation_plan,
        "fineline_rows": fineline_rows,
        "fix_now_rows": fix_rows[:25],
        "ranking_rows": ranking_rows,
        "growth_rows": growth_rows,
        "overstock_rows": overstock_rows,
    }



ACTION_KEYWORDS = (
    "send", "email", "follow up", "follow-up", "check", "arrange", "contact",
    "call", "quote", "price", "pricing", "brochure", "training", "order",
    "confirm", "provide", "forward", "book", "organise", "organize", "review",
    "revisit", "chase", "update", "supply"
)


def clean_action_text(text):
    text = re.sub(r"^[\s\-•*\d.)]+", "", str(text)).strip()
    text = re.sub(r"^(action|task)\s*:\s*", "", text, flags=re.I)
    return text.rstrip(" .")


def detect_store(block):
    patterns = [
        r"(?:store|customer|account|company)\s*[:\-]\s*([^\n]+)",
        r"(?:visited|visit to|meeting with|call with)\s+([^\n,.]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, block, flags=re.I)
        if match:
            return match.group(1).strip(" -:")[:120]
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    if lines:
        first = clean_action_text(lines[0])
        if len(first) <= 80 and not any(keyword in first.lower() for keyword in ACTION_KEYWORDS):
            return first
    return "Store not identified"


def likely_action(text):
    lowered = text.lower()
    return any(re.search(rf"\b{re.escape(keyword)}\b", lowered) for keyword in ACTION_KEYWORDS)


def action_priority(text):
    lowered = text.lower()
    if any(word in lowered for word in ("urgent", "asap", "today", "immediately", "out of stock", "deadline")):
        return "High", 2
    if any(word in lowered for word in ("next visit", "when next", "later", "in the new year")):
        return "Low", 21
    return "Medium", 7


def action_title(text):
    cleaned = clean_action_text(text)
    if len(cleaned) <= 85:
        return cleaned[:1].upper() + cleaned[1:]
    short = re.split(r"[;,.]", cleaned)[0].strip()
    return (short[:82] + "...") if len(short) > 85 else short


def make_email_draft(store, action):
    lowered = action.lower()
    emailworthy = any(word in lowered for word in (
        "send", "email", "pricing", "price list", "brochure", "quote",
        "details", "information", "pdf", "copy", "forward", "attach"
    ))
    if not emailworthy:
        return ""
    subject_detail = action_title(action)
    return (
        f"Hi there,\n\nThanks for your time. As discussed, I’ve attached/provided the information regarding "
        f"{subject_detail[0].lower() + subject_detail[1:] if subject_detail else 'our discussion'}.\n\n"
        "Please let me know if you have any questions or if there is anything further you need from me.\n\n"
        "Kind regards,\nTed"
    )


def extract_followups(raw_notes):
    blocks = [b.strip() for b in re.split(r"\n\s*---+\s*\n|\n\s*\n(?=\S)", raw_notes.strip()) if b.strip()]
    items = []
    seen = set()
    for block in blocks:
        store = detect_store(block)
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        in_actions = False
        candidates = []
        for line in lines:
            lowered = line.lower().strip()
            if re.match(r"^actions?\s*:", lowered):
                in_actions = True
                remainder = re.sub(r"^actions?\s*:\s*", "", line, flags=re.I)
                if remainder:
                    candidates.append(remainder)
                continue
            if re.match(r"^(notes?|summary)\s*:", lowered):
                in_actions = False
                continue
            if in_actions or re.match(r"^[\-•*]", line) or likely_action(line):
                candidates.append(line)
        if not candidates:
            sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", block))
            candidates = [sentence for sentence in sentences if likely_action(sentence)]
        for candidate in candidates:
            text = clean_action_text(candidate)
            if not text or text.lower().startswith(("store:", "customer:", "notes:")) or not likely_action(text):
                continue
            key = (store.lower(), text.lower())
            if key in seen:
                continue
            seen.add(key)
            priority, due_days = action_priority(text)
            armoursteel = bool(re.search(r"armou?rsteel|armorsteel|roofing|flashing|roll form", text, flags=re.I))
            items.append({
                "store": store,
                "title": action_title(text),
                "detail": text,
                "priority": priority,
                "due_date": (date.today() + timedelta(days=due_days)).isoformat(),
                "armoursteel": armoursteel,
                "email_draft": make_email_draft(store, text),
                "source": "Follow-Up Assistant",
                "contact_email": "",
            })
    return items[:40]


class PortatourScheduleParser(HTMLParser):
    """Extract appointment rows from a Portatour saved HTML schedule."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.current_day = ""
        self.day_div_depth = 0
        self.div_depth = 0
        self.current_row = None
        self.current_td = ""
        self.current_div_class = ""
        self.appointments = []

    @staticmethod
    def _classes(attrs):
        return dict(attrs).get("class", "").split()

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = self._classes(attrs)

        if tag == "div":
            self.div_depth += 1
            day_class = next(
                (name for name in classes if name.startswith("scheduleday_")),
                None,
            )
            if day_class:
                raw_date = day_class.replace("scheduleday_", "", 1)
                if re.fullmatch(r"\d{8}", raw_date):
                    self.current_day = raw_date
                    self.day_div_depth = self.div_depth
            self.current_div_class = " ".join(classes)

        if tag == "tr" and "stop" in classes:
            self.current_row = {
                "date": self.current_day,
                "time": "",
                "store": "",
                "call_note": "",
                "reservation": "",
            }

        if self.current_row is not None and tag == "td":
            if "time" in classes:
                self.current_td = "time"
            elif "displayname" in classes:
                self.current_td = "displayname"
            elif "reservation" in classes:
                self.current_td = "reservation"
            else:
                self.current_td = "other"

        if self.current_row is not None and tag == "div":
            if "callnote" in classes:
                self.current_div_class = "callnote"
            elif self.current_td == "displayname":
                self.current_div_class = "displayname"

    def handle_data(self, data):
        if self.current_row is None:
            return
        cleaned = re.sub(r"\s+", " ", data).strip()
        if not cleaned or cleaned == "⇒":
            return

        if self.current_td == "time":
            self.current_row["time"] += (" " if self.current_row["time"] else "") + cleaned
        elif self.current_td == "reservation":
            self.current_row["reservation"] += (
                " " if self.current_row["reservation"] else ""
            ) + cleaned
        elif self.current_td == "displayname":
            if self.current_div_class == "callnote":
                self.current_row["call_note"] += (
                    " " if self.current_row["call_note"] else ""
                ) + cleaned
            elif self.current_div_class == "displayname":
                self.current_row["store"] += (
                    " " if self.current_row["store"] else ""
                ) + cleaned

    def handle_endtag(self, tag):
        if tag == "td" and self.current_row is not None:
            self.current_td = ""
            self.current_div_class = ""

        if tag == "tr" and self.current_row is not None:
            row = self.current_row
            store = re.sub(r"\s+", " ", row["store"]).strip()
            note = re.sub(r"\s+", " ", row["call_note"]).strip(" ⇒")
            reservation = row["reservation"].strip()
            is_customer_stop = (
                store
                and store.lower() not in {"home location"}
                and not store.lower().startswith("overnight stay")
                and ("appointment" in reservation.lower() or note)
            )
            if is_customer_stop:
                row["store"] = store
                row["call_note"] = note
                row["reservation"] = reservation
                row["time"] = row["time"].strip()
                self.appointments.append(row)
            self.current_row = None
            self.current_td = ""
            self.current_div_class = ""

        if tag == "div":
            if self.day_div_depth and self.div_depth == self.day_div_depth:
                self.current_day = ""
                self.day_div_depth = 0
            self.div_depth = max(0, self.div_depth - 1)
            self.current_div_class = ""


def portatour_date_display(raw_date):
    try:
        return datetime.strptime(raw_date, "%Y%m%d").strftime("%d %b %Y")
    except Exception:
        return raw_date


def portatour_due_date(raw_date, fallback_days):
    try:
        appointment_date = datetime.strptime(raw_date, "%Y%m%d").date()
        if appointment_date >= date.today():
            return appointment_date.isoformat()
    except Exception:
        pass
    return (date.today() + timedelta(days=fallback_days)).isoformat()


def clean_portatour_action(action_text):
    """Remove status markers and tidy one action line."""
    action_text = re.sub(r"^[\s\-–—•⇒]+", "", action_text or "").strip()
    action_text = re.sub(
        r"\s*\*(?:DONE|EMAILED|SENT|COMPLETE|COMPLETED)\*\s*$",
        "",
        action_text,
        flags=re.I,
    ).strip()
    action_text = re.sub(r"\s*\*DNT\*\s*$", "", action_text, flags=re.I).strip()
    return re.sub(r"\s+", " ", action_text).strip(" -")


def extract_actions_from_call_comment(comment_text):
    """Return only outstanding dash actions from a Portatour call comment."""
    if not comment_text:
        return []

    text = comment_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.split("\n")]
    actions = []
    in_actions = False

    for line in lines:
        if not line:
            continue

        # Standard headings such as "Actions:" and malformed forms such as
        # "Actions - Send pricing" are both supported.
        action_heading = re.match(r"^actions?\s*[:\-]?\s*(.*)$", line, flags=re.I)
        if action_heading:
            inline_action = action_heading.group(1).strip()
            if in_actions and actions and not inline_action:
                # A second Actions: heading in some reports is actually the
                # beginning of the narrative notes section.
                break
            in_actions = True
            if inline_action:
                status_done = bool(re.search(
                    r"\*(?:DONE|EMAILED|SENT|COMPLETE|COMPLETED)\*",
                    inline_action,
                    flags=re.I,
                ))
                if not status_done:
                    cleaned = clean_portatour_action(inline_action)
                    if cleaned:
                        actions.append(cleaned)
            continue

        if re.match(r"^(?:notes?|nites?)\s*:", line, flags=re.I):
            if in_actions:
                break
            continue

        if not in_actions:
            continue

        if re.match(r"^[\-–—•⇒]", line):
            status_done = bool(re.search(
                r"\*(?:DONE|EMAILED|SENT|COMPLETE|COMPLETED)\*",
                line,
                flags=re.I,
            ))
            if status_done:
                continue
            cleaned = clean_portatour_action(line)
            if cleaned:
                actions.append(cleaned)
            continue

        # Once actions have started, ordinary prose is treated as notes rather
        # than a task. This prevents narrative call notes becoming actions.
        if actions:
            break

    # Preserve order while removing duplicates.
    unique = []
    seen = set()
    for action in actions:
        key = action.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(action)
    return unique


def parse_portatour_report_date(raw_heading, report_year):
    """Convert headings such as 'Wed, 07-08' into a display date."""
    raw_heading = (raw_heading or "").strip()
    match = re.search(r"(\d{1,2})-(\d{1,2})", raw_heading)
    if not match:
        return raw_heading, ""
    try:
        parsed = date(report_year, int(match.group(1)), int(match.group(2)))
        return f"{parsed.day} {parsed.strftime('%b %Y')}", parsed.isoformat()
    except ValueError:
        return raw_heading, ""


def extract_portatour_followups(uploaded_file):
    """Extract outstanding Actions from the Customer call reports section."""
    filename = uploaded_file.filename or "Portatour HTML report"
    file_bytes = uploaded_file.read()
    if not file_bytes:
        raise ValueError(f"{filename} is empty.")

    try:
        html_text = file_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        html_text = file_bytes.decode("windows-1252", errors="replace")

    if "<html" not in html_text.lower() or "portatour" not in html_text.lower():
        raise ValueError(
            "This does not appear to be a Portatour HTML report. "
            "Save or download the Portatour report as an HTML file."
        )

    soup = BeautifulSoup(html_text, "html.parser")

    report_year = date.today().year
    report_info = soup.select_one("td.reportheader_info")
    if report_info:
        year_match = re.search(r"\b(20\d{2})-\d{2}-\d{2}\b", report_info.get_text(" ", strip=True))
        if year_match:
            report_year = int(year_match.group(1))

    call_report_section = soup.select_one("div.visitsofaday")
    if call_report_section is None:
        raise ValueError(
            "No Customer call reports section was found. Make sure that section "
            "is included when creating the Portatour report."
        )

    items = []
    for day_tab in call_report_section.select("div.tabbertab"):
        heading = day_tab.find(["h2", "h3", "h4"])
        date_display, date_iso = parse_portatour_report_date(
            heading.get_text(" ", strip=True) if heading else "",
            report_year,
        )

        for row in day_tab.select("tbody.callreports tr"):
            classes = set(row.get("class", []))
            if "noreports" in classes:
                continue

            cells = row.find_all("td", recursive=False)
            if len(cells) < 6:
                continue

            begin_time = cells[0].get_text(" ", strip=True)
            end_time = cells[1].get_text(" ", strip=True)
            store = re.sub(r"\s+", " ", cells[2].get_text(" ", strip=True)).strip()
            subject = cells[4].get_text(" ", strip=True)
            comment = cells[5].get_text("\n", strip=True)

            if not store or subject.lower() not in {"call", "phone", "other"}:
                continue

            for action_text in extract_actions_from_call_comment(comment):
                priority, due_days = action_priority(action_text)
                armoursteel = bool(re.search(
                    r"armou?rsteel|armorsteel|\ba\s*/\s*s\b|roofing|flashing|cladding|roll form",
                    action_text,
                    flags=re.I,
                ))

                timing = ""
                if date_display:
                    timing = date_display
                if begin_time or end_time:
                    time_range = " - ".join(value for value in [begin_time, end_time] if value)
                    timing = f"{timing} — {time_range}" if timing else time_range

                detail_lines = []
                if timing:
                    detail_lines.append(f"Call report: {timing}")
                detail_lines.append(f"Original action: {action_text}")

                items.append({
                    "store": store,
                    "title": action_title(action_text),
                    "detail": "\n".join(detail_lines),
                    "priority": priority,
                    "due_date": (
                        date.today() + timedelta(days=due_days)
                    ).isoformat(),
                    "armoursteel": armoursteel,
                    "email_draft": make_email_draft(store, action_text),
                    "source": "Portatour Call Report",
                    "contact_email": "",
                })

    if not items:
        raise ValueError(
            "No outstanding dash actions were found under Actions: in the "
            "Customer call reports. Actions marked DONE, EMAILED, SENT or "
            "COMPLETED are intentionally ignored."
        )

    return items[:250]



def normalise_action_key(store, title):
    store_key = re.sub(r"\s+", " ", (store or "").strip()).casefold()
    title_key = re.sub(r"[^a-z0-9]+", " ", (title or "").casefold())
    title_key = re.sub(r"\s+", " ", title_key).strip()
    return store_key, title_key


def mark_existing_action_duplicates(items):
    """Mark suggestions already present as an incomplete action."""
    with sqlite3.connect(DATABASE_PATH) as connection:
        existing_rows = connection.execute(
            "SELECT store_name, title FROM store_actions WHERE status != 'Completed'"
        ).fetchall()

    existing_keys = {
        normalise_action_key(store_name, title)
        for store_name, title in existing_rows
    }

    for item in items:
        item["is_duplicate"] = (
            normalise_action_key(item.get("store"), item.get("title"))
            in existing_keys
        )
    return items


@app.route("/follow-up", methods=["GET", "POST"])
def follow_up():
    raw_notes = ""
    after_trip_visits = []
    active_trip = None
    active_trip_id = request.values.get("trip_id", type=int)
    after_trip_summary = {"total_visits": 0, "completed_visits": 0, "pending_visits": 0, "notes_captured": 0, "actions_today": 0}
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        if active_trip_id:
            active_trip = connection.execute("SELECT * FROM trip_plans WHERE id=?", (active_trip_id,)).fetchone()
            latest_schedule = connection.execute(
                "SELECT * FROM trip_schedules WHERE trip_plan_id=? ORDER BY id DESC LIMIT 1",
                (active_trip_id,),
            ).fetchone()
        else:
            latest_schedule = connection.execute(
                "SELECT * FROM trip_schedules WHERE trip_plan_id IS NOT NULL ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if latest_schedule and latest_schedule["trip_plan_id"]:
                active_trip_id = int(latest_schedule["trip_plan_id"])
                active_trip = connection.execute("SELECT * FROM trip_plans WHERE id=?", (active_trip_id,)).fetchone()
            if latest_schedule is None:
                latest_schedule = connection.execute("SELECT * FROM trip_schedules ORDER BY id DESC LIMIT 1").fetchone()

        if latest_schedule:
            scheduled = parse_schedule_appointments(latest_schedule["raw_html"], from_date=date.min)
            notes = connection.execute(
                "SELECT * FROM road_visit_notes WHERE schedule_id = ? ORDER BY visit_date DESC, visit_time DESC",
                (latest_schedule["id"],),
            ).fetchall()
            note_map = {(row["visit_date"], row["visit_time"], row["store_name"].strip().lower()): row for row in notes}
            notes_by_date_store = {}
            for row in notes:
                notes_by_date_store.setdefault((row["visit_date"], row["store_name"].strip().lower()), []).append(row)
            relevant = list(scheduled)
            after_trip_summary["total_visits"] = len(relevant)
            for item in relevant:
                date_key = item["date"].isoformat()
                store_key = item["store"].strip().lower()
                key = (date_key, item["time"], store_key)
                saved = note_map.get(key)
                if saved is None:
                    candidates = notes_by_date_store.get((date_key, store_key), [])
                    if len(candidates) == 1:
                        saved = candidates[0]
                completed = bool(saved["completed"]) if saved else False
                visit_notes = saved["visit_notes"] if saved else ""
                if completed:
                    after_trip_summary["completed_visits"] += 1
                if visit_notes.strip():
                    after_trip_summary["notes_captured"] += 1
                after_trip_visits.append({
                    "date_display": item["date"].strftime("%d %b %Y"),
                    "visit_time": item["time"],
                    "store_name": item["store"],
                    "visit_notes": visit_notes,
                    "completed": completed,
                    "date_iso": date_key,
                })
            after_trip_visits.sort(key=lambda row: (row["date_iso"], row["visit_time"]), reverse=True)
            after_trip_visits = after_trip_visits[:50]
        today_prefix = date.today().isoformat()
        after_trip_summary["actions_today"] = connection.execute(
            "SELECT COUNT(*) FROM store_actions WHERE created_at LIKE ?", (today_prefix + "%",)
        ).fetchone()[0]
    after_trip_summary["pending_visits"] = max(after_trip_summary["total_visits"] - after_trip_summary["completed_visits"], 0)
    if request.args.get("load_road_notes") == "1":
        blocks = []
        for visit in reversed(after_trip_visits):
            if visit["visit_notes"].strip():
                blocks.append(f"Store: {visit['store_name']}\nNotes:\n{visit['visit_notes']}")
        raw_notes = "\n\n---\n\n".join(blocks)
    items = None
    parsed_notes = False
    error = request.args.get("error")
    success = request.args.get("success")
    if request.method == "POST":
        raw_notes = request.form.get("call_notes", "").strip()
        portatour_file = request.files.get("portatour_report")
        combined_items = []

        try:
            if portatour_file and portatour_file.filename:
                schedule_bytes = portatour_file.read()
                portatour_file.stream.seek(0)
                try:
                    schedule_html = schedule_bytes.decode("utf-8-sig")
                except UnicodeDecodeError:
                    schedule_html = schedule_bytes.decode("windows-1252", errors="replace")

                if "<html" in schedule_html.lower() and "portatour" in schedule_html.lower():
                    with sqlite3.connect(DATABASE_PATH) as connection:
                        connection.execute(
                            "INSERT INTO trip_schedules (filename, raw_html, uploaded_at) VALUES (?, ?, ?)",
                            (
                                portatour_file.filename,
                                schedule_html,
                                datetime.now().isoformat(timespec="seconds"),
                            ),
                        )
                        connection.commit()

                combined_items.extend(extract_portatour_followups(portatour_file))
            if raw_notes:
                combined_items.extend(extract_followups(raw_notes))
            if not (portatour_file and portatour_file.filename) and not raw_notes:
                error = "Upload a Portatour report or paste some call notes before extracting follow-ups."
            else:
                # Remove exact duplicates while preserving order.
                deduplicated = []
                seen = set()
                for item in combined_items:
                    key = (item["store"].lower(), item["title"].lower(), item["detail"].lower())
                    if key not in seen:
                        seen.add(key)
                        deduplicated.append(item)
                items = mark_existing_action_duplicates(deduplicated)
                items.sort(key=lambda item: (item["store"].casefold(), item["title"].casefold()))
                parsed_notes = True
                if portatour_file and portatour_file.filename:
                    raw_notes = raw_notes or f"Imported call report from {portatour_file.filename}"
        except Exception as exc:
            error = str(exc)

    return render_template_string(
        PAGE, view="follow_up", raw_notes=raw_notes, followup_items=items,
        parsed_notes=parsed_notes, error=error, success=success,
        after_trip_summary=after_trip_summary, after_trip_visits=after_trip_visits,
        active_trip=dict(active_trip) if active_trip else None, active_trip_id=active_trip_id,
        open_action_count=get_open_action_count()
    )

@app.route("/follow-up/save", methods=["POST"])
def save_followups():
    raw_notes = request.form.get("raw_notes", "")
    active_trip_id = request.form.get("trip_id", type=int)
    try:
        count = int(request.form.get("item_count", "0"))
    except ValueError:
        count = 0

    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    skipped_duplicates = 0
    extracted = []

    with sqlite3.connect(DATABASE_PATH) as connection:
        existing_rows = connection.execute(
            "SELECT store_name, title FROM store_actions WHERE status != 'Completed'"
        ).fetchall()
        existing_keys = {
            normalise_action_key(store_name, title)
            for store_name, title in existing_rows
        }

        for index in range(count):
            if request.form.get(f"selected_{index}") != "on":
                continue

            store = request.form.get(f"store_{index}", "").strip() or "Store not identified"
            title = request.form.get(f"title_{index}", "").strip()
            detail = request.form.get(f"detail_{index}", "").strip()
            priority = request.form.get(f"priority_{index}", "Medium")
            due_date = request.form.get(f"due_{index}", "")
            armoursteel = request.form.get(f"armoursteel_{index}") == "1"
            email_draft = request.form.get(f"draft_{index}", "").strip()

            if not title:
                continue

            key = normalise_action_key(store, title)
            if key in existing_keys:
                skipped_duplicates += 1
                continue

            source = request.form.get(
                f"source_{index}", "Follow-Up Assistant"
            ).strip() or "Follow-Up Assistant"
            if armoursteel and "Armorsteel" not in source:
                source = f"{source} - Armorsteel"

            connection.execute(
                "INSERT INTO store_actions "
                "(store_name,title,detail,source,priority,due_date,status,notes,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (store, title, detail, source, priority, due_date, "Open", "", now, now),
            )
            existing_keys.add(key)
            extracted.append({
                "store": store,
                "title": title,
                "detail": detail,
                "priority": priority,
                "due_date": due_date,
                "armoursteel": armoursteel,
                "email_draft": email_draft,
            })
            saved += 1

        connection.execute(
            "INSERT INTO follow_up_runs "
            "(raw_notes, extracted_json, saved_action_count, created_at) "
            "VALUES (?,?,?,?)",
            (raw_notes, json.dumps(extracted), saved, now),
        )
        connection.commit()

    message = f"{saved} approved action{'s' if saved != 1 else ''} saved to the Action Tracker."
    if skipped_duplicates:
        message += (
            f" {skipped_duplicates} duplicate action"
            f"{'s were' if skipped_duplicates != 1 else ' was'} skipped."
        )
    return redirect(url_for("follow_up", success=message, trip_id=active_trip_id) if active_trip_id else url_for("follow_up", success=message))


ACTION_STATUSES = ["Open", "In Progress", "Waiting on Store", "Completed"]


def get_open_action_count():
    with sqlite3.connect(DATABASE_PATH) as connection:
        return connection.execute("SELECT COUNT(*) FROM store_actions WHERE status != 'Completed'").fetchone()[0]


def action_template_defaults(source=None):
    source = source or {}
    return {
        "store_name": source.get("store_name", ""), "title": source.get("title", ""),
        "detail": source.get("detail", ""), "source": source.get("source", ""),
        "priority": source.get("priority", "Medium"), "due_date": source.get("due_date", ""),
        "status": source.get("status", "Open"), "notes": source.get("notes", ""),
    }


def consolidate_fineline_rows(rows):
    """Guarantee one display row per fineline, regardless of the source or saved format."""
    grouped = {}
    for row in rows or []:
        name = str(row.get("fineline", "")).replace("\u00a0", " ").strip()
        key = normalise_fineline(name)
        if not key:
            continue
        bucket = grouped.setdefault(key, {
            "fineline": name.title(),
            "ty": 0.0,
            "ly": 0.0,
            "opp_gap": 0.0,
            "stock_risks": 0,
            "core_gaps": 0,
        })
        bucket["ty"] += safe_number(row.get("sales_ty", 0))
        bucket["ly"] += safe_number(row.get("sales_ly", 0))
        bucket["opp_gap"] = max(bucket["opp_gap"], safe_number(row.get("opp_gap", 0)))
        bucket["stock_risks"] = max(bucket["stock_risks"], int(safe_number(row.get("stock_risks", 0))))
        bucket["core_gaps"] = max(bucket["core_gaps"], int(safe_number(row.get("core_gaps", 0))))

    result = []
    for bucket in grouped.values():
        ty, ly = bucket["ty"], bucket["ly"]
        change = ((ty - ly) / ly * 100) if ly else (100.0 if ty else 0.0)
        result.append({
            "fineline": bucket["fineline"],
            "sales_ty": money(ty),
            "sales_ly": money(ly),
            "change_pct": fmt_percent(change),
            "change_raw": change,
            "opp_gap": money(bucket["opp_gap"]),
            "stock_risks": bucket["stock_risks"],
            "core_gaps": bucket["core_gaps"],
        })
    result.sort(key=lambda row: safe_number(row["sales_ty"]), reverse=True)
    return result


@app.route("/opportunities", methods=["GET", "POST"])
def opportunities():
    template_data = {
        "view": "opportunities",
        "opportunity_results": False,
        "error": None,
        "success": None,
        "analysis_id": None,
    }
    if request.method == "POST":
        uploads = {
            "monthly": request.files.get("monthly_report"),
            "woc": request.files.get("woc_report"),
            "ranking": request.files.get("ranking_report"),
            "cfm": request.files.get("cfm_report"),
        }
        labels = {
            "monthly": "Monthly Dashboard",
            "woc": "Weeks of Cover",
            "ranking": "Ranking Report",
            "cfm": "Core For Me",
        }
        missing = [labels[key] for key, file in uploads.items() if not file or not file.filename]
        if missing:
            template_data["error"] = f"Please choose all four reports. Missing: {', '.join(missing)}."
        else:
            try:
                filenames = {}
                frames = {}
                for key, uploaded_file in uploads.items():
                    filenames[key], frames[key] = read_csv_upload(uploaded_file, key)
                store_name = extract_store_name(frames["woc"], frames["cfm"])
                analysis = analyse_mitre10_reports(
                    frames["monthly"], frames["woc"], frames["ranking"], frames["cfm"], store_name
                )
                analysis["fineline_rows"] = consolidate_fineline_rows(analysis.get("fineline_rows", []))
                uploaded_files = [
                    {"label": labels[key], "name": filenames[key]} for key in ["monthly", "woc", "ranking", "cfm"]
                ]
                analysis["uploaded_files"] = uploaded_files
                template_data.update(analysis)
                template_data.update({
                    "opportunity_results": True,
                    "success": "All four reports were combined successfully.",
                    "uploaded_files": uploaded_files,
                    "opportunity_payload": json.dumps(analysis),
                    "analysis_id": None,
                })
            except Exception as exc:
                template_data["error"] = f"The store dashboard could not be built. Details: {exc}"
    return render_template_string(PAGE, **template_data)



@app.route("/save-opportunity", methods=["POST"])
def save_opportunity():
    raw = request.form.get("analysis_json", "")
    if not raw:
        return redirect(url_for("opportunities", error="The store analysis could not be saved because its data was missing."))
    try:
        analysis = json.loads(raw)
        analysis["fineline_rows"] = consolidate_fineline_rows(analysis.get("fineline_rows", []))
        store_name = str(analysis["store_name"]).strip()
        export_date = str(analysis.get("export_date", "date not supplied"))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        return redirect(url_for("opportunities", error=f"The store analysis could not be saved. Details: {exc}"))

    saved_at = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DATABASE_PATH) as connection:
        # Replace the existing store analysis regardless of capitalisation or extra spaces.
        matches = connection.execute(
            "SELECT id FROM opportunity_analyses WHERE lower(trim(store_name)) = lower(trim(?)) ORDER BY id",
            (store_name,),
        ).fetchall()
        if matches:
            keep_id = matches[0][0]
            connection.execute(
                """
                UPDATE opportunity_analyses
                SET store_name = ?, export_date = ?, analysis_json = ?, saved_at = ?
                WHERE id = ?
                """,
                (store_name, export_date, json.dumps(analysis), saved_at, keep_id),
            )
            if len(matches) > 1:
                duplicate_ids = [row[0] for row in matches[1:]]
                placeholders = ",".join("?" for _ in duplicate_ids)
                connection.execute(
                    f"DELETE FROM opportunity_analyses WHERE id IN ({placeholders})",
                    duplicate_ids,
                )
        else:
            connection.execute(
                """
                INSERT INTO opportunity_analyses (store_name, export_date, analysis_json, saved_at)
                VALUES (?, ?, ?, ?)
                """,
                (store_name, export_date, json.dumps(analysis), saved_at),
            )
        connection.commit()
    return redirect(url_for("opportunity_library", success=f"{store_name} was saved successfully."))


@app.route("/opportunity-library")
def opportunity_library():
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        records = connection.execute(
            "SELECT * FROM opportunity_analyses ORDER BY store_name COLLATE NOCASE"
        ).fetchall()
    store_rows = []
    for record in records:
        try:
            analysis = json.loads(record["analysis_json"])
        except json.JSONDecodeError:
            continue
        saved_date = datetime.fromisoformat(record["saved_at"])
        store_rows.append({
            "id": record["id"],
            "store_name": record["store_name"],
            "saved_display": saved_date.strftime("%d %b %Y %H:%M"),
            "total_sales_ty": analysis.get("total_sales_ty", "$0.00"),
            "total_change_pct": analysis.get("total_change_pct", "0.0%"),
            "total_change_raw": analysis.get("total_change_raw", 0),
            "urgent_stock_count": analysis.get("urgent_stock_count", 0),
            "core_gap_count": analysis.get("core_gap_count", 0),
            "total_opp_gap": analysis.get("total_opp_gap", "$0.00"),
        })
    return render_template_string(
        PAGE,
        view="opportunity_library",
        store_rows=store_rows,
        success=request.args.get("success"),
        error=request.args.get("error"),
    )


def load_saved_opportunity(analysis_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        record = connection.execute(
            "SELECT * FROM opportunity_analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
    if record is None:
        return None, None
    try:
        analysis = json.loads(record["analysis_json"])
        # Rebuild older saved analyses by summing duplicate fineline rows. Older versions
        # stored product-level rows, so simply keeping the last duplicate was not sufficient.
        cleaned_rows = {}
        for row in analysis.get("fineline_rows", []):
            name = str(row.get("fineline", "")).strip()
            key = normalise_fineline(name)
            if not key:
                continue
            bucket = cleaned_rows.setdefault(key, {
                "fineline": name.title(), "ty": 0.0, "ly": 0.0,
                "opp_gap": 0.0, "stock_risks": 0, "core_gaps": 0,
            })
            bucket["ty"] += safe_number(row.get("sales_ty", 0))
            bucket["ly"] += safe_number(row.get("sales_ly", 0))
            # These values repeat on each product row; use the maximum rather than summing.
            bucket["opp_gap"] = max(bucket["opp_gap"], safe_number(row.get("opp_gap", 0)))
            bucket["stock_risks"] = max(bucket["stock_risks"], int(safe_number(row.get("stock_risks", 0))))
            bucket["core_gaps"] = max(bucket["core_gaps"], int(safe_number(row.get("core_gaps", 0))))

        rebuilt = []
        for bucket in cleaned_rows.values():
            ty, ly = bucket["ty"], bucket["ly"]
            change = ((ty - ly) / ly * 100) if ly else (100.0 if ty else 0.0)
            rebuilt.append({
                "fineline": bucket["fineline"],
                "sales_ty": money(ty),
                "sales_ly": money(ly),
                "change_pct": fmt_percent(change),
                "change_raw": change,
                "opp_gap": money(bucket["opp_gap"]),
                "stock_risks": bucket["stock_risks"],
                "core_gaps": bucket["core_gaps"],
            })
        rebuilt.sort(key=lambda row: safe_number(row["sales_ty"]), reverse=True)
        analysis["fineline_rows"] = consolidate_fineline_rows(rebuilt)
        return record, analysis
    except json.JSONDecodeError:
        return record, None


@app.route("/opportunity/<int:analysis_id>")
def saved_opportunity(analysis_id):
    record, analysis = load_saved_opportunity(analysis_id)
    if record is None or analysis is None:
        return redirect(url_for("opportunity_library", error="That saved store analysis could not be opened."))
    data = dict(analysis)
    data.update({
        "view": "saved_opportunity",
        "opportunity_results": True,
        "analysis_id": analysis_id,
        "success": None,
        "error": None,
    })
    return render_template_string(PAGE, **data)


@app.route("/opportunity/<int:analysis_id>/brief")
def visit_brief(analysis_id):
    record, analysis = load_saved_opportunity(analysis_id)
    if record is None or analysis is None:
        return redirect(url_for("opportunity_library", error="That saved store analysis could not be opened."))
    data = dict(analysis)
    data.update({
        "view": "visit_brief",
        "opportunity_results": True,
        "analysis_id": analysis_id,
        "success": None,
        "error": None,
    })
    return render_template_string(PAGE, **data)




def trip_readiness_status(plan):
    checks = [
        bool(plan.get("appointments_confirmed")),
        bool(plan.get("brochures_packed")),
        bool(plan.get("samples_packed")),
        bool(plan.get("briefs_reviewed")),
        bool(plan.get("travel_confirmed")),
    ]
    done = sum(checks)
    try:
        end_date = datetime.strptime(plan.get("end_date", ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        end_date = None
    if end_date and end_date < date.today():
        return "complete", "Completed"
    if done == len(checks):
        return "ready", "Trip ready"
    booking_values = [plan.get("flight_status"), plan.get("rental_status"), plan.get("accommodation_status")]
    booking_problem = any(value in {"Not Booked", "Quote Received"} for value in booking_values)
    if booking_problem or (done and done < 3):
        return "attention", "Needs attention"
    if done >= 3:
        return "progress", "Planning underway"
    return "not-started", "Not started"


def build_year_planner(year, plans, selected_trip_id=None):
    trips_by_date = {}
    formatted_plans = []
    for record in plans:
        plan = dict(record)
        try:
            start = datetime.strptime(plan["start_date"], "%Y-%m-%d").date()
            end = datetime.strptime(plan["end_date"], "%Y-%m-%d").date()
        except (KeyError, TypeError, ValueError):
            continue
        status_key, status_label = trip_readiness_status(plan)
        plan.update({"status_key": status_key, "status_label": status_label})
        formatted_plans.append(plan)
        current = max(start, date(year, 1, 1))
        last = min(end, date(year, 12, 31))
        while current <= last:
            trips_by_date.setdefault(current, []).append(plan)
            current += timedelta(days=1)

    months = []
    today = date.today()
    for month_number in range(1, 13):
        if month_number == 12:
            next_month = date(year + 1, 1, 1)
        else:
            next_month = date(year, month_number + 1, 1)
        days_in_month = (next_month - date(year, month_number, 1)).days
        days = []
        for day_number in range(1, days_in_month + 1):
            current = date(year, month_number, day_number)
            days.append({
                "day": day_number,
                "weekday": current.strftime("%a")[:2],
                "weekend": current.weekday() >= 5,
                "today": current == today,
                "trips": trips_by_date.get(current, []),
            })
        months.append({"name": date(year, month_number, 1).strftime("%b"), "days": days})
    return months


@app.route("/plan-trip", methods=["GET", "POST"])
@app.route("/trip-prep", methods=["GET", "POST"])
def trip_prep():
    today = date.today()
    prep_rows, appointments = [], []
    error = None
    success = request.args.get("success")
    should_generate = request.args.get("generate") == "1"

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        requested_id = request.args.get("trip", type=int)
        if requested_id:
            trip_plan = connection.execute("SELECT * FROM trip_plans WHERE id=?", (requested_id,)).fetchone()
            latest_schedule = connection.execute("SELECT * FROM trip_schedules WHERE trip_plan_id=? ORDER BY id DESC LIMIT 1", (requested_id,)).fetchone()
        else:
            trip_plan = connection.execute("SELECT * FROM trip_plans ORDER BY start_date DESC, id DESC LIMIT 1").fetchone()
            latest_schedule = connection.execute("SELECT * FROM trip_schedules ORDER BY id DESC LIMIT 1").fetchone()

    if request.method == "POST":
        action = request.form.get("action", "generate")
        uploaded = request.files.get("schedule_report")
        now = datetime.now().isoformat(timespec="seconds")

        if action == "save_trip_plan":
            name = request.form.get("trip_name", "").strip()
            start_date = request.form.get("start_date", "")
            end_date = request.form.get("end_date", "")
            if not name or not start_date or not end_date:
                error = "Trip name, start date and end date are required."
            elif end_date < start_date:
                error = "The trip end date cannot be before the start date."
            else:
                values=(name, request.form.get("destination", "").strip(), start_date, end_date, request.form.get("travel_method", "Drive"), request.form.get("notes", "").strip(), now)
                plan_id=request.form.get("trip_plan_id", type=int)
                with sqlite3.connect(DATABASE_PATH) as connection:
                    if plan_id:
                        connection.execute("UPDATE trip_plans SET trip_name=?,destination=?,start_date=?,end_date=?,travel_method=?,notes=?,updated_at=? WHERE id=?", values+(plan_id,))
                    else:
                        cur=connection.execute("INSERT INTO trip_plans (trip_name,destination,start_date,end_date,travel_method,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", values[:-1]+(now,now))
                        plan_id=cur.lastrowid
                    connection.commit()
                return redirect(url_for("trip_prep", trip=plan_id, success="Trip details saved."))

        elif action == "save_appointment_objective":
            plan_id=request.form.get("trip_plan_id",type=int) or 0
            appointment_id=request.form.get("appointment_id","").strip()
            if not appointment_id:
                error="The appointment could not be identified."
            else:
                with sqlite3.connect(DATABASE_PATH) as connection:
                    connection.execute("""INSERT INTO appointment_objectives (appointment_id,trip_plan_id,schedule_id,visit_date,visit_time,store_name,desired_outcome,preparation_notes,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(appointment_id) DO UPDATE SET desired_outcome=excluded.desired_outcome,preparation_notes=excluded.preparation_notes,store_name=excluded.store_name,visit_date=excluded.visit_date,visit_time=excluded.visit_time,updated_at=excluded.updated_at""",
                    (appointment_id,plan_id,request.form.get("schedule_id",type=int) or 0,request.form.get("visit_date",""),request.form.get("visit_time",""),request.form.get("store_name","").strip(),request.form.get("desired_outcome","").strip(),request.form.get("preparation_notes","").strip(),now))
                    connection.commit()
                return redirect(url_for("trip_prep",trip=plan_id,success="Appointment objective saved.")+"#appointments")

        elif action == "save_trip_travel":
            plan_id=request.form.get("trip_plan_id", type=int)
            if not plan_id:
                error="Save the trip details before recording travel."
            else:
                confirmed=1 if request.form.get("travel_confirmed") else 0
                with sqlite3.connect(DATABASE_PATH) as connection:
                    connection.execute("UPDATE trip_plans SET flight_status=?,rental_status=?,accommodation_status=?,travel_notes=?,travel_confirmed=?,updated_at=? WHERE id=?", (request.form.get("flight_status","Not Required"),request.form.get("rental_status","Not Required"),request.form.get("accommodation_status","Not Required"),request.form.get("travel_notes","").strip(),confirmed,now,plan_id))
                    connection.commit()
                return redirect(url_for("trip_prep", trip=plan_id, success="Travel arrangements saved."))

        elif action == "save_trip_readiness":
            plan_id=request.form.get("trip_plan_id", type=int)
            if plan_id:
                fields=["appointments_confirmed","brochures_packed","samples_packed","briefs_reviewed"]
                vals=[1 if request.form.get("ready_"+f) else 0 for f in fields]
                with sqlite3.connect(DATABASE_PATH) as connection:
                    connection.execute("UPDATE trip_plans SET appointments_confirmed=?,brochures_packed=?,samples_packed=?,briefs_reviewed=?,updated_at=? WHERE id=?", (*vals,now,plan_id))
                    connection.commit()
                return redirect(url_for("trip_prep", trip=plan_id, success="Readiness checklist saved."))

        elif action == "upload_generate":
            if not uploaded or not uploaded.filename:
                error = "Choose a Portatour HTML schedule before uploading."
            else:
                raw=uploaded.read()
                try: html_text=raw.decode("utf-8-sig")
                except UnicodeDecodeError: html_text=raw.decode("windows-1252",errors="replace")
                if "<html" not in html_text.lower() or "portatour" not in html_text.lower():
                    error="That file does not appear to be a Portatour HTML report."
                else:
                    with sqlite3.connect(DATABASE_PATH) as connection:
                        cur=connection.execute("INSERT INTO trip_schedules (filename,raw_html,uploaded_at) VALUES (?,?,?)",(uploaded.filename,html_text,now)); connection.commit(); sid=cur.lastrowid
                        connection.row_factory=sqlite3.Row; latest_schedule=connection.execute("SELECT * FROM trip_schedules WHERE id=?",(sid,)).fetchone()
                    should_generate=True; success=f"{uploaded.filename} uploaded and appointments loaded."
        else:
            if latest_schedule is None: error="Upload a Portatour schedule first."
            else: should_generate=True

    if latest_schedule is not None:
        raw_appointments=parse_schedule_appointments(latest_schedule["raw_html"], today)
        objective_map={}
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.row_factory=sqlite3.Row
            for row in connection.execute("SELECT * FROM appointment_objectives WHERE schedule_id=?",(latest_schedule["id"],)).fetchall(): objective_map[row["appointment_id"]]=dict(row)
        plan_for_id=(trip_plan["id"] if trip_plan else 0)
        for appt in raw_appointments:
            appt_id=appointment_uid(plan_for_id,latest_schedule["id"],appt['date'].isoformat(),appt['time'],appt['store'])
            obj=objective_map.get(appt_id,{})
            appointments.append({**appt,"date_display":f"{appt['date'].day} {appt['date'].strftime('%b %Y')}","date_iso":appt['date'].isoformat(),"customer_type":classify_customer(appt["store"]),"appointment_id":appt_id,"schedule_id":latest_schedule["id"],"desired_outcome":obj.get("desired_outcome",''),"preparation_notes":obj.get("preparation_notes",'')})
        if should_generate or appointments:
            for index, appointment in enumerate(raw_appointments):
                quantities=default_brochure_quantities(appointment["store"])
                if request.method=="POST" and request.form.get("action") in {"recalculate","download_pdf"}:
                    for item in BROCHURE_TYPES:
                        field=f"qty_{index}_{item.replace(' ', '_')}"
                        try: quantities[item]=max(0,int(request.form.get(field,quantities[item])))
                        except (TypeError,ValueError): pass
                prep_rows.append({"row_index":index,"store":appointment["store"],"date_display":f"{appointment['date'].day} {appointment['date'].strftime('%b %Y')}","time":appointment["time"],"customer_type":classify_customer(appointment["store"]),"quantities":quantities,"total":sum(quantities.values())})

    if request.method=="POST" and request.form.get("action")=="download_pdf" and prep_rows and not error:
        return build_packing_pdf(prep_rows, latest_schedule["filename"] if latest_schedule else None)

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        plan_id=request.args.get("trip", type=int)
        trip_plan=connection.execute("SELECT * FROM trip_plans WHERE id=?",(plan_id,)).fetchone() if plan_id else connection.execute("SELECT * FROM trip_plans ORDER BY start_date DESC,id DESC LIMIT 1").fetchone()
    default_plan={"id":None,"trip_name":"","destination":"","start_date":today.isoformat(),"end_date":today.isoformat(),"travel_method":"Drive","notes":"","flight_status":"Not Required","rental_status":"Not Required","accommodation_status":"Not Required","travel_notes":"","travel_confirmed":0,"appointments_confirmed":0,"brochures_packed":0,"samples_packed":0,"briefs_reviewed":0}
    plan=dict(trip_plan) if trip_plan else default_plan
    ready_items=[{"key":"appointments_confirmed","label":"Appointments confirmed","done":bool(plan.get("appointments_confirmed"))},{"key":"brochures_packed","label":"Brochures packed","done":bool(plan.get("brochures_packed"))},{"key":"samples_packed","label":"Samples and tools packed","done":bool(plan.get("samples_packed"))},{"key":"briefs_reviewed","label":"Customer briefs reviewed","done":bool(plan.get("briefs_reviewed"))},{"key":"travel_confirmed","label":"Travel arrangements confirmed","done":bool(plan.get("travel_confirmed"))}]
    done=sum(1 for x in ready_items if x["done"]); readiness={"items":ready_items,"done":done,"total":len(ready_items),"remaining":len(ready_items)-done,"complete":done==len(ready_items)}
    uploaded_display=None
    if latest_schedule:
        try: uploaded_display=datetime.fromisoformat(latest_schedule["uploaded_at"]).strftime("%d %b %Y %H:%M")
        except Exception: uploaded_display=latest_schedule["uploaded_at"]

    try:
        planner_year = int(request.args.get("year", plan.get("start_date", str(today.year))[:4]))
    except (TypeError, ValueError):
        planner_year = today.year
    planner_year = min(max(planner_year, today.year - 5), today.year + 10)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        planner_plans = connection.execute(
            "SELECT * FROM trip_plans WHERE substr(start_date,1,4)=? OR substr(end_date,1,4)=? OR (start_date < ? AND end_date > ?) ORDER BY start_date, id",
            (str(planner_year), str(planner_year), f"{planner_year}-01-01", f"{planner_year}-12-31"),
        ).fetchall()
    planner_months = build_year_planner(planner_year, planner_plans, plan.get("id"))

    return render_template_string(PAGE,view="trip_prep",open_action_count=get_open_action_count(),prep_rows=prep_rows,appointments=appointments,brochure_types=BROCHURE_TYPES,latest_schedule_filename=latest_schedule["filename"] if latest_schedule else None,latest_schedule_uploaded=uploaded_display,trip_prep_error=error,trip_prep_success=success,trip_plan=plan,readiness=readiness,booking_statuses=["Not Required","Not Booked","Quote Received","Booked","Paid","Completed"],planner_year=planner_year,planner_months=planner_months)


def parse_currency_value(value):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(value or "")) or 0)
    except (TypeError, ValueError):
        return 0.0


def customer_priority_classification(value_points, growth_points, opportunity_points, total_score, yoy):
    if value_points >= 25 and yoy >= -5:
        return "Protect", "protect"
    if growth_points + opportunity_points >= 25:
        return "Grow", "grow"
    if total_score >= 30:
        return "Maintain", "maintain"
    return "Low Priority", "low"


def build_customer_priority_rows():
    today = date.today()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        manual_rows = {r["store_name"].strip().lower(): dict(r) for r in connection.execute("SELECT * FROM customer_priorities").fetchall()}
        analyses = connection.execute("SELECT store_name, analysis_json FROM opportunity_analyses").fetchall()
        actions = connection.execute("SELECT store_name, priority, status FROM store_actions WHERE status != 'Completed'").fetchall()
        mapped_rows = {r["name"].strip().lower(): dict(r) for r in connection.execute("SELECT * FROM route_customers").fetchall() if r["name"]}

    stores = {}
    for record in analyses:
        name = record["store_name"].strip()
        key = name.lower()
        stores.setdefault(key, {"store_name": name, "analysis": None, "actions": []})
        try:
            stores[key]["analysis"] = json.loads(record["analysis_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    for action in actions:
        name = action["store_name"].strip()
        if not name:
            continue
        key = name.lower()
        stores.setdefault(key, {"store_name": name, "analysis": None, "actions": []})
        stores[key]["actions"].append(dict(action))
    for key, manual in manual_rows.items():
        stores.setdefault(key, {"store_name": manual["store_name"], "analysis": None, "actions": []})
    # Every Portatour/map customer is a real customer account.  Include it in
    # the Customers tab even when it has no opportunity analysis or action yet.
    for key, mapped in mapped_rows.items():
        stores.setdefault(key, {"store_name": mapped["name"], "analysis": None, "actions": []})
        stores[key]["mapped"] = mapped

    rows = []
    for key, entry in stores.items():
        manual = manual_rows.get(key, {})
        mapped = entry.get("mapped") or mapped_rows.get(key, {})
        analysis = entry.get("analysis") or {}
        auto_sales = parse_currency_value(analysis.get("total_sales_ty", 0))
        manual_sales = float(manual.get("manual_sales_value", 0) or 0)
        sales = manual_sales if manual_sales > 0 else auto_sales
        yoy = float(analysis.get("total_change_raw", 0) or 0)
        opportunity = parse_currency_value(analysis.get("total_opp_gap", 0))
        urgent = int(analysis.get("urgent_stock_count", 0) or 0)
        core = int(analysis.get("core_gap_count", 0) or 0)
        tier_growth = {"A": 5, "B": 4, "C": 3, "D": 2}
        growth_potential = max(1, min(5, int(manual.get("growth_potential", tier_growth.get(str(mapped.get("priority_tier", "C") or "C").upper(), 3)) or 3)))
        frequency = max(1, int(manual.get("visit_frequency_days", mapped.get("call_interval", 42)) or 42))
        last_visit_text = str(manual.get("last_visit_date", mapped.get("last_call", "")) or "")
        open_actions = len(entry["actions"])
        high_actions = sum(1 for a in entry["actions"] if a.get("priority") == "High")

        if sales >= 100000: value_points = 30
        elif sales >= 50000: value_points = 25
        elif sales >= 20000: value_points = 18
        elif sales > 0: value_points = 10
        else: value_points = 0

        if opportunity >= 20000: opportunity_points = 25
        elif opportunity >= 10000: opportunity_points = 20
        elif opportunity >= 5000: opportunity_points = 15
        elif opportunity > 0: opportunity_points = 8
        else: opportunity_points = 0

        growth_points = growth_potential * 5
        action_points = min(10, high_actions * 4 + max(0, open_actions - high_actions))
        issue_points = min(10, urgent * 2 + core)
        trend_points = 5 if yoy >= 10 else (3 if yoy >= 0 else (7 if yoy <= -15 else 4))

        visit_points = 0
        visit_status = "No visit date"
        visit_detail = f"Suggested cycle: every {frequency} days"
        if last_visit_text:
            try:
                last_visit = datetime.strptime(last_visit_text, "%Y-%m-%d").date()
                days_since = (today - last_visit).days
                days_until_due = frequency - days_since
                if days_until_due < 0:
                    visit_points = 10
                    visit_status = "Overdue"
                    visit_detail = f"{abs(days_until_due)} days past the {frequency}-day cycle"
                elif days_until_due <= 7:
                    visit_points = 5
                    visit_status = "Due soon"
                    visit_detail = f"Due in {days_until_due} days"
                else:
                    visit_status = "On cycle"
                    visit_detail = f"Due in {days_until_due} days"
            except ValueError:
                pass

        total = min(100, value_points + opportunity_points + growth_points + action_points + issue_points + trend_points + visit_points)
        classification, class_key = customer_priority_classification(value_points, growth_points, opportunity_points, total, yoy)
        breakdown = f"Value {value_points} · Opportunity {opportunity_points} · Potential {growth_points} · Follow-up {action_points + issue_points} · Visit {visit_points}"
        rows.append({
            "store_name": entry["store_name"], "score": total, "classification": classification, "class_key": class_key,
            "sales_display": money(sales), "yoy_display": fmt_percent(yoy), "opportunity_display": money(opportunity),
            "open_actions": open_actions, "high_actions": high_actions, "visit_status": visit_status, "visit_detail": visit_detail,
            "growth_potential": growth_potential, "visit_frequency_days": frequency, "last_visit_date": last_visit_text,
            "manual_sales_value": f"{manual_sales:.2f}", "strategic_notes": str(manual.get("strategic_notes", "") or ""),
            "score_breakdown": breakdown,
            "customer_group": str(mapped.get("customer_group", "") or classify_customer(entry["store_name"])),
            "street": str(mapped.get("street", "") or ""), "city": str(mapped.get("city", "") or ""),
            "zip": str(mapped.get("zip", "") or ""), "country": str(mapped.get("country", "") or ""),
            "address_display": ", ".join(str(v).strip() for v in [mapped.get("street", ""), mapped.get("city", ""), mapped.get("zip", "")] if v and str(v).strip()),
            "phone": str(mapped.get("phone", "") or ""), "email": str(mapped.get("email", "") or ""),
            "call_note": str(mapped.get("call_note", "") or ""), "categories": str(mapped.get("categories", "") or ""),
            "urgency": str(mapped.get("urgency", "") or ""), "priority_tier": str(mapped.get("priority_tier", "C") or "C"),
            "ideal_call_minutes": int(mapped.get("ideal_call_minutes", 45) or 45),
            "customer_open_time": str(mapped.get("customer_open_time", "08:00") or "08:00"),
            "customer_close_time": str(mapped.get("customer_close_time", "17:00") or "17:00"),
            "latitude": mapped.get("latitude"), "longitude": mapped.get("longitude"),
            "external_id": str(mapped.get("external_id", "") or ""), "portatour_id": str(mapped.get("portatour_id", "") or ""),
            "is_mapped_customer": bool(mapped),
        })
    return sorted(rows, key=lambda r: (-r["score"], r["store_name"].lower()))


@app.route("/customer-priorities", methods=["GET", "POST"])
def customer_priorities():
    if request.method == "POST":
        store_name = request.form.get("store_name", "").strip()
        if not store_name:
            return redirect(url_for("customer_priorities", error="Customer name is required."))
        try:
            growth = max(1, min(5, int(request.form.get("growth_potential", 3))))
            frequency = max(1, int(request.form.get("visit_frequency_days", 42)))
            manual_sales = max(0.0, float(request.form.get("manual_sales_value", 0) or 0))
        except ValueError:
            return redirect(url_for("customer_priorities", error="Potential, visit cycle and sales value must be valid numbers."))
        last_visit = request.form.get("last_visit_date", "").strip()
        notes = request.form.get("strategic_notes", "").strip()
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute(
                """INSERT INTO customer_priorities (store_name, growth_potential, visit_frequency_days, last_visit_date, manual_sales_value, strategic_notes, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(store_name) DO UPDATE SET growth_potential=excluded.growth_potential, visit_frequency_days=excluded.visit_frequency_days,
                last_visit_date=excluded.last_visit_date, manual_sales_value=excluded.manual_sales_value, strategic_notes=excluded.strategic_notes, updated_at=excluded.updated_at""",
                (store_name, growth, frequency, last_visit, manual_sales, notes, datetime.now().isoformat(timespec="seconds")),
            )
            connection.commit()
        return redirect(url_for("customer_priorities", success=f"{store_name} priority settings saved."))

    rows = build_customer_priority_rows()
    counts = {"protect": 0, "grow": 0, "maintain": 0, "low": 0}
    for row in rows:
        counts[row["class_key"]] += 1
    return render_template_string(PAGE, view="customer_priorities", priority_rows=rows, priority_counts=counts,
                                  success=request.args.get("success"), error=request.args.get("error"),
                                  open_action_count=get_open_action_count())



@app.route("/customers/<path:store_name>")
def customer_detail(store_name):
    rows = build_customer_priority_rows()
    customer = next((row for row in rows if row["store_name"].strip().lower() == store_name.strip().lower()), None)
    if customer is None:
        return redirect(url_for("customer_priorities", error="That customer could not be found."))
    customer["customer_type"] = classify_customer(customer["store_name"])
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        action_records = connection.execute(
            "SELECT * FROM store_actions WHERE lower(trim(store_name)) = lower(trim(?)) AND status != 'Completed' ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, due_date, updated_at DESC",
            (customer["store_name"],),
        ).fetchall()
        analysis_record = connection.execute(
            "SELECT * FROM opportunity_analyses WHERE lower(trim(store_name)) = lower(trim(?)) LIMIT 1",
            (customer["store_name"],),
        ).fetchone()
    customer_actions = [action_display_row(record) for record in action_records]
    analysis = None
    analysis_id = None
    if analysis_record:
        try:
            analysis = json.loads(analysis_record["analysis_json"])
            analysis_id = analysis_record["id"]
        except json.JSONDecodeError:
            analysis = None
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        contacts=[dict(r) for r in connection.execute("SELECT * FROM customer_contacts WHERE lower(trim(store_name))=lower(trim(?)) ORDER BY contact_name COLLATE NOCASE",(store_name,)).fetchall()]
        history_rows=connection.execute("""SELECT n.*,s.trip_plan_id,p.trip_name,o.desired_outcome FROM road_visit_notes n LEFT JOIN trip_schedules s ON s.id=n.schedule_id LEFT JOIN trip_plans p ON p.id=s.trip_plan_id LEFT JOIN appointment_objectives o ON o.appointment_id=n.appointment_id WHERE lower(trim(n.store_name))=lower(trim(?)) ORDER BY n.visit_date DESC,n.visit_time DESC""",(store_name,)).fetchall()
        attachments=connection.execute("SELECT * FROM road_visit_attachments WHERE lower(trim(store_name))=lower(trim(?)) ORDER BY uploaded_at DESC",(store_name,)).fetchall()
        att_map={}
        for a in attachments: att_map.setdefault(a['appointment_id'] or (a['visit_date'],a['visit_time']),[]).append({'original_name':a['original_name'],'url':url_for('road_attachment',attachment_id=a['id'])})
        upcoming=connection.execute("""SELECT o.*,p.trip_name FROM appointment_objectives o LEFT JOIN trip_plans p ON p.id=o.trip_plan_id WHERE lower(trim(o.store_name))=lower(trim(?)) AND o.visit_date>=? ORDER BY o.visit_date,o.visit_time""",(store_name,date.today().isoformat())).fetchall()
    customer_call_history=[]
    for r in history_rows:
        status='Completed' if r['completed'] else ('Skipped' if r['skipped'] else 'Recorded')
        customer_call_history.append({**dict(r),'date_display':datetime.strptime(r['visit_date'],'%Y-%m-%d').strftime('%d %b %Y') if r['visit_date'] else '', 'status':status,'badge_class':'change-positive' if status=='Completed' else ('change-negative' if status=='Skipped' else 'change-neutral'),'attachments':att_map.get(r['appointment_id'] or (r['visit_date'],r['visit_time']),[])})
    upcoming_customer_visits=[{**dict(r),'date_display':datetime.strptime(r['visit_date'],'%Y-%m-%d').strftime('%d %b %Y') if r['visit_date'] else ''} for r in upcoming]
    return render_template_string(
        PAGE, view="customer_detail", customer=customer, customer_actions=customer_actions,
        analysis=analysis, analysis_id=analysis_id, open_action_count=get_open_action_count(),
        customer_contacts=contacts, customer_call_history=customer_call_history, upcoming_customer_visits=upcoming_customer_visits,
    )

@app.route("/customer-contact/add",methods=["POST"])
def add_customer_contact():
    store=request.form.get("store_name","").strip(); name=request.form.get("contact_name","").strip(); now=datetime.now().isoformat(timespec="seconds")
    if store and name:
        with sqlite3.connect(DATABASE_PATH) as connection:
            connection.execute("INSERT INTO customer_contacts (store_name,contact_name,role,phone,email,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",(store,name,request.form.get("role","").strip(),request.form.get("phone","").strip(),request.form.get("email","").strip(),request.form.get("notes","").strip(),now,now)); connection.commit()
    return redirect(url_for("customer_detail",store_name=store))

@app.route("/customer-contact/<int:contact_id>/delete",methods=["POST"])
def delete_customer_contact(contact_id):
    store=request.form.get("store_name","").strip()
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM customer_contacts WHERE id=?",(contact_id,)); connection.commit()
    return redirect(url_for("customer_detail",store_name=store))

@app.route("/on-the-road")
def on_the_road():
    from urllib.parse import quote_plus
    road_rows = []
    requested_trip_id = request.args.get("trip_id", type=int) or get_active_trip_id()
    if requested_trip_id:
        set_active_trip(requested_trip_id)
    active_trip = None
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        if requested_trip_id:
            active_trip = connection.execute("SELECT * FROM trip_plans WHERE id=?", (requested_trip_id,)).fetchone()
            schedule = connection.execute("SELECT * FROM trip_schedules WHERE trip_plan_id=? ORDER BY id DESC LIMIT 1", (requested_trip_id,)).fetchone()
        else:
            schedule = connection.execute("SELECT * FROM trip_schedules ORDER BY id DESC LIMIT 1").fetchone()
            if schedule and schedule["trip_plan_id"]:
                active_trip = connection.execute("SELECT * FROM trip_plans WHERE id=?", (schedule["trip_plan_id"],)).fetchone()
        actions = connection.execute("SELECT store_name, title FROM store_actions WHERE status != 'Completed'").fetchall()
        customers = {row[0].strip().lower() for row in connection.execute(
            "SELECT store_name FROM customer_priorities UNION SELECT store_name FROM opportunity_analyses UNION SELECT DISTINCT store_name FROM store_actions"
        ).fetchall() if row[0]}
        route_customers = {row["name"].strip().lower(): row for row in connection.execute("SELECT * FROM route_customers").fetchall()}
        priority_rows = build_customer_priority_rows()
        priority_map = {row["store_name"].strip().lower(): row for row in priority_rows}
        saved_notes, attachment_map = {}, {}
        objective_map={}
        if schedule:
            for obj in connection.execute("SELECT * FROM appointment_objectives WHERE schedule_id=?",(schedule["id"],)).fetchall(): objective_map[obj["appointment_id"]]=dict(obj)
            for note in connection.execute("SELECT * FROM road_visit_notes WHERE schedule_id = ?", (schedule["id"],)).fetchall():
                key = (note["visit_date"], note["visit_time"], note["store_name"].strip().lower())
                saved_notes[key] = note
            for attachment in connection.execute("SELECT * FROM road_visit_attachments WHERE schedule_id=? ORDER BY uploaded_at DESC", (schedule["id"],)).fetchall():
                key = (attachment["visit_date"], attachment["visit_time"], attachment["store_name"].strip().lower())
                attachment_map.setdefault(key, []).append(attachment)
    action_map = {}
    for action in actions:
        action_map.setdefault(action["store_name"].strip().lower(), []).append(action["title"])
    if schedule:
        appointments = parse_schedule_appointments(schedule["raw_html"], from_date=date.min if active_trip else date.today())[:100]
        for item in appointments:
            store_key = item["store"].strip().lower()
            titles = action_map.get(store_key, [])
            note_key = (item["date"].isoformat(), item["time"], store_key)
            saved = saved_notes.get(note_key)
            priority = priority_map.get(store_key, {})
            mapped = route_customers.get(store_key)
            day_label = item["date"].strftime("%A, %d %B %Y")
            if item["date"] == date.today(): day_label = "Today - " + day_label
            elif item["date"] == date.today() + timedelta(days=1): day_label = "Tomorrow - " + day_label
            address_parts = []
            if mapped:
                address_parts = [mapped["street"], mapped["city"], mapped["zip"]]
            address = ", ".join(str(v).strip() for v in address_parts if v and str(v).strip())
            destination = address or item["store"]
            navigation_url = "https://www.google.com/maps/dir/?api=1&destination=" + quote_plus(destination)
            started = saved["started_at"] if saved and "started_at" in saved.keys() else ""
            finished = saved["finished_at"] if saved and "finished_at" in saved.keys() else ""
            completed = bool(saved["completed"]) if saved else False
            skipped = bool(saved["skipped"]) if saved and "skipped" in saved.keys() else False
            if completed: status_label = "Completed"
            elif skipped: status_label = "Skipped"
            elif started: status_label = "In progress"
            else: status_label = "Planned"
            actual_time_text = ""
            if started:
                try: actual_time_text = "started " + datetime.fromisoformat(started).strftime("%H:%M")
                except ValueError: pass
            if finished:
                try: actual_time_text += (" · " if actual_time_text else "") + "finished " + datetime.fromisoformat(finished).strftime("%H:%M")
                except ValueError: pass
            appt_id = appointment_uid(active_trip["id"] if active_trip else schedule["trip_plan_id"], schedule["id"], item["date"].isoformat(), item["time"], item["store"])
            with sqlite3.connect(DATABASE_PATH) as appt_connection:
                appt_connection.execute("INSERT INTO trip_appointments (appointment_id,trip_id,schedule_id,visit_date,visit_time,store_name,sequence_no,updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(appointment_id) DO UPDATE SET visit_date=excluded.visit_date,visit_time=excluded.visit_time,store_name=excluded.store_name,sequence_no=excluded.sequence_no,updated_at=excluded.updated_at",
                    (appt_id, active_trip["id"] if active_trip else (schedule["trip_plan_id"] or 0), schedule["id"], item["date"].isoformat(), item["time"], item["store"], len(road_rows)+1, datetime.now().isoformat(timespec="seconds")))
                appt_connection.commit()
            hidden_fields = ''.join([
                f'<input type="hidden" name="appointment_id" value="{appt_id}">',
                f'<input type="hidden" name="schedule_id" value="{schedule["id"]}">',
                f'<input type="hidden" name="visit_date" value="{item["date"].isoformat()}">',
                f'<input type="hidden" name="visit_time" value="{item["time"]}">',
                f'<input type="hidden" name="store_name" value="{item["store"]}">',
                f'<input type="hidden" name="trip_id" value="{active_trip["id"] if active_trip else ""}">'
            ])
            attachments=[]
            for attachment in attachment_map.get(note_key, []):
                try: uploaded_display=datetime.fromisoformat(attachment["uploaded_at"]).strftime("%d %b %Y %H:%M")
                except ValueError: uploaded_display=attachment["uploaded_at"]
                attachments.append({"original_name":attachment["original_name"],"uploaded_display":uploaded_display,"url":url_for("road_attachment", attachment_id=attachment["id"])})
            objective=objective_map.get(appt_id,{})
            road_rows.append({
                "schedule_id": schedule["id"], "store": item["store"], "date_iso": item["date"].isoformat(),
                "date_display": item["date"].strftime("%d %b %Y"), "day_label": day_label, "time": item["time"],
                "call_note": item["call_note"], "desired_outcome": objective.get("desired_outcome",""), "preparation_notes": objective.get("preparation_notes",""), "customer_type": mapped["customer_group"] if mapped else classify_customer(item["store"]),
                "customer_exists": store_key in customers or bool(mapped), "priority_classification": priority.get("classification", mapped["priority_tier"] if mapped else ""),
                "open_action_count": len(titles), "open_action_titles": titles[:4], "visit_notes": saved["visit_notes"] if saved else "",
                "completed": completed, "started_at": started, "finished_at": finished, "skipped": skipped, "status_label": status_label,
                "actual_duration": saved["actual_duration_minutes"] if saved and "actual_duration_minutes" in saved.keys() else 0,
                "outcome": saved["outcome"] if saved and "outcome" in saved.keys() else "", "next_step": saved["next_step"] if saved and "next_step" in saved.keys() else "",
                "actual_time_text": actual_time_text, "address": address, "navigation_url": navigation_url, "hidden_fields": hidden_fields,
                "attachments": attachments
            })
    summary={"total":len(road_rows),"completed":sum(1 for r in road_rows if r["completed"]),"active":sum(1 for r in road_rows if r["started_at"] and not r["completed"]),"remaining":sum(1 for r in road_rows if not r["completed"] and not r["skipped"])}
    return render_template_string(PAGE, view="on_the_road", road_rows=road_rows, road_summary=summary,
        active_trip=dict(active_trip) if active_trip else None, success=request.args.get("success"),
        error=request.args.get("error"), open_action_count=get_open_action_count())


def _road_visit_key_from_form():
    try: schedule_id=int(request.form.get("schedule_id","0"))
    except ValueError: schedule_id=0
    return schedule_id, request.form.get("visit_date","").strip(), request.form.get("visit_time","").strip(), request.form.get("store_name","").strip(), request.form.get("appointment_id","").strip()


def _road_redirect(message=None, error=None):
    kwargs = {}
    trip_id = request.form.get("trip_id", type=int) or request.args.get("trip_id", type=int)
    if trip_id:
        kwargs["trip_id"] = trip_id
    if message:
        kwargs["success"] = message
    if error:
        kwargs["error"] = error
    return redirect(url_for("on_the_road", **kwargs))


def _ensure_road_note(connection, schedule_id, visit_date, visit_time, store_name, appt_id=""):
    now=datetime.now().isoformat(timespec="seconds")
    connection.execute("""INSERT INTO road_visit_notes (schedule_id,visit_date,visit_time,store_name,visit_notes,completed,updated_at,appointment_id)
        VALUES (?,?,?,?,?,0,?,?) ON CONFLICT(schedule_id,visit_date,visit_time,store_name) DO UPDATE SET appointment_id=CASE WHEN excluded.appointment_id!='' THEN excluded.appointment_id ELSE road_visit_notes.appointment_id END""",
        (schedule_id,visit_date,visit_time,store_name,"",now,appt_id))


@app.route("/on-the-road/event", methods=["POST"])
def road_visit_event():
    schedule_id, visit_date, visit_time, store_name, appt_id = _road_visit_key_from_form()
    event=request.form.get("event","")
    if not schedule_id or not visit_date or not store_name: return _road_redirect(error="Visit details were incomplete.")
    now=datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        _ensure_road_note(connection,schedule_id,visit_date,visit_time,store_name,appt_id)
        row=connection.execute("SELECT * FROM road_visit_notes WHERE schedule_id=? AND visit_date=? AND visit_time=? AND store_name=?",(schedule_id,visit_date,visit_time,store_name)).fetchone()
        if event=="start":
            connection.execute("UPDATE road_visit_notes SET started_at=?,finished_at='',actual_duration_minutes=0,completed=0,skipped=0,updated_at=? WHERE schedule_id=? AND visit_date=? AND visit_time=? AND store_name=?",(now,now,schedule_id,visit_date,visit_time,store_name))
            message=f"Started visit at {store_name}."
        elif event=="finish":
            started=row["started_at"] or now
            try: duration=max(1,round((datetime.fromisoformat(now)-datetime.fromisoformat(started)).total_seconds()/60))
            except ValueError: duration=0
            connection.execute("UPDATE road_visit_notes SET finished_at=?,actual_duration_minutes=?,completed=1,skipped=0,updated_at=? WHERE schedule_id=? AND visit_date=? AND visit_time=? AND store_name=?",(now,duration,now,schedule_id,visit_date,visit_time,store_name))
            connection.execute("""INSERT INTO customer_priorities (store_name,growth_potential,visit_frequency_days,last_visit_date,manual_sales_value,strategic_notes,updated_at)
                VALUES (?,3,42,?,0,'',?) ON CONFLICT(store_name) DO UPDATE SET last_visit_date=excluded.last_visit_date,updated_at=excluded.updated_at""",(store_name,visit_date,now))
            message=f"Finished {store_name} visit ({duration} minutes)."
        elif event=="skip":
            new_value=0 if row["skipped"] else 1
            connection.execute("UPDATE road_visit_notes SET skipped=?,completed=0,updated_at=? WHERE schedule_id=? AND visit_date=? AND visit_time=? AND store_name=?",(new_value,now,schedule_id,visit_date,visit_time,store_name))
            message=("Skipped " if new_value else "Restored ")+store_name+"."
        else: message="No visit change was made."
        connection.commit()
    return _road_redirect(message=message)


@app.route("/on-the-road/update", methods=["POST"])
def update_road_visit():
    schedule_id, visit_date, visit_time, store_name, appt_id = _road_visit_key_from_form()
    if not schedule_id or not visit_date or not store_name:
        return _road_redirect(error="Visit details were incomplete.")
    form_action = request.form.get("action", "save").strip().lower()
    visit_notes = request.form.get("visit_notes", "").strip()
    outcome = request.form.get("outcome", "").strip()
    next_step = request.form.get("next_step", "").strip()
    followup_title = request.form.get("followup_title", "").strip()
    followup_due = request.form.get("followup_due", "").strip()
    now = datetime.now().isoformat(timespec="seconds")
    duration = 0
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        _ensure_road_note(connection, schedule_id, visit_date, visit_time, store_name, appt_id)
        row = connection.execute(
            "SELECT * FROM road_visit_notes WHERE schedule_id=? AND visit_date=? AND visit_time=? AND store_name=?",
            (schedule_id, visit_date, visit_time, store_name),
        ).fetchone()
        connection.execute(
            "UPDATE road_visit_notes SET visit_notes=?,outcome=?,next_step=?,updated_at=? WHERE schedule_id=? AND visit_date=? AND visit_time=? AND store_name=?",
            (visit_notes, outcome, next_step, now, schedule_id, visit_date, visit_time, store_name),
        )
        if followup_title:
            existing = connection.execute(
                "SELECT id FROM store_actions WHERE lower(trim(store_name))=lower(trim(?)) AND lower(trim(title))=lower(trim(?)) AND status!='Completed' LIMIT 1",
                (store_name, followup_title),
            ).fetchone()
            if not existing:
                connection.execute(
                    "INSERT INTO store_actions (store_name,title,detail,priority,status,due_date,source,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (store_name, followup_title, next_step or visit_notes, "Medium", "Open", followup_due, "On the Road", now, now),
                )
        attachment = request.files.get("attachment")
        if attachment and attachment.filename:
            ext = Path(attachment.filename).suffix.lower()[:10]
            stored_name = f"{uuid.uuid4().hex}{ext}"
            attachment_dir = DATA_DIR / "road_visit_files"
            attachment_dir.mkdir(parents=True, exist_ok=True)
            attachment.save(attachment_dir / stored_name)
            connection.execute(
                "INSERT INTO road_visit_attachments (schedule_id,visit_date,visit_time,store_name,stored_name,original_name,uploaded_at,appointment_id) VALUES (?,?,?,?,?,?,?,?)",
                (schedule_id, visit_date, visit_time, store_name, stored_name, Path(attachment.filename).name, now, appt_id),
            )
        if form_action == "finish":
            started = row["started_at"] or now
            try:
                duration = max(1, round((datetime.fromisoformat(now) - datetime.fromisoformat(started)).total_seconds() / 60))
            except (ValueError, TypeError):
                duration = 0
            connection.execute(
                "UPDATE road_visit_notes SET started_at=?,finished_at=?,actual_duration_minutes=?,completed=1,skipped=0,updated_at=? WHERE schedule_id=? AND visit_date=? AND visit_time=? AND store_name=?",
                (started, now, duration, now, schedule_id, visit_date, visit_time, store_name),
            )
            connection.execute(
                """INSERT INTO customer_priorities (store_name,growth_potential,visit_frequency_days,last_visit_date,manual_sales_value,strategic_notes,updated_at)
                VALUES (?,3,42,?,0,'',?) ON CONFLICT(store_name) DO UPDATE SET last_visit_date=excluded.last_visit_date,updated_at=excluded.updated_at""",
                (store_name, visit_date, now),
            )
        connection.commit()
    audit_event("visit_" + ("completed" if form_action == "finish" else "saved"), store_name, request.form.get("trip_id", type=int), appt_id)
    if form_action == "finish":
        message = f"Saved and completed {store_name} ({duration} minutes)."
    else:
        message = f"Saved visit details for {store_name}."
    if followup_title:
        message += " Follow-up action saved."
    return _road_redirect(message=message)


@app.route("/on-the-road/attachment/<int:attachment_id>")
def road_attachment(attachment_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        row=connection.execute("SELECT * FROM road_visit_attachments WHERE id=?",(attachment_id,)).fetchone()
    if not row: return "Attachment not found",404
    folder=DATA_DIR/"road_visit_files"
    return send_from_directory(folder,row["stored_name"],as_attachment=False,download_name=row["original_name"])


@app.route("/control-centre")
def control_centre():
    today = date.today()
    week_start = monday_for(today)
    week_key = week_start.isoformat()
    week_end = today + timedelta(days=7)
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        incomplete = connection.execute(
            "SELECT * FROM store_actions WHERE status != 'Completed' ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, due_date, updated_at DESC"
        ).fetchall()
        checklist_saved = dict(connection.execute(
            "SELECT item_key, is_done FROM weekly_checklist WHERE week_key = ?", (week_key,)
        ).fetchall())
        latest_run = connection.execute(
            "SELECT raw_notes FROM follow_up_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        saved_stores = {row[0].lower() for row in connection.execute("SELECT store_name FROM opportunity_analyses").fetchall()}

    overdue = [r for r in incomplete if r["due_date"] and r["due_date"] < today.isoformat()]
    high = [r for r in incomplete if r["priority"] == "High"]
    due_soon = [r for r in incomplete if r["due_date"] and today.isoformat() <= r["due_date"] <= week_end.isoformat()]
    armoursteel = [r for r in incomplete if re.search(r"\b(?:armorsteel|armoursteel|a/s)\b", f"{r['title']} {r['detail']}", re.I)]

    recent_cutoff = (today - timedelta(days=7)).isoformat()
    friday_source = [r for r in incomplete if r["created_at"][:10] >= recent_cutoff or "follow-up" in (r["source"] or "").lower()]
    chosen = []
    seen = set()
    for r in overdue + high + due_soon + friday_source:
        if r["id"] not in seen:
            chosen.append(action_display_row(r)); seen.add(r["id"])
    chosen = chosen[:20]

    checklist_items = [{"item_key": key, "label": label, "done": bool(checklist_saved.get(key, 0))} for key, label in WEEKLY_CHECKLIST_ITEMS]

    appointments = parse_schedule_appointments(latest_run["raw_notes"] if latest_run else "", today)
    upcoming = []
    for appt in appointments[:20]:
        matching = [r for r in incomplete if r["store_name"].strip().lower() == appt["store"].strip().lower()]
        combined = f"{appt['call_note']} " + " ".join(f"{r['title']} {r['detail']}" for r in matching)
        upcoming.append({
            "store": appt["store"], "time": appt["time"], "call_note": appt["call_note"],
            "date_display": f"{appt['date'].day} {appt['date'].strftime('%b %Y')}",
            "open_action_count": len(matching), "open_action_titles": [r["title"] for r in matching[:3]],
            "has_analysis": appt["store"].strip().lower() in saved_stores,
            "armoursteel": bool(re.search(r"\b(?:armorsteel|armoursteel|a/s)\b", combined, re.I)),
        })

    return render_template_string(
        PAGE, view="control_centre", open_action_count=get_open_action_count(),
        control_counts={"overdue": len(overdue), "high": len(high), "due_soon": len(due_soon), "armoursteel": len(armoursteel)},
        friday_actions=chosen, action_statuses=ACTION_STATUSES,
        checklist_items=checklist_items, checklist_week_key=week_key,
        checklist_week_display=f"{week_start.day} {week_start.strftime('%B %Y')}",
        checklist_complete=sum(1 for x in checklist_items if x["done"]), checklist_total=len(checklist_items),
        upcoming_briefings=upcoming,
    )


@app.route("/control-centre/checklist", methods=["POST"])
def update_weekly_checklist():
    week_key = request.form.get("week_key", monday_for(date.today()).isoformat())
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DATABASE_PATH) as connection:
        for item_key, _label in WEEKLY_CHECKLIST_ITEMS:
            done = 1 if request.form.get(f"item_{item_key}") else 0
            connection.execute(
                "INSERT INTO weekly_checklist (week_key,item_key,is_done,updated_at) VALUES (?,?,?,?) ON CONFLICT(week_key,item_key) DO UPDATE SET is_done=excluded.is_done, updated_at=excluded.updated_at",
                (week_key, item_key, done, now),
            )
        connection.commit()
    return redirect(url_for("control_centre"))


TRIP_STATUSES = ["Planned", "Booked", "Completed", "Cancelled"]
BOOKING_STATUSES = ["Not Required", "To Book", "Booked", "Paid", "Completed"]
TRAVEL_COST_ITEMS = [
    {"key": "flights", "label": "Flights"},
    {"key": "rental", "label": "Rental Car"},
    {"key": "fuel", "label": "Fuel"},
    {"key": "accommodation", "label": "Accommodation"},
    {"key": "other", "label": "Other"},
]


def _travel_float(value):
    try:
        return max(float(value or 0), 0.0)
    except (TypeError, ValueError):
        return 0.0


def travel_form_defaults(values=None):
    values = values or {}
    result = {
        "id": values.get("id", ""),
        "trip_name": values.get("trip_name", ""),
        "destination": values.get("destination", ""),
        "start_date": values.get("start_date", date.today().isoformat()),
        "end_date": values.get("end_date", date.today().isoformat()),
        "status": values.get("status", "Planned"),
        "flight_status": values.get("flight_status", "Not Required"),
        "rental_status": values.get("rental_status", "Not Required"),
        "accommodation_status": values.get("accommodation_status", "Not Required"),
        "notes": values.get("notes", ""),
    }
    for item in TRAVEL_COST_ITEMS:
        for prefix in ("forecast", "actual"):
            key = f"{prefix}_{item['key']}"
            value = values.get(key, 0)
            result[key] = "" if value in (None, 0, 0.0, "0", "0.0") else f"{float(value):.2f}"
    return result


def travel_row(record):
    row = dict(record)
    forecast = sum(float(row.get(f"forecast_{item['key']}", 0) or 0) for item in TRAVEL_COST_ITEMS)
    actual = sum(float(row.get(f"actual_{item['key']}", 0) or 0) for item in TRAVEL_COST_ITEMS)
    variance = actual - forecast
    start = datetime.strptime(row["start_date"], "%Y-%m-%d").date()
    end = datetime.strptime(row["end_date"], "%Y-%m-%d").date()
    row.update({
        "forecast": forecast,
        "actual": actual,
        "variance": variance,
        "forecast_display": money(forecast),
        "actual_display": money(actual),
        "variance_display": signed_money(variance),
        "variance_text_class": "variance-negative" if variance > 0 else "variance-positive",
        "date_display": f"{start.day} {start.strftime('%b %Y')}" if start == end else f"{start.day} {start.strftime('%b')} - {end.day} {end.strftime('%b %Y')}",
        "status_class": "travel-status-" + row["status"].lower().replace(" ", "-"),
    })
    return row


@app.route("/travel-expenses", methods=["GET", "POST"])
def travel_expenses():
    success = request.args.get("success")
    error = request.args.get("error")
    if request.method == "POST":
        form = travel_form_defaults(request.form)
        trip_id = request.form.get("trip_id", "").strip()
        try:
            start = datetime.strptime(form["start_date"], "%Y-%m-%d").date()
            end = datetime.strptime(form["end_date"], "%Y-%m-%d").date()
            if end < start:
                raise ValueError("The end date cannot be before the start date.")
            if not form["trip_name"].strip():
                raise ValueError("Trip name is required.")
            now = datetime.now().isoformat(timespec="seconds")
            cost_values = []
            for item in TRAVEL_COST_ITEMS:
                cost_values.extend([
                    _travel_float(request.form.get(f"forecast_{item['key']}")),
                    _travel_float(request.form.get(f"actual_{item['key']}")),
                ])
            values = (
                form["trip_name"].strip(), form["destination"].strip(), form["start_date"], form["end_date"],
                form["status"], form["flight_status"], form["rental_status"], form["accommodation_status"],
                *cost_values, form["notes"].strip(), now,
            )
            with sqlite3.connect(DATABASE_PATH) as connection:
                if trip_id:
                    connection.execute(
                        """UPDATE travel_trips SET trip_name=?, destination=?, start_date=?, end_date=?, status=?, flight_status=?, rental_status=?, accommodation_status=?, forecast_flights=?, actual_flights=?, forecast_rental=?, actual_rental=?, forecast_fuel=?, actual_fuel=?, forecast_accommodation=?, actual_accommodation=?, forecast_other=?, actual_other=?, notes=?, updated_at=? WHERE id=?""",
                        (*values, int(trip_id)),
                    )
                    message = "Trip updated successfully."
                else:
                    connection.execute(
                        """INSERT INTO travel_trips (trip_name,destination,start_date,end_date,status,flight_status,rental_status,accommodation_status,forecast_flights,actual_flights,forecast_rental,actual_rental,forecast_fuel,actual_fuel,forecast_accommodation,actual_accommodation,forecast_other,actual_other,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (*values[:-1], now, now),
                    )
                    message = "Trip added successfully."
                connection.commit()
            return redirect(url_for("travel_expenses", success=message, year=start.year))
        except Exception as exc:
            error = str(exc)
            trip_form = form
    else:
        trip_form = travel_form_defaults()

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        year_records = connection.execute("SELECT DISTINCT substr(start_date,1,4) AS year FROM travel_trips ORDER BY year DESC").fetchall()
        travel_years = [int(r["year"]) for r in year_records] or [date.today().year]
        try:
            selected_year = int(request.args.get("year", travel_years[0]))
        except ValueError:
            selected_year = travel_years[0]
        try:
            selected_quarter = int(request.args.get("quarter", 0))
        except ValueError:
            selected_quarter = 0
        if selected_quarter not in (0, 1, 2, 3, 4):
            selected_quarter = 0

        editing_trip = None
        edit_id = request.args.get("edit", "").strip()
        if edit_id and request.method != "POST":
            editing_trip = connection.execute("SELECT * FROM travel_trips WHERE id=?", (edit_id,)).fetchone()
            if editing_trip:
                trip_form = travel_form_defaults(dict(editing_trip))

        query = "SELECT * FROM travel_trips WHERE substr(start_date,1,4)=?"
        params = [str(selected_year)]
        if selected_quarter:
            start_month = (selected_quarter - 1) * 3 + 1
            end_month = start_month + 2
            query += " AND CAST(substr(start_date,6,2) AS INTEGER) BETWEEN ? AND ?"
            params.extend([start_month, end_month])
        query += " ORDER BY start_date DESC, id DESC"
        records = connection.execute(query, params).fetchall()

    rows = [travel_row(r) for r in records]
    forecast_total = sum(r["forecast"] for r in rows)
    actual_total = sum(r["actual"] for r in rows)
    variance = actual_total - forecast_total
    summary = {
        "trip_count": len(rows),
        "forecast_display": money(forecast_total),
        "actual_display": money(actual_total),
        "variance_display": signed_money(variance),
        "variance_class": "negative" if variance > 0 else ("positive" if variance < 0 else "neutral"),
    }
    return render_template_string(
        PAGE, view="travel_expenses", trip_form=trip_form, editing_trip=editing_trip,
        trip_statuses=TRIP_STATUSES, booking_statuses=BOOKING_STATUSES,
        cost_items=TRAVEL_COST_ITEMS, travel_rows=rows, travel_summary=summary,
        travel_years=travel_years, selected_year=selected_year, selected_quarter=selected_quarter,
        success=success, error=error,
    )


@app.route("/travel-expenses/<int:trip_id>/delete", methods=["POST"])
def delete_travel_trip(trip_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM travel_trips WHERE id=?", (trip_id,))
        connection.commit()
    return redirect(url_for("travel_expenses", success="Trip deleted."))


@app.route("/actions")
def actions():
    selected_store = request.args.get("store", "").strip()
    selected_status = request.args.get("status", "").strip()
    query = "SELECT * FROM store_actions WHERE 1=1"
    params = []
    if selected_store:
        query += " AND store_name = ?"; params.append(selected_store)
    if selected_status:
        query += " AND status = ?"; params.append(selected_status)
    query += " ORDER BY CASE priority WHEN 'High' THEN 1 WHEN 'Medium' THEN 2 ELSE 3 END, CASE WHEN due_date = '' THEN 1 ELSE 0 END, due_date, updated_at DESC"
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        records = connection.execute(query, params).fetchall()
        stores = [r[0] for r in connection.execute("SELECT DISTINCT store_name FROM store_actions ORDER BY store_name COLLATE NOCASE").fetchall()]
        counts = dict(connection.execute("SELECT status, COUNT(*) FROM store_actions GROUP BY status").fetchall())
    rows=[]
    for r in records:
        row=dict(r)
        row["due_display"] = datetime.strptime(row["due_date"], "%Y-%m-%d").strftime("%d %b %Y") if row["due_date"] else "No date"
        row["priority_class"] = "critical" if row["priority"] == "High" else ("warning" if row["priority"] == "Medium" else "low")
        rows.append(row)
    return render_template_string(PAGE, view="actions", action_rows=rows, action_stores=stores, selected_store=selected_store, selected_status=selected_status, action_statuses=ACTION_STATUSES, action_counts={"open":counts.get("Open",0),"in_progress":counts.get("In Progress",0),"waiting":counts.get("Waiting on Store",0),"completed":counts.get("Completed",0)}, success=request.args.get("success"), error=request.args.get("error"), open_action_count=get_open_action_count())


@app.route("/actions/new", methods=["GET", "POST"])
def new_action():
    initial = action_template_defaults({
        "store_name": request.args.get("store", ""), "title": request.args.get("title", ""),
        "detail": request.args.get("detail", ""), "priority": request.args.get("priority", "Medium"),
        "source": request.args.get("source", "Opportunity Finder")
    })
    error=None
    if request.method == "POST":
        initial = action_template_defaults(request.form)
        if not initial["store_name"].strip() or not initial["title"].strip():
            error="Store and action title are required."
        else:
            now=datetime.now().isoformat(timespec="seconds")
            with sqlite3.connect(DATABASE_PATH) as connection:
                connection.execute("INSERT INTO store_actions (store_name,title,detail,source,priority,due_date,status,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (initial["store_name"].strip(),initial["title"].strip(),initial["detail"].strip(),initial["source"].strip(),initial["priority"],initial["due_date"],initial["status"],initial["notes"].strip(),now,now)); connection.commit()
            return redirect(url_for("actions", success="Action created successfully."))
    return render_template_string(PAGE, view="action_form", form_heading="Create Store Action", submit_label="Create Action", action_form=initial, action_statuses=ACTION_STATUSES, error=error, open_action_count=get_open_action_count())


@app.route("/actions/<int:action_id>/edit", methods=["GET", "POST"])
def edit_action(action_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        record=connection.execute("SELECT * FROM store_actions WHERE id=?",(action_id,)).fetchone()
    if record is None: return redirect(url_for("actions", error="That action could not be found."))
    form=action_template_defaults(dict(record)); error=None
    if request.method == "POST":
        form=action_template_defaults(request.form)
        if not form["store_name"].strip() or not form["title"].strip(): error="Store and action title are required."
        else:
            with sqlite3.connect(DATABASE_PATH) as connection:
                connection.execute("UPDATE store_actions SET store_name=?,title=?,detail=?,source=?,priority=?,due_date=?,status=?,notes=?,updated_at=? WHERE id=?",(form["store_name"].strip(),form["title"].strip(),form["detail"].strip(),form["source"].strip(),form["priority"],form["due_date"],form["status"],form["notes"].strip(),datetime.now().isoformat(timespec="seconds"),action_id)); connection.commit()
            return redirect(url_for("actions", success="Action updated successfully."))
    return render_template_string(PAGE, view="action_form", form_heading="Edit Store Action", submit_label="Save Changes", action_form=form, action_statuses=ACTION_STATUSES, error=error, open_action_count=get_open_action_count())


@app.route("/actions/<int:action_id>/status", methods=["POST"])
def update_action(action_id):
    status=request.form.get("status", "Open")
    if status not in ACTION_STATUSES: status="Open"
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("UPDATE store_actions SET status=?, updated_at=? WHERE id=?",(status,datetime.now().isoformat(timespec="seconds"),action_id)); connection.commit()
    return redirect(request.referrer or url_for("actions"))



def infer_customer_group(name):
    value = str(name or '').lower()
    checks = [('bunnings','Bunnings'),('mitre 10 mega','Mitre 10 Mega'),('mitre10 mega','Mitre 10 Mega'),('mitre 10','Mitre 10'),('mitre10','Mitre 10'),('placemakers','Placemakers'),('place makers','Placemakers'),('carters','Carters'),('itm','ITM'),('hammer hardware','Hammer Hardware'),('buildlink','Buildlink')]
    for needle, label in checks:
        if needle in value: return label
    return 'Other'

def import_portatour_customers(uploaded_file):
    frame = pd.read_excel(uploaded_file, sheet_name='Data')
    required = {'Name','Latitude','Longitude'}
    if not required.issubset(frame.columns):
        raise ValueError('The workbook must contain a Data sheet with Name, Latitude and Longitude columns.')
    frame = frame.dropna(subset=['Name','Latitude','Longitude'])
    now = datetime.now().isoformat(timespec='seconds')
    imported = 0
    with sqlite3.connect(DATABASE_PATH) as connection:
        for _, row in frame.iterrows():
            def clean(field, default=''):
                value = row.get(field, default)
                if pd.isna(value): return default
                if isinstance(value, (pd.Timestamp, datetime)): return value.date().isoformat()
                return str(value).strip()
            name = clean('Name')
            try: latitude=float(row.get('Latitude')); longitude=float(row.get('Longitude'))
            except (TypeError,ValueError): continue
            external_id=clean('External ID') or clean('Customer #'); portatour_id=clean('Portatour ID')
            try: interval=max(1,int(float(row.get('Call interval',42) or 42)))
            except (TypeError,ValueError): interval=42
            values=(external_id,portatour_id,name,clean('Street'),clean('City'),clean('ZIP'),clean('Country','New Zealand'),infer_customer_group(name),clean('Categories'),clean('Phone'),clean('Store Email'),clean('Call Note'),clean('Last call'),interval,clean('Current urgency name').lower(),latitude,longitude,now)
            connection.execute("""INSERT INTO route_customers (external_id,portatour_id,name,street,city,zip,country,customer_group,categories,phone,email,call_note,last_call,call_interval,urgency,latitude,longitude,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET external_id=excluded.external_id,portatour_id=excluded.portatour_id,street=excluded.street,city=excluded.city,zip=excluded.zip,country=excluded.country,customer_group=excluded.customer_group,categories=excluded.categories,phone=excluded.phone,email=excluded.email,call_note=excluded.call_note,last_call=excluded.last_call,call_interval=excluded.call_interval,urgency=excluded.urgency,latitude=excluded.latitude,longitude=excluded.longitude,updated_at=excluded.updated_at""",values)
            imported += 1
        connection.commit()
    return imported

def build_route_schedule_html(trip_name, start_date, route_payload):
    try:
        first_day = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        first_day = date.today()
    chunks = ["<html><head><meta charset='utf-8'><title>Sales Assistant Route</title></head><body>"]
    for day in route_payload.get("days", []):
        day_index = int(day.get("day_index", 0))
        visit_date = first_day + timedelta(days=day_index)
        chunks.append(f"<div class='scheduleday scheduleday_{visit_date.strftime('%Y%m%d')}'>")
        chunks.append("<table>")
        for stop in day.get("stops", []):
            name = escape(str(stop.get("name", "Customer")))
            arrival = escape(str(stop.get("arrival", "")))
            note = escape(str(stop.get("call_note", "")))
            duration = int(stop.get("duration", 45) or 45)
            note_text = f"Route Planner · {duration} minute visit" + (f" · {note}" if note else "")
            chunks.append("<tr class='stop'>")
            chunks.append(f"<td class='time'>{arrival}</td><td class='reservation'>Appointment</td>")
            chunks.append(f"<td class='displayname'><div>{name}</div><div class='callnote'>⇒ {note_text}</div></td>")
            chunks.append("</tr>")
        chunks.append("</table></div>")
    chunks.append("</body></html>")
    return "".join(chunks)


def _normalise_date_input(value):
    text=str(value or '').strip()
    if not text:
        return ''
    for fmt in ('%Y-%m-%d','%d/%m/%Y','%d-%m-%Y','%Y/%m/%d'):
        try:
            return datetime.strptime(text[:10],fmt).date().isoformat()
        except ValueError:
            pass
    return ''



def _workspace_time_minutes(value):
    text=str(value or '').strip()
    match=re.search(r'(\d{1,2}):(\d{2})',text)
    if not match:
        return 8*60
    return int(match.group(1))*60+int(match.group(2))


def _workspace_clock(minutes):
    minutes=int(minutes)%1440
    return f"{minutes//60:02d}:{minutes%60:02d}"


def _workspace_haversine(lat1,lon1,lat2,lon2):
    from math import radians,sin,cos,asin,sqrt
    r=6371.0; dlat=radians(lat2-lat1); dlon=radians(lon2-lon1)
    q=sin(dlat/2)**2+cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return 2*r*asin(sqrt(q))


def build_trip_workspace(trip_id):
    import calendar as _calendar
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        trip=connection.execute('SELECT * FROM trip_plans WHERE id=?',(trip_id,)).fetchone()
        if not trip:
            return None
        schedule=connection.execute('SELECT * FROM trip_schedules WHERE trip_plan_id=? ORDER BY id DESC LIMIT 1',(trip_id,)).fetchone()
        customers={r['name'].strip().casefold():dict(r) for r in connection.execute('SELECT * FROM route_customers').fetchall()}
        actions={r['store_name'].strip().casefold():r['n'] for r in connection.execute("SELECT store_name,COUNT(*) n FROM store_actions WHERE status!='Completed' GROUP BY lower(store_name)").fetchall()}
        notes={}
        notes_by_appointment={}
        notes_by_date_store={}
        objectives={}
        completed=0
        if schedule:
            for r in connection.execute('SELECT * FROM appointment_objectives WHERE schedule_id=?',(schedule['id'],)).fetchall(): objectives[r['appointment_id']]=dict(r)
            for r in connection.execute('SELECT * FROM road_visit_notes WHERE schedule_id=?',(schedule['id'],)).fetchall():
                record=dict(r)
                key=(r['visit_date'],r['visit_time'],r['store_name'].strip().casefold())
                notes[key]=record
                if record.get('appointment_id'):
                    notes_by_appointment[record['appointment_id']]=record
                notes_by_date_store.setdefault((r['visit_date'],r['store_name'].strip().casefold()),[]).append(record)
                if r['completed']: completed+=1
    trip=dict(trip)
    try:
        sd=datetime.strptime(trip['start_date'],'%Y-%m-%d').date(); ed=datetime.strptime(trip['end_date'],'%Y-%m-%d').date()
        trip['date_display']=sd.strftime('%d %b %Y') if sd==ed else f"{sd.strftime('%d %b')}–{ed.strftime('%d %b %Y')}"
    except Exception:
        sd=date.today(); ed=sd; trip['date_display']=f"{trip.get('start_date','')}–{trip.get('end_date','')}"
    appointments=parse_schedule_appointments(schedule['raw_html'],date.min) if schedule else []
    by_date={}
    for appt in appointments:
        by_date.setdefault(appt['date'],[]).append(appt)
    days=[]; map_days=[]
    for day_date in sorted(by_date):
        calls=[]; timeline=[]; prior=None; total_visit=0
        for appt in sorted(by_date[day_date],key=lambda x:_workspace_time_minutes(x['time'])):
            customer=customers.get(appt['store'].strip().casefold(),{})
            start=_workspace_time_minutes(appt['time']); duration=max(10,int(customer.get('ideal_call_minutes') or 45)); finish=start+duration
            lat=customer.get('latitude'); lon=customer.get('longitude')
            address=', '.join(x for x in [customer.get('street',''),customer.get('city',''),customer.get('zip','')] if x)
            if prior and lat is not None and lon is not None and prior.get('latitude') is not None:
                km=_workspace_haversine(float(prior['latitude']),float(prior['longitude']),float(lat),float(lon))
                drive=max(3,round(km/65*60))
                timeline.append({'type':'travel','start':_workspace_clock(max(0,start-drive)),'title':f"Drive to {appt['store']}",'detail':f"Approximately {km:.1f} km",'duration_display':f"~{drive} min"})
            appt_id=appointment_uid(trip_id,schedule['id'] if schedule else 0,day_date.isoformat(),appt['time'],appt['store'])
            note=notes_by_appointment.get(appt_id) or notes.get((day_date.isoformat(),appt['time'],appt['store'].strip().casefold())) or (notes_by_date_store.get((day_date.isoformat(),appt['store'].strip().casefold()),[{}])[0] if len(notes_by_date_store.get((day_date.isoformat(),appt['store'].strip().casefold()),[]))==1 else {})
            obj=objectives.get(appt_id,{})
            call={'type':'call','name':appt['store'],'start':_workspace_clock(start),'end':_workspace_clock(finish),'arrival':_workspace_clock(start),'departure':_workspace_clock(finish),'duration':duration,'address':address or customer.get('city',''),'customer_group':customer.get('customer_group') or classify_customer(appt['store']),'call_note':appt.get('call_note') or customer.get('call_note',''),'desired_outcome':obj.get('desired_outcome',''),'preparation_notes':obj.get('preparation_notes',''),'customer_url':url_for('customer_detail',store_name=appt['store']),'open_actions':int(actions.get(appt['store'].strip().casefold(),0)),'locked':'locked' in (appt.get('call_note') or '').lower(),'latitude':lat,'longitude':lon,'completed':bool(note.get('completed'))}
            calls.append(call); timeline.append(call); total_visit+=duration; prior=call
        day={'date_iso':day_date.isoformat(),'tab_label':day_date.strftime('%a %d'),'full_label':day_date.strftime('%A %d %B %Y'),'departure':calls[0]['arrival'] if calls else '','calls':calls,'timeline':timeline,'total_visit_display':f"{total_visit//60}h {total_visit%60}m" if total_visit>=60 else f"{total_visit} min"}
        days.append(day); map_days.append({'full_label':day['full_label'],'stops':[c for c in calls if c.get('latitude') is not None and c.get('longitude') is not None]})
    # calendar month containing trip start
    month_start=sd.replace(day=1); cal=_calendar.Calendar(firstweekday=0); cells=[]
    for d in cal.itermonthdates(month_start.year,month_start.month):
        events=[]
        matching=next((x for x in days if x['date_iso']==d.isoformat()),None)
        if matching:
            for c in matching['calls']:
                events.append({'type':'','time':c['start'],'title':c['name']})
        cells.append({'day':d.day,'outside':d.month!=month_start.month,'is_trip_day':bool(matching),'events':events})
    closeout_visits=[]
    completed_count=skipped_count=notes_count=0
    all_customer_names=[]
    for appt in appointments:
        key=(appt['date'].isoformat(),appt['time'],appt['store'].strip().casefold())
        appt_id=appointment_uid(trip_id,schedule['id'] if schedule else 0,appt['date'].isoformat(),appt['time'],appt['store'])
        note=notes_by_appointment.get(appt_id) or notes.get(key) or (notes_by_date_store.get((appt['date'].isoformat(),appt['store'].strip().casefold()),[{}])[0] if len(notes_by_date_store.get((appt['date'].isoformat(),appt['store'].strip().casefold()),[]))==1 else {})
        is_completed=bool(note.get('completed'))
        is_skipped=bool(note.get('skipped'))
        if is_completed:
            status='Completed'; badge='change-positive'; completed_count+=1
        elif is_skipped:
            status='Skipped'; badge='change-negative'; skipped_count+=1
        else:
            status='Open'; badge='change-neutral'
        if is_completed and str(note.get('visit_notes') or '').strip(): notes_count+=1
        closeout_visits.append({'date_display':appt['date'].strftime('%d %b %Y'),'visit_time':appt['time'],'store_name':appt['store'],'status':status,'badge_class':badge,'visit_notes':note.get('visit_notes',''),'outcome':note.get('outcome',''),'next_step':note.get('next_step',''),'actual_duration':int(note.get('actual_duration_minutes') or 0)})
        all_customer_names.append(appt['store'])
    pending_actions=0
    if all_customer_names:
        with sqlite3.connect(DATABASE_PATH) as connection:
            placeholders=','.join('?' for _ in all_customer_names)
            pending_actions=connection.execute(f"SELECT COUNT(*) FROM store_actions WHERE status!='Completed' AND lower(store_name) IN ({placeholders})",tuple(x.strip().lower() for x in all_customer_names)).fetchone()[0]
    trip['completed_at_display']=''
    if trip.get('completed_at'):
        try: trip['completed_at_display']=datetime.fromisoformat(trip['completed_at']).strftime('%d %b %Y at %I:%M %p')
        except Exception: trip['completed_at_display']=trip['completed_at']
    closeout={'total':len(appointments),'completed':completed_count,'skipped':skipped_count,'open':max(len(appointments)-completed_count-skipped_count,0),'notes_count':notes_count,'pending_actions':pending_actions,'visits':closeout_visits}
    return {'trip':trip,'days':days,'total_calls':sum(len(d['calls']) for d in days),'completed_calls':completed,'map_days_json':json.dumps(map_days),'calendar':{'month_label':month_start.strftime('%B %Y'),'cells':cells},'closeout':closeout}


@app.route('/trip-workspace/<int:trip_id>')
def trip_workspace(trip_id):
    set_active_trip(trip_id)
    workspace=build_trip_workspace(trip_id)
    if not workspace:
        return redirect(url_for('trip_prep',error='Trip not found.'))
    tab=request.args.get('tab','schedule').lower()
    if tab not in {'schedule','map','calendar','closeout'}:
        tab='schedule'
    return render_template_string(PAGE,view='trip_workspace',workspace=workspace,workspace_tab=tab,open_action_count=get_open_action_count())

@app.route('/trip-workspace/<int:trip_id>/closeout', methods=['POST'])
def trip_closeout_action(trip_id):
    action=request.form.get('action','save_notes')
    notes_text=request.form.get('closeout_notes','').strip()
    now=datetime.now().isoformat(timespec='seconds')
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        trip=connection.execute('SELECT * FROM trip_plans WHERE id=?',(trip_id,)).fetchone()
        if not trip:
            return redirect(url_for('trip_prep',error='Trip not found.'))
        schedule=connection.execute('SELECT * FROM trip_schedules WHERE trip_plan_id=? ORDER BY id DESC LIMIT 1',(trip_id,)).fetchone()
        if action=='reopen':
            connection.execute('UPDATE trip_plans SET completed_at=?,closeout_notes=?,updated_at=? WHERE id=?',('',notes_text,now,trip_id))
            connection.commit()
            return redirect(url_for('trip_workspace',trip_id=trip_id,tab='closeout',success='Trip reopened.'))
        connection.execute('UPDATE trip_plans SET closeout_notes=?,updated_at=? WHERE id=?',(notes_text,now,trip_id))
        created=0
        if action=='close':
            if request.form.get('create_reschedule_actions')=='1' and schedule:
                appointments=parse_schedule_appointments(schedule['raw_html'],date.min)
                saved=connection.execute('SELECT * FROM road_visit_notes WHERE schedule_id=?',(schedule['id'],)).fetchall()
                note_map={(r['visit_date'],r['visit_time'],r['store_name'].strip().casefold()):r for r in saved}
                existing={(r[0].strip().casefold(),r[1].strip().casefold()) for r in connection.execute("SELECT store_name,title FROM store_actions WHERE status!='Completed'").fetchall()}
                for appt in appointments:
                    row=note_map.get((appt['date'].isoformat(),appt['time'],appt['store'].strip().casefold()))
                    if row and row['completed']:
                        continue
                    title='Reschedule missed visit'
                    key=(appt['store'].strip().casefold(),title.casefold())
                    if key in existing:
                        continue
                    detail='Skipped during trip.' if row and row['skipped'] else 'Planned call was not marked complete.'
                    connection.execute("INSERT INTO store_actions (store_name,title,detail,priority,due_date,status,source,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",(appt['store'],title,detail,'Medium',(date.today()+timedelta(days=14)).isoformat(),'Open','Trip Close-Out','',now,now))
                    existing.add(key); created+=1
            connection.execute('UPDATE trip_plans SET completed_at=?,closeout_notes=?,updated_at=? WHERE id=?',(now,notes_text,now,trip_id))
        connection.commit()
    message='Close-out notes saved.' if action!='close' else f'Trip closed. {created} reschedule action(s) created.'
    return redirect(url_for('trip_workspace',trip_id=trip_id,tab='closeout',success=message))


@app.route('/trip-workspace/<int:trip_id>/closeout.pdf')
def download_trip_closeout(trip_id):
    workspace=build_trip_workspace(trip_id)
    if not workspace:
        return redirect(url_for('trip_prep',error='Trip not found.'))
    buffer=BytesIO()
    doc=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=16*mm,leftMargin=16*mm,topMargin=15*mm,bottomMargin=15*mm)
    styles=getSampleStyleSheet(); story=[]
    story.append(Paragraph(escape(workspace['trip']['trip_name'])+' — Trip Close-Out',styles['Title']))
    story.append(Paragraph(escape(workspace['trip'].get('date_display','')),styles['Normal'])); story.append(Spacer(1,6*mm))
    c=workspace['closeout']
    summary=[['Scheduled','Completed','Skipped','Open'],[str(c['total']),str(c['completed']),str(c['skipped']),str(c['open'])]]
    table=Table(summary,colWidths=[42*mm]*4); table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#101828')),('TEXTCOLOR',(0,0),(-1,0),colors.white),('ALIGN',(0,0),(-1,-1),'CENTER'),('GRID',(0,0),(-1,-1),.5,colors.HexColor('#D0D5DD')),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('PADDING',(0,0),(-1,-1),7)])); story.append(table); story.append(Spacer(1,6*mm))
    rows=[['Date / Time','Customer','Status','Outcome / Notes']]
    for v in c['visits']:
        text='; '.join(x for x in [v.get('outcome',''),v.get('visit_notes',''),('Next: '+v['next_step']) if v.get('next_step') else ''] if x)
        rows.append([f"{v['date_display']} {v['visit_time']}",v['store_name'],v['status'],Paragraph(escape(text or '—'),styles['BodyText'])])
    visit_table=Table(rows,colWidths=[34*mm,48*mm,23*mm,73*mm],repeatRows=1); visit_table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#EAF2FF')),('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),('VALIGN',(0,0),(-1,-1),'TOP'),('GRID',(0,0),(-1,-1),.35,colors.HexColor('#D0D5DD')),('PADDING',(0,0),(-1,-1),5)])); story.append(visit_table)
    if workspace['trip'].get('closeout_notes'):
        story.append(Spacer(1,6*mm)); story.append(Paragraph('Overall Close-Out Notes',styles['Heading2'])); story.append(Paragraph(escape(workspace['trip']['closeout_notes']).replace('\n','<br/>'),styles['BodyText']))
    doc.build(story); buffer.seek(0)
    filename=re.sub(r'[^A-Za-z0-9._-]+','_',workspace['trip']['trip_name']).strip('_')+'_closeout.pdf'
    return send_file(buffer,as_attachment=True,download_name=filename,mimetype='application/pdf')


def build_reliability_summary():
    issues=[]
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        active_id=get_active_trip_id()
        active=connection.execute("SELECT trip_name FROM trip_plans WHERE id=?",(active_id,)).fetchone() if active_id else None
        missing_coords=connection.execute("SELECT COUNT(*) FROM route_customers WHERE latitude IS NULL OR longitude IS NULL OR latitude=0 OR longitude=0").fetchone()[0]
        invalid_hours=connection.execute("SELECT COUNT(*) FROM route_customers WHERE customer_open_time>=customer_close_time").fetchone()[0]
        duplicate_names=connection.execute("SELECT COUNT(*) FROM (SELECT lower(trim(name)) n,COUNT(*) c FROM route_customers GROUP BY n HAVING c>1)").fetchone()[0]
        orphan_schedules=connection.execute("SELECT COUNT(*) FROM trip_schedules s LEFT JOIN trip_plans t ON t.id=s.trip_plan_id WHERE s.trip_plan_id IS NOT NULL AND t.id IS NULL").fetchone()[0]
        orphan_notes=connection.execute("SELECT COUNT(*) FROM road_visit_notes n LEFT JOIN trip_schedules s ON s.id=n.schedule_id WHERE s.id IS NULL").fetchone()[0]
        blank_appt_ids=connection.execute("SELECT COUNT(*) FROM road_visit_notes WHERE appointment_id='' AND (visit_notes!='' OR completed=1 OR skipped=1)").fetchone()[0]
        trips=[dict(r) for r in connection.execute("SELECT id,trip_name,start_date FROM trip_plans ORDER BY start_date DESC,id DESC LIMIT 50").fetchall()]
        recent_audit=[dict(r) for r in connection.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 20").fetchall()]
    if missing_coords: issues.append(f'{missing_coords} customer(s) have missing map coordinates.')
    if invalid_hours: issues.append(f'{invalid_hours} customer(s) have invalid opening hours.')
    if duplicate_names: issues.append(f'{duplicate_names} duplicate customer-name group(s) exist.')
    if orphan_schedules: issues.append(f'{orphan_schedules} schedule(s) are linked to missing trips.')
    if orphan_notes: issues.append(f'{orphan_notes} visit-note record(s) are linked to missing schedules.')
    if blank_appt_ids: issues.append(f'{blank_appt_ids} older visit record(s) do not yet have permanent appointment IDs; they will be upgraded when opened.')
    backups=sorted(BACKUP_DIR.glob('*.db'),key=lambda x:x.stat().st_mtime,reverse=True)
    return {'active_trip_id':active_id,'active_trip_name':active['trip_name'] if active else '', 'issue_count':len(issues),'issues':issues,'backup_count':len(backups),'latest_backup':backups[0].name if backups else '',
            'backups':[{'name':b.name,'size':f'{b.stat().st_size/1024/1024:.1f} MB'} for b in backups[:10]],'trips':trips,'recent_audit':recent_audit}

@app.route('/settings/restore/<path:filename>',methods=['POST'])
def restore_backup(filename):
    safe=Path(filename).name
    source=BACKUP_DIR/safe
    if not source.exists() or source.suffix!='.db':
        return redirect(url_for('settings',error='Backup file not found.'))
    create_database_backup('before_restore')
    shutil.copy2(source,DATABASE_PATH)
    init_database()
    audit_event('backup_restored',safe)
    return redirect(url_for('settings',success=f'Restored backup: {safe}'))

@app.route('/settings/data-health.txt')
def data_health_report():
    summary=build_reliability_summary()
    lines=['Riverlea Group Sales Assistant - Data Health Report',datetime.now().strftime('%d %b %Y %H:%M'),'']
    lines.append('Active trip: '+(summary['active_trip_name'] or 'None'))
    lines.append(f"Issues: {summary['issue_count']}")
    lines.extend('- '+x for x in summary['issues'])
    if not summary['issues']: lines.append('- No structural data problems detected.')
    lines.append('')
    lines.append(f"Backups: {summary['backup_count']}")
    return app.response_class('\n'.join(lines),mimetype='text/plain',headers={'Content-Disposition':'attachment; filename=data-health-report.txt'})

@app.route('/settings', methods=['GET','POST'])
def settings():
    if request.method == 'POST':
        action=request.form.get('action','')
        if action == 'create_backup':
            backup=create_database_backup('manual')
            audit_event('backup_created', backup.name if backup else 'No database')
            return redirect(url_for('settings',success=f'Backup created: {backup.name}' if backup else 'No database was available to back up.'))
        if action == 'set_active_trip':
            try: trip_id=int(request.form.get('active_trip_id','') or 0)
            except ValueError: trip_id=0
            set_active_trip(trip_id or None)
            audit_event('active_trip_changed', str(trip_id or 'none'), trip_id or None)
            return redirect(url_for('settings',success='Active trip updated.'))
        if action == 'save_route_defaults':
            try:
                save_route_defaults(
                    request.form.get('route_rep_start','').strip(),
                    request.form.get('route_rep_end','').strip(),
                    request.form.get('route_customer_open','').strip(),
                    request.form.get('route_customer_close','').strip(),
                )
            except ValueError as exc:
                return redirect(url_for('settings',error=str(exc))+'#route-defaults')
            audit_event('route_defaults_updated', 'General rep and customer hours')
            return redirect(url_for('settings',success='General route defaults saved.')+'#route-defaults')
        now=datetime.now().isoformat(timespec='seconds')
        if action == 'bulk_update_customers':
            ids=[int(x) for x in request.form.get('selected_customer_ids','').split(',') if x.strip().isdigit()]
            if not ids:
                return redirect(url_for('settings',error='Select at least one customer.'))
            updates=[]; values=[]
            raw_minutes=request.form.get('bulk_call_minutes','').strip()
            raw_interval=request.form.get('bulk_call_interval','').strip()
            tier=request.form.get('bulk_priority_tier','').strip().upper()
            group=request.form.get('bulk_customer_group','').strip()
            urgency=request.form.get('bulk_urgency','').strip().lower()
            customer_open=request.form.get('bulk_customer_open','').strip()
            customer_close=request.form.get('bulk_customer_close','').strip()
            try:
                if raw_minutes:
                    updates.append('ideal_call_minutes=?'); values.append(max(10,min(240,int(raw_minutes))))
                if raw_interval:
                    updates.append('call_interval=?'); values.append(max(1,min(365,int(raw_interval))))
            except ValueError:
                return redirect(url_for('settings',error='Call duration and cycle must be valid numbers.'))
            if tier in {'A','B','C','D'}: updates.append('priority_tier=?'); values.append(tier)
            if group: updates.append('customer_group=?'); values.append(group)
            if urgency in {'overdue','due','not due'}: updates.append('urgency=?'); values.append(urgency)
            if re.fullmatch(r'\d{2}:\d{2}', customer_open): updates.append('customer_open_time=?'); values.append(customer_open)
            if re.fullmatch(r'\d{2}:\d{2}', customer_close): updates.append('customer_close_time=?'); values.append(customer_close)
            if customer_open and customer_close and customer_close <= customer_open:
                return redirect(url_for('settings',error='Customer closing time must be later than opening time.'))
            if not updates:
                return redirect(url_for('settings',error='Enter at least one bulk value to apply.'))
            updates.append('updated_at=?'); values.append(now)
            marks=','.join('?' for _ in ids)
            with sqlite3.connect(DATABASE_PATH) as connection:
                connection.execute(f"UPDATE route_customers SET {', '.join(updates)} WHERE id IN ({marks})",values+ids)
                connection.commit()
            return redirect(url_for('settings',success=f'Updated {len(ids)} customers.'))
        if action == 'save_customer_table':
            ids=[]
            for key in request.form.keys():
                if key.startswith('name_') and key[5:].isdigit(): ids.append(int(key[5:]))
            saved=0
            try:
                with sqlite3.connect(DATABASE_PATH) as connection:
                    for customer_id in ids:
                        old=connection.execute('SELECT name FROM route_customers WHERE id=?',(customer_id,)).fetchone()
                        if not old: continue
                        old_name=old[0]
                        name=request.form.get(f'name_{customer_id}','').strip()
                        if not name: raise ValueError(f'Customer {customer_id} has no name.')
                        minutes=max(10,min(240,int(request.form.get(f'ideal_call_minutes_{customer_id}','45') or 45)))
                        interval=max(1,min(365,int(request.form.get(f'call_interval_{customer_id}','42') or 42)))
                        latitude=float(request.form.get(f'latitude_{customer_id}','0') or 0)
                        longitude=float(request.form.get(f'longitude_{customer_id}','0') or 0)
                        tier=request.form.get(f'priority_tier_{customer_id}','C').strip().upper()
                        if tier not in {'A','B','C','D'}: tier='C'
                        urgency=request.form.get(f'urgency_{customer_id}','').strip().lower()
                        if urgency not in {'overdue','due','not due'}: urgency=''
                        customer_open=request.form.get(f'customer_open_time_{customer_id}','08:00').strip() or '08:00'
                        customer_close=request.form.get(f'customer_close_time_{customer_id}','17:00').strip() or '17:00'
                        if customer_close <= customer_open: raise ValueError(f'{name}: closing time must be later than opening time.')
                        connection.execute('''UPDATE route_customers SET name=?,street=?,city=?,zip=?,customer_group=?,priority_tier=?,ideal_call_minutes=?,customer_open_time=?,customer_close_time=?,call_interval=?,last_call=?,urgency=?,phone=?,email=?,latitude=?,longitude=?,call_note=?,updated_at=? WHERE id=?''',(
                            name,request.form.get(f'street_{customer_id}','').strip(),request.form.get(f'city_{customer_id}','').strip(),request.form.get(f'zip_{customer_id}','').strip(),request.form.get(f'customer_group_{customer_id}','Other').strip() or 'Other',tier,minutes,customer_open,customer_close,interval,_normalise_date_input(request.form.get(f'last_call_{customer_id}','')),urgency,request.form.get(f'phone_{customer_id}','').strip(),request.form.get(f'email_{customer_id}','').strip(),latitude,longitude,request.form.get(f'call_note_{customer_id}','').strip(),now,customer_id))
                        if name != old_name:
                            connection.execute('UPDATE OR IGNORE customer_priorities SET store_name=? WHERE store_name=?',(name,old_name))
                            connection.execute('UPDATE store_actions SET store_name=? WHERE store_name=?',(name,old_name))
                            connection.execute('UPDATE road_visit_notes SET store_name=? WHERE store_name=?',(name,old_name))
                        saved += 1
                    connection.commit()
            except sqlite3.IntegrityError:
                return redirect(url_for('settings',error='Two customers cannot have the same name. Check any renamed customers.'))
            except (ValueError,TypeError) as exc:
                return redirect(url_for('settings',error=f'Could not save customer settings: {exc}'))
            return redirect(url_for('settings',success=f'Saved changes for {saved} customers.'))
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        customers=[]
        for row in connection.execute('SELECT * FROM route_customers ORDER BY name').fetchall():
            item=dict(row)
            item['last_call_date']=_normalise_date_input(item.get('last_call',''))
            item['priority_tier']=(item.get('priority_tier') or 'C').upper()
            customers.append(item)
    groups=sorted({c['customer_group'] for c in customers if c.get('customer_group')} | {'Bunnings','Mitre 10 Mega','Mitre 10','ITM','Placemakers','Carters','Hammer Hardware','Buildlink','Other'})
    reliability=build_reliability_summary()
    return render_template_string(PAGE,view='settings',settings_customers=customers,settings_groups=groups,reliability=reliability,route_defaults=get_route_defaults(),success=request.args.get('success'),error=request.args.get('error'),open_action_count=get_open_action_count())

@app.route('/route-planner', methods=['GET','POST'])
def route_planner():
    if request.method == 'POST':
        action=request.form.get('action','')
        if action == 'import_customers':
            uploaded=request.files.get('customer_export')
            if not uploaded or not uploaded.filename:
                return redirect(url_for('route_planner',error='Choose a Portatour customer export first.'))
            try:
                create_database_backup('before_customer_import')
                count=import_portatour_customers(uploaded)
                audit_event('customer_import', f'{count} customers')
            except Exception as exc: return redirect(url_for('route_planner',error=f'Customer import failed: {exc}'))
            return redirect(url_for('route_planner',success=f'Updated {count} mapped customers and synced them to the Customers tab.'))
        if action == 'save_customer_route_settings':
            customer_id=request.form.get('customer_id','').strip()
            try:
                customer_id=int(customer_id)
                ideal_minutes=max(10,min(240,int(request.form.get('ideal_call_minutes','45'))))
            except (TypeError,ValueError):
                return redirect(url_for('route_planner',error='Choose a customer and enter a valid call duration.'))
            with sqlite3.connect(DATABASE_PATH) as connection:
                customer=connection.execute('SELECT name FROM route_customers WHERE id=?',(customer_id,)).fetchone()
                if not customer:
                    return redirect(url_for('route_planner',error='Customer not found.'))
                connection.execute('UPDATE route_customers SET ideal_call_minutes=?, updated_at=? WHERE id=?',(ideal_minutes,datetime.now().isoformat(timespec='seconds'),customer_id)); connection.commit()
            return redirect(url_for('route_planner',success=f'Saved the standard call duration for {customer[0]}.'))
        if action == 'create_trip_from_route':
            trip_name=request.form.get('trip_name','').strip()
            start_date=request.form.get('trip_start_date','').strip()
            destination=request.form.get('route_destination','').strip()
            try:
                payload=json.loads(request.form.get('route_payload','{}'))
                days=payload.get('days',[])
                if not trip_name or not start_date or not days:
                    raise ValueError('Trip name, first day and a built route are required.')
                first=datetime.strptime(start_date,'%Y-%m-%d').date()
                end_date=(first+timedelta(days=len(days)-1)).isoformat()
            except Exception as exc:
                return redirect(url_for('route_planner',error=f'Could not create trip: {exc}'))
            now=datetime.now().isoformat(timespec='seconds')
            schedule_html=build_route_schedule_html(trip_name,start_date,payload)
            with sqlite3.connect(DATABASE_PATH) as connection:
                cur=connection.execute("INSERT INTO trip_plans (trip_name,destination,start_date,end_date,travel_method,notes,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",(trip_name,destination,start_date,end_date,'Drive','Created from Route Planner',now,now))
                plan_id=cur.lastrowid
                connection.execute("INSERT INTO trip_schedules (filename,raw_html,uploaded_at,trip_plan_id) VALUES (?,?,?,?)",(f'Route Planner - {trip_name}.html',schedule_html,now,plan_id))
                connection.commit()
            set_active_trip(plan_id)
            audit_event('trip_created',trip_name,plan_id)
            return redirect(url_for('trip_prep',trip=plan_id,generate=1,success=f'{trip_name} was created from the optimised route. Review appointments, brochures and readiness below.'))
        if action == 'save_selection':
            name=request.form.get('selection_name','').strip(); raw=request.form.get('selected_ids','').strip()
            ids=[int(x) for x in raw.split(',') if x.strip().isdigit()]
            if not name or not ids: return redirect(url_for('route_planner',error='Enter a name and select at least one customer.'))
            with sqlite3.connect(DATABASE_PATH) as connection:
                connection.execute('INSERT INTO route_selections (name,start_date,end_date,customer_ids,created_at) VALUES (?,?,?,?,?)',(name,request.form.get('start_date',''),request.form.get('end_date',''),','.join(map(str,ids)),datetime.now().isoformat(timespec='seconds'))); connection.commit()
            return redirect(url_for('route_planner',success=f'Saved {name} with {len(ids)} customers.'))
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory=sqlite3.Row
        customers=[dict(r) for r in connection.execute('SELECT * FROM route_customers ORDER BY name').fetchall()]
        open_actions=connection.execute("SELECT store_name, COUNT(*) AS n FROM store_actions WHERE status != 'Completed' GROUP BY lower(store_name)").fetchall()
        action_map={str(r['store_name']).strip().lower():int(r['n']) for r in open_actions}
        priority_map={row['store_name'].strip().lower():row for row in build_customer_priority_rows()}
        for customer in customers:
            key=customer['name'].strip().lower()
            customer['open_action_count']=action_map.get(key,0)
            customer['priority_classification']=priority_map.get(key,{}).get('classification','')
            customer['priority_score']=priority_map.get(key,{}).get('score',0)
        saved=[]
        for r in connection.execute('SELECT * FROM route_selections ORDER BY id DESC LIMIT 12').fetchall():
            item=dict(r); item['count']=len([x for x in item['customer_ids'].split(',') if x]);
            if item['start_date'] and item['end_date']: item['date_display']=f"{item['start_date']} to {item['end_date']}"
            elif item['start_date']: item['date_display']=item['start_date']
            else: item['date_display']=''
            saved.append(item)
    groups=sorted({c['customer_group'] for c in customers if c['customer_group']})
    return render_template_string(PAGE,view='route_planner',map_customers=customers,route_groups=groups,saved_route_selections=saved,route_defaults=get_route_defaults(),success=request.args.get('success'),error=request.args.get('error'),open_action_count=get_open_action_count())

@app.route('/route-planner/selections/<int:selection_id>/delete', methods=['POST'])
def delete_route_selection(selection_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        row = connection.execute('SELECT name FROM route_selections WHERE id=?', (selection_id,)).fetchone()
        if not row:
            return redirect(url_for('route_planner', error='Saved route selection was not found.'))
        connection.execute('DELETE FROM route_selections WHERE id=?', (selection_id,))
        connection.commit()
    return redirect(url_for('route_planner', success=f'Deleted saved selection: {row[0]}.'))


@app.route('/trip-plans/<int:trip_id>/delete', methods=['POST'])
def delete_trip_plan(trip_id):
    create_database_backup('before_trip_delete')
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        trip = connection.execute('SELECT * FROM trip_plans WHERE id=?', (trip_id,)).fetchone()
        if not trip:
            return redirect(url_for('trip_prep', error='Planned trip was not found.'))

        schedule_rows = connection.execute(
            "SELECT id FROM trip_schedules WHERE trip_plan_id=? OR (trip_plan_id IS NULL AND filename=?)",
            (trip_id, f"Route Planner - {trip['trip_name']}.html"),
        ).fetchall()
        schedule_ids = [int(row['id']) for row in schedule_rows]
        for schedule_id in schedule_ids:
            connection.execute('DELETE FROM road_visit_notes WHERE schedule_id=?', (schedule_id,))
            connection.execute('DELETE FROM road_visit_attachments WHERE schedule_id=?', (schedule_id,))
        if schedule_ids:
            placeholders = ','.join('?' for _ in schedule_ids)
            connection.execute(f'DELETE FROM trip_schedules WHERE id IN ({placeholders})', schedule_ids)

        connection.execute('DELETE FROM trip_plans WHERE id=?', (trip_id,))
        connection.commit()

    return redirect(url_for('trip_prep', success=f"Deleted planned trip: {trip['trip_name']}."))


@app.route("/actions/<int:action_id>/delete", methods=["POST"])
def delete_action(action_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM store_actions WHERE id=?",(action_id,)); connection.commit()
    return redirect(url_for("actions", success="Action deleted."))


@app.route("/delete-opportunity/<int:analysis_id>", methods=["POST"])
def delete_opportunity(analysis_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM opportunity_analyses WHERE id = ?", (analysis_id,))
        connection.commit()
    return redirect(url_for("opportunity_library", success="Saved store analysis deleted."))


@app.route("/delete-month/<int:record_id>", methods=["POST"])
def delete_month(record_id):
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("DELETE FROM monthly_sales WHERE id = ?", (record_id,))
        connection.commit()
    return redirect(url_for("history", success="Saved month deleted."))


init_database()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=False,
    )
