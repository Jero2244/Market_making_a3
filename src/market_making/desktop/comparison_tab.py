"""Manual real-futures versus simulated-book comparison, without order controls."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from market_making.comparison.controller import ComparisonController, Options
from market_making.comparison.models import DEFAULT_INSTRUMENTS
from market_making.contracts import BA

BG = "#f3f5f9"
INK = "#17243b"
MUTED = "#64748b"
GREEN_BG = "#dff3e9"


def fmt(value, *, signed=False):
    return "--" if value is None else format(value, "+,.2f" if signed else ",.2f")


class ComparisonTab(ttk.Frame):
    def __init__(self, parent, *, env_variable, open_settings):
        super().__init__(parent, padding=(16, 14))
        self.env_variable = env_variable
        self.controller = ComparisonController()
        self.min_ticks = tk.StringVar(value="1")
        self.max_age = tk.StringVar(value="10")
        self.notice = tk.StringVar(value="Start to compare real GGAL, RFX20 and WTI futures with the reMarkets book.")
        self.sources = tk.StringVar(value="A3: Stopped   |   reMarkets: Stopped")
        self.summary = tk.StringVar(value="No qualifying difference")
        self.rows = {}
        self.last_state = None
        self.display_delays = []
        self.viewport = tk.Canvas(self, background=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.viewport.yview)
        self.viewport.configure(yscrollcommand=scrollbar.set)
        self.viewport.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = ttk.Frame(self.viewport)
        window = self.viewport.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _: self.viewport.configure(scrollregion=self.viewport.bbox("all")))
        self.viewport.bind("<Configure>", lambda event: self.viewport.itemconfigure(window, width=event.width))
        actions = ttk.Frame(body)
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="Start comparison", style="Accent.TButton", command=self.start_live)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = ttk.Button(actions, text="Stop", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=(0, 8))
        self.demo_button = ttk.Button(actions, text="Try demo", command=self.start_demo)
        self.demo_button.pack(side="left")
        ttk.Button(actions, text="Settings", command=open_settings).pack(side="right")
        settings = ttk.Frame(body)
        settings.pack(fill="x", pady=10)
        for label, variable in (("Minimum difference (ticks)", self.min_ticks),
                                ("Maximum observation age (seconds)", self.max_age)):
            ttk.Label(settings, text=label).pack(side="left")
            entry = ttk.Entry(settings, textvariable=variable, width=7)
            entry.pack(side="left", padx=(8, 15))
            entry.bind("<Return>", lambda _: self.apply_settings())
        ttk.Button(settings, text="Apply", command=self.apply_settings).pack(side="left")
        ttk.Label(body, textvariable=self.sources, style="Muted.TLabel", wraplength=930).pack(anchor="w")
        self.banner = tk.Label(body, textvariable=self.summary, background=BG, foreground=INK,
                               font=("Segoe UI", 13, "bold"), anchor="w", padx=8, pady=8)
        self.banner.pack(fill="x", pady=(8, 4))
        for symbol, spec in DEFAULT_INSTRUMENTS.items():
            card = ttk.Frame(body, style="Card.TFrame", padding=12)
            card.pack(fill="x", pady=7)
            title = tk.StringVar(value=f"{symbol} / {spec.price_unit} / {spec.contract_size} / tick {spec.tick:g}")
            ttk.Label(card, textvariable=title, style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
            columns = ("source", "bid", "bid_size", "ask", "ask_size", "last", "age")
            table = ttk.Treeview(card, columns=columns, show="headings", height=2, selectmode="none")
            for key, label, width in zip(columns, ("Source", "Bid", "Bid size", "Ask", "Ask size", "Last trade", "Observed ago"),
                                         (105, 95, 80, 95, 80, 180, 105)):
                table.heading(key, text=label)
                table.column(key, width=width, minwidth=60, anchor="w" if key == "source" else "e")
            for source in ("A3", "reMarkets"):
                table.insert("", "end", iid=source, values=(source, "--", "--", "--", "--", "--", "--"))
            table.tag_configure("old", foreground=MUTED)
            table.pack(fill="x")
            directions = {}
            for side in ("BUY", "SELL"):
                text = tk.StringVar(value=side + " / Waiting for both sources")
                label = tk.Label(card, textvariable=text, background="white", foreground=MUTED,
                                 anchor="w", padx=8, pady=7, font=("Segoe UI", 10))
                label.pack(fill="x", pady=(4, 0))
                directions[side] = (text, label)
            self.rows[symbol] = {"table": table, "directions": directions, "title": title}
        ttk.Label(body, textvariable=self.notice, foreground="#97610d", wraplength=930).pack(anchor="w", pady=(6, 4))
        ttk.Label(body, text="Buy difference = A3 bid - reMarkets ask. Sell difference = reMarkets bid - A3 ask.",
                  style="Muted.TLabel", wraplength=930).pack(anchor="w")
        ttk.Label(body, text="Gross differences exclude costs and do not guarantee an exit. Observation age is not exchange freshness. No orders are sent.",
                  style="Muted.TLabel", wraplength=930).pack(anchor="w", pady=4)
        def wheel(event):
            if body.winfo_reqheight() > self.viewport.winfo_height():
                self.viewport.yview_scroll(-int(event.delta / 120), "units")
            return "break"
        def bind_wheel(widget):
            if widget.winfo_class() not in ("Entry", "TEntry"):
                widget.bind("<MouseWheel>", wheel)
            for child in widget.winfo_children():
                bind_wheel(child)
        bind_wheel(self)

    def _values(self):
        try:
            minimum = float(self.min_ticks.get().strip().replace(",", "."))
            age = float(self.max_age.get().strip().replace(",", "."))
        except ValueError:
            raise ValueError("Enter numbers for the minimum difference and maximum observation age.") from None
        return minimum, age

    def apply_settings(self):
        try:
            self.controller.settings(*self._values())
            self.notice.set("Comparison settings applied.")
            self.poll()
            return True
        except ValueError as exc:
            self.notice.set(str(exc))
            return False

    def start_live(self):
        self.start(False)

    def start_demo(self):
        self.start(True)

    def start(self, demo):
        try:
            minimum, age = self._values()
            options = Options(demo, Path(self.env_variable.get()), minimum, age)
            if not self.controller.start(options):
                self.notice.set("Wait for the previous connection to finish stopping.")
                return
            self.notice.set("Synthetic demo / cycles through buy, sell, no difference, empty, invalid and old quotes."
                            if demo else "Connecting / real source freshness remains unverified.")
            self.display_delays.clear()
            self.poll()
        except ValueError as exc:
            self.notice.set(str(exc))

    def stop(self):
        self.controller.stop()
        self.notice.set("Stopped / highlights cleared.")
        self.poll()

    def poll(self):
        state = self.controller.snapshot()
        self.render(state)
        running = self.controller.running
        self.start_button.configure(state="disabled" if running else "normal")
        self.demo_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def render(self, state):
        self.last_state = state
        self.sources.set("   |   ".join(f"{source}: {message}" for source, message in state["statuses"].items()))
        if state["error"]:
            self.notice.set(state["error"])
        qualifying = []
        for row in state["rows"]:
            widgets = self.rows[row["symbol"]]
            spec = row["instrument"]
            widgets["title"].set(f"{row['symbol']} / {spec.price_unit} / {spec.contract_size} / tick {spec.tick:g}")
            for source in ("A3", "reMarkets"):
                quote = row[source]
                if quote is None:
                    values = (source,) + ("--",) * 6
                    old = True
                else:
                    age = quote.age(state["now"])
                    last = fmt(quote.last)
                    if quote.last is not None and quote.last_time is not None:
                        last += " @ " + quote.last_time.astimezone(BA).strftime("%d/%m %H:%M")
                    values = (source, fmt(quote.bid.price if quote.bid else None),
                              fmt(quote.bid.quantity if quote.bid else None),
                              fmt(quote.ask.price if quote.ask else None), fmt(quote.ask.quantity if quote.ask else None),
                              last, f"{age:.1f}s")
                    old = not state["active"] or not quote.usable(state["now"], state["max_age"])
                    previous = getattr(self, "_displayed", {}).get((source, row["symbol"]))
                    marker = (state["generation"], quote.received_monotonic)
                    if previous != marker:
                        if not hasattr(self, "_displayed"):
                            self._displayed = {}
                        self._displayed[source, row["symbol"]] = marker
                        self.display_delays.append(age)
                        self.display_delays = self.display_delays[-200:]
                widgets["table"].item(source, values=values, tags=("old",) if old else ())
            for difference in row["differences"]:
                variable, label = widgets["directions"][difference.side]
                if difference.price is None:
                    text = difference.side + " / " + difference.reason
                else:
                    text = (f"{difference.side} in reMarkets  |  {fmt(difference.price, signed=True)} {spec.price_unit}"
                            f"  |  {fmt(difference.ticks, signed=True)} ticks  |  {fmt(difference.percent, signed=True)}%"
                            f"  |  {fmt(difference.per_contract, signed=True)} {spec.display_currency}/contract")
                variable.set(text)
                label.configure(background=GREEN_BG if difference.qualifies else "white",
                                foreground="#127454" if difference.qualifies else MUTED)
                if difference.qualifies:
                    qualifying.append((difference.ticks, row["symbol"], difference, spec.display_currency))
        if qualifying:
            _, symbol, best, currency = max(qualifying, key=lambda item: item[0])
            self.summary.set(f"{best.side} {symbol} / {fmt(best.ticks)} ticks / {fmt(best.per_contract, signed=True)} {currency} per contract")
        else:
            self.summary.set("No qualifying difference" if state["active"] else "Comparison stopped")
        self.banner.configure(background=GREEN_BG if qualifying else BG)

    def close(self):
        self.controller.stop()
