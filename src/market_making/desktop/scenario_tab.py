"""Native inputs, theoretical P/L table, and empirical return histogram."""
from datetime import date
import math
from pathlib import Path
from queue import Empty, Queue
import threading
import tkinter as tk
from tkinter import ttk

from market_making.contracts import CONTRACTS, CONTRACT_SHARES, DAY_BASIS
from market_making.scenarios.calendar import BA, today, validate_horizon, advance_sessions
from market_making.scenarios.calculation import (
    DEFAULT_EXPIRIES, Position, calculate, simulated_midpoints, validate_cost, validate_move, finite,
)
from market_making.scenarios.history import daily_log_returns, historical_frequency
from market_making.scenarios.distribution import (
    RANGE_COMPARISONS, ReturnDensity, effective_observations, pnl_probabilities, pnl_tail_risk,
    range_probability, range_returns, quantile,
)
from market_making.scenarios.model import DRIFT_MODES, VOLATILITY_MODES, ModelSettings, model_sample
from market_making.scenarios.simulation import SimulationLeg, SimulationSettings, simulate
from .scenario_controller import ScenarioController

BLUE = "#245bd8"
MUTED = "#64748b"
RED = "#b42318"
GREEN = "#127454"
GRID_MOVES = (-20, -10, -5, -2, 0, 2, 5, 10, 20)
GRID_TNA_SHIFTS = (-10, -5, 0, 5, 10)


def interval_text(interval):
    return "--" if interval is None else f"{interval[0]:.1f}-{interval[1]:.1f}%"


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
        self.account_snapshot = None
        self._manual_positions = None
        self._active_position_mode = "Manual simulation"
        self.model = None  # ModelSample behind horizon_sample, when built here.
        self.simulation = None
        self._simulation_events = Queue()
        self._simulation_generation = 0
        self._simulation_running = False
        self._margin_buffer_edited = False
        self._setting_margin_buffer = False
        self._pnl_cache_key = self._pnl_cache = None
        # Inputs behind self.result, reused by the grid and the simulation.
        self._last_positions, self._last_assumption, self._last_cost = (), None, 0.0
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
        self._env_trace = self.env_variable.trace_add("write", self._credentials_changed)
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
        ttk.Label(body, text=f"GGAL futures / {CONTRACT_SHARES} shares per contract / ARS per share",
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
        model_controls = ttk.Frame(body)
        model_controls.pack(fill="x", pady=(0, 2))
        self.drift_mode = tk.StringVar(value="Historical")
        self.volatility_mode = tk.StringVar(value="Equal weight")
        self.exclude_jumps = tk.BooleanVar(value=False)
        ttk.Label(model_controls, text="Return model: drift").pack(side="left")
        ttk.Combobox(model_controls, textvariable=self.drift_mode, values=DRIFT_MODES,
                     state="readonly", width=12).pack(side="left", padx=(8, 18))
        ttk.Label(model_controls, text="Volatility").pack(side="left")
        ttk.Combobox(model_controls, textvariable=self.volatility_mode, values=VOLATILITY_MODES,
                     state="readonly", width=15).pack(side="left", padx=(8, 18))
        ttk.Checkbutton(model_controls, text="Exclude flagged jumps", variable=self.exclude_jumps).pack(side="left")
        ttk.Label(body, text="Zero drift removes the historical ARS trend; Assumed TNA centres outcomes on the assumed "
                             "future TNA (Futures P/L tab). EWMA rescales every past move to today's volatility. "
                             "Defaults use the raw history.",
                  style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(0, 4))

        self.views = ttk.Notebook(body)
        self.views.pack(fill="both", expand=True, pady=(2, 0))
        positions_body = ttk.Frame(self.views, padding=(8, 10))
        distribution_body = ttk.Frame(self.views, padding=(8, 10))
        self.views.add(positions_body, text="Futures P/L")
        self.views.add(distribution_body, text="Return distribution")
        self.views.select(distribution_body)
        position_controls = ttk.Frame(positions_body)
        position_controls.pack(fill="x", pady=(0, 5))
        self.position_mode = tk.StringVar(value="Manual simulation")
        ttk.Label(position_controls, text="Position source").pack(side="left", padx=(0, 8))
        for mode in ("Manual simulation", "reMarkets account"):
            ttk.Radiobutton(position_controls, text=mode, value=mode, variable=self.position_mode).pack(side="left", padx=(0, 10))
        self.account_button = ttk.Button(position_controls, text="Load / refresh account", command=lambda: self.fetch("account"), state="disabled")
        self.account_button.pack(side="left")
        self.account_simulation_button = ttk.Button(position_controls, text="Use as manual simulation", command=self.account_to_manual, state="disabled")
        self.account_simulation_button.pack(side="left", padx=8)
        self.account_status = tk.StringVar(value="Manual simulation. Switch to reMarkets account to load GGAL futures using Settings credentials.")
        self.account_balances = tk.StringVar()
        for variable in (self.account_status, self.account_balances):
            ttk.Label(positions_body, textvariable=variable, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(0, 4))
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
        cost_controls = ttk.Frame(positions_body)
        cost_controls.pack(fill="x", pady=(0, 5))
        self.close_cost = tk.StringVar(value="0")
        ttk.Label(cost_controls, text="Closing cost per contract (ARS)").pack(side="left")
        cost_entry = ttk.Entry(cost_controls, textvariable=self.close_cost, width=12)
        cost_entry.pack(side="left", padx=8)
        cost_entry.bind("<Return>", lambda _: self.recalculate())
        ttk.Label(cost_controls, text="Commission, fees and half the bid/ask spread to close one contract. "
                                      "Charged on every open contract; 0 = gross P/L.",
                  style="Muted.TLabel").pack(side="left", padx=10)
        self.position_rows = ttk.Frame(positions_body)
        self.position_rows.pack(fill="x", pady=(0, 5))
        self._render_positions(self._default_positions())

        self.summary = tk.StringVar(value="Enter spot and your active positions, then click Calculate.")
        ttk.Label(positions_body, textvariable=self.summary, foreground=BLUE, wraplength=920).pack(anchor="w", pady=(8, 5))
        pnl_card = ttk.Frame(positions_body, padding=12, style="Card.TFrame")
        pnl_card.pack(fill="x", pady=(4, 10))
        self.pnl_scenario = tk.StringVar(value="Scenario P/L: -- ARS")
        self.pnl_target_chance = tk.StringVar(value="Chance of finishing with at least the scenario P/L: --")
        self.pnl_profit_chance = tk.StringVar(value="Chance of finishing profitable: --")
        self.pnl_probability_note = tk.StringVar(value="Enter positions and click Calculate.")
        self.pnl_intervals = tk.StringVar()
        self.pnl_tail = tk.StringVar()
        ttk.Label(pnl_card, textvariable=self.pnl_scenario, style="CardTitle.TLabel",
                  foreground=BLUE, wraplength=890).pack(anchor="w")
        for variable in (self.pnl_target_chance, self.pnl_profit_chance, self.pnl_intervals, self.pnl_tail):
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
        ttk.Label(positions_body, text=f"Projected future = projected spot x (1 + projection TNA / 100 x remaining calendar days / {DAY_BASIS}). "
                            "An assumed TNA models repricing to that rate by the scenario date. "
                            "After the closing cost entered above; before taxes and margin financing. Since-entry P/L includes differences already settled.",
                  style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(5, 8))
        ttk.Label(positions_body, text="Combined P/L sensitivity (ARS): stock move x change in projection TNA",
                  style="CardTitle.TLabel").pack(anchor="w", pady=(6, 2))
        grid_columns = ("move",) + tuple(f"tna{index}" for index in range(len(GRID_TNA_SHIFTS)))
        self.sensitivity = ttk.Treeview(positions_body, columns=grid_columns, show="headings",
                                        height=len(GRID_MOVES) + 1, selectmode="none")
        self.sensitivity.heading("move", text="Stock move")
        self.sensitivity.column("move", width=110, anchor="w")
        for index, shift in enumerate(GRID_TNA_SHIFTS):
            self.sensitivity.heading(f"tna{index}", text="Projection TNA" if shift == 0 else f"TNA {shift:+d} pp")
            self.sensitivity.column(f"tna{index}", width=130, anchor="e")
        self.sensitivity.tag_configure("selected", background="#e8eefc")
        self.sensitivity.pack(fill="x")
        self.sensitivity_note = tk.StringVar(value="Calculate a scenario to fill the grid.")
        ttk.Label(positions_body, textvariable=self.sensitivity_note, style="Muted.TLabel", wraplength=920).pack(anchor="w", pady=(3, 8))
        self._layout_simulation(positions_body)
        history_controls = ttk.Frame(distribution_body)
        history_controls.pack(fill="x")
        ttk.Label(history_controls, text="Historical return distribution (PDF)", style="CardTitle.TLabel").pack(side="left")
        ttk.Label(history_controls, text="Years").pack(side="left", padx=(20, 6))
        ttk.Combobox(history_controls, textvariable=self.years, values=("1", "3", "5"),
                     state="readonly", width=4).pack(side="left")
        self.history_button = ttk.Button(history_controls, text="Fetch / refresh PPI history", command=lambda: self.fetch("history"))
        self.history_button.pack(side="left", padx=10)
        self.fetch_status = tk.StringVar(value="Data reads start when you click Fetch or Load. Inputs stay in this session.")
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
        self.position_mode.trace_add("write", self._position_mode_changed)
        self.spot_entry.bind("<Return>", lambda _: self.recalculate())
        for variable in (self.drift_mode, self.volatility_mode, self.exclude_jumps):
            variable.trace_add("write", lambda *_: self._model_changed())
        self.close_cost.trace_add("write", lambda *_: self._inputs_changed())
        for variable in (self.sim_paths, self.sim_rate_vol, self.sim_correlation):
            variable.trace_add("write", lambda *_: self._simulation_inputs_changed())
        self.sim_margin.trace_add("write", self._margin_buffer_changed)

    def _layout_simulation(self, parent):
        card = ttk.Frame(parent, padding=12, style="Card.TFrame")
        card.pack(fill="x", pady=(4, 10))
        ttk.Label(card, text="Monte Carlo daily paths", style="CardTitle.TLabel", foreground=BLUE).pack(anchor="w")
        ttk.Label(card, text="Resamples blocks of consecutive daily GGAL moves (same return model as above) and marks "
                             "the book to market every session. Adds intra-horizon losses, expiry settlement and "
                             "optional random TNA moves.",
                  style="CardMuted.TLabel", wraplength=890).pack(anchor="w", pady=(2, 6))
        controls = ttk.Frame(card, style="Card.TFrame")
        controls.pack(fill="x")
        self.sim_paths = tk.StringVar(value="5000")
        self.sim_rate_vol = tk.StringVar(value="0")
        self.sim_correlation = tk.StringVar(value="0")
        self.sim_margin = tk.StringVar()
        for label, variable, width in (("Paths", self.sim_paths, 8),
                                       ("TNA volatility (pp at horizon)", self.sim_rate_vol, 7),
                                       ("Stock/TNA correlation", self.sim_correlation, 6),
                                       ("Margin buffer (ARS)", self.sim_margin, 14)):
            ttk.Label(controls, text=label, style="Card.TLabel").pack(side="left")
            entry = ttk.Entry(controls, textvariable=variable, width=width)
            entry.pack(side="left", padx=(6, 14))
            entry.bind("<Return>", lambda _: self.run_simulation())
        self.simulate_button = ttk.Button(controls, text="Run simulation", style="Accent.TButton",
                                          command=self.run_simulation)
        self.simulate_button.pack(side="left")
        ttk.Label(card, text="TNA volatility 0 keeps rates on their path to the projection TNA. No GGAL futures history "
                             "is available here, so rate volatility and correlation are your assumptions. Margin buffer "
                             "is the mark-to-market loss you can absorb (prefilled from the account's available-to-operate).",
                  style="CardMuted.TLabel", wraplength=890).pack(anchor="w", pady=(6, 0))
        self.sim_status = tk.StringVar(value="Calculate a scenario and fetch history, then run the simulation.")
        self.sim_final = tk.StringVar()
        self.sim_path_risk = tk.StringVar()
        ttk.Label(card, textvariable=self.sim_status, style="CardMuted.TLabel", wraplength=890).pack(anchor="w", pady=(6, 0))
        for variable in (self.sim_final, self.sim_path_risk):
            ttk.Label(card, textvariable=variable, style="Card.TLabel", wraplength=890).pack(anchor="w", pady=(4, 0))
        self.sim_chart = tk.Canvas(card, height=200, background="white", highlightthickness=0)
        self.sim_chart.pack(fill="x", pady=(8, 0))
        self.sim_chart.bind("<Configure>", lambda _: self.draw_simulation())
        ttk.Label(card, text="Grey: simulated final P/L. Red: 5% worst-case P/L (VaR). Green: break-even. "
                             "Orange: scenario P/L.", style="CardMuted.TLabel", wraplength=890).pack(anchor="w", pady=(3, 0))

    def _simulation_inputs_changed(self):
        self._simulation_generation += 1
        self._simulation_running = False
        if not hasattr(self, "simulate_button"):
            return
        self.simulate_button.configure(state="normal")
        if self.simulation is not None:
            self.simulation = None
            self.sim_final.set("")
            self.sim_path_risk.set("")
            self.sim_status.set("Inputs changed. Run the simulation again.")
            self.draw_simulation()

    def _margin_buffer_changed(self, *_):
        if not self._setting_margin_buffer:
            self._margin_buffer_edited = bool(self.sim_margin.get().strip())
        self._simulation_inputs_changed()

    def _simulation_request(self):
        """Validated, immutable inputs for the worker thread."""
        if self.result is None:
            raise ValueError("Click Calculate first; the simulation uses the same positions and assumptions.")
        if self.history is None or self.horizon_sample is None:
            raise ValueError("Fetch PPI history first; paths resample its daily moves.")
        text = self.sim_paths.get().strip()
        if not text.isascii() or not text.isdigit():
            raise ValueError("Enter a whole number of simulated paths.")
        margin = self.sim_margin.get().strip()
        settings = SimulationSettings(paths=int(text), rate_volatility=numeric(self.sim_rate_vol.get()),
                                      correlation=numeric(self.sim_correlation.get()),
                                      margin_buffer=numeric(margin) if margin else None)
        daily = ([value for _, value in self.model.daily] if self.model is not None
                 else [value for _, value in daily_log_returns(self.history)])
        legs = [SimulationLeg(position.expiry, position.contracts, position.current_price, position.maturity)
                for position in self._last_positions if position.contracts]
        return dict(daily_logs=daily, legs=legs, spot=numeric(self.spot.get()), reference_date=self.as_of(),
                    horizon=self.horizon_days(), settings=settings, assumed_tna=self._last_assumption,
                    close_cost=self._last_cost, target=self.result.total_from_today)

    def run_simulation(self):
        try:
            request = self._simulation_request()
            # Validate on the UI thread so errors appear immediately.
            from market_making.scenarios.simulation import validate_settings
            validate_settings(request["settings"])
        except (ValueError, OverflowError) as exc:
            self.sim_status.set(str(exc))
            return
        self._simulation_generation += 1
        generation = self._simulation_generation
        self._simulation_running = True
        self.simulation = None
        self.sim_final.set("")
        self.sim_path_risk.set("")
        self.draw_simulation()
        self.simulate_button.configure(state="disabled")
        self.sim_status.set(f"Simulating {request['settings'].paths:,} paths...")

        def work():
            try:
                outcome, error = simulate(**request), None
            except (ValueError, OverflowError, ZeroDivisionError) as exc:
                outcome, error = None, str(exc)
            except Exception:
                outcome, error = None, "The simulation failed unexpectedly."
            self._simulation_events.put((generation, outcome, error))
        threading.Thread(target=work, name="ggal-scenario-simulation", daemon=True).start()

    def _poll_simulation(self):
        try:
            while True:
                generation, outcome, error = self._simulation_events.get_nowait()
                if generation != self._simulation_generation:
                    continue
                self._simulation_running = False
                self.simulate_button.configure(state="normal")
                if error:
                    self.sim_status.set(error)
                    continue
                self.simulation = outcome
                self._show_simulation(outcome)
        except Empty:
            pass

    def _show_simulation(self, outcome):
        model = self.model.settings.label() if self.model is not None else "historical drift, equal-weight volatility"
        settled = (" Settled at spot inside the horizon: " + ", ".join(
            self.positions[expiry]["label"] if expiry in self.positions else expiry for expiry in outcome.settled) + "."
                   if outcome.settled else "")
        self.sim_status.set(f"{outcome.paths:,} paths / {outcome.horizon} trading days to {outcome.target_date} / "
                            f"{model}.{settled}")
        tail = {level: (value, shortfall) for level, value, shortfall in outcome.tail}
        target = ("" if outcome.at_least_target is None else
                  f" / at least scenario P/L {outcome.at_least_target:.1f}%")
        self.sim_final.set(
            f"Final P/L: mean {money(outcome.mean, signed=True)} / median {money(outcome.percentiles[50], signed=True)} / "
            f"5th-95th pct {money(outcome.percentiles[5], signed=True)} to {money(outcome.percentiles[95], signed=True)} ARS. "
            f"5% worst case (VaR) {money(tail[.05][0], signed=True)}, average beyond it (ES) {money(tail[.05][1], signed=True)}; "
            f"1%: {money(tail[.01][0], signed=True)} / {money(tail[.01][1], signed=True)}. "
            f"Profitable {outcome.profitable:.1f}%{target}.")
        breach = ("Enter a margin buffer to estimate margin-call risk." if outcome.margin_breach is None else
                  f"Chance the loss at some daily close reaches the {money(self._margin_buffer_value())} ARS buffer: "
                  f"{outcome.margin_breach:.1f}%.")
        self.sim_path_risk.set(f"Worst daily-close P/L along the path: average {money(outcome.worst_mean, signed=True)}, "
                               f"5% of paths {money(outcome.worst_p05, signed=True)} ARS or lower. {breach}")
        self.draw_simulation()

    def _margin_buffer_value(self):
        try:
            return numeric(self.sim_margin.get())
        except ValueError:
            return None

    def _prefill_margin_buffer(self, available):
        if self._margin_buffer_edited or available is None or available <= 0:
            return
        self._setting_margin_buffer = True
        try:
            self.sim_margin.set(format(available, ".2f"))
        finally:
            self._setting_margin_buffer = False

    def draw_simulation(self):
        if not hasattr(self, "sim_chart"):
            return
        chart = self.sim_chart
        chart.delete("all")
        width, height = max(300, chart.winfo_width()), 200
        outcome = self.simulation
        if outcome is None:
            chart.create_text(width / 2, height / 2, text="Run the simulation to see the distribution of final P/L.",
                              fill=MUTED, font=("Segoe UI", 10))
            return
        edges, counts = outcome.histogram
        low, high = edges[0], edges[-1]
        tail = outcome.tail[0][1]
        markers = [(0.0, GREEN, "zero"), (tail, RED, "var")]
        if self.result is not None and self.result.total_from_today is not None:
            markers.append((self.result.total_from_today, "#d07c20", "target"))
        low = min(low, *(value for value, _, _ in markers))
        high = max(high, *(value for value, _, _ in markers))
        if high <= low:
            low, high = low - 1, high + 1
        left, right, top, bottom = 20, width - 20, 15, height - 35
        peak = max(counts) or 1

        def x(value):
            return left + (value - low) / (high - low) * (right - left)
        for index, count in enumerate(counts):
            colour = "#f2c4c0" if edges[index + 1] <= tail else "#d5dce6"
            chart.create_rectangle(x(edges[index]) + 1, bottom - count / peak * (bottom - top),
                                   x(edges[index + 1]) - 1, bottom, fill=colour, outline="", tags="bar")
        for value, colour, tag in markers:
            chart.create_line(x(value), top, x(value), bottom, fill=colour, dash=(4, 3), width=2, tags=tag)
        chart.create_line(left, bottom, right, bottom, fill=MUTED)
        for fraction in (0, .25, .5, .75, 1):
            value = low + fraction * (high - low)
            chart.create_text(left + fraction * (right - left), bottom + 13, text=money(value, signed=True),
                              fill=MUTED, anchor="w" if fraction == 0 else "e" if fraction == 1 else "center")
        chart.create_text((left + right) / 2, height - 8, text="Final combined P/L (ARS)", fill=MUTED)

    def _model_settings(self):
        drift_tna = None
        if self.drift_mode.get() == "Assumed TNA":
            text = self.assumed_tna.get().strip()
            if not text:
                raise ValueError("Enter an assumed future TNA (Futures P/L tab) to use it as the drift.")
            drift_tna = finite(numeric(text), "assumed future TNA")
        return ModelSettings(self.drift_mode.get(), self.volatility_mode.get(), bool(self.exclude_jumps.get()), drift_tna)

    def _model_changed(self):
        self._sample_key = None
        self._inputs_changed()
        self.refresh_frequency()

    def _sensitivity_rows(self, positions, spot, move, days, assumption, cost):
        moves = sorted(set(GRID_MOVES) | {move})
        rows = []
        for value in moves:
            cells = []
            for shift in GRID_TNA_SHIFTS:
                try:
                    total = calculate(spot, value, positions, self.as_of(), horizon=days, assumed_tna=assumption,
                                      tna_shift=shift, close_cost=cost).total_from_today
                except (ValueError, OverflowError):
                    total = None
                cells.append(total)
            rows.append((value, cells))
        return rows

    def _fill_sensitivity(self, positions, spot, move, days, assumption, cost):
        self.sensitivity.delete(*self.sensitivity.get_children())
        for value, cells in self._sensitivity_rows(positions, spot, move, days, assumption, cost):
            label = f"{value:+g}%" + ("  (scenario)" if value == move else "")
            self.sensitivity.insert("", "end", values=(label, *(
                "expiry" if cell is None else money(cell, signed=True) for cell in cells)),
                tags=("selected",) if value == move else ())
        base = "each leg's current implied TNA" if assumption is None else f"{assumption:g}% assumed TNA"
        self.sensitivity_note.set(f"Columns shift the projection rate from {base}; the highlighted row is your scenario. "
                                  "'expiry' = an active leg expires inside the horizon.")

    @staticmethod
    def _default_positions():
        return {expiry: {"direction": "Long", "contracts": "0", "entry": "", "current": "",
                         "maturity": maturity.isoformat(), "source": "Manual futures price",
                         "symbol": CONTRACTS[expiry].symbol, "label": CONTRACTS[expiry].month_label}
                for expiry, maturity in DEFAULT_EXPIRIES.items()}

    def _position_values(self):
        return {expiry: {**{key: row[key].get() for key in ("direction", "contracts", "entry", "current", "maturity", "source")},
                         "symbol": row["symbol"], "label": row["label"]} for expiry, row in self.positions.items()}

    def _render_positions(self, data):
        for widget in self.position_rows.winfo_children():
            widget.destroy()
        self.positions = {}
        columns = ("Future", "Direction", "Contracts", "Average entry", "Current price", "Expiry (YYYY-MM-DD)")
        for column, label in enumerate(columns):
            self.position_rows.columnconfigure(column, weight=1)
            ttk.Label(self.position_rows, text=label, style="Muted.TLabel").grid(row=0, column=column, sticky="w", padx=4)
        for index, (expiry, record) in enumerate(sorted(data.items())):
            row = 2 * index + 1
            values = {key: tk.StringVar(value=record[key]) for key in ("direction", "contracts", "entry", "current", "maturity", "source")}
            values.update(symbol=record["symbol"], label=record["label"], widgets={})
            self.positions[expiry] = values
            ttk.Label(self.position_rows, text=record["symbol"]).grid(row=row, column=0, sticky="w", padx=4, pady=4)
            for column, key in enumerate(("direction", "contracts", "entry", "current", "maturity"), 1):
                widget = (ttk.Combobox(self.position_rows, textvariable=values[key], values=("Long", "Short"), width=8)
                          if key == "direction" else ttk.Entry(self.position_rows, textvariable=values[key], width=13 if key != "contracts" else 9))
                widget.grid(row=row, column=column, sticky="ew", padx=4, pady=4)
                widget.bind("<Return>", lambda _: self.recalculate())
                values["widgets"][key] = widget
            ttk.Label(self.position_rows, textvariable=values["source"], style="Muted.TLabel", wraplength=900).grid(
                row=row + 1, column=0, columnspan=6, sticky="w", padx=4)
            values["contracts"].trace_add("write", lambda *_, expiry=expiry: self._quantity_changed(expiry))
            values["current"].trace_add("write", lambda *_, expiry=expiry: self._future_edited(expiry))
            for key in ("direction", "entry", "maturity"):
                values[key].trace_add("write", lambda *_: self._inputs_changed())
            self._set_row_state(expiry)
        if hasattr(self, "table"):
            self.table.configure(height=min(10, max(3, len(data) + 1)))

    def _position_mode_changed(self, *_):
        mode = self.position_mode.get()
        if mode == self._active_position_mode:
            return
        self._active_position_mode = mode
        account_mode = self.position_mode.get() == "reMarkets account"
        if self.pending_kind == "account":
            self.cancel()
        if account_mode:
            self._manual_positions = self._position_values()
            self._render_positions(self._default_positions())
            self.account_status.set("Click Load / refresh account to import simulated GGAL futures. Other instruments are excluded.")
            if self.account_snapshot is not None:
                self._apply_account(self.account_snapshot)
        else:
            self._render_positions(self._manual_positions or self._default_positions())
            self.account_status.set("Manual simulation. Edit direction, contracts and prices; your manual inputs are kept when switching sources.")
            self.account_balances.set("")
        self.account_button.configure(state="normal" if account_mode else "disabled")
        self.copy_button.configure(state="disabled" if account_mode else "normal")
        self.account_simulation_button.configure(state="normal" if account_mode and self.account_snapshot is not None else "disabled")
        self._inputs_changed()

    def _credentials_changed(self, *_):
        if self._closed:
            return
        if self.pending_kind is not None:
            self.cancel()
        self.account_snapshot = None
        self.account_balances.set("")
        self.account_simulation_button.configure(state="disabled")
        if self.position_mode.get() == "reMarkets account":
            self._render_positions(self._default_positions())
            self.account_status.set("Credentials file changed. Load the account to use the new login.")
            self._inputs_changed()

    def _apply_account(self, snapshot):
        data = self._default_positions()
        for record in data.values():
            record["source"] = "No open GGAL position in this account."
        stamp = (snapshot.positions_calculated_at.astimezone(BA).strftime("%Y-%m-%d %H:%M:%S Buenos Aires")
                 if snapshot.positions_calculated_at else "calculation time unknown")
        for position in snapshot.positions:
            data[position.expiry] = {"symbol": position.symbol, "label": position.symbol,
                "direction": "Long" if position.contracts > 0 else "Short", "contracts": str(abs(position.contracts)),
                "entry": "" if position.entry_price is None else format(position.entry_price, ".15g"),
                "current": "" if position.current_price is None else format(position.current_price, ".15g"),
                "maturity": position.maturity.isoformat(),
                "source": f"{position.symbol} / reMarkets account market price / {stamp}. {position.note}"}
        self._render_positions(data)
        self.account_status.set(f"{snapshot.account} / simulated account / {len(snapshot.positions)} open GGAL future(s); "
                                f"{snapshot.ignored_instruments} other instrument(s) excluded. "
                                f"Received {snapshot.received_at.astimezone(BA):%Y-%m-%d %H:%M:%S Buenos Aires}.")
        stamp = (snapshot.calculated_at.astimezone(BA).strftime("%Y-%m-%d %H:%M:%S Buenos Aires")
                 if snapshot.calculated_at else "calculation time unknown")
        self.account_balances.set(f"Whole account (all instruments): cash {money(snapshot.current_cash)} | available to operate {money(snapshot.available)} | "
                                  f"margin {money(snapshot.margin)} | collateral {money(snapshot.collateral)}. ARS / calculated {stamp}.")
        self._prefill_margin_buffer(snapshot.available)
        self.account_simulation_button.configure(state="normal")
        self._inputs_changed()

    def account_to_manual(self):
        if self.position_mode.get() != "reMarkets account" or self.account_snapshot is None:
            return
        data = self._position_values()
        for row in data.values():
            row["source"] = "Copied from reMarkets / editable manual simulation"
        self.position_mode.set("Manual simulation")
        self._render_positions(data)
        self._inputs_changed()

    def _set_row_state(self, expiry):
        row = self.positions[expiry]
        quantity = row["contracts"].get().strip()
        inactive = bool(quantity) and not quantity.strip("0")
        for key, widget in row["widgets"].items():
            if self.position_mode.get() == "reMarkets account":
                widget.configure(state="disabled")
                continue
            if key != "contracts":
                widget.configure(state="disabled" if inactive else "readonly" if key == "direction" else "normal")

    def _quantity_changed(self, expiry):
        self._set_row_state(expiry)
        self._inputs_changed()

    def _future_edited(self, expiry):
        if not self._programmatic:
            self.positions[expiry]["source"].set(self.positions[expiry]["label"] + " / Manual futures price")
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
        self.sensitivity.delete(*self.sensitivity.get_children())
        self.sensitivity_note.set("Calculate a scenario to fill the grid.")
        self.summary.set("Inputs changed. Click Calculate to update the scenario.")
        self._simulation_inputs_changed()
        self._refresh_pnl_summary()

    def _refresh_pnl_summary(self):
        self.pnl_scenario.set("Scenario P/L: -- ARS")
        self.pnl_target_chance.set("Chance of finishing with at least the scenario P/L: --")
        self.pnl_profit_chance.set("Chance of finishing profitable: --")
        self.pnl_intervals.set("")
        self.pnl_tail.set("")
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
            spot = numeric(self.spot.get())
            key = (self.result, self.horizon_sample, self.density, spot)
            cached = self._pnl_cache_key
            if cached is None or any(a is not b for a, b in zip(cached[:3], key[:3])) or cached[3] != spot:
                # Bootstrap intervals cost more than the point estimates; reuse
                # them while only the distribution-range inputs change.
                self._pnl_cache = (pnl_probabilities(self.horizon_sample, self.result, spot, self.density, intervals=True),
                                   pnl_tail_risk(self.horizon_sample, self.result, spot, self.density))
                self._pnl_cache_key = key
            probabilities, tail = self._pnl_cache
        except (ValueError, OverflowError, ZeroDivisionError) as exc:
            self.pnl_probability_note.set(str(exc))
            return
        if probabilities.at_least_target is None:
            self.pnl_probability_note.set(
                f"Insufficient history: {len(self.horizon_sample.returns)} valid periods; at least 30 required.")
            return
        self.pnl_target_chance.set(target_prefix + f"{probabilities.at_least_target:.2f}%")
        self.pnl_profit_chance.set(f"Chance of finishing profitable: {probabilities.profitable:.2f}%")
        observations = len(self.horizon_sample.returns)
        self.pnl_intervals.set(
            f"95% sampling ranges: at least scenario P/L {interval_text(probabilities.at_least_target_interval)} / "
            f"profitable {interval_text(probabilities.profitable_interval)}. {observations} overlapping periods "
            f"~ {effective_observations(observations, self.horizon_sample.horizon):.0f} independent.")
        if tail:
            self.pnl_tail.set("Tail risk: " + " / ".join(
                f"{100 - risk.level * 100:g}% VaR {money(risk.value_at_risk, signed=True)}, "
                f"ES {money(risk.expected_shortfall, signed=True)}" for risk in tail)
                + " ARS (P/L at the worst 5% / 1% outcome, and the average beyond it).")
        kind = "gross" if not self._last_cost else "net"
        model = self.model.settings.label() if self.model is not None else "historical drift, equal-weight volatility"
        self.pnl_probability_note.set(
            f"Combined {kind} P/L from today, ending {self.result.target_date} "
            f"({self.horizon_sample.horizon} trading days). Smoothed historical estimates using the scenario's TNA assumptions "
            f"and {model}. Ranges are block-bootstrap sampling uncertainty, not model risk.")

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
        if self.drift_mode.get() == "Assumed TNA":
            self._model_changed()
        else:
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
            if self.position_mode.get() == "reMarkets account" and (self.account_snapshot is None or self.pending_kind == "account"):
                raise ValueError("Load the reMarkets account before calculating its positions.")
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
                                          date.fromisoformat(row["maturity"].get().strip()) if quantity else None,
                                          symbol=row["symbol"]))
            days = self.horizon_days()
            text = self.assumed_tna.get().strip()
            assumption = finite(numeric(text), "assumed future TNA") if text else None
            cost_text = self.close_cost.get().strip()
            cost = validate_cost(numeric(cost_text)) if cost_text else 0.0
            spot, move = numeric(self.spot.get()), numeric(self.move.get())
            result = calculate(spot, move, positions, self.as_of(),
                               horizon=days, assumed_tna=assumption, close_cost=cost)
        except (ValueError, OverflowError) as exc:
            self.summary.set(str(exc))
            return
        self._last_positions, self._last_assumption, self._last_cost = tuple(positions), assumption, cost
        self.result = result
        self.summary.set(f"{result.reference_date:%d %b %Y} -> {result.target_date:%d %b %Y} / "
                         f"{days} trading days, {(result.target_date - result.reference_date).days} calendar days / "
                         f"projected spot {money(result.projected_spot)} ARS / "
                         + ("using current implied TNAs" if assumption is None else f"assumed future TNA {assumption:g}%")
                         + (f" / net of {money(cost)} ARS closing cost per contract" if cost else ""))
        self._fill_sensitivity(positions, spot, move, days, assumption, cost)
        for leg in result.legs:
            self.table.insert("", "end", values=(self.positions[leg.expiry]["label"],
                                                   money(leg.implied_tna) + ("%" if leg.implied_tna is not None else ""),
                                                   money(leg.projection_tna) + ("%" if leg.projection_tna is not None else ""),
                                                   money(leg.projected_future), money(leg.from_today, signed=True),
                                                   money(leg.since_entry, signed=True),
                                                   leg.reason or ("Inactive" if leg.contracts == 0 else "Theoretical")))
        self.table.insert("", "end", values=("Combined", "", "", "", money(result.total_from_today, signed=True),
                                               money(result.total_since_entry, signed=True),
                                               "Unavailable active leg" if result.total_from_today is None
                                               else "Net P/L" if cost else "Gross P/L"))
        self.refresh_frequency()

    def copy_simulated(self):
        if self.position_mode.get() == "reMarkets account":
            self.fetch_status.set("Use as manual simulation to change imported futures prices.")
            return
        prices = simulated_midpoints(self.get_reports())
        if not prices:
            self.fetch_status.set("No valid simulated futures snapshot is available. Run the arbitrage checker or enter prices manually.")
            return
        self._programmatic = True
        try:
            for expiry, price in prices.items():
                row = self.positions[expiry]
                row["current"].set(format(price.price, ".12g"))
                row["source"].set(CONTRACTS[expiry].month_label +
                                   f" / {price.source} midpoint / received {price.receipt_timestamp or 'time unknown'}")
        finally:
            self._programmatic = False
        self.fetch_status.set(f"Copied {len(prices)} simulated future price(s). Manual spot and entry prices remain your inputs.")

    def fetch(self, kind):
        try:
            reference = self.as_of()
            if reference > today():
                raise ValueError("Use today's date or an earlier date for a PPI data request.")
            if kind == "account" and self.position_mode.get() != "reMarkets account":
                raise ValueError("Select reMarkets account to load its positions.")
            if kind == "account" and reference != today():
                raise ValueError("Account positions are current. Use today's reference date to load the account.")
            if kind == "spot" and reference != today():
                raise ValueError("PPI spot is a current price. Use today's reference date or enter a historical spot manually.")
            self.controller.start(kind, Path(self.env_variable.get()), reference_date=reference,
                                  years=int(self.years.get()) if kind == "history" else 3)
        except (ValueError, OverflowError) as exc:
            self.fetch_status.set(str(exc))
            return
        self.pending_kind = kind
        if kind == "account":
            self.account_snapshot = None
            self.account_balances.set("")
            self.account_simulation_button.configure(state="disabled")
            self._render_positions(self._default_positions())
            self.account_status.set("Loading the simulated account and filtering GGAL futures...")
            self._inputs_changed()
        if kind == "history":
            self.history = None
            self.refresh_frequency()
        self.cancel_button.configure(state="normal")
        self.fetch_status.set("Fetching reMarkets simulated account..." if kind == "account" else
                              "Fetching PPI " + ("last-trade spot..." if kind == "spot" else "daily GGAL history..."))

    def cancel(self):
        if self.pending_kind == "account":
            self.account_status.set("Account load cancelled. Click Load / refresh account to try again.")
        self.controller.cancel()
        self.pending_kind = None
        self.cancel_button.configure(state="disabled")
        self.fetch_status.set("Fetch cancelled. You can calculate with manual prices or start a new fetch.")

    def poll(self):
        if self._closed:
            return
        self._poll_simulation()
        try:
            while True:
                generation, kind, result, error = self.controller.events.get_nowait()
                if generation != self.controller.generation:
                    continue
                self.pending_kind = None
                self.cancel_button.configure(state="disabled")
                if error:
                    self.fetch_status.set(error)
                    if kind == "account":
                        self.account_status.set(error)
                elif kind == "account":
                    if self.position_mode.get() == "reMarkets account":
                        self.account_snapshot = result
                        self._apply_account(result)
                        self.fetch_status.set("reMarkets GGAL positions loaded. Set GGAL spot and scenario assumptions, then click Calculate.")
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
            self.horizon_sample = self.density = self.model = None
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
            settings = self._model_settings()
            reference = self.as_of() if settings.drift == "Assumed TNA" else None
            # Default settings keep the original key: the raw sample is unchanged.
            key = (id(self.history), days) if not settings.adjusted else (id(self.history), days, settings, reference)
            if key != self._sample_key:
                self.model = model_sample(self.history, days, settings, reference)
                self.horizon_sample = self.model.sample
                self.density = ReturnDensity.fit(self.horizon_sample.returns)
                self._sample_key = key
                self._curve_key = None
            sample = self.horizon_sample
        except (ValueError, OverflowError) as exc:
            self.horizon_sample = self.density = self.model = None
            self._sample_key = None
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
                                      f"{outside_label} 95% sampling range: smoothed "
                                      f"{interval_text(probability.smoothed_interval)}, historical "
                                      f"{interval_text(probability.empirical_interval)} "
                                      f"(~{effective_observations(probability.observations, days):.0f} independent periods).")
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
        coverage = (f"Coverage: {dates} / {len(sample.closes)} of {sample.expected_sessions} sessions / "
                    f"{missing} missing / {sample.excluded_windows} windows excluded / "
                    f"{sample.invalid_records} invalid records / {sample.conflicting_dates} conflicting dates")
        if self.model is not None:
            model = self.model
            coverage += f"\nModel: {model.settings.label()}"
            if model.current_volatility is not None:
                coverage += f" (current daily volatility {model.current_volatility * 100:.2f}%)"
            if model.jumps:
                listed = ", ".join(f"{jump.day} {math.expm1(jump.log_return) * 100:+.1f}% ({abs(jump.robust_z):.0f} sd)"
                                   for jump in model.jumps[:4])
                more = f" and {len(model.jumps) - 4} more" if len(model.jumps) > 4 else ""
                coverage += (f"\nFlagged daily jumps (possible unadjusted split/dividend or data error): {listed}{more}. "
                             + ("Windows containing them are excluded." if model.settings.exclude_jumps else
                                "Check them against corporate actions; tick Exclude flagged jumps to drop them."))
            else:
                coverage += "\nNo extreme daily jumps flagged. Ordinary dividends are too small to detect this way."
        self.coverage.set(coverage)
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
        self._simulation_generation += 1  # A running simulation's result is discarded.
        self.env_variable.trace_remove("write", self._env_trace)
        if self.caucion_variable is not None:
            self.caucion_variable.trace_remove("write", self._caucion_trace)
        self.controller.cancel()
