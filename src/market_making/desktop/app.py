"""Simple native Windows interface for the existing GGAL read-only checker."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from queue import Empty
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from market_making.contracts import CONTRACTS, DAY_BASIS
from market_making.desktop.controller import MonitorController, WatchOptions, default_env_file
from market_making.desktop.scenario_tab import ScenarioTab
from market_making.desktop.comparison_tab import ComparisonTab
from market_making.ppi import monitor
from market_making.ppi.models import ManualSpotQuote

BG = "#f3f5f9"
INK = "#17243b"
MUTED = "#64748b"
BLUE = "#245bd8"
GREEN = "#127454"
AMBER = "#97610d"
BA = timezone(timedelta(hours=-3))
LABELS = {monitor.EDGE: "Theoretical edge", monitor.NO_EDGE: "No theoretical edge",
          monitor.UNAVAILABLE: "Unavailable", "PRICE CHECK": "Price comparison"}
REASONS = {
    "credentials_unavailable": "Choose a .env with PPI_API_KEY, PPI_API_SECRET, PRIMARY_USER and PRIMARY_PASSWORD.",
    "authentication_failure_stop": "Login failed. Check credentials and market-data access, then start again.",
    "transient_market_data_failure": "Provider temporarily unavailable. Retrying at the next refresh.",
    "safe_market_data_failure_stop": "Market-data request failed. Check access and connection, then start again.",
    "expired_watch_maturity": "A monitored contract has expired. Update the project's target contracts before restarting.",
    "local_watch_configuration_or_processing_failure": "Unable to load settings or process data. Check the .env and assessment configuration.",
}


def fmt(value, suffix="", signed=False):
    return "--" if value is None else format(value, "+,.2f" if signed else ",.2f") + suffix


def best(book, side):
    rows = (book or {}).get(side, [])
    return rows[0]["price"] if rows else None


class DesktopApp:
    def __init__(self, root):
        self.root = root
        self.controller = MonitorController()
        self.reports = []
        self.received_at = None
        self.received_monotonic = None
        self.session_demo = False
        self.stopping = False
        self.closing = False
        root.title("GGAL Desk")
        root.geometry("1100x790")
        root.minsize(1000, 740)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.report_callback_exception = self.callback_error
        self._style()
        self._layout()
        self.poll_id = root.after(100, self.poll)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=INK, font=("Segoe UI", 10))
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 25, "bold"))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 6), borderwidth=0, background="#e4eaf4")
        style.configure("Accent.TButton", background=BLUE, foreground="white")
        style.map("Accent.TButton", background=[("active", "#1746b0"), ("disabled", "#a8b8d8")])
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(20, 8), font=("Segoe UI", 10), background="#e4eaf4")
        style.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", BLUE)])
        style.configure("Treeview", rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))
        style.configure("Card.TFrame", background="white")
        style.configure("Card.TLabel", background="white")
        style.configure("CardTitle.TLabel", background="white", font=("Segoe UI", 13, "bold"))
        style.configure("Value.TLabel", background="white", font=("Segoe UI", 24, "bold"))
        style.configure("CardMuted.TLabel", background="white", foreground=MUTED)

    def _layout(self):
        page = ttk.Frame(self.root, padding=20)
        page.pack(fill="both", expand=True)
        header = ttk.Frame(page)
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="GGAL Desk", style="Title.TLabel").pack(side="left")
        ttk.Label(header, text="Futures prices / carry / position scenarios", style="Muted.TLabel").pack(side="right", pady=12)
        self.tabs = ttk.Notebook(page)
        self.tabs.pack(fill="both", expand=True)
        checker = ttk.Frame(self.tabs, padding=(16, 14))
        self.tabs.add(checker, text="GGAL arbitrage")

        actions = ttk.Frame(checker)
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="Start live check", style="Accent.TButton", command=self.start_live)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="Stop", state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=(0, 8))
        self.demo_button = ttk.Button(actions, text="Try demo", command=self.start_demo)
        self.demo_button.pack(side="left")
        self.settings_button = ttk.Button(actions, text="Settings", command=self.settings)
        self.settings_button.pack(side="right")
        self.sources = tk.StringVar(value="PPI production spot + REMARKETS simulated futures / read-only price comparison")
        ttk.Label(checker, textvariable=self.sources, style="Muted.TLabel").pack(anchor="w", pady=(10, 8))
        inputs = ttk.Frame(checker)
        inputs.pack(fill="x", pady=(0, 12))
        ttk.Label(inputs, text="Caucion TNA (%)").pack(side="left")
        self.rate = tk.StringVar()
        self.rate_entry = ttk.Entry(inputs, textvariable=self.rate, width=10)
        self.rate_entry.pack(side="left", padx=(8, 18))
        ttk.Label(inputs, text="Refresh (seconds)").pack(side="left")
        self.interval = tk.StringVar(value="5")
        self.interval_entry = ttk.Entry(inputs, textvariable=self.interval, width=8)
        self.interval_entry.pack(side="left", padx=8)
        ttk.Label(inputs, text="Blank rate = unknown. Demo refreshes every 2s.", style="Muted.TLabel").pack(side="right")
        self.env = tk.StringVar(value=str(default_env_file()))
        self.config = tk.StringVar()
        self.manual = tk.BooleanVar(value=False)
        spot_inputs = ttk.Frame(checker)
        spot_inputs.pack(fill="x", pady=(0, 4))
        self.manual_spot_enabled = tk.BooleanVar(value=False)
        self.manual_spot_fields = {side: tk.StringVar() for side in ("bid", "ask")}
        self.manual_spot_toggle = ttk.Checkbutton(spot_inputs, text="Manual GGAL spot (ARS/share)",
                                                variable=self.manual_spot_enabled,
                                                command=self.toggle_manual_spot)
        self.manual_spot_toggle.pack(side="left")
        self.manual_spot_entries = {}
        for side, variable in self.manual_spot_fields.items():
            ttk.Label(spot_inputs, text=side.title()).pack(side="left", padx=(12, 0))
            entry = ttk.Entry(spot_inputs, textvariable=variable, width=12, state="disabled")
            entry.pack(side="left", padx=8)
            entry.bind("<Return>", lambda _: self.apply_manual_spot())
            self.manual_spot_entries[side] = entry
        self.manual_spot_apply = ttk.Button(spot_inputs, text="Apply bid/ask", command=self.apply_manual_spot,
                                           state="disabled")
        self.manual_spot_apply.pack(side="left")
        ttk.Label(checker, text="Apply bid/ask or press Enter to use both prices on the next refresh.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 12))

        self.cards = {}
        cards = ttk.Frame(checker)
        cards.pack(fill="x")
        card_specs = [
            ("spot", "GGAL spot", "PPI / ARS per share"),
            *((expiry, contract.label, contract.symbol + " / simulated futures")
              for expiry, contract in CONTRACTS.items()),
        ]
        for column, (key, title, subtitle) in enumerate(card_specs):
            cards.columnconfigure(column, weight=1, uniform="cards")
            card = ttk.Frame(cards, padding=14, style="Card.TFrame")
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == len(card_specs) - 1 else 6))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(anchor="w")
            self.cards[key] = fields = {}
            fields["subtitle"] = tk.StringVar(value=subtitle)
            ttk.Label(card, textvariable=fields["subtitle"], style="CardMuted.TLabel").pack(anchor="w", pady=(3, 12))
            for name, label in (("bid", "BID"), ("ask", "ASK")):
                ttk.Label(card, text=label, style="CardMuted.TLabel").pack(anchor="w")
                fields[name] = tk.StringVar(value="--")
                ttk.Label(card, textvariable=fields[name], style="Value.TLabel").pack(anchor="w", pady=(0, 6))
            fields["yield"] = tk.StringVar(value="Spot ask is used for cash carry" if key == "spot" else "Implied TNA   --")
            ttk.Label(card, textvariable=fields["yield"], style="Card.TLabel").pack(anchor="w", pady=(3, 5))
            fields["spread"] = tk.StringVar(value="")
            ttk.Label(card, textvariable=fields["spread"], style="CardMuted.TLabel").pack(anchor="w")
            fields["status"] = tk.StringVar(value="Waiting for prices")
            fields["status_label"] = ttk.Label(card, textvariable=fields["status"], style="Card.TLabel")
            fields["status_label"].pack(anchor="w", pady=(10, 0))

        self.assessment = tk.StringVar(value="Default: gross yield comparison. Full assessment is available in Settings.")
        ttk.Label(checker, textvariable=self.assessment, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(12, 4))
        self.notice = tk.StringVar(value="Press Start live check to connect, or Try demo to preview without credentials.")
        ttk.Label(checker, textvariable=self.notice, wraplength=970).pack(anchor="w", pady=5)
        ttk.Separator(checker).pack(fill="x", pady=(10, 8))
        footer = ttk.Frame(checker)
        footer.pack(fill="x")
        self.state = tk.StringVar(value="Ready")
        ttk.Label(footer, textvariable=self.state, foreground=BLUE).pack(side="left")
        self.details_button = ttk.Button(footer, text="Book details", command=self.details, state="disabled")
        self.details_button.pack(side="right")
        self.export_button = ttk.Button(footer, text="Export snapshot", command=self.export, state="disabled")
        self.export_button.pack(side="right", padx=8)
        self.updated = tk.StringVar(value="No snapshot received")
        ttk.Label(checker, textvariable=self.updated, style="Muted.TLabel").pack(anchor="w", pady=(8, 5))
        ttk.Label(checker, text="Gross yield excludes fees. Caucion is your manual input. Source freshness is unverified; no orders are sent.",
                  style="Muted.TLabel", wraplength=970).pack(anchor="w")
        self.scenarios = ScenarioTab(self.tabs, env_variable=self.env, get_reports=lambda: self.reports,
                                     caucion_variable=self.rate)
        self.tabs.add(self.scenarios, text="Position scenarios")
        self.comparison = ComparisonTab(self.tabs, env_variable=self.env, open_settings=self.settings)
        self.tabs.add(self.comparison, text="Real vs reMarkets")

    def options(self, demo=False):
        try:
            rate = float(self.rate.get().strip().replace(",", ".")) if self.rate.get().strip() else None
            interval = 2 if demo else float(self.interval.get().strip())
        except ValueError:
            raise ValueError("Enter numbers for the rate and refresh interval.") from None
        options = WatchOptions(demo=demo, interval=interval, caucion_tna=rate,
                               env_file=Path(self.env.get()),
                               watch_config=Path(self.config.get()) if self.config.get() else None,
                               manual_check=self.manual.get(), manual_spot=self.parse_manual_spot())
        options.validate()
        return options

    def parse_manual_spot(self):
        if not self.manual_spot_enabled.get():
            return None
        try:
            return ManualSpotQuote(**{side: float(variable.get().strip().replace(",", "."))
                                      for side, variable in self.manual_spot_fields.items()})
        except (ValueError, OverflowError):
            raise ValueError("Enter positive, finite GGAL spot bid and ask prices in ARS per share, "
                             "with bid no greater than ask.") from None

    def toggle_manual_spot(self):
        enabled = self.manual_spot_enabled.get()
        state = "normal" if enabled else "disabled"
        for entry in self.manual_spot_entries.values():
            entry.configure(state=state)
        self.manual_spot_apply.configure(state=state)
        if enabled:
            if self.reports:
                spot = (self.reports[0].get("books") or {}).get("spot")
                for side, variable in self.manual_spot_fields.items():
                    price = best(spot, "bids" if side == "bid" else "offers")
                    if not variable.get().strip() and price is not None:
                        variable.set(format(price, ".12g"))
            self.manual_spot_entries["bid"].focus_set()
            self.notice.set("Enter GGAL spot bid and ask in ARS/share, then click Apply bid/ask or press Enter.")
        else:
            self.controller.set_manual_spot(None)
            source = "Synthetic demo spot" if self.session_demo and self.controller.running else "PPI spot"
            self.notice.set(source + " selected" +
                            (" / used on the next refresh" if self.controller.running else " / ready for the next check"))

    def apply_manual_spot(self):
        try:
            price = self.parse_manual_spot()
        except ValueError as exc:
            messagebox.showinfo("Check spot price", str(exc), parent=self.root)
            return
        if price is None:
            return
        self.controller.set_manual_spot(price)
        self.notice.set("Manual GGAL spot saved: bid " + fmt(price.bid) + " / ask " + fmt(price.ask, " ARS/share") +
                        (" / used on the next refresh" if self.controller.running else " / ready for the next check"))

    def start_live(self):
        self.start(False)

    def start_demo(self):
        self.start(True)

    def start(self, demo):
        if self.controller.running or self.closing:
            return
        try:
            options = self.options(demo)
        except ValueError as exc:
            messagebox.showinfo("Check settings", str(exc), parent=self.root)
            return
        # Drain the previous session's events before a new worker can publish.
        while not self.controller.events.empty():
            self.controller.events.get_nowait()
        self.session_demo = demo
        self.stopping = False
        self.clear()
        self.set_running(True)
        self.notice.set("Synthetic demo / no broker connection" if demo else
                        "Connecting to REMARKETS / using manual GGAL spot..." if options.manual_spot is not None else
                        "Connecting to PPI and REMARKETS...")
        self.state.set("Starting demo..." if demo else "Connecting...")
        self.controller.start(options)

    def set_running(self, running):
        for widget in (self.start_button, self.demo_button, self.settings_button, self.rate_entry, self.interval_entry):
            widget.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def clear(self):
        self.reports = []
        self.received_at = self.received_monotonic = None
        self.updated.set("No snapshot received")
        self.details_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        for fields in self.cards.values():
            for key in ("bid", "ask"):
                fields[key].set("--")
            fields["status"].set("Waiting for prices")
            fields["spread"].set("")
            fields["yield"].set("--")
            fields["status_label"].configure(foreground=INK)
        self.assessment.set("Demo uses synthetic rates and costs." if self.session_demo else "Waiting for current comparison...")

    def stop(self):
        self.stopping = True
        self.controller.stop()
        self.stop_button.configure(state="disabled")
        self.state.set("Stopping / cancelling any pending read...")

    def render(self, reports):
        self.reports = reports
        self.received_at = datetime.now(BA)
        self.received_monotonic = time.monotonic()
        self.details_button.configure(state="normal")
        self.export_button.configure(state="normal")
        by_expiry = {report["expiry"]: report for report in reports}
        first = reports[0]
        self.sources.set("Synthetic demo / no broker connection" if first["mode"] == "DEMO" else
                         "PPI production spot + REMARKETS simulated futures / read-only price comparison")
        spot = (first.get("books") or {}).get("spot")
        self.cards["spot"]["bid"].set(fmt(best(spot, "bids")))
        self.cards["spot"]["ask"].set(fmt(best(spot, "offers")))
        self.cards["spot"]["subtitle"].set("Synthetic / ARS per share" if first["mode"] == "DEMO" else "PPI production / ARS per share")
        self.cards["spot"]["status"].set("Unavailable" if not spot else "Demo snapshot" if first["mode"] == "DEMO" else "Received / freshness unverified")
        self.cards["spot"]["yield"].set("Spot ask is used for cash carry")
        if spot and spot["provider"] == "manual":
            self.sources.set("Manual GGAL spot + " + ("synthetic demo futures" if first["mode"] == "DEMO" else
                                                       "REMARKETS simulated futures / read-only price comparison"))
            self.cards["spot"]["subtitle"].set("Manual reference / ARS per share")
            self.cards["spot"]["status"].set("User input / depth unverified")
            self.cards["spot"]["yield"].set("Manual spot ask is used for cash carry")
        summaries = []
        for expiry in monitor.TARGETS:
            report = by_expiry[expiry]
            fields = self.cards[expiry]
            book = (report.get("books") or {}).get("future")
            fields["bid"].set(fmt(best(book, "bids")))
            fields["ask"].set(fmt(best(book, "offers")))
            fields["subtitle"].set(monitor.SYMBOLS[expiry] + (" / synthetic" if report["mode"] == "DEMO" else " / simulated futures"))
            summary = report["quote_summary"]
            value, rate = summary["implied_yield_tna"], summary["caucion_tna"]
            fields["yield"].set("Implied TNA   " + fmt(value, "%"))
            fields["spread"].set("vs caucion   " + (fmt(value - rate, " pp", True) if value is not None and rate is not None else "--"))
            fields["status"].set(LABELS.get(report["status"], report["status"]))
            fields["status_label"].configure(foreground=GREEN if report["status"] == monitor.EDGE else AMBER if report["status"] == monitor.UNAVAILABLE else BLUE)
            if not report.get("quote_only"):
                direction = report.get("selected_direction")
                edge = report["directions"].get(direction, {}).get("net_edge")
                summaries.append(expiry + ": " + (direction or "unavailable") + " / net " + fmt(edge, " ARS/share", True))
        if first.get("quote_only"):
            self.assessment.set(f"Gross cash-carry yield / shared assumed expiry dates / {DAY_BASIS}-day basis / fees excluded")
        else:
            self.assessment.set(("Synthetic assessment / " if first["mode"] == "DEMO" else "Full assessment / ") + "   |   ".join(summaries))
        blockers = list(dict.fromkeys(reason for report in reports for reason in report["blockers"]))
        self.notice.set(REASONS.get(blockers[0], "Inputs unavailable. Open Book details for diagnostics.") if blockers else
                        "Synthetic demo / fixed fixture dates, prices, rates and costs" if first["mode"] == "DEMO" else
                        "Prices received / source freshness unverified" + (" / manual check" if first["manual_check"] else ""))

    def poll(self):
        self.scenarios.poll()
        self.comparison.poll()
        try:
            while True:
                kind, payload = self.controller.events.get_nowait()
                if kind == "reports" and not self.stopping:
                    self.render(payload)
                elif kind == "status" and not self.stopping:
                    self.state.set(payload)
                elif kind == "finished":
                    # Thread completion and queue delivery can differ by a scheduling tick.
                    self.root.after(20, self.finish)
        except Empty:
            pass
        if self.received_at:
            age = max(0, int(time.monotonic() - self.received_monotonic))
            prefix = "DEMO / " if self.session_demo else ""
            self.updated.set(f"{prefix}Last received {self.received_at:%H:%M:%S} Buenos Aires / {age}s ago / "
                             + ("stopped snapshot" if self.stopping or not self.controller.running else "receipt time, not exchange freshness"))
        if not self.closing:
            self.poll_id = self.root.after(100, self.poll)

    def finish(self):
        if self.controller.running:
            self.root.after(20, self.finish)
            return
        self.set_running(False)
        self.state.set("Stopped" if self.stopping else "Monitor stopped / review the message above")

    def settings(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("GGAL settings")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        body = ttk.Frame(dialog, padding=24)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Connection", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 12))
        ttk.Label(body, text="Credentials file (.env)").grid(row=1, column=0, sticky="w")
        ttk.Entry(body, textvariable=self.env, width=70).grid(row=2, column=0, pady=(4, 15))
        ttk.Button(body, text="Browse", command=lambda: self.choose(self.env, "Credentials file", [("Environment file", ".env*"), ("All files", "*.*")])).grid(row=2, column=1, padx=8)
        ttk.Label(body, text="Use your existing PPI and Primary credentials. Values stay out of the interface.", style="Muted.TLabel").grid(row=3, column=0, columnspan=2, sticky="w")
        ttk.Separator(body).grid(row=4, column=0, columnspan=2, sticky="ew", pady=20)
        ttk.Label(body, text="Full arbitrage assessment (optional)").grid(row=5, column=0, sticky="w")
        ttk.Label(body, text="Leave blank for the simple price/yield checker.", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=5)
        ttk.Entry(body, textvariable=self.config, width=70).grid(row=7, column=0, pady=5)
        ttk.Button(body, text="Browse", command=lambda: self.choose(self.config, "Assessment configuration", [("JSON configuration", "*.json")])).grid(row=7, column=1, padx=8)
        ttk.Button(body, text="Use simple checker", command=lambda: self.config.set("")).grid(row=8, column=0, sticky="w", pady=8)
        ttk.Checkbutton(body, text="Manual price check for full assessment (timestamps advisory)", variable=self.manual).grid(row=9, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(body, text="A full config supplies funding, costs and contract units; clear the main rate field.\nStrict assessment can remain unavailable when source timestamps are unknown.", style="Muted.TLabel").grid(row=10, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(body, text="Done", command=dialog.destroy, style="Accent.TButton").grid(row=11, column=1, pady=8)

    def choose(self, variable, title, types):
        selected = filedialog.askopenfilename(parent=self.root, title=title, filetypes=types)
        if selected:
            variable.set(selected)

    def details(self):
        if not self.reports:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Book details / last received snapshot")
        dialog.geometry("900x650")
        notebook = ttk.Notebook(dialog)
        notebook.pack(fill="both", expand=True, padx=15, pady=15)
        for report in self.reports:
            frame = ttk.Frame(notebook, padding=15)
            notebook.add(frame, text=report["expiry"])
            columns = ("leg", "side", "price", "quantity")
            book_frame = ttk.Frame(frame)
            book_frame.pack(fill="x")
            book_frame.columnconfigure(0, weight=1)
            table = ttk.Treeview(book_frame, columns=columns, show="headings", height=8)
            for column in columns:
                table.heading(column, text=column.title())
                table.column(column, width=140, anchor="e" if column in ("price", "quantity") else "w")
            scroll = ttk.Scrollbar(book_frame, orient="vertical", command=table.yview)
            table.configure(yscrollcommand=scroll.set)
            table.grid(row=0, column=0, sticky="ew")
            scroll.grid(row=0, column=1, sticky="ns")
            for leg, book in (report.get("books") or {}).items():
                for side in ("bids", "offers"):
                    for level in book[side]:
                        table.insert("", "end", values=(leg, side, fmt(level["price"]), fmt(level["quantity"])))
            text = tk.Text(frame, wrap="word", font=("Consolas", 10), relief="flat", padx=10, pady=10)
            text.pack(fill="both", expand=True, pady=12)
            # Only normalized monitor reports, never credentials or raw responses.
            text.insert("1.0", json.dumps(report, indent=2, allow_nan=False))
            text.configure(state="disabled")

    def export(self):
        if not self.reports:
            return
        selected = filedialog.asksaveasfilename(parent=self.root, title="Export last snapshot", defaultextension=".json",
                                               initialfile="ggal_snapshot.json", filetypes=[("JSON snapshot", "*.json")])
        if selected:
            try:
                Path(selected).write_text(json.dumps({"received_at": self.received_at.isoformat(), "reports": self.reports},
                                                     indent=2, allow_nan=False), encoding="utf-8")
            except OSError:
                messagebox.showerror("Export failed", "Choose a writable location for the snapshot.", parent=self.root)

    def callback_error(self, *_):
        self.controller.stop()
        self.scenarios.controller.cancel()
        self.comparison.close()
        self.stopping = True
        self.notice.set("Unable to display the last update. Restart the checker.")

    def close(self):
        self.closing = True
        self.controller.stop()
        self.scenarios.close()
        self.comparison.close()
        self.root.after_cancel(self.poll_id)
        self.root.destroy()


def self_test(output):
    """Exercise actual Tk widgets and all demo states, including in the frozen exe."""
    root = tk.Tk()
    root.withdraw()
    app = DesktopApp(root)
    try:
        assert app.interval.get() == "5"
        app.interval.set("1")
        assert app.options().interval == 1
        app.interval.set("5")
        app.manual_spot_enabled.set(True)
        app.toggle_manual_spot()
        app.manual_spot_fields["bid"].set("99,5")
        app.manual_spot_fields["ask"].set("100,5")
        assert app.options().manual_spot == ManualSpotQuote(99.5, 100.5)
        app.apply_manual_spot()
        assert app.controller.get_manual_spot() == ManualSpotQuote(99.5, 100.5)
        app.render(monitor.demo_cycle(1, manual_spot=app.parse_manual_spot()))
        assert app.cards["spot"]["subtitle"].get() == "Manual reference / ARS per share"
        assert app.cards["spot"]["bid"].get() == "99.50"
        assert app.cards["spot"]["ask"].get() == "100.50"
        app.manual_spot_fields["bid"].set("103")
        app.manual_spot_fields["ask"].set("105")
        app.apply_manual_spot()
        assert app.controller.get_manual_spot() == ManualSpotQuote(103, 105)
        for bid, ask in (("", "105"), ("103", ""), ("106", "105"), ("nan", "105")):
            app.manual_spot_fields["bid"].set(bid)
            app.manual_spot_fields["ask"].set(ask)
            try:
                app.options()
            except ValueError:
                pass
            else:
                raise AssertionError("Invalid manual bid/ask accepted")
        assert app.controller.get_manual_spot() == ManualSpotQuote(103, 105)
        app.manual_spot_enabled.set(False)
        app.toggle_manual_spot()
        assert app.controller.get_manual_spot() is None
        assert all(entry.instate(["disabled"]) for entry in app.manual_spot_entries.values())
        assert app.manual_spot_apply.instate(["disabled"])
        for cycle in range(1, 5):
            app.render(monitor.demo_cycle(cycle))
            root.update()
            assert len(app.reports) == 2
            assert app.cards["2026-10"]["status"].get() == LABELS[monitor.demo_cycle(cycle)[0]["status"]]
        assert app.cards["2026-10"]["bid"].get() == "--"
        assert len(app.tabs.tabs()) == 3
        assert app.tabs.tab(0, "text") == "GGAL arbitrage"
        app.tabs.select(app.scenarios)
        root.update()
        assert app.tabs.tab(app.scenarios, "text") == "Position scenarios"
        for expiry, contract in CONTRACTS.items():
            assert app.scenarios.positions[expiry]["maturity"].get() == contract.maturity.date().isoformat()
        scenario_widget_check(app)
        comparison_widget_check(app)
        app.tabs.select(0)
        app.start_demo()
        deadline = time.monotonic() + 5
        while not app.reports and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert app.reports and app.reports[0]["mode"] == "DEMO"
        assert not app.manual_spot_toggle.instate(["disabled"])
        for variable in app.manual_spot_fields.values():
            variable.set("")
        spot_book = app.reports[0]["books"]["spot"]
        spot_quote = ManualSpotQuote(best(spot_book, "bids"), best(spot_book, "offers"))
        app.manual_spot_toggle.invoke()
        assert app.manual_spot_enabled.get()
        assert app.parse_manual_spot() == spot_quote
        assert all(not entry.instate(["disabled"]) for entry in app.manual_spot_entries.values())
        app.manual_spot_fields["bid"].set("107,5")
        app.manual_spot_fields["ask"].set("108,5")
        app.manual_spot_apply.invoke()
        deadline = time.monotonic() + 5
        while best(app.reports[0]["books"]["spot"], "offers") != 108.5 and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert app.reports[0]["books"]["spot"]["provider"] == "manual"
        assert best(app.reports[0]["books"]["spot"], "bids") == 107.5
        assert best(app.reports[0]["books"]["spot"], "offers") == 108.5
        assert app.cards["spot"]["bid"].get() == "107.50"
        assert app.cards["spot"]["ask"].get() == "108.50"
        assert app.cards["spot"]["subtitle"].get() == "Manual reference / ARS per share"
        app.manual_spot_toggle.invoke()
        assert app.controller.get_manual_spot() is None
        deadline = time.monotonic() + 5
        while app.reports[0]["books"]["spot"]["provider"] == "manual" and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert app.reports[0]["books"]["spot"]["provider"] == "synthetic"
        app.stop()
        deadline = time.monotonic() + 5
        while app.controller.running and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert not app.controller.running
        app.finish()
        assert app.state.get() == "Stopped"
        Path(output).write_text(json.dumps({"ok": True, "tk": tk.TkVersion, "demo_scenarios": 4,
                                           "start_stop": True, "live_minimum_seconds": 1,
                                           "live_default_seconds": 5, "futures_helper_tabs": 3,
                                           "real_vs_remarkets": True,
                                           "manual_spot_during_monitoring": True,
                                           "manual_spot_switch_back": True,
                                           "manual_spot_bid_ask": True,
                                           "manual_spot_input_validation": True,
                                           "shared_contract_defaults": True,
                                           "position_scenarios": True, "scenario_calendar": "XBUE",
                                           "scenario_offline_pnl": True, "scenario_histogram": True,
                                           "scenario_late_response_discarded": True,
                                           "variable_horizon": True, "return_pdf": True,
                                           "return_and_price_ranges": True, "assumed_future_tna": True,
                                           "caucion_prefill": True, "scenario_pnl_probabilities": True}), encoding="utf-8")
    finally:
        app.close()
    return 0


def comparison_widget_check(app):
    """Exercise visual eligibility, expiry, settings, demo and lifecycle in the exe."""
    from market_making.comparison.controller import demo_quotes
    from market_making.comparison.models import DEFAULT_INSTRUMENTS, SYMBOLS, compare

    tab = app.comparison
    app.tabs.select(tab)
    app.root.update()
    assert tab.min_ticks.get() == "1" and tab.max_age.get() == "10"
    assert tab.viewport.cget("yscrollcommand")
    assert tab.winfo_reqheight() < 500  # Content can scroll at the minimum window size.
    for cycle in range(1, 7):
        now = time.monotonic()
        quotes = demo_quotes(cycle, now)
        rows = []
        for symbol in SYMBOLS:
            real = next(q for q in quotes if q.symbol == symbol and q.source == "A3")
            sim = next(q for q in quotes if q.symbol == symbol and q.source == "reMarkets")
            rows.append({"symbol": symbol, "instrument": DEFAULT_INSTRUMENTS[symbol], "A3": real,
                         "reMarkets": sim, "differences": compare(DEFAULT_INSTRUMENTS[symbol], real, sim, now=now)})
        state = {"generation": 0, "active": True, "demo": True, "statuses": {"A3": "Demo", "reMarkets": "Demo"},
                 "error": None, "rows": rows, "now": now, "max_age": 10}
        tab.render(state)
        buy = tab.rows[SYMBOLS[0]]["directions"]["BUY"][1]
        sell = tab.rows[SYMBOLS[0]]["directions"]["SELL"][1]
        assert (buy.cget("background") == "#dff3e9") == (cycle == 1)
        assert (sell.cget("background") == "#dff3e9") == (cycle == 2)
    tab.max_age.set("nan")
    assert not tab.apply_settings()
    tab.max_age.set("10")
    tab.start_demo()
    deadline = time.monotonic() + 3
    while not tab.last_state["rows"][0]["A3"] and time.monotonic() < deadline:
        app.root.update()
        time.sleep(.01)
    assert tab.controller.running and tab.last_state["demo"]
    assert tab.last_state["rows"][0]["differences"][0].qualifies
    tab.min_ticks.set("3")
    assert tab.apply_settings()
    assert not tab.last_state["rows"][0]["differences"][0].qualifies
    tab.stop()
    assert not any(d.qualifies for row in tab.last_state["rows"] for d in row["differences"])
    deadline = time.monotonic() + 3
    while tab.controller.running and time.monotonic() < deadline:
        app.root.update()
        time.sleep(.01)
    assert not tab.controller.running
    tab.min_ticks.set("1")
    tab.start_demo()
    assert tab.controller.generation == 2
    tab.stop()
    app.tabs.select(0)


def scenario_widget_check(app):
    """Deterministic widget acceptance; also verifies calendar imports in the exe."""
    from datetime import date
    from market_making.desktop.scenario_controller import SpotQuote
    from market_making.scenarios.calendar import advance_sessions, sessions_between
    from market_making.scenarios.history import normalize_history

    app.tabs.select(app.scenarios)
    app.root.update()
    assert app.tabs.tab(app.scenarios, "text") == "Position scenarios"
    tab = app.scenarios
    assert tab.years.get() == "3"
    assert tab.move.get() == "0"
    assert str(tab.positions["2026-10"]["widgets"]["entry"].cget("state")) == "disabled"
    tab.reference_date.set("2026-10-14")
    tab.spot.set("100,0")
    tab.move.set("5")
    row = tab.positions["2026-10"]
    row["contracts"].set("2")
    row["entry"].set("100")
    row["current"].set("110")
    tab.recalculate()
    assert tab.result is not None
    assert abs(tab.result.total_from_today - 837.5) < 1e-7
    assert len(tab.table.get_children()) == 3
    app.rate.set("35,0")
    assert tab.assumed_tna.get() == "35,0"
    tab.recalculate()
    assert tab.result.legs[0].projection_tna == 35
    assert abs(tab.result.legs[0].implied_tna - 228.125) < 1e-7
    assert abs(tab.result.legs[0].projected_future - 105 * (1 + .35 * 14 / 365)) < 1e-7
    tab.assumed_tna.set("40")
    app.rate.set("30")
    assert tab.assumed_tna.get() == "40"
    tab.use_caucion()
    assert tab.assumed_tna.get() == "30"
    tab.assumed_tna.set("nan")
    tab.recalculate()
    assert tab.result is None and not tab.table.get_children()
    tab.assumed_tna.set("")
    app.rate.set("15")
    assert tab.assumed_tna.get() == ""
    tab.recalculate()
    assert abs(tab.result.total_from_today - 837.5) < 1e-7
    app.rate.set("")
    app.render(monitor.demo_cycle(1))
    tab.copy_simulated()
    assert tab.spot.get() == "100,0"
    assert "Synthetic demo" in row["source"].get()
    assert "2026-10-07" in row["source"].get()
    row["current"].set("110")
    assert "Manual" in row["source"].get()
    assert tab.result is None
    tab.move.set("-100")
    tab.recalculate()
    assert tab.result is None and not tab.table.get_children()
    tab.move.set("5")
    days = sessions_between(date(2026, 1, 1), date(2026, 3, 31))
    tab.history = normalize_history([{"date": day.isoformat(), "price": 100 + index}
                                    for index, day in enumerate(days)], days[0], days[-1],
                                   completed_before=date(2026, 4, 1))
    tab.refresh_frequency()
    assert "2-trading-day periods" in tab.frequency.get()
    assert tab.chart.find_withtag("target")
    assert tab.chart.find_withtag("pdf")
    assert tab.chart.find_withtag("range_area")
    assert "smoothed estimate" in tab.range_result.get()
    cached_history = tab.history
    tab.horizon.set("5")
    assert tab.history is cached_history
    assert tab.horizon_sample.horizon == 5
    assert len(tab.horizon_sample.returns) == len(days) - 5
    tab.range_unit.set("Final price (ARS)")
    assert abs(float(tab.range_lower.get()) - 95) < 1e-8
    assert abs(float(tab.range_upper.get()) - 105) < 1e-8
    assert "ARS" in tab.range_result.get()
    tab.horizon.set("bad")
    assert tab.density is None
    tab.horizon.set("2")
    assert tab.density is not None
    assert "2-trading-day periods" in tab.frequency.get()
    tab.range_lower.set("200")
    tab.range_upper.set("100")
    assert "lower bound" in tab.range_result.get()
    tab.range_lower.set("95")
    tab.range_upper.set("105")
    assert "smoothed estimate" in tab.range_result.get()
    tab.views.select(0)
    tab.recalculate()
    assert tab.result is not None
    assert tab.pnl_scenario.get() == "Scenario P/L: +837.50 ARS"
    assert tab.pnl_target_chance.get().endswith("%")
    assert tab.pnl_profit_chance.get().endswith("%")
    assert "Combined gross P/L from today" in tab.pnl_probability_note.get()
    tab.views.select(1)
    assert advance_sessions(date(2026, 11, 5)) == date(2026, 11, 11)
    # Editing a manual spot cancels a pending quote before its queue delivery.
    generation = tab.controller.generation
    tab.pending_kind = "spot"
    tab.spot.set("120")
    tab.controller.events.put((generation, "spot", SpotQuote(999, None, datetime.now(timezone.utc)), None))
    tab.poll()
    assert tab.spot.get() == "120"
    tab.years.set("1")
    assert tab.history is None


def main(argv=None):
    parser = argparse.ArgumentParser(description="GGAL desktop price and arbitrage checker")
    parser.add_argument("--self-test", metavar="OUTPUT_JSON", help="Run offline widget checks and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        try:
            return self_test(args.self_test)
        except Exception as exc:
            # Avoid a hidden modal crash dialog during offline executable checks.
            import traceback
            Path(args.self_test).write_text(json.dumps({"ok": False, "error": type(exc).__name__,
                                                       "traceback": traceback.format_exc()}), encoding="utf-8")
            return 1
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
