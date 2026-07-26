import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from io import BytesIO, StringIO
from html.parser import HTMLParser
from datetime import date, datetime, timedelta
from pathlib import Path
import os

from flask import Flask, redirect, render_template_string, request, send_file, url_for
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
    <title>Sales Assistant Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
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

        @media print {
            .nav, .button-row, .subtitle, .no-print { display:none !important; }
            .page { max-width:none; padding:0; }
            .panel { box-shadow:none; border:1px solid #ddd; break-inside:avoid; }
            .print-only { display:block; }
        }
    </style>
</head>
<body>
<div class="page">
    <h1>Sales Assistant Dashboard <span style="font-size:14px;color:#667085;vertical-align:middle;">M10 Fix v4</span></h1>
    <p class="subtitle">Save monthly NetSuite results, compare performance, and build year-to-date history.</p>

    <div class="workflow-nav">
        <a href="{{ url_for('control_centre') }}" class="{% if view == 'control_centre' %}active{% endif %}"><span>1</span><strong>Home</strong><small>This week</small></a>
        <a href="{{ url_for('trip_prep') }}" class="{% if view == 'trip_prep' %}active{% endif %}"><span>2</span><strong>Plan Trip</strong><small>Schedule &amp; materials</small></a>
        <a href="{{ url_for('on_the_road') }}" class="{% if view == 'on_the_road' %}active{% endif %}"><span>3</span><strong>On the Road</strong><small>Today’s calls</small></a>
        <a href="{{ url_for('follow_up') }}" class="{% if view == 'follow_up' %}active{% endif %}"><span>4</span><strong>After Trip</strong><small>Notes &amp; follow-ups</small></a>
        <a href="{{ url_for('customer_priorities') }}" class="{% if view in ['customer_priorities', 'customer_detail'] %}active{% endif %}"><span>5</span><strong>Customers</strong><small>Priorities &amp; actions</small></a>
        <a href="{{ url_for('home') }}" class="{% if view in ['dashboard', 'history', 'saved_month', 'opportunities', 'opportunity_library', 'saved_opportunity', 'visit_brief', 'actions', 'action_form', 'qtr_bonus'] %}active{% endif %}"><span>6</span><strong>Sales Performance</strong><small>Monthly reports &amp; trends</small></a>
        <a href="{{ url_for('travel_expenses') }}" class="{% if view == 'travel_expenses' %}active{% endif %}"><span>7</span><strong>Travel &amp; Expenses</strong><small>Bookings &amp; costs</small></a>
    </div>


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
            <div><h2>On the Road</h2><p>A phone-friendly call list for completing visits and capturing quick notes.</p></div>
            <a class="button-link secondary" href="{{ url_for('trip_prep') }}">Back to Plan Trip</a>
        </div>
        {% if success %}<div class="success">{{ success }}</div>{% endif %}
        {% if road_rows %}
            {% set ns = namespace(day='') %}
            {% for row in road_rows %}
                {% if row.date_display != ns.day %}<div class="road-day-heading">{{ row.day_label }}</div>{% set ns.day = row.date_display %}{% endif %}
                <div class="road-card {% if row.completed %}complete{% endif %}">
                    <div class="road-time">{{ row.time }}{% if row.completed %} · Visit complete{% endif %}</div>
                    <div class="road-store">{{ row.store }}</div>
                    <div class="mini-tags">
                        <span class="mini-tag">{{ row.customer_type }}</span>
                        {% if row.open_action_count %}<span class="mini-tag">{{ row.open_action_count }} open action{% if row.open_action_count != 1 %}s{% endif %}</span>{% endif %}
                        {% if row.priority_classification %}<span class="mini-tag">{{ row.priority_classification }}</span>{% endif %}
                    </div>
                    {% if row.call_note %}<div class="briefing-note"><strong>Portatour note:</strong> {{ row.call_note }}</div>{% endif %}
                    {% if row.open_action_titles %}<div class="briefing-note"><strong>Outstanding:</strong> {{ row.open_action_titles|join('; ') }}</div>{% endif %}
                    <div class="road-actions">
                        {% if row.customer_exists %}<a class="view-link" href="{{ url_for('customer_detail', store_name=row.store) }}">Customer Brief</a>{% endif %}
                        <a class="view-link" href="{{ url_for('new_action', store=row.store, source='On the Road') }}">Add Action</a>
                    </div>
                    <form class="road-note-form" method="post" action="{{ url_for('update_road_visit') }}">
                        <input type="hidden" name="schedule_id" value="{{ row.schedule_id }}">
                        <input type="hidden" name="visit_date" value="{{ row.date_iso }}">
                        <input type="hidden" name="visit_time" value="{{ row.time }}">
                        <input type="hidden" name="store_name" value="{{ row.store }}">
                        <label><strong>Quick visit notes</strong></label>
                        <textarea name="visit_notes" placeholder="Key discussion, order agreed, display issue, follow-up needed...">{{ row.visit_notes }}</textarea>
                        <div class="road-note-toolbar">
                            <label><input type="checkbox" name="completed" value="1" {% if row.completed %}checked{% endif %}> Mark visit complete</label>
                            <button type="submit">Save Visit</button>
                        </div>
                    </form>
                </div>
            {% endfor %}
        {% else %}<div class="empty">No upcoming appointments were found. Upload a Portatour schedule in Plan Trip first.</div>{% endif %}
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
            {% if trip_plan.id %}<span class="change-badge {% if readiness.complete %}change-positive{% else %}change-neutral{% endif %}">{{ readiness.done }}/{{ readiness.total }} ready</span>{% endif %}
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
            {% if appointments %}<div style="margin-top:14px;">{% for item in appointments %}<div class="appointment-mini"><strong>{{ item.store }}</strong><br><span class="small">{{ item.date_display }} · {{ item.time }} · {{ item.customer_type }}</span></div>{% endfor %}</div>{% else %}<div class="empty">No appointments loaded yet.</div>{% endif %}
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
                <td><a class="customer-link" href="{{ url_for('customer_detail', store_name=row.store_name) }}">{{ row.store_name }}</a>{% if row.strategic_notes %}<br><span class="small">{{ row.strategic_notes }}</span>{% endif %}</td>
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
            <div><h2>After Trip Close-Out</h2><p>Finish the trip in order: confirm visits, bring in notes, create follow-ups, then clear the remaining work.</p></div>
            <a class="button-link secondary" href="{{ url_for('on_the_road') }}">Review Visit Notes</a>
        </div>
        <div class="after-trip-progress">
            <div class="after-trip-step {% if after_trip_summary.completed_visits %}done{% else %}attention{% endif %}"><div class="number">1</div><h4>Confirm visits</h4><p>{{ after_trip_summary.completed_visits }} of {{ after_trip_summary.total_visits }} scheduled visits marked complete.</p></div>
            <div class="after-trip-step {% if after_trip_summary.notes_captured %}done{% else %}attention{% endif %}"><div class="number">2</div><h4>Collect notes</h4><p>{{ after_trip_summary.notes_captured }} completed visits have notes captured.</p></div>
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
        <div class="heading-row"><div><h2>Captured Visit Notes</h2><p>Latest completed calls from the On the Road page.</p></div><a class="button-link" href="{{ url_for('follow_up', load_road_notes=1) }}">Load Road Notes Below</a></div>
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
        <form method="post" action="{{ url_for('follow_up') }}" enctype="multipart/form-data">
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
    after_trip_summary = {"total_visits": 0, "completed_visits": 0, "pending_visits": 0, "notes_captured": 0, "actions_today": 0}
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        latest_schedule = connection.execute("SELECT * FROM trip_schedules ORDER BY id DESC LIMIT 1").fetchone()
        if latest_schedule:
            scheduled = parse_schedule_appointments(latest_schedule["raw_html"], from_date=date.today() - timedelta(days=21))
            notes = connection.execute(
                "SELECT * FROM road_visit_notes WHERE schedule_id = ? ORDER BY visit_date DESC, visit_time DESC",
                (latest_schedule["id"],),
            ).fetchall()
            note_map = {(row["visit_date"], row["visit_time"], row["store_name"].strip().lower()): row for row in notes}
            relevant = [item for item in scheduled if item["date"] <= date.today()]
            after_trip_summary["total_visits"] = len(relevant)
            for item in relevant:
                key = (item["date"].isoformat(), item["time"], item["store"].strip().lower())
                saved = note_map.get(key)
                completed = bool(saved["completed"]) if saved else False
                visit_notes = saved["visit_notes"] if saved else ""
                if completed:
                    after_trip_summary["completed_visits"] += 1
                if completed and visit_notes.strip():
                    after_trip_summary["notes_captured"] += 1
                after_trip_visits.append({
                    "date_display": item["date"].strftime("%d %b %Y"),
                    "visit_time": item["time"],
                    "store_name": item["store"],
                    "visit_notes": visit_notes,
                    "completed": completed,
                    "date_iso": item["date"].isoformat(),
                })
            after_trip_visits.sort(key=lambda row: (row["date_iso"], row["visit_time"]), reverse=True)
            after_trip_visits = after_trip_visits[:20]
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
        open_action_count=get_open_action_count()
    )

@app.route("/follow-up/save", methods=["POST"])
def save_followups():
    raw_notes = request.form.get("raw_notes", "")
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
    return redirect(url_for("follow_up", success=message))


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
        latest_schedule = connection.execute("SELECT * FROM trip_schedules ORDER BY id DESC LIMIT 1").fetchone()
        requested_id = request.args.get("trip", type=int)
        if requested_id:
            trip_plan = connection.execute("SELECT * FROM trip_plans WHERE id=?", (requested_id,)).fetchone()
        else:
            trip_plan = connection.execute("SELECT * FROM trip_plans ORDER BY start_date DESC, id DESC LIMIT 1").fetchone()

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
        for appt in raw_appointments:
            appointments.append({**appt,"date_display":f"{appt['date'].day} {appt['date'].strftime('%b %Y')}","customer_type":classify_customer(appt["store"])})
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

    rows = []
    for key, entry in stores.items():
        manual = manual_rows.get(key, {})
        analysis = entry.get("analysis") or {}
        auto_sales = parse_currency_value(analysis.get("total_sales_ty", 0))
        manual_sales = float(manual.get("manual_sales_value", 0) or 0)
        sales = manual_sales if manual_sales > 0 else auto_sales
        yoy = float(analysis.get("total_change_raw", 0) or 0)
        opportunity = parse_currency_value(analysis.get("total_opp_gap", 0))
        urgent = int(analysis.get("urgent_stock_count", 0) or 0)
        core = int(analysis.get("core_gap_count", 0) or 0)
        growth_potential = max(1, min(5, int(manual.get("growth_potential", 3) or 3)))
        frequency = max(1, int(manual.get("visit_frequency_days", 42) or 42))
        last_visit_text = str(manual.get("last_visit_date", "") or "")
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
    return render_template_string(
        PAGE, view="customer_detail", customer=customer, customer_actions=customer_actions,
        analysis=analysis, analysis_id=analysis_id, open_action_count=get_open_action_count(),
    )

@app.route("/on-the-road")
def on_the_road():
    road_rows = []
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.row_factory = sqlite3.Row
        schedule = connection.execute("SELECT * FROM trip_schedules ORDER BY id DESC LIMIT 1").fetchone()
        actions = connection.execute("SELECT store_name, title FROM store_actions WHERE status != 'Completed'").fetchall()
        customers = {row[0].strip().lower() for row in connection.execute(
            "SELECT store_name FROM customer_priorities UNION SELECT store_name FROM opportunity_analyses UNION SELECT DISTINCT store_name FROM store_actions"
        ).fetchall() if row[0]}
        priority_rows = build_customer_priority_rows()
        priority_map = {row["store_name"].strip().lower(): row for row in priority_rows}
        saved_notes = {}
        if schedule:
            for note in connection.execute("SELECT * FROM road_visit_notes WHERE schedule_id = ?", (schedule["id"],)).fetchall():
                key = (note["visit_date"], note["visit_time"], note["store_name"].strip().lower())
                saved_notes[key] = note
    action_map = {}
    for action in actions:
        action_map.setdefault(action["store_name"].strip().lower(), []).append(action["title"])
    if schedule:
        appointments = parse_schedule_appointments(schedule["raw_html"], from_date=date.today())[:30]
        for item in appointments:
            store_key = item["store"].strip().lower()
            titles = action_map.get(store_key, [])
            note_key = (item["date"].isoformat(), item["time"], store_key)
            saved = saved_notes.get(note_key)
            priority = priority_map.get(store_key, {})
            day_label = item["date"].strftime("%A, %d %B %Y")
            if item["date"] == date.today():
                day_label = "Today - " + day_label
            elif item["date"] == date.today() + timedelta(days=1):
                day_label = "Tomorrow - " + day_label
            road_rows.append({
                "schedule_id": schedule["id"],
                "store": item["store"],
                "date_iso": item["date"].isoformat(),
                "date_display": item["date"].strftime("%d %b %Y"),
                "day_label": day_label,
                "time": item["time"],
                "call_note": item["call_note"],
                "customer_type": classify_customer(item["store"]),
                "customer_exists": store_key in customers,
                "priority_classification": priority.get("classification", ""),
                "open_action_count": len(titles),
                "open_action_titles": titles[:4],
                "visit_notes": saved["visit_notes"] if saved else "",
                "completed": bool(saved["completed"]) if saved else False,
            })
    return render_template_string(
        PAGE, view="on_the_road", road_rows=road_rows,
        success=request.args.get("success"), open_action_count=get_open_action_count(),
    )


@app.route("/on-the-road/update", methods=["POST"])
def update_road_visit():
    try:
        schedule_id = int(request.form.get("schedule_id", "0"))
    except ValueError:
        schedule_id = 0
    visit_date = request.form.get("visit_date", "").strip()
    visit_time = request.form.get("visit_time", "").strip()
    store_name = request.form.get("store_name", "").strip()
    visit_notes = request.form.get("visit_notes", "").strip()
    completed = 1 if request.form.get("completed") == "1" else 0
    if not schedule_id or not visit_date or not store_name:
        return redirect(url_for("on_the_road"))
    now = datetime.now().isoformat(timespec="seconds")
    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute(
            """
            INSERT INTO road_visit_notes (schedule_id, visit_date, visit_time, store_name, visit_notes, completed, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(schedule_id, visit_date, visit_time, store_name) DO UPDATE SET
                visit_notes=excluded.visit_notes,
                completed=excluded.completed,
                updated_at=excluded.updated_at
            """,
            (schedule_id, visit_date, visit_time, store_name, visit_notes, completed, now),
        )
        if completed:
            connection.execute(
                """
                INSERT INTO customer_priorities (store_name, growth_potential, visit_frequency_days, last_visit_date, manual_sales_value, strategic_notes, updated_at)
                VALUES (?, 3, 42, ?, 0, '', ?)
                ON CONFLICT(store_name) DO UPDATE SET last_visit_date=excluded.last_visit_date, updated_at=excluded.updated_at
                """,
                (store_name, visit_date, now),
            )
        connection.commit()
    message = f"{store_name} visit saved" + (" and marked complete." if completed else ".")
    return redirect(url_for("on_the_road", success=message))


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
