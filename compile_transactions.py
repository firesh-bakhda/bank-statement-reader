#!/usr/bin/env python3
"""Compile transaction date, description, and amount from statement PDFs in one folder."""

from __future__ import annotations

import argparse
import csv
import json
import re
import threading
import tkinter as tk
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Iterable

import customtkinter as ctk
import seaborn as sns
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
from pypdf import PdfReader

DATE_RE = r"\d{1,2}\s+[A-Z]{3}"
DATE_NUM_RE = r"\d{1,2}/\d{1,2}"
TXN_LINE_TEXT_DATE_RE = re.compile(rf"^({DATE_RE})\s+({DATE_RE})\s+(.+)$")
TXN_LINE_NUM_DATE_RE = re.compile(rf"^({DATE_NUM_RE})\s+({DATE_NUM_RE})\s+(.+)$")
AMOUNT_AT_END_RE = re.compile(r"^(.*?)\s+([0-9][0-9,]*\.\d{2})(?:\s*(CR))?$")
AMOUNT_ONLY_RE = re.compile(r"^([0-9][0-9,]*\.\d{2})(?:\s*(CR))?$")

# Headers/footers and non-transaction lines commonly found in statements.
IGNORE_PREFIXES = (
    "Posting Date",
    "Tarikh Pos",
    "Transaction Date",
    "Tarikh Transaksi",
    "Transaction Description",
    "Deskripsi Transaksi",
    "Amount (RM)",
    "Amaun (RM)",
    "Page / Mukasurat",
    "For Lost / Stolen Card Reporting",
    "Untuk Laporan Kad Hilang",
    "Pertanyaan atau Aduan",
    "Persekutuan; Tel:",
)

STOP_PREFIXES = (
    "IMPORTANT INFORMATION",
    "1. Payment Procedure",
    "Payment Procedure",
    "STATEMENT BALANCE",
)

APP_TITLE = "Bank Statement Compiler"
DEFAULT_GEOMETRY = "1250x800"
MONTH_TO_NUMBER = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}
NUMBER_TO_MONTH = {value: key for key, value in MONTH_TO_NUMBER.items()}
DEFAULT_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Housing & Utilities": (
        "TNB",
        "UNIFI",
        "TM",
        "MAXIS",
        "CELCOM",
        "DIGI",
        "BILL PAYMENT",
        "ELECTRIC",
        "WATER",
        "SEWER",
        "RENT",
        "MAINTENANCE",
    ),
    "Groceries & Essentials": (
        "TESCO",
        "LOTUS",
        "AEON",
        "GIANT",
        "MYDIN",
        "JAYA GROCER",
        "ECONSAVE",
        "MART",
        "SUPERMARKET",
        "GROCERY",
        "99 SPEEDMART",
        "KK MART",
    ),
    "Dining & Beverages": (
        "FOODPANDA",
        "GRABFOOD",
        "MCDONALD",
        "MCD",
        "KFC",
        "PIZZA",
        "RESTAURANT",
        "CAFE",
        "STARBUCKS",
        "COFFEE",
        "TEA",
        "BAKERY",
    ),
    "Transportation & Fuel": (
        "GRAB",
        "RIDES",
        "TNG",
        "TOUCH N GO",
        "TOLL",
        "PARKING",
        "PETRONAS",
        "SHELL",
        "BHP",
        "PETROL",
        "FUEL",
        "MRT",
        "LRT",
    ),
    "Health & Pharmacy": (
        "PHARMACY",
        "WATSON",
        "GUARDIAN",
        "CLINIC",
        "HOSPITAL",
        "MEDICAL",
        "DENTAL",
        "OPTICAL",
        "HEALTH",
    ),
    "Education & Childcare": (
        "SCHOOL",
        "UNIVERSITY",
        "COLLEGE",
        "TUITION",
        "BOOKSTORE",
        "KINDERGARTEN",
        "DAYCARE",
        "CHILDCARE",
        "EDUCATION",
    ),
    "Shopping & Retail": (
        "SHOPEE",
        "LAZADA",
        "AMAZON",
        "IKEA",
        "ZALORA",
        "STORE",
        "ECOM",
        "RETAIL",
        "BOUTIQUE",
        "DEPARTMENT STORE",
    ),
    "Travel & Accommodation": (
        "AIRASIA",
        "MALAYSIA AIRLINES",
        "BOOKING",
        "AGODA",
        "EXPEDIA",
        "HOTEL",
        "RESORT",
        "AIRBNB",
        "TRAVEL",
        "FLIGHT",
    ),
    "Entertainment & Subscriptions": (
        "NETFLIX",
        "SPOTIFY",
        "YOUTUBE",
        "DISNEY",
        "APPLE.COM/BILL",
        "GOOGLE PLAY",
        "STEAM",
        "PLAYSTATION",
        "XBOX",
        "CINEMA",
    ),
    "Insurance & Financial Fees": (
        "INSURANCE",
        "TAKAFUL",
        "GREAT EASTERN",
        "AIA",
        "ALLIANZ",
        "FINANCE CHARGES",
        "LATE CHARGES",
        "SERVICE TAX",
        "ANNUAL FEE",
        "INTEREST",
    ),
    "Transfers & Cash": (
        "TRANSFER",
        "DUITNOW",
        "IBG",
        "GIRO",
        "ATM",
        "WITHDRAWAL",
        "TOP-UP",
        "RELOAD",
        "PAYMENT",
        "BANK",
    ),
    "Government & Taxes": (
        "LHDN",
        "JPJ",
        "SST",
        "DUTY",
        "TAX",
        "GOVERNMENT",
        "MAJLIS",
        "DBKL",
        "PDRM",
    ),
}

CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = dict(DEFAULT_CATEGORY_KEYWORDS)


@dataclass
class Transaction:
    source_file: str
    transaction_date: str
    description: str
    amount: float


def parse_amount(amount_text: str, is_credit: bool) -> float:
    amount = float(amount_text.replace(",", ""))
    return -amount if is_credit else amount


def should_ignore(line: str) -> bool:
    if not line:
        return True
    if any(line.startswith(prefix) for prefix in IGNORE_PREFIXES):
        return True
    if line.startswith("Transaction Details /"):
        return True
    if re.match(r"^(?:X{4}|\d{4})-(?:X{4}|\d{4})-(?:X{4}|\d{4})-(?:X{4}|\d{4})", line):
        return True
    if line == "CONTINUED ON NEXT PAGE.." or line == "ON-GOING PROMOTION":
        return True
    return False


def parse_statement_period(source_file: str) -> tuple[int, int]:
    # Prefer YYYYMMDD found in filenames like eStatement20250127.pdf.
    for year_text, month_text, day_text in re.findall(r"(20\d{2})(\d{2})(\d{2})", source_file):
        year = int(year_text)
        month = int(month_text)
        day = int(day_text)
        if 1 <= month <= 12 and 1 <= day <= 31:
            return year, month
    today = date.today()
    return today.year, today.month


def add_year_to_transaction_date(
    date_text: str,
    statement_year: int,
    statement_month: int,
) -> str:
    # Statement month can include late transactions from the previous month/year.
    parts = date_text.split()
    if len(parts) != 2:
        return date_text

    day_text, month_text = parts
    month_num = MONTH_TO_NUMBER.get(month_text.upper())
    if month_num is None:
        return date_text

    txn_year = statement_year - 1 if month_num > statement_month else statement_year
    return f"{day_text} {month_text.upper()} {txn_year}"


def add_year_to_numeric_transaction_date(
    date_text: str,
    statement_year: int,
    statement_month: int,
) -> str:
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", date_text)
    if not m:
        return date_text

    day_num = int(m.group(1))
    month_num = int(m.group(2))
    if not (1 <= day_num <= 31 and 1 <= month_num <= 12):
        return date_text

    txn_year = statement_year - 1 if month_num > statement_month else statement_year
    month_text = NUMBER_TO_MONTH.get(month_num)
    if month_text is None:
        return date_text

    return f"{day_num:02d} {month_text} {txn_year}"


def parse_txn_date(date_text: str) -> datetime | None:
    for fmt in ("%d %b %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date_text, fmt)
        except ValueError:
            continue
    return None


def normalize_merchant(description: str) -> str:
    cleaned = " ".join(description.upper().split())
    cleaned = re.sub(r"\b\d{4,}\b", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:80] if cleaned else "UNKNOWN"


@lru_cache(maxsize=2048)
def _compile_sql_like_pattern(pattern: str) -> re.Pattern[str]:
    regex_parts: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            i += 1
            regex_parts.append(re.escape(pattern[i]))
        elif ch == "%":
            regex_parts.append(".*")
        elif ch == "_":
            regex_parts.append(".")
        else:
            regex_parts.append(re.escape(ch))
        i += 1
    regex_parts.append("$")
    return re.compile("".join(regex_parts), re.IGNORECASE)


def sql_like_match(value: str, pattern: str) -> bool:
    return bool(_compile_sql_like_pattern(pattern).match(value))


def _normalize_for_keyword_matching(text: str) -> str:
    # Normalize punctuation/spacing so word and phrase matching is stable.
    cleaned = re.sub(r"[^A-Z0-9]+", " ", text.upper())
    return " ".join(cleaned.split())


def categorize_transaction(description: str) -> str:
    desc_for_like = " ".join(description.split())
    normalized_desc = _normalize_for_keyword_matching(description)
    padded_desc = f" {normalized_desc} " if normalized_desc else " "

    for category, keywords in CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            normalized_keyword = _normalize_for_keyword_matching(keyword)
            if not normalized_keyword:
                continue

            # Rule:
            # - keyword length < 5 chars: match whole word/phrase only
            # - keyword length >= 5 chars: use SQL LIKE behavior
            keyword_len = len(re.sub(r"\s+", "", normalized_keyword))
            if keyword_len < 5:
                if f" {normalized_keyword} " in padded_desc:
                    return category
                continue

            like_pattern = keyword if ("%" in keyword or "_" in keyword) else f"%{keyword}%"
            if sql_like_match(desc_for_like, like_pattern):
                return category
    return "Other"


def _serialize_category_config(config: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    return {category: list(keywords) for category, keywords in config.items()}


def _validate_category_config(raw_config: object) -> dict[str, tuple[str, ...]] | None:
    if not isinstance(raw_config, dict):
        return None

    validated: dict[str, tuple[str, ...]] = {}
    for category, keywords in raw_config.items():
        if not isinstance(category, str) or not category.strip():
            return None
        if not isinstance(keywords, list):
            return None

        clean_keywords = tuple(
            keyword.strip().upper()
            for keyword in keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not clean_keywords:
            continue
        validated[category.strip()] = clean_keywords

    return validated or None


def load_category_keywords(config_path: Path) -> tuple[dict[str, tuple[str, ...]], str | None]:
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            json.dumps(_serialize_category_config(DEFAULT_CATEGORY_KEYWORDS), indent=2),
            encoding="utf-8",
        )
        return dict(DEFAULT_CATEGORY_KEYWORDS), f"Created default category config: {config_path.resolve()}"

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return dict(DEFAULT_CATEGORY_KEYWORDS), (
            f"Category config read failed ({config_path.resolve()}): {exc}. Using built-in defaults."
        )

    validated = _validate_category_config(raw)
    if validated is None:
        return dict(DEFAULT_CATEGORY_KEYWORDS), (
            f"Category config has invalid format ({config_path.resolve()}). Using built-in defaults."
        )

    return validated, None


def extract_transactions_from_text(
    text: str,
    source_file: str,
    statement_year: int,
    statement_month: int,
) -> list[Transaction]:
    transactions: list[Transaction] = []
    pending: dict[str, str] | None = None

    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())

        if any(line.startswith(prefix) for prefix in STOP_PREFIXES):
            pending = None
            continue

        if should_ignore(line):
            continue

        # New transaction line starts with posting date + transaction date.
        txn_match = TXN_LINE_TEXT_DATE_RE.match(line)
        is_numeric_date = False
        if txn_match is None:
            txn_match = TXN_LINE_NUM_DATE_RE.match(line)
            is_numeric_date = txn_match is not None

        if txn_match:
            _, transaction_date, remainder = txn_match.groups()
            if is_numeric_date:
                transaction_date = add_year_to_numeric_transaction_date(
                    transaction_date, statement_year, statement_month
                )
            else:
                transaction_date = add_year_to_transaction_date(
                    transaction_date, statement_year, statement_month
                )

            amount_match = AMOUNT_AT_END_RE.match(remainder)
            if amount_match:
                description, amount_text, cr_marker = amount_match.groups()
                transactions.append(
                    Transaction(
                        source_file=source_file,
                        transaction_date=transaction_date,
                        description=description.strip(),
                        amount=parse_amount(amount_text, cr_marker == "CR"),
                    )
                )
                pending = None
            else:
                pending = {"transaction_date": transaction_date, "description": remainder.strip()}
            continue

        if pending is None:
            continue

        amount_only_match = AMOUNT_ONLY_RE.match(line)
        if amount_only_match:
            amount_text, cr_marker = amount_only_match.groups()
            transactions.append(
                Transaction(
                    source_file=source_file,
                    transaction_date=pending["transaction_date"],
                    description=pending["description"].strip(),
                    amount=parse_amount(amount_text, cr_marker == "CR"),
                )
            )
            pending = None
            continue

        # Continuation of multiline description.
        pending["description"] = f"{pending['description']} {line}".strip()

    return transactions


def extract_transactions_from_pdf(pdf_path: Path) -> list[Transaction]:
    reader = PdfReader(str(pdf_path))
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    statement_year, statement_month = parse_statement_period(pdf_path.name)
    return extract_transactions_from_text(text, pdf_path.name, statement_year, statement_month)


def extract_transactions_from_folder(folder: Path) -> tuple[list[Transaction], list[str]]:
    pdf_files = sorted(folder.glob("*.pdf"))
    if not pdf_files:
        return [], [f"No PDF files found in: {folder.resolve()}"]

    all_transactions: list[Transaction] = []
    warnings: list[str] = []
    for pdf in pdf_files:
        try:
            all_transactions.extend(extract_transactions_from_pdf(pdf))
        except Exception as exc:
            warnings.append(f"Failed to parse {pdf.name}: {exc}")

    return all_transactions, warnings


def render_table(rows: Iterable[Transaction]) -> str:
    rows = list(rows)
    if not rows:
        return "No transactions found."

    date_w = max(len("Date"), *(len(r.transaction_date) for r in rows))
    amount_w = max(len("Amount"), *(len(f"{r.amount:.2f}") for r in rows))

    header = f"{'Date':<{date_w}}  {'Description'}  {'Amount':>{amount_w}}  {'Source'}"
    sep = "-" * len(header)
    body = [
        f"{r.transaction_date:<{date_w}}  {r.description}  {r.amount:>{amount_w}.2f}  {r.source_file}"
        for r in rows
    ]
    return "\n".join([header, sep, *body])


def save_csv(rows: Iterable[Transaction], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["source_file", "transaction_date", "description", "amount"])
        for r in rows:
            writer.writerow([r.source_file, r.transaction_date, r.description, f"{r.amount:.2f}"])


class App(ctk.CTk):
    def __init__(self, initial_folder: Path, output_path: Path, category_config_path: Path) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry(DEFAULT_GEOMETRY)
        self.minsize(800, 700)

        self.current_folder = initial_folder
        self.output_path = output_path
        self.category_config_path = category_config_path
        self.transactions: list[Transaction] = []
        self.year_filtered_transactions: list[Transaction] = []
        self.filtered_transactions: list[Transaction] = []
        self.selected_year: str = "All"
        self.selected_category: str = "All"
        self.available_years: list[str] = []
        self.available_categories: list[str] = []
        self.is_loading = False
        self.menu_buttons: list[ctk.CTkButton] = []
        self.dashboard_year_buttons: list[ctk.CTkButton] = []
        self.transactions_year_buttons: list[ctk.CTkButton] = []
        self.transactions_category_buttons: list[ctk.CTkButton] = []
        self.visual_year_buttons: list[ctk.CTkButton] = []
        self.chart_canvases: list[FigureCanvasTkAgg] = []
        self._category_wrap_after_id: str | None = None

        self.status_var = tk.StringVar(value="Ready")
        self.folder_var = tk.StringVar(value=f"Folder: {self.current_folder.resolve()}")

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")
        sns.set_theme(style="whitegrid", context="notebook")

        self._build_layout()
        self.refresh_data()

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()
        self._build_body()
        self._build_footer()

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, corner_radius=0)
        header.grid(row=0, column=0, sticky="nsew")
        header.grid_columnconfigure(1, weight=1)

        logo = ctk.CTkLabel(
            header,
            text="LOGO",
            width=80,
            height=40,
            fg_color=("#dbeafe", "#1e3a8a"),
            corner_radius=8,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        logo.grid(row=0, column=0, padx=12, pady=10)

        title = ctk.CTkLabel(
            header,
            text=APP_TITLE,
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title.grid(row=0, column=1, sticky="w", padx=4)

        folder_label = ctk.CTkLabel(
            header,
            textvariable=self.folder_var,
            font=ctk.CTkFont(size=12),
        )
        folder_label.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 10))

    def _build_body(self) -> None:
        body = ctk.CTkFrame(self)
        body.grid(row=1, column=0, sticky="nsew")
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._build_left_menu(body)
        self._build_content_area(body)

    def _build_left_menu(self, parent: ctk.CTkFrame) -> None:
        menu = ctk.CTkFrame(parent, width=180)
        menu.grid(row=0, column=0, sticky="nsw", padx=(10, 6), pady=10)
        menu.grid_propagate(False)

        ctk.CTkLabel(
            menu,
            text="Menu",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(anchor="w", padx=12, pady=(12, 10))

        self.dashboard_button = ctk.CTkButton(menu, text="Dashboard", command=self.show_dashboard)
        self.dashboard_button.pack(fill="x", padx=12, pady=6)
        self.transactions_button = ctk.CTkButton(
            menu, text="Transactions", command=self.show_transactions
        )
        self.transactions_button.pack(fill="x", padx=12, pady=6)
        self.visual_dashboard_button = ctk.CTkButton(
            menu, text="Visual Dashboard", command=self.show_visual_dashboard
        )
        self.visual_dashboard_button.pack(fill="x", padx=12, pady=6)
        self.category_config_button = ctk.CTkButton(
            menu,
            text="Category Config",
            command=self.show_category_config,
        )
        self.category_config_button.pack(fill="x", padx=12, pady=6)
        self.choose_folder_button = ctk.CTkButton(menu, text="Choose Folder", command=self.choose_folder)
        self.choose_folder_button.pack(fill="x", padx=12, pady=6)
        self.refresh_button = ctk.CTkButton(menu, text="Refresh", command=self.refresh_data)
        self.refresh_button.pack(fill="x", padx=12, pady=6)
        self.export_button = ctk.CTkButton(menu, text="Export CSV", command=self.export_csv)
        self.export_button.pack(fill="x", padx=12, pady=6)
        self.menu_buttons = [
            self.dashboard_button,
            self.transactions_button,
            self.visual_dashboard_button,
            self.category_config_button,
            self.choose_folder_button,
            self.refresh_button,
            self.export_button,
        ]

    def _build_content_area(self, parent: ctk.CTkFrame) -> None:
        self.content = ctk.CTkFrame(parent)
        self.content.grid(row=0, column=1, sticky="nsew", padx=(6, 10), pady=10)
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.dashboard_frame = ctk.CTkScrollableFrame(self.content)
        self.transactions_frame = ctk.CTkFrame(self.content)
        self.visual_frame = ctk.CTkScrollableFrame(self.content)
        self.category_config_frame = ctk.CTkFrame(self.content)

        self.dashboard_frame.grid(row=0, column=0, sticky="nsew")
        self.transactions_frame.grid(row=0, column=0, sticky="nsew")
        self.visual_frame.grid(row=0, column=0, sticky="nsew")
        self.category_config_frame.grid(row=0, column=0, sticky="nsew")
        self.transactions_frame.grid_remove()
        self.visual_frame.grid_remove()
        self.category_config_frame.grid_remove()

        self.dashboard_filter_bar = ctk.CTkFrame(self.dashboard_frame)
        self.dashboard_filter_bar.pack(fill="x", padx=10, pady=(10, 4))
        self.dashboard_filter_label = ctk.CTkLabel(
            self.dashboard_filter_bar,
            text="Year Filter",
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.dashboard_filter_label.pack(side="left", padx=(10, 8), pady=8)

        self.dashboard_filter_buttons = ctk.CTkFrame(self.dashboard_filter_bar, fg_color="transparent")
        self.dashboard_filter_buttons.pack(side="left", fill="x", expand=True, pady=6)

        self.dashboard_content = ctk.CTkFrame(self.dashboard_frame, fg_color="transparent")
        self.dashboard_content.pack(fill="both", expand=True)

        self.visual_filter_bar = ctk.CTkFrame(self.visual_frame)
        self.visual_filter_bar.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(
            self.visual_filter_bar,
            text="Year Filter",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left", padx=(10, 8), pady=8)
        self.visual_filter_buttons = ctk.CTkFrame(self.visual_filter_bar, fg_color="transparent")
        self.visual_filter_buttons.pack(side="left", fill="x", expand=True, pady=6)

        self.visual_content = ctk.CTkFrame(self.visual_frame, fg_color="transparent")
        self.visual_content.pack(fill="both", expand=True)

        self.loading_overlay = ctk.CTkFrame(self.content, corner_radius=12)
        self.loading_message_var = tk.StringVar(value="Processing...")
        self.loading_label = ctk.CTkLabel(
            self.loading_overlay,
            textvariable=self.loading_message_var,
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.loading_label.pack(padx=24, pady=(18, 10))
        self.loading_bar = ctk.CTkProgressBar(self.loading_overlay, mode="indeterminate", width=220)
        self.loading_bar.pack(padx=24, pady=(0, 18))
        self.loading_overlay.place_forget()

        self._build_transactions_table()
        self._build_category_config_editor()

    def _build_category_config_editor(self) -> None:
        self.category_config_frame.grid_rowconfigure(1, weight=1)
        self.category_config_frame.grid_rowconfigure(2, weight=0)
        self.category_config_frame.grid_columnconfigure(0, weight=3)
        self.category_config_frame.grid_columnconfigure(1, weight=2)

        top_bar = ctk.CTkFrame(self.category_config_frame, corner_radius=12)
        top_bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 8))
        top_bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top_bar,
            text="Category Config Studio",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))

        ctk.CTkLabel(
            top_bar,
            text="Edit and validate category rules used for transaction classification.",
            font=ctk.CTkFont(size=12),
            text_color=("#475569", "#cbd5e1"),
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 4))

        ctk.CTkLabel(
            top_bar,
            text=f"Path: {self.category_config_path.resolve()}",
            font=ctk.CTkFont(size=11),
            text_color=("#64748b", "#94a3b8"),
        ).grid(row=2, column=0, sticky="w", padx=12, pady=(0, 10))

        editor_card = ctk.CTkFrame(self.category_config_frame, corner_radius=12)
        editor_card.grid(row=1, column=0, sticky="nsew", padx=(10, 6), pady=(0, 8))
        editor_card.grid_rowconfigure(1, weight=1)
        editor_card.grid_columnconfigure(0, weight=1)

        button_row = ctk.CTkFrame(editor_card, fg_color="transparent")
        button_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))

        self.config_reload_button = ctk.CTkButton(
            button_row,
            text="Reload",
            width=90,
            command=self._reload_category_config_text,
        )
        self.config_reload_button.pack(side="left", padx=(0, 6))

        self.config_format_button = ctk.CTkButton(
            button_row,
            text="Format",
            width=90,
            command=self._format_category_config_text,
            fg_color=("#334155", "#334155"),
            hover_color=("#1e293b", "#1e293b"),
        )
        self.config_format_button.pack(side="left", padx=6)

        self.config_validate_button = ctk.CTkButton(
            button_row,
            text="Validate",
            width=90,
            command=self._validate_category_config_text,
            fg_color=("#0f766e", "#0f766e"),
            hover_color=("#115e59", "#115e59"),
        )
        self.config_validate_button.pack(side="left", padx=6)

        self.config_save_button = ctk.CTkButton(
            button_row,
            text="Save",
            width=100,
            command=self._save_category_config_text,
            fg_color=("#1d4ed8", "#1d4ed8"),
            hover_color=("#1e40af", "#1e40af"),
        )
        self.config_save_button.pack(side="left", padx=6)

        self.config_textbox = ctk.CTkTextbox(
            editor_card,
            wrap="none",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        self.config_textbox.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        guide_card = ctk.CTkFrame(self.category_config_frame, corner_radius=12)
        guide_card.grid(row=1, column=1, sticky="nsew", padx=(6, 10), pady=(0, 8))
        guide_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            guide_card,
            text="How Matching Works",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 6))

        ctk.CTkLabel(
            guide_card,
            text=(
                "- Keywords under 5 chars: whole-word match\n"
                "- Keywords 5+ chars: SQL LIKE behavior\n"
                "- Use % for wildcard and _ for one character\n"
                "- Example: %GREAT EASTERN%"
            ),
            justify="left",
            anchor="nw",
            font=ctk.CTkFont(size=12),
            text_color=("#475569", "#cbd5e1"),
        ).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))

        ctk.CTkLabel(
            guide_card,
            text="JSON Shape",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=12, pady=(6, 4))

        example_box = ctk.CTkTextbox(
            guide_card,
            height=160,
            wrap="word",
            font=ctk.CTkFont(family="Consolas", size=11),
        )
        example_box.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 10))
        example_box.insert(
            "1.0",
            '{\n'
            '  "Insurance & Financial Fees": [\n'
            '    "%GREAT EASTERN%",\n'
            '    "INSURANCE",\n'
            '    "AIA"\n'
            '  ]\n'
            '}',
        )
        example_box.configure(state="disabled")

        self.config_editor_status_var = tk.StringVar(value="Ready")
        status_bar = ctk.CTkFrame(self.category_config_frame, corner_radius=10)
        status_bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        ctk.CTkLabel(
            status_bar,
            textvariable=self.config_editor_status_var,
            anchor="w",
            font=ctk.CTkFont(size=12),
        ).pack(fill="x", padx=10, pady=8)

        self._reload_category_config_text()

    def _set_config_editor_status(self, message: str) -> None:
        self.config_editor_status_var.set(message)
        self.set_status(message)

    def _reload_category_config_text(self) -> None:
        try:
            content = self.category_config_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = json.dumps(_serialize_category_config(DEFAULT_CATEGORY_KEYWORDS), indent=2)
            self.category_config_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            self._set_config_editor_status(f"Unable to read config: {exc}")
            return

        self.config_textbox.delete("1.0", "end")
        self.config_textbox.insert("1.0", content)
        self._set_config_editor_status("Category config loaded.")

    def _format_category_config_text(self) -> None:
        raw_content = self.config_textbox.get("1.0", "end").strip()
        if not raw_content:
            self._set_config_editor_status("Config is empty. Nothing to format.")
            return

        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            self._set_config_editor_status(f"Invalid JSON at line {exc.lineno}, column {exc.colno}.")
            return

        pretty_content = json.dumps(parsed, indent=2)
        self.config_textbox.delete("1.0", "end")
        self.config_textbox.insert("1.0", pretty_content)
        self._set_config_editor_status("Formatted JSON.")

    def _validate_category_config_text(self) -> None:
        raw_content = self.config_textbox.get("1.0", "end").strip()
        if not raw_content:
            self._set_config_editor_status("Config is empty. Provide a JSON object.")
            return

        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            self._set_config_editor_status(f"Invalid JSON at line {exc.lineno}, column {exc.colno}.")
            return

        validated = _validate_category_config(parsed)
        if validated is None:
            self._set_config_editor_status("Invalid shape. Expected {\"Category\": [\"KEYWORD\", ...]}.")
            return

        self._set_config_editor_status(f"Validation passed: {len(validated)} categories.")

    def _set_config_controls_state(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        self.config_reload_button.configure(state=state)
        self.config_format_button.configure(state=state)
        self.config_validate_button.configure(state=state)
        self.config_save_button.configure(state=state)
        self.config_textbox.configure(state=state)

    def _save_category_config_text(self) -> None:
        if self.is_loading:
            return

        raw_content = self.config_textbox.get("1.0", "end").strip()
        if not raw_content:
            self._set_config_editor_status("Config is empty. Provide a JSON object before saving.")
            return

        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError as exc:
            self._set_config_editor_status(f"Invalid JSON at line {exc.lineno}, column {exc.colno}.")
            return

        validated = _validate_category_config(parsed)
        if validated is None:
            self._set_config_editor_status("Invalid config format. Use {\"Category\": [\"KEYWORD\", ...]}.")
            return

        pretty_content = json.dumps(_serialize_category_config(validated), indent=2)
        self._set_config_controls_state(False)
        self._start_loading("Saving category config...")

        def worker() -> None:
            try:
                self.category_config_path.parent.mkdir(parents=True, exist_ok=True)
                self.category_config_path.write_text(pretty_content, encoding="utf-8")
                self.after(0, lambda: self._on_category_config_saved(validated, pretty_content))
            except Exception as exc:
                self.after(0, lambda: self._on_background_error(f"Save config failed: {exc}"))
                self.after(0, lambda: self._set_config_controls_state(True))

        threading.Thread(target=worker, daemon=True).start()

    def _on_category_config_saved(
        self,
        validated_keywords: dict[str, tuple[str, ...]],
        pretty_content: str,
    ) -> None:
        global CATEGORY_KEYWORDS
        CATEGORY_KEYWORDS = validated_keywords

        self.config_textbox.configure(state="normal")
        self.config_textbox.delete("1.0", "end")
        self.config_textbox.insert("1.0", pretty_content)

        self._rebuild_category_filters()
        self._apply_selected_category_filter()
        self._refresh_transactions_table()
        self._refresh_dashboard()
        self._refresh_visual_dashboard()

        self._stop_loading()
        self._set_config_controls_state(True)
        self._set_config_editor_status("Category config saved and reloaded.")

    def _build_transactions_table(self) -> None:
        self.transactions_frame.grid_rowconfigure(2, weight=1)
        self.transactions_frame.grid_columnconfigure(0, weight=1)

        filter_bar = ctk.CTkFrame(self.transactions_frame)
        filter_bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=10, pady=(10, 4))
        ctk.CTkLabel(
            filter_bar,
            text="Year Filter",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left", padx=(10, 8), pady=8)
        self.transactions_filter_buttons = ctk.CTkFrame(filter_bar, fg_color="transparent")
        self.transactions_filter_buttons.pack(side="left", fill="x", expand=True, pady=6)

        category_bar = ctk.CTkFrame(self.transactions_frame)
        category_bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 4))
        ctk.CTkLabel(
            category_bar,
            text="Category Filter",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(side="left", padx=(10, 8), pady=8)
        self.transactions_category_filter_buttons = ctk.CTkFrame(category_bar, fg_color="transparent")
        self.transactions_category_filter_buttons.pack(side="left", fill="x", expand=True, pady=6)
        self.transactions_category_filter_buttons.bind(
            "<Configure>",
            lambda _event: self._schedule_category_wrap(),
        )

        columns = ("date", "description", "amount", "source")
        tree = ttk.Treeview(self.transactions_frame, columns=columns, show="headings")
        tree.heading("date", text="Date")
        tree.heading("description", text="Description")
        tree.heading("amount", text="Amount")
        tree.heading("source", text="Source")

        tree.column("date", width=130, anchor="w")
        tree.column("description", width=340, anchor="w")
        tree.column("amount", width=120, anchor="e")
        tree.column("source", width=150, anchor="w")

        scrollbar = ttk.Scrollbar(self.transactions_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)

        tree.grid(row=2, column=0, sticky="nsew", padx=(10, 0), pady=10)
        scrollbar.grid(row=2, column=1, sticky="ns", padx=(0, 10), pady=10)

        self.tree = tree

    def _extract_year(self, txn: Transaction) -> str:
        parts = txn.transaction_date.split()
        if len(parts) >= 3 and parts[2].isdigit():
            return parts[2]
        dt = parse_txn_date(txn.transaction_date)
        if dt:
            return str(dt.year)
        return "Unknown"

    def _update_filter_button_styles(self) -> None:
        active_color = ("#1d4ed8", "#1d4ed8")
        active_hover = ("#1e40af", "#1e40af")
        inactive_color = ("#374151", "#374151")
        inactive_hover = ("#4b5563", "#4b5563")

        for button in [
            *self.dashboard_year_buttons,
            *self.transactions_year_buttons,
            *self.visual_year_buttons,
        ]:
            year_value = button.cget("text")
            if year_value == self.selected_year:
                button.configure(fg_color=active_color, hover_color=active_hover)
            else:
                button.configure(fg_color=inactive_color, hover_color=inactive_hover)

        active_cat_color = ("#0f766e", "#0f766e")
        active_cat_hover = ("#115e59", "#115e59")
        for button in self.transactions_category_buttons:
            category_value = button.cget("text")
            if category_value == self.selected_category:
                button.configure(fg_color=active_cat_color, hover_color=active_cat_hover)
            else:
                button.configure(fg_color=inactive_color, hover_color=inactive_hover)

    def _rebuild_year_filters(self) -> None:
        years = sorted(
            {self._extract_year(txn) for txn in self.transactions if self._extract_year(txn).isdigit()},
            key=int,
            reverse=True,
        )
        self.available_years = ["All", *years]

        if self.selected_year != "All" and self.selected_year not in years:
            self.selected_year = "All"

        for widget in self.dashboard_filter_buttons.winfo_children():
            widget.destroy()
        for widget in self.transactions_filter_buttons.winfo_children():
            widget.destroy()
        for widget in self.visual_filter_buttons.winfo_children():
            widget.destroy()

        self.dashboard_year_buttons = []
        self.transactions_year_buttons = []
        self.visual_year_buttons = []

        for year in self.available_years:
            btn_dash = ctk.CTkButton(
                self.dashboard_filter_buttons,
                text=year,
                width=68,
                height=28,
                corner_radius=14,
                command=lambda y=year: self.select_year_filter(y),
            )
            btn_dash.pack(side="left", padx=4)
            self.dashboard_year_buttons.append(btn_dash)

            btn_txn = ctk.CTkButton(
                self.transactions_filter_buttons,
                text=year,
                width=68,
                height=28,
                corner_radius=14,
                command=lambda y=year: self.select_year_filter(y),
            )
            btn_txn.pack(side="left", padx=4)
            self.transactions_year_buttons.append(btn_txn)

            btn_visual = ctk.CTkButton(
                self.visual_filter_buttons,
                text=year,
                width=68,
                height=28,
                corner_radius=14,
                command=lambda y=year: self.select_year_filter(y),
            )
            btn_visual.pack(side="left", padx=4)
            self.visual_year_buttons.append(btn_visual)

        self._update_filter_button_styles()

    def _apply_selected_year_filter(self) -> None:
        if self.selected_year == "All":
            self.year_filtered_transactions = list(self.transactions)
        else:
            self.year_filtered_transactions = [
                txn for txn in self.transactions if self._extract_year(txn) == self.selected_year
            ]

    def _rebuild_category_filters(self) -> None:
        categories = sorted(
            {
                categorize_transaction(txn.description)
                for txn in self.year_filtered_transactions
                if txn.amount > 0
            }
        )
        self.available_categories = ["All", *categories]

        if self.selected_category != "All" and self.selected_category not in categories:
            self.selected_category = "All"

        for widget in self.transactions_category_filter_buttons.winfo_children():
            widget.destroy()

        self.transactions_category_buttons = []
        for category in self.available_categories:
            btn = ctk.CTkButton(
                self.transactions_category_filter_buttons,
                text=category,
                width=self._category_button_width(category),
                height=28,
                corner_radius=14,
                command=lambda c=category: self.select_category_filter(c),
            )
            self.transactions_category_buttons.append(btn)

        self._layout_category_filter_buttons()
        self._update_filter_button_styles()

    def _schedule_category_wrap(self) -> None:
        if self._category_wrap_after_id is not None:
            self.after_cancel(self._category_wrap_after_id)
        self._category_wrap_after_id = self.after(80, self._layout_category_filter_buttons)

    def _category_button_width(self, label: str) -> int:
        # Approximate text width to avoid clipping and improve wrap decisions.
        return max(78, min(280, 26 + len(label) * 7))

    def _layout_category_filter_buttons(self) -> None:
        self._category_wrap_after_id = None
        container = self.transactions_category_filter_buttons

        if not self.transactions_category_buttons:
            container.configure(height=1)
            return

        container.update_idletasks()
        available_width = container.winfo_width()
        if available_width <= 1:
            available_width = container.winfo_reqwidth()

        for button in self.transactions_category_buttons:
            button.place_forget()

        x = 0
        y = 0
        row_height = 0
        gap_x = 8
        gap_y = 8

        for button in self.transactions_category_buttons:
            button.update_idletasks()
            button_width = button.winfo_reqwidth()
            button_height = button.winfo_reqheight()

            if x > 0 and (x + gap_x + button_width) > available_width:
                x = 0
                y += row_height + gap_y
                row_height = 0

            if x > 0:
                x += gap_x

            button.place(x=x, y=y)
            x += button_width
            row_height = max(row_height, button_height)

        container.configure(height=y + row_height)

    def _apply_selected_category_filter(self) -> None:
        if self.selected_category == "All":
            self.filtered_transactions = list(self.year_filtered_transactions)
            return

        self.filtered_transactions = [
            txn
            for txn in self.year_filtered_transactions
            if categorize_transaction(txn.description) == self.selected_category
        ]

    def select_year_filter(self, year: str) -> None:
        self.selected_year = year
        self._apply_selected_year_filter()
        self._rebuild_category_filters()
        self._apply_selected_category_filter()
        self._update_filter_button_styles()
        self._refresh_transactions_table()
        self._refresh_dashboard()
        self._refresh_visual_dashboard()
        self.set_status(
            f"Filters: {self.selected_year} / {self.selected_category} ({len(self.filtered_transactions)} shown)"
        )

    def select_category_filter(self, category: str) -> None:
        self.selected_category = category
        self._apply_selected_category_filter()
        self._update_filter_button_styles()
        self._refresh_transactions_table()
        self._refresh_dashboard()
        self._refresh_visual_dashboard()
        self.set_status(
            f"Filters: {self.selected_year} / {self.selected_category} ({len(self.filtered_transactions)} shown)"
        )

    def _build_footer(self) -> None:
        footer = ctk.CTkFrame(self, corner_radius=0, height=34)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)

        status = ctk.CTkLabel(
            footer,
            textvariable=self.status_var,
            anchor="w",
            font=ctk.CTkFont(size=12),
        )
        status.grid(row=0, column=0, sticky="ew", padx=12, pady=6)

    def set_status(self, message: str) -> None:
        self.status_var.set(message)

    def choose_folder(self) -> None:
        if self.is_loading:
            return
        selected = filedialog.askdirectory(initialdir=str(self.current_folder))
        if not selected:
            return
        self.current_folder = Path(selected)
        self.folder_var.set(f"Folder: {self.current_folder.resolve()}")
        self.refresh_data()

    def refresh_data(self) -> None:
        if self.is_loading:
            return
        self._start_loading("Reading statement PDFs...")

        def worker() -> None:
            try:
                rows, warnings = extract_transactions_from_folder(self.current_folder)
                self.after(0, lambda: self._on_refresh_success(rows, warnings))
            except Exception as exc:
                self.after(0, lambda: self._on_background_error(f"Refresh failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_refresh_success(self, rows: list[Transaction], warnings: list[str]) -> None:
        self.transactions = rows
        self._rebuild_year_filters()
        self._apply_selected_year_filter()
        self._rebuild_category_filters()
        self._apply_selected_category_filter()
        self._refresh_dashboard()
        self._refresh_transactions_table()
        self._refresh_visual_dashboard()

        if warnings:
            self.set_status(f"Loaded {len(rows)} transactions with {len(warnings)} warning(s).")
        else:
            self.set_status(f"Loaded {len(rows)} transactions.")

        self.show_dashboard()
        self._stop_loading()

    def _start_loading(self, message: str) -> None:
        self.is_loading = True
        self.loading_message_var.set(message)
        self.loading_overlay.place(relx=0.5, rely=0.5, anchor="center")
        self.loading_overlay.lift()
        self.loading_bar.start()
        self.set_status(message)
        for button in self.menu_buttons:
            button.configure(state="disabled")

    def _stop_loading(self) -> None:
        self.loading_bar.stop()
        self.loading_overlay.place_forget()
        self.is_loading = False
        for button in self.menu_buttons:
            button.configure(state="normal")

    def _on_background_error(self, message: str) -> None:
        self._stop_loading()
        self.set_status(message)

    def _refresh_transactions_table(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for txn in self.filtered_transactions:
            self.tree.insert(
                "",
                "end",
                values=(txn.transaction_date, txn.description, f"{txn.amount:.2f}", txn.source_file),
            )

    def _dashboard_card(self, parent: ctk.CTkFrame, title: str, value: str) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent)
        ctk.CTkLabel(
            card,
            text=title,
            font=ctk.CTkFont(size=13),
            anchor="w",
        ).pack(anchor="w", padx=12, pady=(10, 4))
        ctk.CTkLabel(
            card,
            text=value,
            font=ctk.CTkFont(size=24, weight="bold"),
            anchor="w",
        ).pack(anchor="w", padx=12, pady=(0, 10))
        return card

    def _refresh_dashboard(self) -> None:
        for child in self.dashboard_content.winfo_children():
            child.destroy()

        if not self.filtered_transactions:
            ctk.CTkLabel(
                self.dashboard_content,
                text="No transactions available. Choose a folder and refresh.",
                font=ctk.CTkFont(size=14),
            ).pack(anchor="w", padx=10, pady=10)
            return

        txns = self.filtered_transactions
        dated_txns = [(t, parse_txn_date(t.transaction_date)) for t in txns]
        dated_txns = [(t, d) for t, d in dated_txns if d is not None]

        total_count = len(txns)
        total_debit = sum(t.amount for t in txns if t.amount > 0)
        total_credit = -sum(t.amount for t in txns if t.amount < 0)
        net_flow = sum(t.amount for t in txns)

        largest_debit = max((t.amount for t in txns if t.amount > 0), default=0.0)
        largest_credit = max((-t.amount for t in txns if t.amount < 0), default=0.0)

        earliest = min((d for _, d in dated_txns), default=None)
        latest = max((d for _, d in dated_txns), default=None)
        period_days = ((latest - earliest).days + 1) if earliest and latest else 0
        avg_daily_spend = (total_debit / period_days) if period_days else 0.0
        savings_ratio = ((total_credit - total_debit) / total_credit * 100.0) if total_credit else 0.0

        card_grid = ctk.CTkFrame(self.dashboard_content, fg_color="transparent")
        card_grid.pack(fill="x", padx=10, pady=(10, 6))
        for i in range(2):
            card_grid.grid_columnconfigure(i, weight=1)

        cards = [
            ("Total Transactions", str(total_count)),
            ("Total Spending", f"RM {total_debit:,.2f}"),
            ("Total Credits", f"RM {total_credit:,.2f}"),
            ("Net Flow", f"RM {net_flow:,.2f}"),
            ("Avg Daily Spend", f"RM {avg_daily_spend:,.2f}"),
            ("Savings Ratio", f"{savings_ratio:+.1f}%"),
        ]

        for idx, (title, value) in enumerate(cards):
            card = self._dashboard_card(card_grid, title, value)
            card.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=6, pady=6)

        monthly_cashflow: dict[str, dict[str, float]] = {}
        for t, d in dated_txns:
            key = d.strftime("%Y-%m")
            if key not in monthly_cashflow:
                monthly_cashflow[key] = {"spend": 0.0, "credit": 0.0, "net": 0.0, "count": 0.0}
            monthly_cashflow[key]["count"] += 1
            monthly_cashflow[key]["net"] += t.amount
            if t.amount > 0:
                monthly_cashflow[key]["spend"] += t.amount
            elif t.amount < 0:
                monthly_cashflow[key]["credit"] += -t.amount

        merchant_spend: dict[str, float] = {}
        merchant_count: dict[str, int] = {}
        merchant_amounts: dict[str, list[float]] = {}
        for t in txns:
            merchant = normalize_merchant(t.description)
            merchant_count[merchant] = merchant_count.get(merchant, 0) + 1
            if t.amount > 0:
                merchant_spend[merchant] = merchant_spend.get(merchant, 0.0) + t.amount
                merchant_amounts.setdefault(merchant, []).append(t.amount)

        top_spend_merchants = sorted(merchant_spend.items(), key=lambda item: item[1], reverse=True)[:5]
        recurring_candidates = sorted(
            (
                (m, sum(vals) / len(vals), len(vals))
                for m, vals in merchant_amounts.items()
                if len(vals) >= 3
            ),
            key=lambda item: (item[2], item[1]),
            reverse=True,
        )[:5]

        concentration = 0.0
        if total_debit > 0:
            top3_spend = sum(amount for _, amount in top_spend_merchants[:3])
            concentration = top3_spend / total_debit * 100.0

        monthly_stats = sorted(monthly_cashflow.items(), key=lambda item: item[0])

        insights = ctk.CTkFrame(self.dashboard_content)
        insights.pack(fill="both", expand=True, padx=10, pady=10)
        insights.grid_columnconfigure(0, weight=1)
        insights.grid_columnconfigure(1, weight=1)
        insights.grid_rowconfigure(1, weight=1)

        left = ctk.CTkFrame(insights)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=0)
        ctk.CTkLabel(left, text="Spending Concentration", font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 6)
        )
        ctk.CTkLabel(
            left,
            text=f"Top 3 merchants contribute {concentration:.1f}% of all spending",
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(2, 8))
        for merchant, amount in top_spend_merchants:
            ctk.CTkLabel(
                left,
                text=f"- {merchant}: RM {amount:,.2f}",
                anchor="w",
            ).pack(anchor="w", padx=10, pady=2)

        right = ctk.CTkFrame(insights)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=0)
        ctk.CTkLabel(right, text="Largest Movements", font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 6)
        )
        ctk.CTkLabel(right, text=f"- Largest debit: RM {largest_debit:,.2f}", anchor="w").pack(
            anchor="w", padx=10, pady=2
        )
        ctk.CTkLabel(right, text=f"- Largest credit: RM {largest_credit:,.2f}", anchor="w").pack(
            anchor="w", padx=10, pady=2
        )
        if earliest and latest:
            ctk.CTkLabel(
                right,
                text=f"- Coverage: {earliest.strftime('%d %b %Y')} to {latest.strftime('%d %b %Y')}",
                anchor="w",
            ).pack(anchor="w", padx=10, pady=2)

        monthly_frame = ctk.CTkFrame(insights)
        monthly_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(12, 0))
        ctk.CTkLabel(monthly_frame, text="Monthly Cashflow", font=ctk.CTkFont(size=15, weight="bold")).pack(
            anchor="w", padx=10, pady=(10, 6)
        )
        for month_key, values in monthly_stats[-8:]:
            label = (
                f"- {month_key}: In RM {values['credit']:,.2f} | "
                f"Out RM {values['spend']:,.2f} | Net RM {values['net']:,.2f}"
            )
            ctk.CTkLabel(monthly_frame, text=label, anchor="w").pack(anchor="w", padx=10, pady=2)

        recurring_frame = ctk.CTkFrame(insights)
        recurring_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(12, 0))
        ctk.CTkLabel(
            recurring_frame,
            text="Recurring Charge Candidates",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(10, 6))
        if recurring_candidates:
            for merchant, avg_amount, count in recurring_candidates:
                ctk.CTkLabel(
                    recurring_frame,
                    text=f"- {merchant}: ~RM {avg_amount:,.2f} ({count}x)",
                    anchor="w",
                ).pack(anchor="w", padx=10, pady=2)
        else:
            ctk.CTkLabel(
                recurring_frame,
                text="- No clear recurring charges detected yet.",
                anchor="w",
            ).pack(anchor="w", padx=10, pady=2)

    def _refresh_visual_dashboard(self) -> None:
        for child in self.visual_content.winfo_children():
            child.destroy()

        for canvas in self.chart_canvases:
            canvas.get_tk_widget().destroy()
        self.chart_canvases = []

        if not self.filtered_transactions:
            ctk.CTkLabel(
                self.visual_content,
                text="No transactions available for visual analytics.",
                font=ctk.CTkFont(size=14),
            ).pack(anchor="w", padx=10, pady=10)
            return

        txns = self.filtered_transactions
        spend_txns = [t for t in txns if t.amount > 0]
        dated_txns = [(t, parse_txn_date(t.transaction_date)) for t in txns]
        dated_txns = [(t, d) for t, d in dated_txns if d is not None]

        category_spend: dict[str, float] = {}
        for txn in spend_txns:
            category = categorize_transaction(txn.description)
            category_spend[category] = category_spend.get(category, 0.0) + txn.amount

        merchant_spend: dict[str, float] = {}
        for txn in spend_txns:
            merchant = normalize_merchant(txn.description)
            merchant_spend[merchant] = merchant_spend.get(merchant, 0.0) + txn.amount
        top_merchants = sorted(merchant_spend.items(), key=lambda item: item[1], reverse=True)[:8]

        monthly: dict[str, dict[str, float]] = {}
        for txn, dt in dated_txns:
            month_key = dt.strftime("%Y-%m")
            if month_key not in monthly:
                monthly[month_key] = {"spend": 0.0, "credit": 0.0, "net": 0.0}
            monthly[month_key]["net"] += txn.amount
            if txn.amount > 0:
                monthly[month_key]["spend"] += txn.amount
            elif txn.amount < 0:
                monthly[month_key]["credit"] += -txn.amount

        month_keys = sorted(monthly.keys())
        month_spend = [monthly[m]["spend"] for m in month_keys]
        month_credit = [monthly[m]["credit"] for m in month_keys]
        month_net = [monthly[m]["net"] for m in month_keys]

        charts_grid = ctk.CTkFrame(self.visual_content, fg_color="transparent")
        charts_grid.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        charts_grid.grid_columnconfigure(0, weight=1)
        charts_grid.grid_columnconfigure(1, weight=1)

        def make_card(
            row: int,
            col: int,
            title: str,
            subtitle: str,
            row_span: int = 1,
            col_span: int = 1,
        ) -> ctk.CTkFrame:
            card = ctk.CTkFrame(charts_grid, corner_radius=14, fg_color=("#f8fafc", "#1f2937"))
            card.grid(row=row, column=col, rowspan=row_span, columnspan=col_span, sticky="nsew", padx=6, pady=6)
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(
                card,
                text=title,
                font=ctk.CTkFont(size=16, weight="bold"),
            ).grid(row=0, column=0, sticky="w", padx=14, pady=(12, 2))
            ctk.CTkLabel(
                card,
                text=subtitle,
                font=ctk.CTkFont(size=12),
                text_color=("#475569", "#cbd5e1"),
            ).grid(row=1, column=0, sticky="w", padx=14, pady=(0, 6))
            plot_host = ctk.CTkFrame(card, fg_color="transparent")
            plot_host.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 10))
            card.grid_rowconfigure(2, weight=1)
            return plot_host

        def add_canvas(host: ctk.CTkFrame, fig: Figure) -> None:
            fig.tight_layout(pad=1.4)
            canvas = FigureCanvasTkAgg(fig, master=host)
            canvas.draw()
            canvas.get_tk_widget().pack(fill="both", expand=True)
            self.chart_canvases.append(canvas)

        # Card 1: Category donut chart.
        donut_host = make_card(0, 0, "Category Spend Mix", "Distribution of outgoing spending by category")
        fig1 = Figure(figsize=(4.2, 3.1), dpi=100)
        ax1 = fig1.add_subplot(111)
        fig1.patch.set_facecolor("#f8fafc")
        if category_spend:
            top_categories = sorted(category_spend.items(), key=lambda item: item[1], reverse=True)
            if len(top_categories) > 6:
                top_categories = [*top_categories[:6], ("Other", sum(v for _, v in top_categories[6:]))]
            labels = [c for c, _ in top_categories]
            values = [v for _, v in top_categories]
            palette = sns.color_palette("Set2", n_colors=len(labels))
            ax1.pie(
                values,
                labels=labels,
                autopct="%1.1f%%",
                startangle=130,
                wedgeprops={"width": 0.42, "edgecolor": "white"},
                colors=palette,
                textprops={"fontsize": 8},
            )
            ax1.set_aspect("equal")
        else:
            ax1.text(0.5, 0.5, "No spending data", ha="center", va="center")
            ax1.set_axis_off()
        add_canvas(donut_host, fig1)

        # Card 2: Monthly inflow vs outflow grouped bars.
        in_out_host = make_card(0, 1, "Monthly Inflow vs Outflow", "Compares credits versus spending per month")
        fig2 = Figure(figsize=(4.2, 3.1), dpi=100)
        ax2 = fig2.add_subplot(111)
        fig2.patch.set_facecolor("#f8fafc")
        if month_keys:
            x = list(range(len(month_keys)))
            ax2.bar([i - 0.2 for i in x], month_spend, width=0.4, label="Outflow", color="#fb7185")
            ax2.bar([i + 0.2 for i in x], month_credit, width=0.4, label="Inflow", color="#34d399")
            ax2.set_xticks(x)
            ax2.set_xticklabels(month_keys, rotation=30, ha="right", fontsize=8)
            ax2.legend(fontsize=8, frameon=False)
            ax2.grid(axis="y", linestyle="--", alpha=0.25)
        else:
            ax2.text(0.5, 0.5, "No monthly data", ha="center", va="center")
            ax2.set_axis_off()
        add_canvas(in_out_host, fig2)

        # Card 3: Top merchants horizontal bars.
        merchants_host = make_card(1, 0, "Top Merchants", "Highest merchants by total spending")
        fig3 = Figure(figsize=(4.2, 3.2), dpi=100)
        ax3 = fig3.add_subplot(111)
        fig3.patch.set_facecolor("#f8fafc")
        if top_merchants:
            merchant_labels = [m[:24] for m, _ in top_merchants][::-1]
            merchant_values = [v for _, v in top_merchants][::-1]
            bars = ax3.barh(merchant_labels, merchant_values, color=sns.color_palette("Blues", len(merchant_values)))
            ax3.grid(axis="x", linestyle="--", alpha=0.25)
            ax3.tick_params(axis="y", labelsize=8)
            for bar in bars[-3:]:
                ax3.text(
                    bar.get_width(),
                    bar.get_y() + bar.get_height() / 2,
                    f" RM {bar.get_width():,.0f}",
                    va="center",
                    fontsize=8,
                )
        else:
            ax3.text(0.5, 0.5, "No merchant spend", ha="center", va="center")
            ax3.set_axis_off()
        add_canvas(merchants_host, fig3)

        # Card 4: Net trend area/line chart.
        trend_host = make_card(1, 1, "Net Cashflow Trend", "Month-by-month net direction and momentum")
        fig4 = Figure(figsize=(4.2, 3.2), dpi=100)
        ax4 = fig4.add_subplot(111)
        fig4.patch.set_facecolor("#f8fafc")
        if month_keys:
            x = list(range(len(month_keys)))
            ax4.plot(x, month_net, marker="o", linewidth=2.5, color="#8b5cf6")
            ax4.fill_between(x, month_net, [0] * len(x), color="#c4b5fd", alpha=0.25)
            ax4.axhline(0, color="#64748b", linewidth=1)
            ax4.set_xticks(x)
            ax4.set_xticklabels(month_keys, rotation=30, ha="right", fontsize=8)
            ax4.grid(axis="y", linestyle="--", alpha=0.25)
        else:
            ax4.text(0.5, 0.5, "No trend data", ha="center", va="center")
            ax4.set_axis_off()
        add_canvas(trend_host, fig4)

        insights_frame = ctk.CTkFrame(self.visual_content)
        insights_frame.pack(fill="x", padx=10, pady=(0, 10))

        total_spend = sum(t.amount for t in spend_txns)
        top_category = (
            max(category_spend.items(), key=lambda item: item[1]) if category_spend else ("N/A", 0.0)
        )
        top_category_share = (top_category[1] / total_spend * 100.0) if total_spend else 0.0
        top_merchant = top_merchants[0] if top_merchants else ("N/A", 0.0)
        volatile_month = (
            max(monthly.items(), key=lambda item: abs(item[1]["net"]))[0] if monthly else "N/A"
        )

        ctk.CTkLabel(
            insights_frame,
            text="Visual Insights",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(anchor="w", padx=10, pady=(10, 6))
        ctk.CTkLabel(
            insights_frame,
            text=(
                f"- Category leader: {top_category[0]} at RM {top_category[1]:,.2f} "
                f"({top_category_share:.1f}% of spending)"
            ),
            anchor="w",
        ).pack(anchor="w", padx=10, pady=2)
        ctk.CTkLabel(
            insights_frame,
            text=f"- Highest spend merchant: {top_merchant[0]} at RM {top_merchant[1]:,.2f}",
            anchor="w",
        ).pack(anchor="w", padx=10, pady=2)
        ctk.CTkLabel(
            insights_frame,
            text=f"- Highest net volatility month: {volatile_month}",
            anchor="w",
        ).pack(anchor="w", padx=10, pady=(2, 10))

    def show_dashboard(self) -> None:
        self.dashboard_frame.grid()
        self.transactions_frame.grid_remove()
        self.visual_frame.grid_remove()
        self.category_config_frame.grid_remove()
        self.set_status(
            f"Dashboard view - {self.selected_year} ({len(self.filtered_transactions)} transactions)"
        )

    def show_transactions(self) -> None:
        self.transactions_frame.grid()
        self.dashboard_frame.grid_remove()
        self.visual_frame.grid_remove()
        self.category_config_frame.grid_remove()
        self.set_status(
            f"Transactions view - {self.selected_year} ({len(self.filtered_transactions)} rows)"
        )

    def show_visual_dashboard(self) -> None:
        self.visual_frame.grid()
        self.dashboard_frame.grid_remove()
        self.transactions_frame.grid_remove()
        self.category_config_frame.grid_remove()
        self.set_status(
            f"Visual dashboard - {self.selected_year} ({len(self.filtered_transactions)} transactions)"
        )

    def show_category_config(self) -> None:
        self.category_config_frame.grid()
        self.dashboard_frame.grid_remove()
        self.transactions_frame.grid_remove()
        self.visual_frame.grid_remove()
        self.set_status("Category config editor")

    def export_csv(self) -> None:
        if self.is_loading:
            return
        save_path = filedialog.asksaveasfilename(
            initialfile=self.output_path.name,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
        )
        if not save_path:
            return
        self._start_loading("Exporting CSV...")

        def worker() -> None:
            try:
                save_csv(self.transactions, Path(save_path))
                self.after(0, lambda: self._on_export_success(save_path))
            except Exception as exc:
                self.after(0, lambda: self._on_background_error(f"Export failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _on_export_success(self, save_path: str) -> None:
        self._stop_loading()
        self.set_status(f"Saved CSV: {save_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compile transaction date, description, and amount from all PDFs in a folder."
    )
    parser.add_argument(
        "--folder",
        default=".",
        help="Folder containing PDF statements (default: current folder).",
    )
    parser.add_argument(
        "--output",
        default="compiled_transactions.csv",
        help="Output CSV path (default: compiled_transactions.csv).",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="Run in CLI mode without launching CustomTkinter UI.",
    )
    parser.add_argument(
        "--category-config",
        default="category_keywords.json",
        help="Path to category keyword JSON config (default: category_keywords.json).",
    )
    args = parser.parse_args()

    folder = Path(args.folder)
    config_path = Path(args.category_config)

    global CATEGORY_KEYWORDS
    CATEGORY_KEYWORDS, category_warning = load_category_keywords(config_path)
    if category_warning:
        print(f"Info: {category_warning}")

    rows, warnings = extract_transactions_from_folder(folder)
    for warning in warnings:
        print(f"Warning: {warning}")

    if args.no_gui:
        if not rows:
            print("No transactions extracted from the provided PDFs.")
            return 2
        print(render_table(rows))
        save_csv(rows, Path(args.output))
        print(f"\nSaved {len(rows)} rows to: {Path(args.output).resolve()}")
        return 0

    app = App(
        initial_folder=folder,
        output_path=Path(args.output),
        category_config_path=config_path,
    )
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
