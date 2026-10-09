"""Simple native Windows interface for the existing GGAL read-only checker."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from queue import Empty
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from market_making.desktop.controller import MonitorController, WatchOptions, default_env_file
from market_making.ppi import monitor

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
        ttk.Label(header, text="Prices / carry / market making", style="Muted.TLabel").pack(side="right", pady=12)
        self.tabs = ttk.Notebook(page)
        self.tabs.pack(fill="both", expand=True)
        checker = ttk.Frame(self.tabs, padding=(16, 14))
        placeholder = ttk.Frame(self.tabs, padding=40)
        self.tabs.add(checker, text="GGAL arbitrage")
        self.tabs.add(placeholder, text="Market making")
        ttk.Label(placeholder, text="Market-making bot", style="Title.TLabel").pack(anchor="w", pady=(40, 12))
        ttk.Label(placeholder, text="Coming later", font=("Segoe UI", 15), foreground=BLUE).pack(anchor="w")
        ttk.Label(placeholder, text="This area is reserved for the general bot and its controls.",
                  style="Muted.TLabel").pack(anchor="w", pady=15)

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

        self.cards = {}
        cards = ttk.Frame(checker)
        cards.pack(fill="x")
        for column, (key, title, subtitle) in enumerate([
            ("spot", "GGAL spot", "PPI / ARS per share"),
            ("2026-10", "October 2026", "GGAL/OCT26 / simulated futures"),
            ("2026-12", "December 2026", "GGAL/DIC26 / simulated futures"),
        ]):
            cards.columnconfigure(column, weight=1, uniform="cards")
            card = ttk.Frame(cards, padding=14, style="Card.TFrame")
            card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0 if column == 2 else 6))
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

    def options(self, demo=False):
        try:
            rate = float(self.rate.get().strip().replace(",", ".")) if self.rate.get().strip() else None
            interval = 2 if demo else float(self.interval.get().strip())
        except ValueError:
            raise ValueError("Enter numbers for the rate and refresh interval.") from None
        options = WatchOptions(demo=demo, interval=interval, caucion_tna=rate,
                               env_file=Path(self.env.get()),
                               watch_config=Path(self.config.get()) if self.config.get() else None,
                               manual_check=self.manual.get())
        options.validate()
        return options

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
        self.notice.set("Synthetic demo / no broker connection" if demo else "Connecting to PPI and REMARKETS...")
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
            self.assessment.set("Gross cash-carry yield / assumed month-end weekday maturity / 365-day basis / fees excluded")
        else:
            self.assessment.set(("Synthetic assessment / " if first["mode"] == "DEMO" else "Full assessment / ") + "   |   ".join(summaries))
        blockers = list(dict.fromkeys(reason for report in reports for reason in report["blockers"]))
        self.notice.set(REASONS.get(blockers[0], "Inputs unavailable. Open Book details for diagnostics.") if blockers else
                        "Synthetic demo / fixed fixture dates, prices, rates and costs" if first["mode"] == "DEMO" else
                        "Prices received / source freshness unverified" + (" / manual check" if first["manual_check"] else ""))

    def poll(self):
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
        self.stopping = True
        self.notice.set("Unable to display the last update. Restart the checker.")

    def close(self):
        self.closing = True
        self.controller.stop()
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
        for cycle in range(1, 5):
            app.render(monitor.demo_cycle(cycle))
            root.update()
            assert len(app.reports) == 2
            assert app.cards["2026-10"]["status"].get() == LABELS[monitor.demo_cycle(cycle)[0]["status"]]
        assert app.cards["2026-10"]["bid"].get() == "--"
        app.tabs.select(1)
        root.update()
        assert app.tabs.tab(1, "text") == "Market making"
        app.tabs.select(0)
        app.start_demo()
        deadline = time.monotonic() + 5
        while not app.reports and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert app.reports and app.reports[0]["mode"] == "DEMO"
        app.stop()
        while app.controller.running and time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
        assert not app.controller.running
        app.finish()
        assert app.state.get() == "Stopped"
        Path(output).write_text(json.dumps({"ok": True, "tk": tk.TkVersion, "demo_scenarios": 4,
                                           "start_stop": True, "live_minimum_seconds": 1,
                                           "live_default_seconds": 5, "market_making_placeholder": True}), encoding="utf-8")
    finally:
        app.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="GGAL desktop price and arbitrage checker")
    parser.add_argument("--self-test", metavar="OUTPUT_JSON", help="Run offline widget checks and exit")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test(args.self_test)
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
