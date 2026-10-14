"""Native inputs, theoretical P/L table, and empirical return histogram."""
from datetime import date
import math
from pathlib import Path
from queue import Empty
import tkinter as tk
from tkinter import ttk

from market_making.scenarios.calendar import BA, today, validate_horizon, advance_sessions
from market_making.scenarios.calculation import (
    DEFAULT_EXPIRIES, Position, calculate, simulated_midpoints, validate_move, finite,
)
from market_making.scenarios.history import historical_frequency, with_horizon
from market_making.scenarios.distribution import (
    RANGE_COMPARISONS, ReturnDensity, pnl_probabilities, range_probability, range_returns, quantile,
)
from .scenario_controller import ScenarioController

BLUE = "#245bd8"
MUTED = "#64748b"


def numeric(text):
    try:
        return float(text.strip().replace(",", "."))
    except (ValueError, OverflowError):
        raise ValueError("Enter numbers using a decimal point or decimal comma.") from None


def money(value, *, signed=False):
    return "--" if value is None else format(value, "+,.2f" if signed else ",.2f")


class ScenarioTab(ttk.Frame):
    def __init__(self, parent, *, env_variable, get_reports, caucion_variable=None):
        super().__init__(parent)
        self.env_variable = env_variable
        self.get_reports = get_reports
        self.caucion_variable = caucion_variable
        self._setting_assumed_tna = False
        self._assumed_tna_edited = False
        self.controller = ScenarioController()
        self.history = None
        self.horizon_sample = None
        self.density = None
        self._sample_key = self._curve_key = None
        self._curve = None
        self.result = None
        self.pending_kind = None
        self._programmatic = False
        self._closed = False
        # Scroll at smaller window sizes without hiding the histogram or notes.
        viewport = tk.Canvas(self, background="#f3f5f9", highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=viewport.yview)
        viewport.configure(yscrollcommand=scrollbar.set)
        viewport.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        body = ttk.Frame(viewport, padding=(16, 14))
        window = viewport.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _: viewport.configure(scrollregion=viewport.bbox("all")))
        viewport.bind("<Configure>", lambda event: viewport.itemconfigure(window, width=event.width))
        def wheel(event):
            viewport.yview_scroll(-int(event.delta / 120), "units")
        viewport.bind("<MouseWheel>", wheel)
        self.viewport = viewport
        self._layout(body)
        if self.caucion_variable is not None:
            self._caucion_trace = self.caucion_variable.trace_add("write", self._caucion_changed)
            self._caucion_changed()
        # Bind scrolling only to this tab's children, without a global handler.
        def bind_children(widget):
            if widget.winfo_class() not in ("Entry", "TEntry", "TCombobox"):
                widget.bind("<MouseWheel>", wheel, add="+")
            for child in widget.winfo_children():
                bind_children(child)
        bind_children(body)

    def _layout(self, body):
        ttk.Label(body, text="Position scenarios", style="CardTitle.TLabel").pack(anchor="w")
        ttk.Label(body, text="Choose a trading-day horizon / 100 shares per contract / ARS per share",
                  style="Muted.TLabel").pack(anchor="w", pady=(2, 8))
        controls = ttk.Frame(body)
        controls.pack(fill="x", pady=(0, 4))
        self.spot = tk.StringVar()
        self.move = tk.StringVar(value="0")
        self.reference_date = tk.StringVar(value=today().isoformat())
        self.years = tk.StringVar(value="3")
        self.horizon = tk.StringVar(value="2")
        self.range_unit = tk.StringVar(value="Return (%)")
        self.range_comparison = tk.StringVar(value="From X to Y")
        self.range_lower = tk.StringVar(value="-5")
        self.range_upper = tk.StringVar(value="5")
        self._previous_unit = self.range_unit.get()
        ttk.Label(controls, text="GGAL spot").pack(side="left")
        self.spot_entry = ttk.Entry(controls, textvariable=self.spot, width=13)
        self.spot_entry.pack(side="left", padx=8)
        self.spot_button = ttk.Button(controls, text="Fetch PPI spot", command=lambda: self.fetch("spot"))
        self.spot_button.pack(side="left")
        self.copy_button = ttk.Button(controls, text="Copy simulated futures", command=self.copy_simulated)
        self.copy_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(controls, text="Cancel fetch", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="right")
        self.spot_source = tk.StringVar(value="Manual spot / PPI uses the credentials file selected in Settings.")
        ttk.Label(body, textvariable=self.spot_source, style="Muted.TLabel", wraplength=920).pack(anchor="w")
        inputs = ttk.Frame(body)
        inputs.pack(fill="x", pady=8)
        for label, variable, width in (("Reference date (YYYY-MM-DD)", self.reference_date, 13),
                                       ("Trading days", self.horizon, 5),
                                       ("Stock move (%)", self.move, 10)):
            ttk.Label(inputs, text=label).pack(side="left")
            entry = ttk.Entry(inputs, textvariable=variable, width=width)
            entry.pack(side="left", padx=(8, 18))
            entry.bind("<Return>", lambda _: self.recalculate())
        ttk.Button(inputs, text="Calculate", style="Accent.TButton", command=self.recalculate).pack(side="left")

        self.views = ttk.Notebook(body)
        self.views.pack(fill="both", expand=True, pady=(2, 0))
        positions_body = ttk.Frame(self.views, padding=(8, 10))
        distribution_body = ttk.Frame(self.views, padding=(8, 10))
        self.views.add(positions_body, text="Futures P/L")
        self.views.add(distribution_body, text="Return distribution")
        self.views.select(distribution_body)
        rate_controls = ttk.Frame(positions_body)
        rate_controls.pack(fill="x", pady=(0, 5))
        self.assumed_tna = tk.StringVar()
        ttk.Label(rate_controls, text="Assumed future TNA (%)").pack(side="left")
        self.assumed_tna_entry = ttk.Entry(rate_controls, textvariable=self.assumed_tna, width=12)
        self.assumed_tna_entry.pack(side="left", padx=8)
        self.assumed_tna_entry.bind("<Return>", lambda _: self.recalculate())
        ttk.Button(rate_controls, text="Use caucion", command=self.use_caucion).pack(side="left")
        ttk.Label(rate_controls, text="Applies to both expiries. Blank = use each current implied TNA.",
                  style="Muted.TLabel").pack(side="left", padx=10)
        self.rate_source = tk.StringVar(value="Enter your assumed projection rate, or copy the caucion rate from GGAL arbitrage.")
        ttk.Label(positions_body, textvariable=self.rate_source, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(0, 5))
        rows = ttk.Frame(positions_body)
        rows.pack(fill="x", pady=(0, 5))
        columns = ("Future", "Direction", "Contracts", "Average entry", "Current price", "Expiry (YYYY-MM-DD)")
        for column, label in enumerate(columns):
            rows.columnconfigure(column, weight=1)
            ttk.Label(rows, text=label, style="Muted.TLabel").grid(row=0, column=column, sticky="w", padx=4)
        self.positions = {}
        for row, (expiry, maturity) in enumerate(DEFAULT_EXPIRIES.items(), 1):
            values = {"direction": tk.StringVar(value="Long"), "contracts": tk.StringVar(value="0"),
                      "entry": tk.StringVar(), "current": tk.StringVar(),
                      "maturity": tk.StringVar(value=maturity.isoformat()),
                      "source": tk.StringVar(value="Manual futures price")}
            self.positions[expiry] = values
            ttk.Label(rows, text="October 2026" if expiry == "2026-10" else "December 2026").grid(
                row=row, column=0, sticky="w", padx=4, pady=4)
            widgets = {}
            for column, key in enumerate(("direction", "contracts", "entry", "current", "maturity"), 1):
                widget = (ttk.Combobox(rows, textvariable=values[key], values=("Long", "Short"),
                                       state="readonly", width=8) if key == "direction" else
                          ttk.Entry(rows, textvariable=values[key], width=13 if key != "contracts" else 9))
                widget.grid(row=row, column=column, sticky="ew", padx=4, pady=4)
                widget.bind("<Return>", lambda _: self.recalculate())
                widgets[key] = widget
            values["widgets"] = widgets
            values["contracts"].trace_add("write", lambda *_, expiry=expiry: self._quantity_changed(expiry))
            values["current"].trace_add("write", lambda *_, expiry=expiry: self._future_edited(expiry))
            for key in ("direction", "entry", "maturity"):
                values[key].trace_add("write", lambda *_: self._inputs_changed())
            self._set_row_state(expiry)
        for values in self.positions.values():
            ttk.Label(positions_body, textvariable=values["source"], style="Muted.TLabel", wraplength=920).pack(anchor="w")

        self.summary = tk.StringVar(value="Enter spot and your active positions, then click Calculate.")
        ttk.Label(positions_body, textvariable=self.summary, foreground=BLUE, wraplength=920).pack(anchor="w", pady=(8, 5))
        pnl_card = ttk.Frame(positions_body, padding=12, style="Card.TFrame")
        pnl_card.pack(fill="x", pady=(4, 10))
        self.pnl_scenario = tk.StringVar(value="Scenario P/L: -- ARS")
        self.pnl_target_chance = tk.StringVar(value="Chance of finishing with at least the scenario P/L: --")
        self.pnl_profit_chance = tk.StringVar(value="Chance of finishing profitable: --")
        self.pnl_probability_note = tk.StringVar(value="Enter positions and click Calculate.")
        ttk.Label(pnl_card, textvariable=self.pnl_scenario, style="CardTitle.TLabel",
                  foreground=BLUE, wraplength=890).pack(anchor="w")
        for variable in (self.pnl_target_chance, self.pnl_profit_chance):
            ttk.Label(pnl_card, textvariable=variable, style="Card.TLabel", wraplength=890).pack(anchor="w", pady=(4, 0))
        ttk.Label(pnl_card, textvariable=self.pnl_probability_note, style="CardMuted.TLabel",
                  wraplength=890).pack(anchor="w", pady=(6, 0))
        pnl_history_controls = ttk.Frame(pnl_card, style="Card.TFrame")
        pnl_history_controls.pack(fill="x", pady=(8, 0))
        ttk.Label(pnl_history_controls, text="History (years)", style="Card.TLabel").pack(side="left")
        ttk.Combobox(pnl_history_controls, textvariable=self.years, values=("1", "3", "5"),
                     state="readonly", width=4).pack(side="left", padx=8)
        ttk.Button(pnl_history_controls, text="Fetch / refresh PPI history",
                   command=lambda: self.fetch("history")).pack(side="left")
        columns = ("expiry", "rate", "projection_rate", "future", "today", "entry", "status")
        self.table = ttk.Treeview(positions_body, columns=columns, show="headings", height=3, selectmode="none")
        headings = ("Position", "Implied TNA", "Projection TNA", "Projected future", "P/L from today (ARS)", "P/L since entry (ARS)", "Status")
        widths = (100, 95, 105, 110, 160, 160, 210)
        for key, label, width in zip(columns, headings, widths):
            self.table.heading(key, text=label)
            self.table.column(key, width=width, minwidth=70, anchor="w" if key in ("expiry", "status") else "e")
        self.table.pack(fill="x")
        ttk.Label(positions_body, text="Projected future = projected spot x (1 + projection TNA / 100 x remaining calendar days / 365). "
                            "An assumed TNA models repricing to that rate by the scenario date. "
                            "Before commissions, spread, taxes and margin financing. Since-entry P/L includes differences already settled.",
                  style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(5, 8))
        history_controls = ttk.Frame(distribution_body)
        history_controls.pack(fill="x")
        ttk.Label(history_controls, text="Historical return distribution (PDF)", style="CardTitle.TLabel").pack(side="left")
        ttk.Label(history_controls, text="Years").pack(side="left", padx=(20, 6))
        ttk.Combobox(history_controls, textvariable=self.years, values=("1", "3", "5"),
                     state="readonly", width=4).pack(side="left")
        self.history_button = ttk.Button(history_controls, text="Fetch / refresh PPI history", command=lambda: self.fetch("history"))
        self.history_button.pack(side="left", padx=10)
        self.fetch_status = tk.StringVar(value="PPI reads start only when you click Fetch. Inputs stay in this session.")
        ttk.Label(positions_body, textvariable=self.fetch_status, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=5)
        ttk.Label(distribution_body, textvariable=self.fetch_status, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(4, 2))
        range_controls = ttk.Frame(distribution_body)
        range_controls.pack(fill="x", pady=(4, 6))
        ttk.Label(range_controls, text="End-of-horizon range").pack(side="left")
        ttk.Combobox(range_controls, textvariable=self.range_unit,
                     values=("Return (%)", "Final price (ARS)"), state="readonly", width=17).pack(side="left", padx=8)
        ttk.Combobox(range_controls, textvariable=self.range_comparison,
                     values=RANGE_COMPARISONS, state="readonly", width=15).pack(side="left", padx=(0, 8))
        self.range_lower_label = ttk.Label(range_controls, text="From")
        self.range_lower_label.pack(side="left")
        ttk.Entry(range_controls, textvariable=self.range_lower, width=12).pack(side="left", padx=8)
        self.range_upper_controls = ttk.Frame(range_controls)
        self.range_upper_controls.pack(side="left")
        ttk.Label(self.range_upper_controls, text="To").pack(side="left")
        ttk.Entry(self.range_upper_controls, textvariable=self.range_upper, width=12).pack(side="left", padx=8)
        self.range_result = tk.StringVar(value="Fetch history to estimate the chance of ending inside your range.")
        ttk.Label(distribution_body, textvariable=self.range_result, foreground=BLUE, wraplength=920).pack(anchor="w", pady=3)
        self.frequency = tk.StringVar(value="Fetch GGAL daily history to compare the selected move.")
        ttk.Label(distribution_body, textvariable=self.frequency, wraplength=920).pack(anchor="w", pady=3)
        self.coverage = tk.StringVar()
        ttk.Label(distribution_body, textvariable=self.coverage, style="Muted.TLabel", wraplength=920).pack(anchor="w")
        self.chart = tk.Canvas(distribution_body, height=240, background="white", highlightthickness=0)
        self.chart.pack(fill="x", pady=6)
        self.chart.bind("<Configure>", lambda _: self.draw_histogram())
        self.chart_hint = tk.StringVar(value="Blue: smoothed PDF. Green: your range. Grey: historical density. Orange: selected stock move.")
        ttk.Label(distribution_body, textvariable=self.chart_hint, style="Muted.TLabel", wraplength=920).pack(anchor="w")
        self.percentiles = tk.StringVar()
        ttk.Label(distribution_body, textvariable=self.percentiles, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=3)
        ttk.Label(distribution_body, text="Historical outcomes are a guide to possible future outcomes. Overlapping periods are not independent. "
                            "The PDF is smoothed in log returns; density height is not probability at one exact price. "
                            "PPI dividend/split adjustment treatment is unverified.",
                  style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(0, 6))
        self.spot.trace_add("write", self._spot_edited)
        self.move.trace_add("write", lambda *_: (self._inputs_changed(), self.refresh_frequency()))
        self.horizon.trace_add("write", lambda *_: (self._inputs_changed(), self.refresh_frequency()))
        self.assumed_tna.trace_add("write", self._assumed_tna_changed)
        self.range_unit.trace_add("write", lambda *_: self._range_unit_changed())
        self.range_comparison.trace_add("write", lambda *_: self._range_comparison_changed())
        self.range_lower.trace_add("write", lambda *_: self.refresh_frequency())
        self.range_upper.trace_add("write", lambda *_: self.refresh_frequency())
        self.reference_date.trace_add("write", lambda *_: self._history_inputs_changed())
        self.years.trace_add("write", lambda *_: self._history_inputs_changed())
        self.spot_entry.bind("<Return>", lambda _: self.recalculate())

    def _set_row_state(self, expiry):
        row = self.positions[expiry]
        quantity = row["contracts"].get().strip()
        inactive = bool(quantity) and not quantity.strip("0")
        for key, widget in row["widgets"].items():
            if key != "contracts":
                widget.configure(state="disabled" if inactive else "readonly" if key == "direction" else "normal")

    def _quantity_changed(self, expiry):
        self._set_row_state(expiry)
        self._inputs_changed()

    def _future_edited(self, expiry):
        if not self._programmatic:
            self.positions[expiry]["source"].set(("October" if expiry == "2026-10" else "December") + " / Manual futures price")
        self._inputs_changed()

    def _spot_edited(self, *_):
        if not self._programmatic:
            self.spot_source.set("Manual spot / ARS per share")
            if self.pending_kind == "spot":
                self.cancel()
        self._inputs_changed()
        self.refresh_frequency()

    def _inputs_changed(self):
        if not hasattr(self, "table"):
            return
        self.result = None
        self.table.delete(*self.table.get_children())
        self.summary.set("Inputs changed. Click Calculate to update the scenario.")
        self._refresh_pnl_summary()

    def _refresh_pnl_summary(self):
        self.pnl_scenario.set("Scenario P/L: -- ARS")
        self.pnl_target_chance.set("Chance of finishing with at least the scenario P/L: --")
        self.pnl_profit_chance.set("Chance of finishing profitable: --")
        if self.result is None:
            self.pnl_probability_note.set("Enter positions and click Calculate.")
            return
        target = self.result.total_from_today
        if target is None:
            self.pnl_scenario.set("Scenario P/L: unavailable")
            self.pnl_probability_note.set("The scenario reaches or passes an active position's expiry.")
            return
        target_label = f"{money(target, signed=True)} ARS"
        self.pnl_scenario.set(f"Scenario P/L: {target_label}")
        target_prefix = f"Chance of finishing with at least {target_label}: "
        self.pnl_target_chance.set(target_prefix + "--")
        if not any(leg.contracts for leg in self.result.legs):
            self.pnl_probability_note.set("Enter at least one active position to estimate P/L probabilities.")
            return
        if self.history is None or self.horizon_sample is None:
            self.pnl_probability_note.set("Combined P/L from today. Fetch PPI history to estimate the probabilities.")
            return
        try:
            probabilities = pnl_probabilities(self.horizon_sample, self.result, numeric(self.spot.get()), self.density)
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            self.pnl_probability_note.set(str(exc))
            return
        if probabilities.at_least_target is None:
            self.pnl_probability_note.set(
                f"Insufficient history: {len(self.horizon_sample.returns)} valid periods; at least 30 required.")
            return
        self.pnl_target_chance.set(target_prefix + f"{probabilities.at_least_target:.2f}%")
        self.pnl_profit_chance.set(f"Chance of finishing profitable: {probabilities.profitable:.2f}%")
        self.pnl_probability_note.set(
            f"Combined gross P/L from today, ending {self.result.target_date} "
            f"({self.horizon_sample.horizon} trading days). Smoothed historical estimates using the scenario's TNA assumptions.")

    def _history_inputs_changed(self):
        self._inputs_changed()
        self.history = None
        if self.pending_kind == "history":
            self.cancel()
        self.refresh_frequency()

    def as_of(self):
        try:
            value = self.reference_date.get().strip()
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
            return parsed
        except ValueError:
            raise ValueError("Enter the reference date as YYYY-MM-DD.") from None

    def horizon_days(self):
        text = self.horizon.get().strip()
        if not text.isascii() or not text.isdigit():
            raise ValueError("Enter a whole number of trading days between 1 and 252.")
        return validate_horizon(int(text))

    def _range_comparison_changed(self):
        interval = self.range_comparison.get() == "From X to Y"
        self.range_lower_label.configure(text="From" if interval else "X")
        if interval:
            self.range_upper_controls.pack(side="left")
        else:
            self.range_upper_controls.pack_forget()
        self.refresh_frequency()

    def _range_unit_changed(self):
        new_unit = self.range_unit.get()
        if new_unit == self._previous_unit:
            return
        self._programmatic = True
        try:
            # Convert each bound independently: a hidden Y must not invalidate X.
            for variable, default in ((self.range_lower, "-5"), (self.range_upper, "5")):
                try:
                    spot = finite(numeric(self.spot.get()), "spot price", positive=True)
                    value = numeric(variable.get())
                    low, _ = range_returns(value, value, unit=self._previous_unit, spot=spot)
                    converted = spot * (1 + low) if new_unit == "Final price (ARS)" else low * 100
                    variable.set(format(converted, ".12g"))
                except (ValueError, OverflowError):
                    variable.set("" if new_unit == "Final price (ARS)" else default)
        finally:
            self._previous_unit = new_unit
            self._programmatic = False
        self.refresh_frequency()

    def _assumed_tna_changed(self, *_):
        if not self._setting_assumed_tna:
            self._assumed_tna_edited = True
            self.rate_source.set("Manual assumed future TNA." if self.assumed_tna.get().strip() else
                                 "Blank assumption: preserve each future's current implied TNA.")
        self._inputs_changed()

    def _set_caucion_assumption(self, text):
        self._setting_assumed_tna = True
        try:
            self.assumed_tna.set(text)
        finally:
            self._setting_assumed_tna = False
        self.rate_source.set("Prefilled from the manual caucion TNA in GGAL arbitrage. Edit to use a different assumption." if text else
                             "Caucion is unavailable. Enter an assumed TNA; blank preserves each current implied TNA.")

    def _caucion_changed(self, *_):
        if self._assumed_tna_edited or self._closed:
            return
        text = self.caucion_variable.get().strip()
        try:
            if text:
                rate = finite(numeric(text), "caucion TNA")
                if rate < 0:
                    raise ValueError
        except ValueError:
            text = ""
        self._set_caucion_assumption(text)

    def use_caucion(self):
        text = self.caucion_variable.get().strip() if self.caucion_variable is not None else ""
        try:
            if not text:
                raise ValueError
            rate = finite(numeric(text), "caucion TNA")
            if rate < 0:
                raise ValueError
        except ValueError:
            self.rate_source.set("Enter a valid, non-negative caucion TNA in the GGAL arbitrage tab, then click Use caucion.")
            return
        self._assumed_tna_edited = False
        self._set_caucion_assumption(text)

    def recalculate(self):
        self._inputs_changed()
        try:
            positions = []
            for expiry, row in self.positions.items():
                text = row["contracts"].get().strip()
                if not text.isascii() or not text.isdigit():
                    raise ValueError("Enter a non-negative whole number of contracts, then choose Long or Short.")
                quantity = int(text)
                if row["direction"].get() not in ("Long", "Short"):
                    raise ValueError("Choose Long or Short for each position.")
                positions.append(Position(expiry, quantity * (-1 if row["direction"].get() == "Short" else 1),
                                          numeric(row["entry"].get()) if quantity else None,
                                          numeric(row["current"].get()) if quantity else None,
                                          date.fromisoformat(row["maturity"].get().strip()) if quantity else None))
            days = self.horizon_days()
            text = self.assumed_tna.get().strip()
            assumption = finite(numeric(text), "assumed future TNA") if text else None
            result = calculate(numeric(self.spot.get()), numeric(self.move.get()), positions, self.as_of(),
                               horizon=days, assumed_tna=assumption)
        except (ValueError, OverflowError) as exc:
            self.summary.set(str(exc))
            return
        self.result = result
        self.summary.set(f"{result.reference_date:%d %b %Y} -> {result.target_date:%d %b %Y} / "
                         f"{days} trading days, {(result.target_date - result.reference_date).days} calendar days / "
                         f"projected spot {money(result.projected_spot)} ARS / "
                         + ("using current implied TNAs" if assumption is None else f"assumed future TNA {assumption:g}%"))
        for leg in result.legs:
            self.table.insert("", "end", values=("October" if leg.expiry == "2026-10" else "December",
                                                   money(leg.implied_tna) + ("%" if leg.implied_tna is not None else ""),
                                                   money(leg.projection_tna) + ("%" if leg.projection_tna is not None else ""),
                                                   money(leg.projected_future), money(leg.from_today, signed=True),
                                                   money(leg.since_entry, signed=True),
                                                   leg.reason or ("Inactive" if leg.contracts == 0 else "Theoretical")))
        self.table.insert("", "end", values=("Combined", "", "", "", money(result.total_from_today, signed=True),
                                               money(result.total_since_entry, signed=True),
                                               "Unavailable active leg" if result.total_from_today is None else "Gross P/L"))
        self.refresh_frequency()

    def copy_simulated(self):
        prices = simulated_midpoints(self.get_reports())
        if not prices:
            self.fetch_status.set("No valid simulated futures snapshot is available. Run the arbitrage checker or enter prices manually.")
            return
        self._programmatic = True
        try:
            for expiry, price in prices.items():
                row = self.positions[expiry]
                row["current"].set(format(price.price, ".12g"))
                row["source"].set(("October" if expiry == "2026-10" else "December") +
                                   f" / {price.source} midpoint / received {price.receipt_timestamp or 'time unknown'}")
        finally:
            self._programmatic = False
        self.fetch_status.set(f"Copied {len(prices)} simulated future price(s). Manual spot and entry prices remain your inputs.")

    def fetch(self, kind):
        try:
            reference = self.as_of()
            if reference > today():
                raise ValueError("Use today's date or an earlier date for a PPI data request.")
            if kind == "spot" and reference != today():
                raise ValueError("PPI spot is a current price. Use today's reference date or enter a historical spot manually.")
            self.controller.start(kind, Path(self.env_variable.get()), reference_date=reference, years=int(self.years.get()))
        except (ValueError, OverflowError) as exc:
            self.fetch_status.set(str(exc))
            return
        self.pending_kind = kind
        if kind == "history":
            self.history = None
            self.refresh_frequency()
        self.cancel_button.configure(state="normal")
        self.fetch_status.set("Fetching PPI " + ("last-trade spot..." if kind == "spot" else "daily GGAL history..."))

    def cancel(self):
        self.controller.cancel()
        self.pending_kind = None
        self.cancel_button.configure(state="disabled")
        self.fetch_status.set("Fetch cancelled. You can calculate with manual prices or start a new fetch.")

    def poll(self):
        if self._closed:
            return
        try:
            while True:
                generation, kind, result, error = self.controller.events.get_nowait()
                if generation != self.controller.generation:
                    continue
                self.pending_kind = None
                self.cancel_button.configure(state="disabled")
                if error:
                    self.fetch_status.set(error)
                elif kind == "spot":
                    self._programmatic = True
                    try:
                        self.spot.set(format(result.price, ".12g"))
                    finally:
                        self._programmatic = False
                    stamp = result.source_time.astimezone(BA).strftime("%Y-%m-%d %H:%M:%S Buenos Aires") if result.source_time else "timestamp unknown"
                    self.spot_source.set("PPI production last trade / " + stamp + " / freshness unverified")
                    self.fetch_status.set("PPI spot received. Click Calculate to use this price.")
                    self.refresh_frequency()
                else:
                    self.history = result
                    self.fetch_status.set("PPI daily GGAL / ACCIONES / INMEDIATA history received. Stored only in this session.")
                    self.refresh_frequency()
        except Empty:
            pass

    def refresh_frequency(self):
        if self._programmatic:
            return
        self.selected_range = None
        if self.history is None:
            self.horizon_sample = self.density = None
            self._sample_key = None
            self._curve_key = None
            self.frequency.set("Fetch GGAL daily history to compare the selected move.")
            self.range_result.set("Set the trading days and your desired range, then fetch PPI history.")
            self.coverage.set("")
            self.percentiles.set("")
            self._refresh_pnl_summary()
            self.draw_histogram()
            return
        try:
            days = self.horizon_days()
            key = (id(self.history), days)
            if key != self._sample_key:
                self.horizon_sample = with_horizon(self.history, days)
                self.density = ReturnDensity.fit(self.horizon_sample.returns)
                self._sample_key = key
                self._curve_key = None
            sample = self.horizon_sample
        except (ValueError, OverflowError) as exc:
            self.horizon_sample = self.density = None
            self.frequency.set(str(exc))
            self.range_result.set(str(exc))
            self.percentiles.set("")
            self._refresh_pnl_summary()
            self.draw_histogram()
            return
        self._refresh_pnl_summary()
        try:
            move = validate_move(numeric(self.move.get()))
            frequency = historical_frequency(sample, move)
            description = (f"rises of at least {move:g}%" if move > 0 else
                           f"falls of at least {abs(move):g}%" if move < 0 else "nonnegative moves")
            if frequency.percentage is None:
                self.frequency.set(f"Insufficient history: {frequency.observations} valid {days}-day periods; at least 30 required.")
            else:
                self.frequency.set(f"In this sample, {description} occurred in {frequency.matches} of "
                                   f"{frequency.observations} {days}-trading-day periods ({frequency.percentage:.2f}%).")
        except ValueError as exc:
            self.frequency.set(str(exc))
        try:
            comparison = self.range_comparison.get()
            low_text = numeric(self.range_lower.get())
            high_text = numeric(self.range_upper.get()) if comparison == "From X to Y" else None
            price_mode = self.range_unit.get() == "Final price (ARS)"
            spot = numeric(self.spot.get()) if price_mode else None
            low, high = range_returns(low_text, high_text, unit=self.range_unit.get(), spot=spot,
                                      comparison=comparison)
            self.selected_range = (low, high)
            probability = range_probability(sample, low, high, self.density,
                                            include_lower=comparison != "More than X",
                                            include_upper=comparison != "Less than X")
            threshold_label = f"{money(low_text)} ARS" if price_mode else f"{low_text:+g}%"
            if comparison == "From X to Y":
                range_label = (f"{money(low_text)} to {money(high_text)} ARS" if price_mode else
                               f"{low_text:+g}% to {high_text:+g}%")
            else:
                range_label = comparison.replace("X", threshold_label)
            target_date = advance_sessions(self.as_of(), days)
            if probability.empirical is None:
                self.range_result.set(f"Range {range_label} after {days} trading days: insufficient history (need 30 periods).")
            else:
                if comparison == "From X to Y":
                    outside_label = f"Historical below {probability.below:.2f}%, above {probability.above:.2f}%."
                else:
                    operator = {"More than X": "<=", ">= X": "<", "Less than X": ">=", "<= X": ">"}[comparison]
                    outside = probability.below if comparison in ("More than X", ">= X") else probability.above
                    outside_label = f"Historical {operator} {threshold_label}: {outside:.2f}%."
                self.range_result.set(f"End-of-horizon outcome {range_label} on {target_date} ({days} trading days): "
                                      f"smoothed estimate {probability.smoothed:.2f}% / historical {probability.empirical:.2f}% "
                                      f"({probability.matches}/{probability.observations}). "
                                      f"{outside_label}")
        except (ValueError, OverflowError) as exc:
            self.range_result.set(str(exc))
        try:
            if sample.returns:
                price_mode = self.range_unit.get() == "Final price (ARS)"
                spot = finite(numeric(self.spot.get()), "spot price", positive=True) if price_mode else None
                values = [quantile(sample.returns, probability) for probability in (.05, .5, .95)]
                labels = [money(spot * (1 + value)) + " ARS" if price_mode else f"{value * 100:+.2f}%" for value in values]
                self.percentiles.set(f"Historical {days}-day outcomes: 5th percentile {labels[0]} / median {labels[1]} / "
                                     f"95th percentile {labels[2]}.")
            else:
                self.percentiles.set("")
        except (ValueError, OverflowError):
            self.percentiles.set("Enter spot to express historical outcomes as final prices.")
        dates = (f"{sample.closes[0][0]} to {sample.closes[-1][0]}" if sample.closes else "No usable daily prices")
        missing = sample.expected_sessions - len(sample.closes)
        self.coverage.set(f"Coverage: {dates} / {len(sample.closes)} of {sample.expected_sessions} sessions / "
                          f"{missing} missing / {sample.excluded_windows} windows excluded / "
                          f"{sample.invalid_records} invalid records / {sample.conflicting_dates} conflicting dates")
        self.draw_histogram()

    def draw_histogram(self):
        if not hasattr(self, "chart"):
            return
        chart = self.chart
        chart.delete("all")
        width, height = max(300, chart.winfo_width()), 240
        if self.density is None:
            text = ("Fetch history to build the PDF for your selected trading-day horizon." if self.history is None else
                    "At least 30 complete historical periods are needed to estimate the PDF.")
            chart.create_text(width / 2, height / 2, text=text,
                              fill=MUTED, font=("Segoe UI", 10))
            return
        try:
            self._draw_density(width, height)
        except (ValueError, OverflowError, ZeroDivisionError):
            chart.delete("all")
            chart.create_text(width / 2, height / 2, text="Enter a positive spot for the final-price view, or use Return (%).",
                              fill=MUTED, font=("Segoe UI", 10))

    def _draw_density(self, width, height):
        chart, density = self.chart, self.density
        price_mode = self.range_unit.get() == "Final price (ARS)"
        spot = numeric(self.spot.get()) if price_mode else 100.0
        if not math.isfinite(spot) or spot <= 0:
            raise ValueError("Invalid spot.")
        try:
            move = validate_move(numeric(self.move.get()))
        except ValueError:
            move = None
        values = [value * 100 for value in self.horizon_sample.returns]
        low, high = density.plot_bounds()
        low, high = min(low, min(values)), max(high, max(values))
        if move is not None:
            low, high = min(low, move), max(high, move)
        selected = self.selected_range
        finite_bounds = [bound * 100 for bound in selected if math.isfinite(bound)] if selected is not None else []
        if finite_bounds:
            low, high = min(low, *finite_bounds), max(high, *finite_bounds)
        if high <= low:
            low, high = low - .5, high + .5
        if not math.isfinite(high - low):
            raise ValueError("Range is too large.")
        left, right, top, bottom = 62, width - 28, 25, height - 45
        def x(percent):
            return left + (percent - low) / (high - low) * (right - left)
        # Reuse evaluated curve points when only the range shading changes.
        key = (id(density), low, high)
        if key != self._curve_key:
            grid = [low + index / 300 * (high - low) for index in range(301)]
            self._curve = tuple((point, density.pdf(point)) for point in grid)
            self._curve_key = key
        curve = list(self._curve)
        if selected is not None:
            for point in finite_bounds:
                curve.append((point, density.pdf(point)))
            curve.sort()
        counts = [0] * 30
        bin_width = (high - low) / len(counts)
        for value in values:
            index = min(len(counts) - 1, max(0, int((value - low) / bin_width)))
            counts[index] += 1
        bars = [count / (len(values) * bin_width) for count in counts]
        peak = max(max(bars), max(value for _, value in curve)) * 1.08
        def y(value):
            return bottom - value / peak * (bottom - top)
        for index, bar in enumerate(bars):
            chart.create_rectangle(x(low + index * bin_width) + 1, y(bar), x(low + (index + 1) * bin_width) - 1,
                                   bottom, fill="#e1e7ef", outline="")
        if density.bandwidth is None:
            point = values[0]
            chart.create_line(x(point), top, x(point), bottom, fill=BLUE, width=3, tags="pdf")
            chart.create_text((left + right) / 2, 12, text="All observed returns coincide: point mass, no continuous PDF.", fill=MUTED)
        else:
            if selected is not None:
                shade = [(point, value) for point, value in curve if selected[0] * 100 <= point <= selected[1] * 100]
                if len(shade) >= 2 and selected[0] < selected[1]:
                    polygon = [x(shade[0][0]), bottom]
                    for point, value in shade:
                        polygon.extend((x(point), y(value)))
                    polygon.extend((x(shade[-1][0]), bottom))
                    chart.create_polygon(polygon, fill="#b2e4cf", outline="", tags="range_area")
            points = [coordinate for point, value in curve for coordinate in (x(point), y(value))]
            chart.create_line(points, fill=BLUE, width=2, tags="pdf")
            chart.create_text((left + right) / 2, 12, text=f"{self.horizon_sample.horizon}-trading-day probability density", fill=MUTED)
        if selected is not None:
            for bound in finite_bounds:
                chart.create_line(x(bound), top, x(bound), bottom, fill="#127454", dash=(3, 3), tags="range_bound")
        if move is not None:
            chart.create_line(x(move), top, x(move), bottom, fill="#d07c20", dash=(4, 3), tags="target")
        chart.create_line(left, bottom, right, bottom, fill=MUTED)
        factor = 100 / spot if price_mode else 1
        chart.create_text(left - 7, top, anchor="e", text=f"{peak * factor:.3g}", fill=MUTED)
        chart.create_text(left - 7, bottom, anchor="e", text="0", fill=MUTED)
        chart.create_text(14, (top + bottom) / 2, angle=90, text="Density / ARS" if price_mode else "Density / %-point", fill=MUTED)
        for fraction in (0, .25, .5, .75, 1):
            percent = low + fraction * (high - low)
            label = money(spot * (1 + percent / 100)) if price_mode else f"{percent:+.1f}%"
            chart.create_text(left + fraction * (right - left), bottom + 15, text=label, fill=MUTED)
        chart.create_text((left + right) / 2, height - 7, text="Final GGAL price (ARS)" if price_mode else "GGAL return (%)", fill=MUTED)

    def close(self):
        self._closed = True
        if self.caucion_variable is not None:
            self.caucion_variable.trace_remove("write", self._caucion_trace)
        self.controller.cancel()
