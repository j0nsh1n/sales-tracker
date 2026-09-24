#!/usr/bin/env python3
"""Desktop ledger for Sales Tracker.

Layout: a dark sidebar switches between four pages -- Orders, Buyers,
Products, and Money. Orders is the working surface: stat tiles in its
header, a sentence-style form for logging a sale, the order list, and an
inspector beside it for handing items over. Dialogs (the product wizard,
Settings, the edit dialogs) open over the window.

Colours come from salestracker.ui.theme: pages sit on PAGE, content on white
CARD surfaces with hairline borders, one green accent, amber for anything
still due. Monospace is reserved for figures.
"""

from __future__ import annotations

import argparse
import queue
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime
from decimal import Decimal
from tkinter import messagebox, ttk

from salestracker import (
    CASH,
    DEFAULT_DB,
    DENOMINATIONS,
    PAYMENT_METHODS,
    UNITS,
    Order,
    Product,
    SalesTracker,
    TrackerError,
    count_cash,
    format_money,
    format_payment_method,
    format_qty,
    parse_payment_method,
    parse_quantity,
    reconcile,
)
from tkinter import filedialog

from salestracker import update
from salestracker._version import __version__
from salestracker.ui import theme

# Neutral surfaces, one accent, monospace reserved for figures. The values
# come from salestracker.ui.theme and are rebound onto this module whenever the
# operator switches theme, so read them at call time -- never capture one in a
# default argument or copy it into a constant of your own, or that widget will
# keep the old theme's colour after a switch.
BG = PANEL = SUBTLE = INK = MUTED = ""
ACCENT = ACCENT_DARK = ACCENT_SOFT = ""
DANGER = DANGER_SOFT = DISABLED = ""
LINE = ROW_ALT = FOCUS = FIELD = ON_ACCENT = RECEIVED_BG = ""
PAGE = CARD = WARN = WARN_SOFT = ""
SIDEBAR = SIDEBAR_ACTIVE = SIDEBAR_INK = SIDEBAR_MUTED = SIDEBAR_ACCENT = ""


def apply_palette(choice: str) -> str:
    """Bind the palette for `choice` onto this module; returns what it painted."""
    resolved = theme.resolve(choice)
    globals().update(theme.PALETTES[resolved])
    return resolved


apply_palette(theme.DEFAULT_CHOICE)

# Widgets that scroll themselves. The wheel is left alone over these, so one
# flick moves the list under the pointer instead of the list and the panel
# behind it at the same time.
SELF_SCROLLING = (ttk.Treeview, tk.Listbox, tk.Text)

# Key under which the appearance choice is kept in the ledger's settings table.
THEME_SETTING = "theme"

# What one wheel unit is worth, matching Tk 9's own `tk::MouseWheel` binding.
# A classic mouse notch reports 120, but a precision touchpad sends far
# smaller deltas: dividing by 120 and truncating to an int discarded every one
# of them, so the dialogs only moved when dragged by the scrollbar. Tk accepts
# a fractional amount here and accumulates it, which is how the order list has
# always scrolled smoothly under the same fingers.
WHEEL_DIVISOR = 40.0


def _accepts_fractional_scroll(widget: tk.Misc) -> bool:
    """Whether this Tk lets `yview scroll` take a fraction of a unit.

    Tk 9 does; 8.6 raises "expected integer". Scrolling by zero is a no-op on
    either, so this asks the question without moving anything.
    """
    try:
        widget.yview_scroll(0.0, "units")
    except tk.TclError:
        return False
    return True


def _wheel_amount(event: tk.Event) -> float | None:
    """Scroll units for one wheel event, or None if it carries no scroll."""
    delta = getattr(event, "delta", 0)
    num = getattr(event, "num", None)
    if delta:
        return -delta / WHEEL_DIVISOR
    if num == 4:
        return -1.0
    if num == 5:
        return 1.0
    return None


def make_wheel_handler(view: tk.Misc):
    """A wheel handler for `view` that never discards a small delta.

    Tk 8.6's own bindings divide by 120 and round down, so a precision
    touchpad moves nothing at all under them. This keeps the leftover instead.
    """
    state: dict[str, object] = {"smooth": None, "carry": 0.0}

    def handler(event: tk.Event) -> str | None:
        amount = _wheel_amount(event)
        if amount is None:
            return None
        if state["smooth"] is None:
            state["smooth"] = _accepts_fractional_scroll(view)
        if state["smooth"]:
            view.yview_scroll(amount, "units")
            return "break"
        carry = float(state["carry"]) + amount
        units = int(carry)
        state["carry"] = carry - units
        if units:
            view.yview_scroll(units, "units")
        return "break"

    return handler


def bind_wheel_scroll(widget: tk.Misc, view: tk.Misc | None = None) -> None:
    """Scroll `view` when the wheel turns over `widget`.

    Bound on the widget itself so it runs before Tk's class binding, and it
    returns "break" so the two cannot both act on one event.
    """
    handler = make_wheel_handler(view if view is not None else widget)
    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        widget.bind(sequence, handler)


def _descendants(widget: tk.Misc, cls: type) -> list:
    """Every descendant of widget that is an instance of cls."""
    found: list = []
    stack = list(widget.winfo_children())
    while stack:
        child = stack.pop()
        if isinstance(child, cls):
            found.append(child)
        stack.extend(child.winfo_children())
    return found


def _descendant_canvases(widget: tk.Misc) -> list[tk.Canvas]:
    """Every Canvas under widget. These hold a colour ttk styles cannot set."""
    return _descendants(widget, tk.Canvas)


def _drop_combobox_popdowns(widget: tk.Misc) -> None:
    """Destroy every combobox dropdown list that was already built.

    The dropdown is a plain Tk listbox ttk cannot restyle: it is born with
    whatever the option database said the day it was first opened, so it
    would keep the old palette after a switch. Tk 8.6 and 9 both rebuild the
    popdown on demand once it is gone, so the next open reads the new
    colours. One that was open at that moment simply closes.

    The popdown is created by Tcl, not by this module, so it is invisible to
    Python's widget registry: existence and destruction go through Tcl.
    """
    for combo in _descendants(widget, ttk.Combobox):
        path = f"{combo}.popdown"
        if combo.tk.call("winfo", "exists", path):
            combo.tk.call("destroy", path)


def center_on_parent(window: tk.Toplevel, parent: tk.Misc) -> None:
    """Put window over the middle of parent, kept fully on screen.

    Setting only a size leaves a Toplevel at +0+0, which is where every dialog
    in this app used to open regardless of where the main window was.
    """
    window.update_idletasks()
    width = window.winfo_width()
    height = window.winfo_height()
    if width <= 1 or height <= 1:  # not mapped yet
        width, height = window.winfo_reqwidth(), window.winfo_reqheight()
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
    # Clamp so a dialog taller than the screen, or a main window shoved against
    # an edge, still opens with its title bar reachable.
    x = max(0, min(x, window.winfo_screenwidth() - width))
    y = max(0, min(y, window.winfo_screenheight() - height))
    window.geometry(f"+{x}+{y}")


def scrollable_body(window: tk.Toplevel) -> ttk.Frame:
    """Fill window with a vertically scrolling area and return the inner frame.

    The wheel is bound once on the toplevel, not on each descendant. Every
    child carries the toplevel in its bindtags, so widgets built later scroll
    without being registered, and nothing has to be re-bound after a reload.
    """
    shell = ttk.Frame(window, style="Panel.TFrame")
    shell.pack(fill="both", expand=True)
    canvas = tk.Canvas(shell, bg=PANEL, highlightthickness=0, borderwidth=0)
    vsb = ttk.Scrollbar(
        shell,
        orient="vertical",
        command=canvas.yview,
        style="Ledger.Vertical.TScrollbar",
    )
    canvas.configure(yscrollcommand=vsb.set)
    vsb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    body = ttk.Frame(canvas, style="Panel.TFrame", padding=24)
    inner = canvas.create_window((0, 0), window=body, anchor="nw")

    def _stretch(event: tk.Event) -> None:
        canvas.itemconfigure(inner, width=event.width)

    def _region(_event: object = None) -> None:
        canvas.configure(scrollregion=canvas.bbox("all"))

    canvas.bind("<Configure>", _stretch)
    body.bind("<Configure>", _region)

    def _owns_scroll(widget: tk.Misc | None) -> bool:
        while widget is not None and widget is not window:
            if isinstance(widget, SELF_SCROLLING):
                return True
            widget = getattr(widget, "master", None)
        return False

    scroll_panel = make_wheel_handler(canvas)

    def _wheel(event: tk.Event) -> str | None:
        # A list inside the panel scrolls itself, through its own binding on
        # the widget. Standing aside here keeps one flick from moving both.
        if _owns_scroll(getattr(event, "widget", None)):
            return None
        return scroll_panel(event)

    for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        window.bind(sequence, _wheel)
    return body


class ProductWizard(tk.Toplevel):
    """One question at a time, then a review before the product is saved."""

    STEPS = (
        ("name", "What are you selling?", "A short name you will recognize on the list."),
        ("unit", "How do you count it?", "Jars, boxes, pounds, each — whatever you hand out."),
        ("price", "Price per unit?", "Use 0 if you are not tracking money."),
        ("sku", "Stock code or SKU?", "Optional. Leave blank if you do not use codes."),
        ("notes", "Anything else to remember?", "Optional. Origin, size, flavor, whatever helps."),
        ("review", "Save this product?", "Check the summary, then save. You can add more later."),
    )

    def __init__(self, master: tk.Tk, tracker: SalesTracker, on_saved) -> None:
        super().__init__(master)
        self.tracker = tracker
        self.on_saved = on_saved
        self.step = 0
        self.answers = {
            "name": "",
            "unit": "each",
            "price": "0",
            "sku": "",
            "notes": "",
        }

        self.title("Establish a product")
        self.configure(bg=PANEL)
        self.transient(master)
        self.grab_set()
        self.resizable(False, False)
        self.geometry("520x420")

        self.font_title = master.font_title
        self.font_body = master.font_body
        self.font_muted = master.font_muted
        self.font_label = master.font_label
        self.font_button = master.font_button

        self.var_input = tk.StringVar()
        self.var_error = tk.StringVar()
        self.var_question = tk.StringVar()
        self.var_hint = tk.StringVar()
        self.var_progress = tk.StringVar()

        pad = ttk.Frame(self, style="Panel.TFrame", padding=28)
        pad.pack(fill="both", expand=True)

        ttk.Label(pad, textvariable=self.var_progress, style="Field.TLabel").pack(anchor="w")
        ttk.Label(pad, textvariable=self.var_question, style="Section.TLabel").pack(
            anchor="w", pady=(8, 4)
        )
        ttk.Label(pad, textvariable=self.var_hint, style="Hint.TLabel", wraplength=440).pack(
            anchor="w", pady=(0, 16)
        )

        self.entry = ttk.Entry(pad, textvariable=self.var_input, style="Ticket.TEntry")
        self.entry.pack(fill="x", ipady=6)
        self.unit_combo = ttk.Combobox(
            pad,
            textvariable=self.var_input,
            values=UNITS,
            style="Ticket.TCombobox",
        )
        self.review = ttk.Label(pad, style="Hint.TLabel", justify="left", wraplength=440)

        ttk.Label(pad, textvariable=self.var_error, style="Error.TLabel").pack(
            anchor="w", pady=(10, 16)
        )

        nav = ttk.Frame(pad, style="Panel.TFrame")
        nav.pack(fill="x", side="bottom")
        ttk.Button(nav, text="Back", style="Ghost.TButton", command=self.back).pack(
            side="left"
        )
        self.next_btn = ttk.Button(
            nav, text="Next", style="Primary.TButton", command=self.next_step
        )
        self.next_btn.pack(side="right")

        self.bind("<Return>", lambda _e: self.next_step())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._show_step()
        center_on_parent(self, master)
        self.after(50, lambda: self.entry.focus_set())

    def _show_step(self) -> None:
        key, question, hint = self.STEPS[self.step]
        self.var_progress.set(f"STEP {self.step + 1} OF {len(self.STEPS)}")
        self.var_question.set(question)
        self.var_hint.set(hint)
        self.var_error.set("")
        self.entry.pack_forget()
        self.unit_combo.pack_forget()
        self.review.pack_forget()

        if key == "unit":
            self.var_input.set(self.answers["unit"])
            self.unit_combo.pack(fill="x", ipady=6)
            self.unit_combo.focus_set()
        elif key == "review":
            self.var_input.set("")
            summary = (
                f"Product: {self.answers['name'] or '—'}\n"
                f"Counted as: {self.answers['unit'] or 'each'}\n"
                f"Price: {self.answers['price'] or '0'} each\n"
                f"SKU: {self.answers['sku'] or '(none)'}\n"
                f"Notes: {self.answers['notes'] or '(none)'}"
            )
            self.review.configure(text=summary)
            self.review.pack(anchor="w")
            self.next_btn.configure(text="Save product")
        else:
            self.var_input.set(self.answers.get(key, ""))
            self.entry.pack(fill="x", ipady=6)
            self.entry.focus_set()
            self.next_btn.configure(text="Next")
        if key != "review":
            self.next_btn.configure(text="Next" if self.step < len(self.STEPS) - 1 else "Save product")

    def back(self) -> None:
        if self.step == 0:
            self.destroy()
            return
        self._stash()
        self.step -= 1
        self._show_step()

    def _stash(self) -> None:
        key = self.STEPS[self.step][0]
        if key in self.answers:
            self.answers[key] = self.var_input.get()

    def next_step(self) -> None:
        self.var_error.set("")
        key = self.STEPS[self.step][0]
        if key != "review":
            self._stash()
        if key == "name" and not self.answers["name"].strip():
            self.var_error.set("Give the product a name.")
            return
        if key == "price":
            try:
                from salestracker import parse_money

                parse_money(self.answers["price"] or "0")
            except TrackerError as exc:
                self.var_error.set(str(exc))
                return
        if self.step < len(self.STEPS) - 1:
            self.step += 1
            self._show_step()
            return
        try:
            product = self.tracker.add_product(
                name=self.answers["name"],
                unit=self.answers["unit"] or "each",
                unit_price=self.answers["price"] or "0",
                sku=self.answers["sku"],
                notes=self.answers["notes"],
            )
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        self.on_saved(product)
        self.destroy()


class UpdateController:
    """The app's view of the updater: one check or install at a time, run
    off the Tk thread, with the result handed back through ``after``."""

    def __init__(self, app: tk.Tk, tracker: SalesTracker) -> None:
        self.app = app
        self.updater = update.Updater(__version__, tracker.get_setting, tracker.set_setting)
        self.release: update.Release | None = None
        self.busy = False
        self.listeners: list = []

    @property
    def available(self) -> bool:
        return self.release is not None and self.updater.available(self.release)

    def _run(self, work, done, *, sync: bool = False) -> None:
        """Run work() and call done(kind, value) with ("ok", result) or
        ("error", exception). sync runs it in place, for tests."""
        if self.busy:
            return
        self.busy = True

        def finish(kind, value):
            self.busy = False
            done(kind, value)

        if sync:
            try:
                result = work()
            except Exception as exc:  # reported, not raised: it is the operator's message
                finish("error", exc)
                return
            finish("ok", result)
            return
        box: queue.Queue = queue.Queue()

        def job() -> None:
            try:
                box.put(("ok", work()))
            except Exception as exc:
                box.put(("error", exc))

        threading.Thread(target=job, daemon=True).start()

        def poll() -> None:
            try:
                kind, value = box.get_nowait()
            except queue.Empty:
                self.app.after(150, poll)
                return
            finish(kind, value)

        self.app.after(150, poll)

    def check(self, done, *, sync: bool = False) -> None:
        def work():
            self.release = self.updater.check()
            return self.release
        self._run(work, done, sync=sync)

    def install(self, done, *, sync: bool = False) -> None:
        release = self.release

        def work():
            target = update.target_path()
            if target is None:
                raise update.UpdateError(
                    "Updates install into the packaged build only. From a source "
                    "checkout, pull the repository instead."
                )
            fresh = self.updater.download(release, target.parent)
            return self.updater.install(fresh, target)
        self._run(work, done, sync=sync)

    @staticmethod
    def relaunch(target) -> None:
        """Start the installed binary; the caller closes this window."""
        subprocess.Popen([str(target)], cwd=str(target.parent))


class SettingsDialog(tk.Toplevel):
    """The only place orders and products can be removed."""

    def __init__(self, master: tk.Tk, tracker: SalesTracker, on_change) -> None:
        super().__init__(master)
        self.app = master
        self.tracker = tracker
        self.on_change = on_change
        self.title("Settings")
        self.configure(bg=PANEL)
        self.transient(master)
        self.grab_set()
        self.minsize(560, 520)
        self.geometry("640x680")

        pad = scrollable_body(self)

        ttk.Label(pad, text="Settings", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            pad,
            text="Nothing on the main list can be deleted by accident. "
            "Removing rows happens only here.",
            style="Hint.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(6, 14))

        ttk.Label(pad, text="APPEARANCE", style="Field.TLabel").pack(anchor="w")
        ttk.Label(
            pad,
            text="System follows your desktop and changes with it. "
            "Light and Dark stay put.",
            style="Hint.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(2, 6))
        self.var_theme = tk.StringVar(value=getattr(master, "theme_choice", theme.SYSTEM))
        themes = ttk.Frame(pad, style="Panel.TFrame")
        themes.pack(anchor="w", fill="x", pady=(0, 16))
        for choice in theme.THEME_CHOICES:
            ttk.Radiobutton(
                themes,
                text=theme.THEME_LABELS[choice],
                value=choice,
                variable=self.var_theme,
                style="Panel.TRadiobutton",
                command=self._change_theme,
            ).pack(side="left", padx=(0, 18))

        ttk.Label(pad, text="LEDGER FILE", style="Field.TLabel").pack(anchor="w")
        ttk.Label(pad, text=str(tracker.db_path), style="Hint.TLabel", wraplength=560).pack(
            anchor="w", pady=(2, 16)
        )

        self._build_updates(pad)

        ttk.Label(pad, text="UNLOCK", style="Field.TLabel").pack(anchor="w")
        ttk.Label(
            pad,
            text="Type RESET to enable every destructive action below. "
            "None of them can be undone.",
            style="Hint.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(4, 6))
        self.var_confirm = tk.StringVar()
        ttk.Entry(pad, textvariable=self.var_confirm, style="Ticket.TEntry").pack(
            fill="x", ipady=4, pady=(0, 16)
        )

        self.products_tree = self._picker(
            pad,
            "DELETE A PRODUCT",
            "Click a registered item, then Delete selected product. "
            "If it still has orders, delete those orders first.",
            ("name", "unit", "price", "orders"),
            {
                "name": ("Product", 190, "w"),
                "unit": ("Unit", 90, "w"),
                "price": ("Price", 90, "e"),
                "orders": ("Orders", 70, "e"),
            },
            "Delete selected product",
            self._delete_product,
        )
        self.orders_tree = self._picker(
            pad,
            "DELETE AN ORDER",
            "Removes one purchaser's row entirely. To record a hand-off instead, "
            "close this and use the received box.",
            ("purchaser", "product", "progress"),
            {
                "purchaser": ("Purchaser", 170, "w"),
                "product": ("Product", 150, "w"),
                "progress": ("Received / ordered", 150, "e"),
            },
            "Delete selected order",
            self._delete_order,
        )

        ttk.Label(pad, text="RESET EVERYTHING", style="Field.TLabel").pack(
            anchor="w", pady=(4, 4)
        )
        ttk.Button(
            pad,
            text="Reset all orders (keep products)",
            style="Danger.TButton",
            command=self._reset_orders,
        ).pack(fill="x", pady=(0, 6))
        ttk.Button(
            pad,
            text="Reset everything",
            style="Danger.TButton",
            command=self._reset_all,
        ).pack(fill="x")
        ttk.Button(pad, text="Close", style="Ghost.TButton", command=self.destroy).pack(
            fill="x", pady=(14, 0)
        )

        self.bind("<Escape>", lambda _e: self.destroy())
        self._reload()
        center_on_parent(self, master)

    # ---------------------------------------------------------------- updates

    def _build_updates(self, pad: ttk.Frame) -> None:
        ttk.Label(pad, text="UPDATES", style="Field.TLabel").pack(anchor="w")
        ttk.Label(
            pad,
            text="Where releases are published: github:owner/repo, a web address, "
                 "or a folder. A private repository needs a token; it is kept in "
                 "this ledger file only. Nothing from the ledger is ever sent.",
            style="Hint.TLabel", wraplength=560,
        ).pack(anchor="w", pady=(2, 6))
        self.updates = getattr(self.app, "updates", None)
        if self.updates is None:
            self.updates = UpdateController(self.app, self.tracker)
        updater = self.updates.updater
        self.var_update_source = tk.StringVar(value=updater.source_text)
        self.var_update_token = tk.StringVar(value=self.tracker.get_setting(
            update.SETTING_TOKEN, ""))
        self.var_update_status = tk.StringVar()

        row = ttk.Frame(pad, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Entry(row, textvariable=self.var_update_source, style="Ticket.TEntry").pack(
            side="left", fill="x", expand=True, ipady=2
        )
        self.check_button = ttk.Button(row, text="Check now", style="Ghost.TButton",
                                       command=self._check_updates)
        self.check_button.pack(side="left", padx=(8, 0))
        row = ttk.Frame(pad, style="Panel.TFrame")
        row.pack(fill="x", pady=(6, 0))
        ttk.Label(row, text="Token", style="Hint.TLabel").pack(side="left", padx=(0, 8))
        ttk.Entry(row, textvariable=self.var_update_token, style="Ticket.TEntry",
                  show="\u2022").pack(side="left", fill="x", expand=True, ipady=2)
        self.update_status = ttk.Label(pad, textvariable=self.var_update_status,
                                       style="Hint.TLabel", wraplength=560)
        self.update_status.pack(anchor="w", pady=(8, 6))
        row = ttk.Frame(pad, style="Panel.TFrame")
        row.pack(fill="x", pady=(0, 16))
        self.install_button = ttk.Button(row, text="Install and restart",
                                         style="Primary.TButton", command=self._install_update)
        self.install_button.pack(side="left")
        self.restore_button = ttk.Button(row, text="Restore previous version",
                                         style="Ghost.TButton", command=self._restore_previous)
        self.restore_button.pack(side="left", padx=(8, 0))
        self._sync_update_buttons()

    def _sync_update_buttons(self) -> None:
        release = self.updates.release
        if self.updates.busy:
            text = self.var_update_status.get()
        elif release is None:
            last = self.updates.updater.last_check()
            text = f"You have {__version__}." + (
                f"  Last checked {friendly_stamp(last.isoformat())}." if last else
                "  Not checked yet."
            )
        elif self.updates.available:
            text = (f"You have {__version__}. Version {release.version} is available"
                    + (f", published {release.published}" if release.published else "")
                    + ".")
            if release.notes:
                text += "\n" + release.notes.strip().splitlines()[0]
        else:
            text = f"You have {__version__}, the latest at {self.updates.updater.source_text}."
        self.var_update_status.set(text)
        self.update_status.configure(style="Hint.TLabel")
        self.install_button.state(["!disabled"] if self.updates.available and not self.updates.busy
                                  else ["disabled"])
        self.restore_button.state(["!disabled"] if update.Updater.previous() else ["disabled"])
        self.check_button.state(["disabled"] if self.updates.busy else ["!disabled"])

    def _update_failed(self, exc: Exception) -> None:
        self.var_update_status.set(str(exc))
        self.update_status.configure(style="Error.TLabel")
        self.install_button.state(["disabled"])
        self.check_button.state(["!disabled"])

    def _check_updates(self, sync: bool = False) -> None:
        try:
            self.updates.updater.set_source(self.var_update_source.get())
        except TrackerError as exc:
            self._update_failed(exc)
            return
        self.updates.updater.set_token(self.var_update_token.get())
        self.var_update_status.set("Checking\u2026")
        self.update_status.configure(style="Hint.TLabel")
        self.check_button.state(["disabled"])

        def done(kind, value):
            if not self.winfo_exists():
                return
            if kind == "error":
                self._update_failed(value)
            else:
                self._sync_update_buttons()
        self.updates.check(done, sync=sync)

    def _install_update(self, sync: bool = False) -> None:
        release = self.updates.release
        if release is None or not self.updates.available:
            return
        if not messagebox.askyesno(
            "Install update",
            f"Install version {release.version}? Sales Tracker will close and start "
            "again. The version you have now is kept, and can be restored from here.",
            parent=self,
        ):
            return
        self.var_update_status.set(f"Downloading version {release.version}\u2026")
        self.update_status.configure(style="Hint.TLabel")
        self.install_button.state(["disabled"])
        self.check_button.state(["disabled"])

        def done(kind, value):
            if not self.winfo_exists():
                return
            if kind == "error":
                self._update_failed(value)
                return
            messagebox.showinfo(
                "Update installed",
                f"Version {release.version} is installed. Sales Tracker will now restart.",
                parent=self,
            )
            self.updates.relaunch(value)
            self.app._on_close()
        self.updates.install(done, sync=sync)

    def _restore_previous(self) -> None:
        if not messagebox.askyesno(
            "Restore previous version",
            "Go back to the previous build? Sales Tracker will close and start again.",
            parent=self,
        ):
            return
        try:
            target = update.Updater.restore_previous()
        except TrackerError as exc:
            self._update_failed(exc)
            return
        messagebox.showinfo("Restored", "The previous build is back. Sales Tracker will "
                            "now restart.", parent=self)
        self.updates.relaunch(target)
        self.app._on_close()

    def _change_theme(self) -> None:
        """Repaint immediately; the main window persists the choice."""
        setter = getattr(self.app, "set_theme", None)
        if setter is not None:
            setter(self.var_theme.get())

    def _picker(
        self,
        parent: ttk.Frame,
        label: str,
        hint: str,
        columns: tuple[str, ...],
        headings: dict[str, tuple[str, int, str]],
        button_text: str,
        command,
    ) -> ttk.Treeview:
        ttk.Label(parent, text=label, style="Field.TLabel").pack(anchor="w")
        ttk.Label(parent, text=hint, style="Hint.TLabel", wraplength=580).pack(
            anchor="w", pady=(2, 6)
        )
        wrap = ttk.Frame(parent, style="Panel.TFrame")
        wrap.pack(fill="x", pady=(0, 6))
        tree = ttk.Treeview(
            wrap,
            columns=columns,
            show="headings",
            style="Ledger.Treeview",
            selectmode="browse",
            height=4,
        )
        for key, (title, width, anchor) in headings.items():
            tree.heading(key, text=title)
            tree.column(key, width=width, anchor=anchor, stretch=True)
        scroll = ttk.Scrollbar(
            wrap, orient="vertical", command=tree.yview, style="Ledger.Vertical.TScrollbar"
        )
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        bind_wheel_scroll(tree)
        ttk.Button(parent, text=button_text, style="Danger.TButton", command=command).pack(
            fill="x", pady=(0, 16)
        )
        return tree

    def _reload(self) -> None:
        self.orders_tree.delete(*self.orders_tree.get_children())
        for order in self.tracker.list_orders():
            self.orders_tree.insert(
                "",
                "end",
                iid=str(order.id),
                values=(
                    order.purchaser,
                    order.product_name,
                    f"{format_qty(order.quantity_received)} / "
                    f"{format_qty(order.quantity_ordered)}",
                ),
            )
        self.products_tree.delete(*self.products_tree.get_children())
        for product in self.tracker.list_products():
            self.products_tree.insert(
                "",
                "end",
                iid=str(product.id),
                values=(
                    product.name,
                    product.unit,
                    format_money(product.unit_price),
                    self.tracker.count_orders_for_product(product.id),
                ),
            )

    def _confirmed(self) -> bool:
        if self.var_confirm.get().strip() != "RESET":
            messagebox.showinfo(
                "Settings",
                "Type RESET in the unlock box first so this cannot happen by accident.",
                parent=self,
            )
            return False
        return True

    @staticmethod
    def _selected(tree: ttk.Treeview) -> int | None:
        selection = tree.selection()
        return int(selection[0]) if selection else None

    def _delete_order(self) -> None:
        order_id = self._selected(self.orders_tree)
        if order_id is None:
            messagebox.showinfo("Settings", "Pick an order to delete.", parent=self)
            return
        if not self._confirmed():
            return
        try:
            order = self.tracker.delete_order(order_id)
        except TrackerError as exc:
            messagebox.showerror("Settings", str(exc), parent=self)
            return
        self._reload()
        self.on_change()
        messagebox.showinfo(
            "Settings",
            f"Deleted {order.purchaser}'s order for {order.product_name}.",
            parent=self,
        )

    def _delete_product(self) -> None:
        product_id = self._selected(self.products_tree)
        if product_id is None:
            messagebox.showinfo("Settings", "Pick a product to delete.", parent=self)
            return
        if not self._confirmed():
            return
        try:
            product = self.tracker.delete_product(product_id)
        except TrackerError as exc:
            messagebox.showerror("Settings", str(exc), parent=self)
            return
        self._reload()
        self.on_change()
        messagebox.showinfo("Settings", f"Deleted {product.name}.", parent=self)

    def _reset_orders(self) -> None:
        if not self._confirmed():
            return
        count = self.tracker.reset_orders()
        self._reload()
        self.on_change()
        messagebox.showinfo(
            "Reset", f"Cleared {count} order(s). Products are still on file.", parent=self
        )

    def _reset_all(self) -> None:
        if not self._confirmed():
            return
        self.tracker.reset_all()
        self._reload()
        self.on_change()
        messagebox.showinfo("Reset", "Products and orders were cleared.", parent=self)


class _EditDialog(tk.Toplevel):
    """Shared frame for the two edit dialogs: fields, an error line, buttons."""

    def __init__(self, master: tk.Tk, tracker: SalesTracker, title: str) -> None:
        super().__init__(master)
        self.tracker = tracker
        self.title(title)
        self.configure(bg=PANEL)
        self.transient(master)
        self.grab_set()
        self.resizable(False, False)
        self.var_error = tk.StringVar()
        self._focus_job: str | None = None
        self.pad = ttk.Frame(self, style="Panel.TFrame", padding=24)
        self.pad.pack(fill="both", expand=True)

    def _entry(self, label: str, variable: tk.StringVar) -> ttk.Entry:
        ttk.Label(self.pad, text=label, style="Field.TLabel").pack(anchor="w")
        entry = ttk.Entry(self.pad, textvariable=variable, style="Ticket.TEntry",
                          width=36)
        entry.pack(fill="x", ipady=2, pady=(3, 12))
        return entry

    def _combo(self, label: str, variable: tk.StringVar, values,
               readonly: bool = True) -> ttk.Combobox:
        ttk.Label(self.pad, text=label, style="Field.TLabel").pack(anchor="w")
        combo = ttk.Combobox(self.pad, textvariable=variable, values=list(values),
                             state="readonly" if readonly else "normal",
                             style="Ticket.TCombobox")
        combo.pack(fill="x", ipady=2, pady=(3, 12))
        return combo

    def _focus_later(self, widget: tk.Misc) -> None:
        # Deferred until the window is mapped, or the focus does not stick.
        self._focus_job = self.after(50, widget.focus_set)

    def destroy(self) -> None:
        # A dialog closed within the delay would otherwise leave the callback
        # to fire on a widget that no longer exists.
        if self._focus_job is not None:
            self.after_cancel(self._focus_job)
            self._focus_job = None
        super().destroy()

    def _finish(self, save_text: str) -> None:
        ttk.Label(self.pad, textvariable=self.var_error, style="Error.TLabel",
                  wraplength=380).pack(anchor="w", pady=(0, 12))
        nav = ttk.Frame(self.pad, style="Panel.TFrame")
        nav.pack(fill="x")
        ttk.Button(nav, text="Cancel", style="Ghost.TButton",
                   command=self.destroy).pack(side="left")
        ttk.Button(nav, text=save_text, style="Primary.TButton",
                   command=self.save).pack(side="right")
        self.bind("<Return>", lambda _e: self.save())
        self.bind("<Escape>", lambda _e: self.destroy())
        center_on_parent(self, self.master)

    def save(self) -> None:
        raise NotImplementedError


class OrderEditor(_EditDialog):
    """Correct a logged order. Received is left to the inline box on the list."""

    def __init__(self, master: tk.Tk, tracker: SalesTracker, order_id: int,
                 on_saved) -> None:
        super().__init__(master, tracker, "Edit order")
        self.order_id = order_id
        self.on_saved = on_saved
        order = tracker.get_order(order_id)

        ttk.Label(self.pad, text=f"Edit {order.purchaser}'s order",
                  style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            self.pad,
            text=f"{format_qty(order.quantity_received)} {order.product_unit} "
            "received so far. To record a hand-off, use the received box on "
            "the list instead.",
            style="Hint.TLabel", wraplength=380,
        ).pack(anchor="w", pady=(4, 16))

        self.var_purchaser = tk.StringVar(value=order.purchaser)
        self.var_product = tk.StringVar(value=order.product_name)
        self.var_qty = tk.StringVar(value=format_qty(order.quantity_ordered))
        self.var_method = tk.StringVar(value=format_payment_method(order.payment_method))

        self.purchaser_entry = self._entry("PURCHASER", self.var_purchaser)
        self._combo("PRODUCT", self.var_product,
                    [product.name for product in tracker.list_products()])
        self._entry("HOW MANY ORDERED", self.var_qty)
        self._combo("PAID BY", self.var_method,
                    [format_payment_method(m) for m in PAYMENT_METHODS])
        self._finish("Save changes")
        self._focus_later(self.purchaser_entry)

    def save(self) -> None:
        try:
            order = self.tracker.edit_order(
                self.order_id,
                purchaser=self.var_purchaser.get(),
                quantity=self.var_qty.get(),
                product=self.var_product.get(),
                payment_method=self.var_method.get().strip().lower() or CASH,
            )
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        self.on_saved(order)
        self.destroy()


class ProductEditor(_EditDialog):
    """Correct a product. Warns before a price change reprices its orders."""

    def __init__(self, master: tk.Tk, tracker: SalesTracker, product_id: int,
                 on_saved) -> None:
        super().__init__(master, tracker, "Edit product")
        self.on_saved = on_saved
        self.products = {product.name: product for product in tracker.list_products()}
        self.product = tracker.get_product(product_id)

        ttk.Label(self.pad, text="Edit a product", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            self.pad,
            text="Changes show on every order for this product.",
            style="Hint.TLabel", wraplength=380,
        ).pack(anchor="w", pady=(4, 16))

        self.var_pick = tk.StringVar(value=self.product.name)
        self.var_name = tk.StringVar()
        self.var_unit = tk.StringVar()
        self.var_price = tk.StringVar()
        self.var_sku = tk.StringVar()
        self.var_notes = tk.StringVar()
        self.var_attached = tk.StringVar()

        self.pick_combo = self._combo("EDITING", self.var_pick, self.products)
        self.pick_combo.bind("<<ComboboxSelected>>", lambda _e: self._load())
        self.name_entry = self._entry("NAME", self.var_name)
        self._combo("COUNTED AS", self.var_unit, UNITS, readonly=False)
        self._entry("PRICE PER UNIT", self.var_price)
        ttk.Label(self.pad, textvariable=self.var_attached, style="Hint.TLabel",
                  wraplength=380).pack(anchor="w", pady=(0, 12))
        self._entry("SKU", self.var_sku)
        self._entry("NOTES", self.var_notes)
        self._finish("Save changes")
        self._load()
        self._focus_later(self.name_entry)

    def _load(self) -> None:
        self.product = self.products.get(self.var_pick.get(), self.product)
        self.var_name.set(self.product.name)
        self.var_unit.set(self.product.unit)
        self.var_price.set(str(self.product.unit_price))
        self.var_sku.set(self.product.sku)
        self.var_notes.set(self.product.notes)
        self.var_error.set("")
        attached = self.tracker.count_orders_for_product(self.product.id)
        self.var_attached.set(
            f"Used by {attached} order(s). A new price applies to all of them."
            if attached else ""
        )

    def save(self) -> None:
        try:
            warning = self.tracker.price_change_warning(
                self.product.id, self.var_price.get()
            )
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        if warning and not messagebox.askyesno(
            "Edit product", f"{warning}\n\nChange the price anyway?", parent=self
        ):
            return
        previous = self.product.name
        try:
            product = self.tracker.edit_product(
                self.product.id,
                name=self.var_name.get(),
                unit=self.var_unit.get(),
                unit_price=self.var_price.get(),
                sku=self.var_sku.get(),
                notes=self.var_notes.get(),
            )
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        self.on_saved(product, previous)
        self.destroy()


def initials(name: str) -> str:
    """Up to two letters for an avatar: first and last word, or one word's start."""
    words = [word for word in name.replace("-", " ").split() if word]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def friendly_stamp(stamp: str) -> str:
    """"Sep 12, 10:00" from a stored ISO stamp; the stamp itself if unreadable."""
    try:
        moment = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return str(stamp)
    return f"{moment:%b} {moment.day}, {moment:%H:%M}"


class Placeholder:
    """Grey hint text laid over an empty entry.

    An overlay rather than text inside the entry, so the entry's variable only
    ever holds what the operator typed and code reading it needs no special
    case. It steps aside while the entry has focus.
    """

    def __init__(self, entry: ttk.Entry, variable: tk.StringVar, text: str) -> None:
        self.entry = entry
        self.variable = variable
        self.label = ttk.Label(entry.master, text=text, style="Placeholder.TLabel")
        self.label.bind("<Button-1>", lambda _e: entry.focus_set())
        entry.bind("<FocusIn>", lambda _e: self.sync(), add="+")
        entry.bind("<FocusOut>", lambda _e: self.sync(), add="+")
        variable.trace_add("write", lambda *_: self.sync())
        self.sync()

    def sync(self) -> None:
        try:
            focused = self.entry.focus_get() is self.entry
        except (KeyError, tk.TclError):  # focus is in a popdown Tk cannot name
            focused = False
        if self.variable.get() or focused:
            self.label.place_forget()
        else:
            self.label.place(in_=self.entry, x=9, rely=0.5, anchor="w")


class LogSaleDialog(_EditDialog):
    """Log a sale from the Counter. The fields are the app's own variables,
    so ``SalesApp.log_order`` reads them whether or not this dialog is open."""

    def __init__(self, master: tk.Tk, tracker: SalesTracker) -> None:
        super().__init__(master, tracker, "Log a sale")
        self.app = master
        # Errors go where the form is: the app's error variable feeds the
        # label this dialog builds.
        self.var_error = master.var_error
        products = tracker.list_products()
        purchasers = sorted({o.purchaser for o in tracker.list_orders()},
                            key=str.casefold)

        ttk.Label(self.pad, text="Log a sale", style="Section.TLabel").pack(anchor="w")
        ttk.Label(self.pad, text="Received starts at 0. They stay on the list "
                  "until everything is handed over.",
                  style="Hint.TLabel", wraplength=380).pack(anchor="w", pady=(4, 16))
        self.purchaser_combo = self._combo("WHO BOUGHT?", master.var_purchaser,
                                           purchasers, readonly=False)
        self.product_combo = self._combo("WHAT", master.var_product,
                                         [p.name for p in products])
        self.qty_entry = self._entry("HOW MANY", master.var_qty)
        self.method_combo = self._combo(
            "PAID BY", master.var_method,
            [format_payment_method(m) for m in PAYMENT_METHODS],
        )
        self._finish("Log order")
        self._focus_later(self.purchaser_combo)

    def save(self) -> None:
        self.app.log_order()

    def destroy(self) -> None:
        self.app.var_error.set("")
        if self.app.log_dialog is self:
            self.app.log_dialog = None
            self.app.product_combo = None
            self.app.method_combo = None
        super().destroy()


class MoneyPanel(ttk.Frame):
    """Expected money beside an independent bill count, and the verdict.

    The ledger figure and the drawer count are arrived at separately; the
    verdict at the bottom is the whole point of the page. Counts are held in
    memory only and are never written anywhere.
    """

    def __init__(self, master: ttk.Frame, app: "SalesApp") -> None:
        super().__init__(master, style="Page.TFrame")
        self.app = app
        self.tracker = app.tracker
        self.money = self.tracker.financials()
        self._rules: list[tk.Frame] = []
        self.var_counts: dict[int, tk.StringVar] = {}
        self.var_subtotals: dict[int, tk.StringVar] = {}
        self.var_expected_rows: dict[str, tk.StringVar] = {}
        self.var_counted = tk.StringVar(value=format_money(Decimal("0.00")))
        self.var_expected = tk.StringVar()
        self.var_difference = tk.StringVar(value="—")
        self.var_verdict = tk.StringVar()
        self.var_note = tk.StringVar()

        self.columnconfigure(0, weight=1, uniform="money")
        self.columnconfigure(1, weight=1, uniform="money")
        self._build_verdict()
        self._build_expected()
        self._build_count()
        self.reload()

    def _rule(self, parent: ttk.Frame, pady) -> None:
        rule = tk.Frame(parent, height=1)
        self.app.paint(rule, bg="LINE")
        rule.pack(fill="x", pady=pady)
        self._rules.append(rule)

    def _build_verdict(self) -> None:
        border, card = self.app.card(self, padding=(22, 18, 22, 18))
        border.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 16))
        ttk.Label(card, text="DRAWER CHECK", style="CardCaption.TLabel").pack(anchor="w")
        self.verdict_label = ttk.Label(card, textvariable=self.var_verdict,
                                       style="VerdictIdle.TLabel")
        self.verdict_label.pack(anchor="w", pady=(6, 10))
        figures = ttk.Frame(card, style="Card.TFrame")
        figures.pack(fill="x")
        for caption, var in (("COUNTED", self.var_counted),
                             ("EXPECTED CASH", self.var_expected),
                             ("DIFFERENCE", self.var_difference)):
            cell = ttk.Frame(figures, style="Card.TFrame")
            cell.pack(side="left", padx=(0, 48))
            ttk.Label(cell, text=caption, style="CardCaption.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=var, style="CardFigureBig.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=self.var_note, style="CardHint.TLabel",
                  wraplength=720).pack(anchor="w", pady=(10, 0))

    def _build_expected(self) -> None:
        border, card = self.app.card(self, padding=(22, 18, 22, 18))
        border.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        ttk.Label(card, text="EXPECTED FROM THE LEDGER",
                  style="CardCaption.TLabel").pack(anchor="w", pady=(0, 10))
        rows = (
            ("cash_collected", "Cash collected", True),
            ("cash_uncollected", "Cash still to collect", False),
            ("other_collected", "Venmo / other collected", False),
            ("other_uncollected", "Venmo / other still to collect", False),
            (None, None, False),
            ("total_collected", "Total collected", False),
            ("total_uncollected", "Total still to collect", False),
            ("book_value", "Full order value", False),
        )
        for key, label, emphasis in rows:
            if key is None:
                self._rule(card, pady=8)
                continue
            var = tk.StringVar()
            self.var_expected_rows[key] = var
            line = ttk.Frame(card, style="Card.TFrame")
            line.pack(fill="x", pady=3)
            ttk.Label(line, text=label,
                      style="CardStrong.TLabel" if emphasis else "CardHint.TLabel").pack(side="left")
            ttk.Label(line, textvariable=var,
                      style="CardFigure.TLabel" if emphasis else "CardFigureMuted.TLabel").pack(
                side="right"
            )
        ttk.Label(
            card,
            text="Only cash reaches the drawer. Venmo and other payments are "
                 "left out of the check.",
            style="CardHint.TLabel", wraplength=330,
        ).pack(anchor="w", pady=(14, 0))

    def _build_count(self) -> None:
        border, card = self.app.card(self, padding=(22, 18, 22, 18))
        border.grid(row=1, column=1, sticky="nsew", padx=(8, 0))
        ttk.Label(card, text="COUNT THE DRAWER", style="CardCaption.TLabel").pack(
            anchor="w"
        )
        ttk.Label(card, text="How many of each bill are you holding?",
                  style="CardHint.TLabel").pack(anchor="w", pady=(2, 10))
        grid = ttk.Frame(card, style="Card.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(2, weight=1)
        for row, denomination in enumerate(DENOMINATIONS):
            ttk.Label(grid, text=f"${denomination}", style="CardFigure.TLabel").grid(
                row=row, column=0, sticky="w", pady=3
            )
            var = tk.StringVar(value="")
            self.var_counts[denomination] = var
            ttk.Entry(grid, textvariable=var, style="Ticket.TEntry", width=6,
                      justify="right").grid(row=row, column=1, sticky="w",
                                            padx=(14, 10), pady=3)
            var.trace_add("write", lambda *_: self._recount())
            sub = tk.StringVar(value=format_money(Decimal("0.00")))
            self.var_subtotals[denomination] = sub
            ttk.Label(grid, textvariable=sub, style="CardFigureMuted.TLabel").grid(
                row=row, column=2, sticky="e", pady=3
            )
        self._rule(card, pady=10)
        total = ttk.Frame(card, style="Card.TFrame")
        total.pack(fill="x")
        ttk.Label(total, text="COUNTED", style="CardCaption.TLabel").pack(side="left")
        ttk.Label(total, textvariable=self.var_counted,
                  style="CardFigure.TLabel").pack(side="right")
        ttk.Button(card, text="Clear counts", style="Ghost.TButton",
                   command=self._clear).pack(anchor="e", pady=(12, 0))

    def reload(self) -> None:
        """Re-read the ledger; the typed counts are kept."""
        self.money = self.tracker.financials()
        for key, var in self.var_expected_rows.items():
            var.set(format_money(getattr(self.money, key)))
        self.var_expected.set(format_money(self.money.cash_collected))
        self._recount()

    def _clear(self) -> None:
        for var in self.var_counts.values():
            var.set("")

    def _verdict(self, text: str, style: str, difference: str = "—", note: str = "") -> None:
        self.var_verdict.set(text)
        self.verdict_label.configure(style=style)
        self.var_difference.set(difference)
        self.var_note.set(note)

    def _recount(self) -> None:
        raw = {d: v.get() for d, v in self.var_counts.items()}
        try:
            counted = count_cash(raw)
        except TrackerError as exc:
            self.var_counted.set("—")
            self._verdict(str(exc), "VerdictBad.TLabel")
            return
        for denomination, var in self.var_subtotals.items():
            text = str(raw.get(denomination, "")).strip()
            number = int(text) if text.isdigit() else 0
            var.set(format_money(Decimal(denomination) * number))
        self.var_counted.set(format_money(counted))
        if not any(str(v).strip() for v in raw.values()):
            self._verdict("Count your bills to check the drawer.", "VerdictIdle.TLabel")
            return
        result = reconcile(self.money.cash_collected, raw)
        gap = result.counted - result.expected
        sign = "+" if gap > 0 else "−" if gap < 0 else ""
        difference = f"{sign}{format_money(abs(gap))}"
        if result.balanced:
            self._verdict(result.headline, "VerdictGood.TLabel", difference)
        else:
            self._verdict(
                result.headline, "VerdictBad.TLabel", difference,
                "Check for a mislogged quantity, an order paid by Venmo but "
                "recorded as cash, or change given from the drawer.",
            )


class SalesApp(tk.Tk):
    """Main window: a sidebar of pages.

    Counter is where the operator works: a queue of who is waiting, each a
    card with a hand-over stepper. Details is the dense grid with every
    column, a command line for fast entry, sorting and filters.
    """

    COLUMNS = ("id", "purchaser", "product", "ordered", "received", "left",
               "owed", "status", "method", "logged")
    HEADINGS = {
        "id": ("#", 40, "e"),
        "purchaser": ("Purchaser", 150, "w"),
        "product": ("Product", 140, "w"),
        "ordered": ("Ordered", 90, "e"),
        "received": ("Received", 80, "e"),
        "left": ("Left", 60, "e"),
        "owed": ("Owed", 85, "e"),
        "status": ("Status", 100, "w"),
        "method": ("Paid by", 75, "w"),
        "logged": ("Logged", 75, "w"),
    }
    SORT_KEYS = {
        "id": lambda o: o.id,
        "purchaser": lambda o: o.purchaser.casefold(),
        "product": lambda o: o.product_name.casefold(),
        "ordered": lambda o: o.quantity_ordered,
        "received": lambda o: o.quantity_received,
        "left": lambda o: o.remaining,
        "owed": lambda o: o.uncollected,
        "status": lambda o: o.fulfilled,
        "method": lambda o: o.payment_method,
        "logged": lambda o: o.created_at,
    }
    PAGES = (
        ("counter", "Counter"),
        ("details", "Details"),
        ("buyers", "Buyers"),
        ("products", "Products"),
        ("money", "Money"),
    )
    HINTS = {
        "counter": "Hand over opens a stepper  ·  Enter records the figure  ·  "
                   "Ctrl+E edits the open order",
        "details": "↑↓ row  ·  + / − hand over  ·  Enter types received  ·  "
                   "a all  ·  e edit  ·  n log line  ·  / search",
        "buyers": "Double-click an order to open it on Details",
        "products": "Ctrl+N adds a product",
        "money": "Counts are never saved",
    }
    # Pips would be the natural control, but Tk buttons are heavy; the card
    # keeps a stepper, and the grid edits the figure in place.
    EVERY_PRODUCT = "Every product"
    SIDEBAR_WIDTH = 216

    def __init__(self, db_path: str | None = None, *, auto_setup: bool = True) -> None:
        super().__init__()
        self.tracker = SalesTracker(db_path)
        self.selected_order_id: int | None = None
        self._auto_setup = auto_setup
        self._flash_job: str | None = None
        self._theme_job: str | None = None
        # Plain Tk widgets hold their own colours; each is listed here with
        # the palette names it takes, and repainted from them on a switch.
        self._painted: list[tuple[tk.Misc, dict[str, str]]] = []
        self._bars: list[tk.Canvas] = []
        self._page = "counter"
        self._sort: tuple[str, bool] | None = None
        # Counter state: the one card whose stepper is open, its error, and
        # whether the Collected section is unfolded.
        self._open_card: int | None = None
        self._card_error = ""
        self._show_collected = False
        self._card_widgets: dict[int, dict[str, tk.Misc]] = {}
        self._refreshing = False
        # Details state: the in-place editor over the received cell.
        self._editor: ttk.Entry | None = None
        self.log_dialog: LogSaleDialog | None = None
        self.product_combo: ttk.Combobox | None = None
        self.method_combo: ttk.Combobox | None = None
        self.updates = UpdateController(self, self.tracker)

        # Paint before any widget is built, so nothing is created in the
        # outgoing theme's colours.
        self.theme_choice = theme.normalize_choice(
            self.tracker.get_setting(THEME_SETTING, theme.DEFAULT_CHOICE)
        )
        self.theme_painted = apply_palette(self.theme_choice)

        self.title("Sales Tracker")
        self.minsize(1180, 700)
        self.geometry("1260x800")
        self.configure(bg=BG)

        self._fonts()
        self._style()
        self._vars()
        self._build_menu()
        self._build()
        self._binds()
        self.show_page("counter")
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._watch_os_theme()
        if self._auto_setup:
            self.after(200, self._maybe_prompt_product)
            # A packaged build looks for a newer release once a day, quietly:
            # the answer is a line in the status bar, never a dialog.
            if update.target_path() is not None and self.updates.updater.due():
                self.after(2500, self.check_updates_quietly)

    # ------------------------------------------------------------------- theme

    def set_theme(self, choice: str) -> str:
        """Remember the operator's pick and repaint. Returns what it painted."""
        self.theme_choice = theme.normalize_choice(choice)
        self.tracker.set_setting(THEME_SETTING, self.theme_choice)
        painted = self._repaint()
        self._sync_theme_button()
        return painted

    def _cycle_theme(self) -> None:
        order = theme.THEME_CHOICES
        self.set_theme(order[(order.index(self.theme_choice) + 1) % len(order)])

    def _sync_theme_button(self) -> None:
        self.theme_button.configure(
            text=f"Appearance: {theme.THEME_LABELS[self.theme_choice]}"
        )

    def paint(self, widget: tk.Misc, **tokens: str) -> tk.Misc:
        """Colour a plain Tk widget from palette names, now and on every switch."""
        widget.configure(**{option: globals()[name] for option, name in tokens.items()})
        self._painted.append((widget, tokens))
        return widget

    def _repaint(self) -> str:
        self.theme_painted = apply_palette(self.theme_choice)
        self._style()
        self.configure(bg=BG)
        # ttk styles cover most of the app, but a handful of plain Tk widgets
        # and the row tags hold their own colours and have to be told again.
        self._tag_rows()
        for window in self.winfo_children():
            if isinstance(window, tk.Toplevel):
                window.configure(bg=PANEL)
                for rule in getattr(window, "_rules", ()):
                    rule.configure(bg=LINE)
            for canvas in _descendant_canvases(window):
                canvas.configure(bg=PANEL)
        # Registered widgets last, so a canvas that sits on a card or the page
        # rather than a dialog panel gets its own colour back.
        alive = []
        for widget, tokens in self._painted:
            if widget.winfo_exists():
                widget.configure(**{o: globals()[n] for o, n in tokens.items()})
                alive.append((widget, tokens))
        self._painted = alive
        self._sync_nav()
        self._draw_brand()
        self._tag_rows()
        self._render_counter()
        self._redraw_bars()
        _drop_combobox_popdowns(self)
        return self.theme_painted

    def _watch_os_theme(self) -> None:
        """Re-check the desktop theme while "System" is selected.

        Tk has no notification for this, so it is a poll, at the cadence in
        theme.OS_THEME_POLL_MS: a Windows check reads the registry, while the
        other platforms spawn a subprocess and are polled more gently.
        """
        if self.theme_choice == theme.SYSTEM:
            if theme.detect_os_theme() != self.theme_painted:
                self._repaint()
        self._theme_job = self.after(theme.OS_THEME_POLL_MS, self._watch_os_theme)

    # ------------------------------------------------------------------ chrome

    def _fonts(self) -> None:
        available = set(tkfont.families())

        def pick(*names: str, fallback: str = "TkDefaultFont") -> str:
            for name in names:
                if name in available:
                    return name
            return fallback

        body = pick("Inter", "Segoe UI", "Adwaita Sans", "Cantarell", "Noto Sans")
        figures = pick(
            "JetBrainsMono Nerd Font", "JetBrains Mono", "Cascadia Mono",
            "Source Code Pro", "Consolas", "Hack", "Liberation Mono",
        )

        # Names read by the dialogs; keep them.
        self.font_title = tkfont.Font(family=body, size=15, weight="bold")
        self.font_body = tkfont.Font(family=body, size=11)
        self.font_muted = tkfont.Font(family=body, size=10)
        self.font_label = tkfont.Font(family=body, size=9, weight="bold")
        self.font_button = tkfont.Font(family=body, size=11, weight="bold")

        self.font_row = tkfont.Font(family=body, size=11)
        self.font_figures = tkfont.Font(family=figures, size=11)
        self.font_stat = tkfont.Font(family=figures, size=17, weight="bold")

        self.font_page = tkfont.Font(family=body, size=22, weight="bold")
        self.font_brand = tkfont.Font(family=body, size=13, weight="bold")
        self.font_nav = tkfont.Font(family=body, size=11)
        self.font_nav_on = tkfont.Font(family=body, size=11, weight="bold")
        self.font_card_title = tkfont.Font(family=body, size=14, weight="bold")
        self.font_word = tkfont.Font(family=body, size=12)
        self.font_avatar = tkfont.Font(family=body, size=14, weight="bold")
        self.font_big_figure = tkfont.Font(family=figures, size=15, weight="bold")
        self.font_verdict = tkfont.Font(family=body, size=20, weight="bold")

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("App.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Bar.TFrame", background=PANEL)
        style.configure("Page.TFrame", background=PAGE)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Subtle.TFrame", background=SUBTLE)
        style.configure("Side.TFrame", background=SIDEBAR)
        style.configure("Nav.TFrame", background=SIDEBAR)
        style.configure("NavHover.TFrame", background=SIDEBAR_ACTIVE)
        style.configure("NavOn.TFrame", background=SIDEBAR_ACTIVE)

        # Dialogs (wizard, settings, editors) sit on PANEL.
        style.configure("Title.TLabel", background=BG, foreground=INK, font=self.font_title)
        style.configure("Section.TLabel", background=PANEL, foreground=INK, font=self.font_title)
        style.configure("Field.TLabel", background=PANEL, foreground=MUTED, font=self.font_label)
        style.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=self.font_muted)
        style.configure("Error.TLabel", background=PANEL, foreground=DANGER, font=self.font_muted)
        style.configure("Figure.TLabel", background=PANEL, foreground=INK,
                        font=self.font_figures)
        style.configure("FigureMuted.TLabel", background=PANEL, foreground=MUTED,
                        font=self.font_figures)
        style.configure("Good.TLabel", background=PANEL, foreground=ACCENT,
                        font=self.font_muted)

        # The main window: page, cards, sidebar.
        style.configure("PageTitle.TLabel", background=PAGE, foreground=INK,
                        font=self.font_page)
        style.configure("PageSub.TLabel", background=PAGE, foreground=MUTED,
                        font=self.font_muted)
        style.configure("PageOk.TLabel", background=PAGE, foreground=ACCENT,
                        font=self.font_muted)
        style.configure("PageHint.TLabel", background=PAGE, foreground=MUTED,
                        font=self.font_muted)
        style.configure("CardCaption.TLabel", background=CARD, foreground=MUTED,
                        font=self.font_label)
        style.configure("CardTitle.TLabel", background=CARD, foreground=INK,
                        font=self.font_card_title)
        style.configure("CardStrong.TLabel", background=CARD, foreground=INK,
                        font=self.font_button)
        style.configure("CardHint.TLabel", background=CARD, foreground=MUTED,
                        font=self.font_muted)
        style.configure("CardError.TLabel", background=CARD, foreground=DANGER,
                        font=self.font_muted)
        style.configure("CardFigure.TLabel", background=CARD, foreground=INK,
                        font=self.font_figures)
        style.configure("CardFigureMuted.TLabel", background=CARD, foreground=MUTED,
                        font=self.font_figures)
        style.configure("CardFigureBig.TLabel", background=CARD, foreground=INK,
                        font=self.font_big_figure)
        style.configure("CardFigureMutedBig.TLabel", background=CARD, foreground=MUTED,
                        font=self.font_big_figure)
        style.configure("CardGood.TLabel", background=CARD, foreground=ACCENT,
                        font=self.font_muted)
        style.configure("PageCaption.TLabel", background=PAGE, foreground=MUTED,
                        font=self.font_label)
        style.configure("Fold.TButton", background=PAGE, foreground=MUTED,
                        bordercolor=PAGE, focusthickness=3, focuscolor=FOCUS,
                        padding=(2, 4), font=self.font_label)
        style.map("Fold.TButton", background=[("active", PAGE)],
                  foreground=[("active", INK)])
        style.configure("Kpi.TLabel", background=CARD, foreground=INK, font=self.font_stat)
        style.configure("Word.TLabel", background=CARD, foreground=MUTED, font=self.font_word)
        style.configure("Placeholder.TLabel", background=FIELD, foreground=MUTED,
                        font=self.font_body)
        style.configure("Empty.TLabel", background=CARD, foreground=MUTED,
                        font=self.font_muted)
        style.configure("HeroTitle.TLabel", background=CARD, foreground=INK,
                        font=self.font_page)
        style.configure("Chip.TLabel", background=SUBTLE, foreground=MUTED,
                        font=self.font_label, padding=(8, 2))
        style.configure("PillWarn.TLabel", background=WARN_SOFT, foreground=WARN,
                        font=self.font_label, padding=(10, 3))
        style.configure("PillOk.TLabel", background=ACCENT_SOFT, foreground=ACCENT,
                        font=self.font_label, padding=(10, 3))
        style.configure("VerdictIdle.TLabel", background=CARD, foreground=MUTED,
                        font=self.font_verdict)
        style.configure("VerdictGood.TLabel", background=CARD, foreground=ACCENT,
                        font=self.font_verdict)
        style.configure("VerdictBad.TLabel", background=CARD, foreground=DANGER,
                        font=self.font_verdict)

        style.configure("Brand.TLabel", background=SIDEBAR, foreground=SIDEBAR_INK,
                        font=self.font_brand)
        style.configure("SideCaption.TLabel", background=SIDEBAR,
                        foreground=SIDEBAR_MUTED, font=self.font_label)
        for name, bg, fg, font in (
            ("Nav", SIDEBAR, SIDEBAR_MUTED, self.font_nav),
            ("NavHover", SIDEBAR_ACTIVE, SIDEBAR_INK, self.font_nav),
            ("NavOn", SIDEBAR_ACTIVE, SIDEBAR_INK, self.font_nav_on),
        ):
            style.configure(f"{name}.TLabel", background=bg, foreground=fg, font=font)
            style.configure(f"{name}Badge.TLabel", background=bg, foreground=fg,
                            font=self.font_label)

        for name in ("Ticket.TEntry", "Search.TEntry"):
            style.configure(
                name, fieldbackground=FIELD, foreground=INK, insertcolor=INK,
                bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                padding=6, font=self.font_body,
            )
            style.map(
                name,
                bordercolor=[("focus", FOCUS)],
                lightcolor=[("focus", FOCUS)],
                darkcolor=[("focus", FOCUS)],
            )
        # A rejected figure carries its own state, in the box itself.
        style.configure(
            "Bad.TEntry", fieldbackground=FIELD, foreground=DANGER, insertcolor=DANGER,
            bordercolor=DANGER, lightcolor=DANGER, darkcolor=DANGER,
            padding=6, font=self.font_body,
        )

        # The arrow button draws from `background`; without it an editable
        # combobox keeps clam's light grey arrow in dark mode.
        style.configure("Ticket.TCombobox", fieldbackground=FIELD, foreground=INK,
                        background=FIELD, arrowcolor=INK,
                        bordercolor=LINE, padding=5, font=self.font_body)
        # A readonly combobox draws from its state map, not from configure, so
        # without this it keeps clam's default grey in dark mode.
        style.map(
            "Ticket.TCombobox",
            fieldbackground=[("readonly", FIELD), ("disabled", PANEL)],
            background=[("readonly", FIELD), ("active", FIELD)],
            foreground=[("readonly", INK), ("disabled", MUTED)],
            selectbackground=[("readonly", FIELD)],
            selectforeground=[("readonly", INK)],
            arrowcolor=[("disabled", MUTED), ("!disabled", INK)],
            bordercolor=[("focus", FOCUS)],
            lightcolor=[("focus", FOCUS), ("!focus", LINE)],
            darkcolor=[("focus", FOCUS), ("!focus", LINE)],
        )
        # The dropdown itself is a plain Tk listbox that ttk cannot reach.
        self.option_add("*TCombobox*Listbox.background", FIELD)
        self.option_add("*TCombobox*Listbox.foreground", INK)
        self.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        self.option_add("*TCombobox*Listbox.selectForeground", INK)

        style.configure("Primary.TButton", background=ACCENT, foreground=ON_ACCENT,
                        bordercolor=ACCENT, focusthickness=3, focuscolor=ACCENT_SOFT,
                        padding=(14, 8), font=self.font_button)
        style.map("Primary.TButton",
                  background=[("active", ACCENT_DARK), ("disabled", DISABLED)],
                  foreground=[("disabled", ON_ACCENT)])

        style.configure("Big.TButton", background=ACCENT, foreground=ON_ACCENT,
                        bordercolor=ACCENT, focusthickness=3, focuscolor=ACCENT_SOFT,
                        padding=(20, 11), font=self.font_card_title)
        style.map("Big.TButton", background=[("active", ACCENT_DARK)])
        style.configure("SubtleStep.TButton", background=CARD, foreground=INK,
                        bordercolor=LINE, focusthickness=3, focuscolor=FOCUS,
                        padding=(12, 8), font=self.font_card_title, width=3)
        style.map("SubtleStep.TButton", background=[("active", SUBTLE)],
                  foreground=[("disabled", DISABLED)])
        style.configure("CounterHint.TLabel", background=SUBTLE, foreground=MUTED,
                        font=self.font_muted)
        style.configure("CounterError.TLabel", background=SUBTLE, foreground=DANGER,
                        font=self.font_muted)
        style.configure("Ghost.TButton", background=PANEL, foreground=INK,
                        bordercolor=LINE, focusthickness=3, focuscolor=FOCUS,
                        padding=(11, 7), font=self.font_body)
        style.map("Ghost.TButton", background=[("active", SUBTLE)],
                  foreground=[("disabled", MUTED)])
        style.configure("Step.TButton", background=CARD, foreground=INK,
                        bordercolor=LINE, focusthickness=3, focuscolor=FOCUS,
                        padding=(10, 6), font=self.font_button, width=3)
        style.map("Step.TButton", background=[("active", SUBTLE)],
                  foreground=[("disabled", DISABLED)])

        style.configure("Danger.TButton", background=PANEL, foreground=DANGER,
                        bordercolor=LINE, focusthickness=3, focuscolor=DANGER,
                        padding=(11, 7), font=self.font_body)
        style.map("Danger.TButton", background=[("active", DANGER_SOFT)])

        style.configure("SidePrimary.TButton", background=SIDEBAR_ACCENT,
                        foreground=SIDEBAR, bordercolor=SIDEBAR_ACCENT,
                        focusthickness=2, focuscolor=SIDEBAR_INK,
                        padding=(12, 8), font=self.font_button)
        style.map("SidePrimary.TButton", background=[("active", SIDEBAR_INK)])
        style.configure("Side.TButton", background=SIDEBAR, foreground=SIDEBAR_INK,
                        bordercolor=SIDEBAR_ACTIVE, focusthickness=2,
                        focuscolor=SIDEBAR_ACCENT, padding=(12, 7), font=self.font_body)
        style.map("Side.TButton", background=[("active", SIDEBAR_ACTIVE)])
        style.configure("SideLink.TButton", background=SIDEBAR,
                        foreground=SIDEBAR_MUTED, bordercolor=SIDEBAR,
                        focusthickness=2, focuscolor=SIDEBAR_ACCENT,
                        padding=(4, 4), font=self.font_muted)
        style.map("SideLink.TButton", background=[("active", SIDEBAR)],
                  foreground=[("active", SIDEBAR_INK)])

        style.configure("Panel.TRadiobutton", background=PANEL, foreground=INK,
                        font=self.font_muted, focuscolor=FOCUS)
        style.map("Panel.TRadiobutton", background=[("active", PANEL)])

        # A radio button drawn as a pill: no indicator, the fill is the state.
        style.layout("Pill.TRadiobutton", [
            ("Radiobutton.border", {"sticky": "nswe", "children": [
                ("Radiobutton.padding", {"sticky": "nswe", "children": [
                    ("Radiobutton.label", {"sticky": "nswe"}),
                ]}),
            ]}),
        ])
        style.configure("Pill.TRadiobutton", background=CARD, foreground=MUTED,
                        bordercolor=LINE, lightcolor=CARD, darkcolor=CARD,
                        relief="solid", borderwidth=1, padding=(12, 5),
                        font=self.font_label, focuscolor=FOCUS, anchor="center")
        style.map("Pill.TRadiobutton",
                  background=[("selected", ACCENT_SOFT), ("active", SUBTLE)],
                  foreground=[("selected", ACCENT), ("active", INK)],
                  bordercolor=[("selected", ACCENT)],
                  lightcolor=[("selected", ACCENT_SOFT), ("active", SUBTLE)],
                  darkcolor=[("selected", ACCENT_SOFT), ("active", SUBTLE)])

        for name, surface in (("Ledger", FIELD), ("List", CARD)):
            style.configure(f"{name}.Treeview", background=surface,
                            fieldbackground=surface, foreground=INK,
                            rowheight=38 if name == "List" else 34,
                            font=self.font_row, bordercolor=surface,
                            lightcolor=surface, darkcolor=surface)
            style.configure(f"{name}.Treeview.Heading", background=surface,
                            foreground=MUTED, font=self.font_label, relief="flat",
                            bordercolor=LINE, lightcolor=surface, darkcolor=LINE,
                            padding=(8, 9))
            style.map(f"{name}.Treeview.Heading",
                      background=[("active", SUBTLE)], foreground=[("active", INK)])
            style.map(f"{name}.Treeview",
                      background=[("selected", ACCENT_SOFT)],
                      foreground=[("selected", INK)])

        style.configure("Ledger.Vertical.TScrollbar", background=PANEL,
                        troughcolor=SUBTLE, bordercolor=LINE, arrowcolor=MUTED)
        style.configure("Page.Vertical.TScrollbar", background=CARD,
                        troughcolor=PAGE, bordercolor=PAGE, arrowcolor=MUTED,
                        lightcolor=CARD, darkcolor=CARD)
        # clam paints a scrollbar with nothing to scroll in its own light grey
        # "disabled" colour, which glared on a dark page.
        style.map("Page.Vertical.TScrollbar",
                  background=[("disabled", PAGE), ("active", SUBTLE)],
                  arrowcolor=[("disabled", PAGE)])
        style.map("Ledger.Vertical.TScrollbar",
                  background=[("disabled", PANEL), ("active", SUBTLE)])

    def _vars(self) -> None:
        self.var_purchaser = tk.StringVar()
        self.var_product = tk.StringVar()
        self.var_qty = tk.StringVar()
        self.var_qty_label = tk.StringVar(value="of")
        # Holds the display form ("Cash"); parse_payment_method lowercases it
        # again on the way into the ledger.
        self.var_method = tk.StringVar(value=format_payment_method(CASH))
        self.var_error = tk.StringVar()
        self.var_ok = tk.StringVar()
        self.var_notice = tk.StringVar()
        self.var_hint = tk.StringVar()
        self.var_search = tk.StringVar()
        self.var_filter = tk.StringVar(value="all")
        self.var_count = tk.StringVar(value="0")
        self.var_outstanding = tk.StringVar(value="0")
        self.var_received = tk.StringVar(value="0")
        self.var_due = tk.StringVar(value="0")
        self.var_got = tk.StringVar()
        self.var_buyer_search = tk.StringVar()
        self.var_subs = {key: tk.StringVar() for key, _title in self.PAGES}
        # Details page: the command line, its preview, the product filter,
        # and the status line under the grid.
        self.var_cmd = tk.StringVar()
        self.var_cmd_preview = tk.StringVar(
            value="Type a purchaser, quantity, product and paid-by separated "
                  "by commas, then press Enter."
        )
        self.var_product_filter = tk.StringVar(value=self.EVERY_PRODUCT)
        self.var_status = tk.StringVar()
        self.var_cmd.trace_add("write", lambda *_: self._preview_cmd())
        self.var_search.trace_add("write", lambda *_: self.refresh())
        self.var_buyer_search.trace_add("write", lambda *_: self._render_buyers())
        self.var_product.trace_add("write", lambda *_: self._sync_qty_label())

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        ledger = tk.Menu(menu, tearoff=0)
        ledger.add_command(label="New product…", accelerator="Ctrl+N",
                           command=self.open_wizard)
        ledger.add_command(label="Edit selected order…", accelerator="Ctrl+E",
                           command=self.open_order_editor)
        ledger.add_command(label="Edit product…", command=self.open_product_editor)
        ledger.add_command(label="Export CSV…", command=self.export_csv)
        ledger.add_command(label="Settings…", accelerator="Ctrl+,",
                           command=self.open_settings)
        ledger.add_separator()
        ledger.add_command(label="Quit", command=self._on_close)
        menu.add_cascade(label="Ledger", menu=ledger)
        view = tk.Menu(menu, tearoff=0)
        for number, (key, title) in enumerate(self.PAGES, start=1):
            view.add_command(label=title, accelerator=f"Ctrl+{number}",
                             command=lambda k=key: self.show_page(k))
        menu.add_cascade(label="View", menu=view)
        self.config(menu=menu)

    def card(self, parent: tk.Misc, padding=18) -> tuple[tk.Frame, ttk.Frame]:
        """A white surface with a hairline border. Returns (outer, inner)."""
        border = tk.Frame(parent, bd=0, highlightthickness=0)
        self.paint(border, bg="LINE")
        inner = ttk.Frame(border, style="Card.TFrame", padding=padding)
        inner.pack(fill="both", expand=True, padx=1, pady=1)
        return border, inner

    def _hairline(self, parent: tk.Misc, **pack) -> tk.Frame:
        rule = tk.Frame(parent, height=1)
        self.paint(rule, bg="LINE")
        rule.pack(fill="x", **pack)
        return rule

    def _build(self) -> None:
        self._build_sidebar()
        self.content = ttk.Frame(self, style="Page.TFrame")
        self.content.pack(side="left", fill="both", expand=True)
        # Packed before the pages, so it keeps its strip at any window height.
        self._build_statusbar()
        self.pages = {
            "counter": self._build_counter_page(),
            "details": self._build_details_page(),
            "buyers": self._build_buyers_page(),
            "products": self._build_products_page(),
            "money": self._build_money_page(),
        }

    # ---------------------------------------------------------------- sidebar

    def _build_sidebar(self) -> None:
        side = ttk.Frame(self, style="Side.TFrame", width=self.SIDEBAR_WIDTH)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        brand = ttk.Frame(side, style="Side.TFrame", padding=(20, 22, 20, 28))
        brand.pack(fill="x")
        self.brand_mark = tk.Canvas(brand, width=32, height=32, bd=0,
                                    highlightthickness=0)
        self.paint(self.brand_mark, bg="SIDEBAR")
        self.brand_mark.pack(side="left")
        ttk.Label(brand, text="Sales Tracker", style="Brand.TLabel").pack(
            side="left", padx=(10, 0)
        )
        self._draw_brand()

        ttk.Label(side, text="LEDGER", style="SideCaption.TLabel").pack(
            anchor="w", padx=24, pady=(0, 8)
        )
        self._nav: dict[str, dict[str, tk.Misc]] = {}
        self._nav_hover: str | None = None
        for key, title in self.PAGES:
            self._nav[key] = self._nav_row(side, key, title)

        bottom = ttk.Frame(side, style="Side.TFrame", padding=(16, 0, 16, 18))
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="New product", style="SidePrimary.TButton",
                   command=self.open_wizard).pack(fill="x")
        ttk.Button(bottom, text="Export CSV", style="Side.TButton",
                   command=self.export_csv).pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="Settings", style="Side.TButton",
                   command=self.open_settings).pack(fill="x", pady=(8, 0))
        self.theme_button = ttk.Button(bottom, style="SideLink.TButton",
                                       command=self._cycle_theme)
        self.theme_button.pack(fill="x", pady=(14, 0))
        self._sync_theme_button()

    def _nav_row(self, parent: tk.Misc, key: str, title: str) -> dict[str, tk.Misc]:
        row = ttk.Frame(parent, style="Nav.TFrame")
        row.pack(fill="x", padx=10, pady=1)
        strip = tk.Frame(row, width=3)
        strip.pack(side="left", fill="y")
        name = ttk.Label(row, text=title, style="Nav.TLabel", padding=(14, 9, 0, 9))
        name.pack(side="left")
        badge = ttk.Label(row, text="", style="NavBadge.TLabel", padding=(0, 0, 14, 0))
        badge.pack(side="right")
        parts = {"row": row, "strip": strip, "name": name, "badge": badge}
        for widget in parts.values():
            widget.bind("<Button-1>", lambda _e, k=key: self.show_page(k))
            widget.bind("<Enter>", lambda _e, k=key: self._hover_nav(k))
            widget.bind("<Leave>", lambda _e: self._hover_nav(None))
        return parts

    def _hover_nav(self, key: str | None) -> None:
        self._nav_hover = key
        self._sync_nav()

    def _sync_nav(self) -> None:
        for key, parts in self._nav.items():
            state = "NavOn" if key == self._page else (
                "NavHover" if key == self._nav_hover else "Nav"
            )
            parts["row"].configure(style=f"{state}.TFrame")
            parts["name"].configure(style=f"{state}.TLabel")
            parts["badge"].configure(style=f"{state}Badge.TLabel")
            parts["strip"].configure(
                bg=SIDEBAR_ACCENT if key == self._page else
                SIDEBAR_ACTIVE if key == self._nav_hover else SIDEBAR
            )

    def _draw_brand(self) -> None:
        mark = self.brand_mark
        mark.delete("all")
        mark.configure(bg=SIDEBAR)
        mark.create_oval(1, 1, 31, 31, fill=SIDEBAR_ACCENT, outline="")
        mark.create_text(16, 16, text="$", fill=SIDEBAR, font=self.font_brand)

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self.content, style="Page.TFrame", padding=(28, 4, 28, 10))
        bar.pack(side="bottom", fill="x")
        ttk.Label(bar, textvariable=self.var_ok, style="PageOk.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self.var_hint, style="PageHint.TLabel").pack(
            side="right"
        )
        notice = ttk.Label(bar, textvariable=self.var_notice, style="PageOk.TLabel",
                           cursor="hand2")
        notice.pack(side="right", padx=(0, 18))
        notice.bind("<Button-1>", lambda _e: self.open_settings())

    def _page_frame(self, key: str, title: str) -> tuple[ttk.Frame, ttk.Frame]:
        page = ttk.Frame(self.content, style="Page.TFrame", padding=(28, 22, 28, 6))
        head = ttk.Frame(page, style="Page.TFrame")
        head.pack(fill="x", pady=(0, 18))
        text = ttk.Frame(head, style="Page.TFrame")
        text.pack(side="left", anchor="s")
        ttk.Label(text, text=title, style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(text, textvariable=self.var_subs[key], style="PageSub.TLabel").pack(
            anchor="w"
        )
        return page, head

    def show_page(self, key: str) -> None:
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self._page = key
        self.var_hint.set(self.HINTS[key])
        self._sync_nav()
        self._render_page(key)

    def _render_page(self, key: str) -> None:
        if key == "counter":
            self._render_counter()
        elif key == "buyers":
            self._render_buyers()
        elif key == "products":
            self._render_products()
        elif key == "money":
            self.money_panel.reload()

    # ---------------------------------------------------------- counter page

    def _build_counter_page(self) -> ttk.Frame:
        page, head = self._page_frame("counter", "Counter")
        self.log_button = ttk.Button(head, text="Log a sale", style="Big.TButton",
                                     command=self.open_log_dialog)
        self.log_button.pack(side="right", anchor="s")
        self._build_welcome(page)
        # Everything below the welcome card; hidden until a product exists.
        self.order_form = ttk.Frame(page, style="Page.TFrame")
        tools = ttk.Frame(self.order_form, style="Page.TFrame")
        tools.pack(fill="x", pady=(0, 14))
        self.search_entry = ttk.Entry(tools, textvariable=self.var_search,
                                      style="Search.TEntry", font=self.font_word)
        self.search_entry.pack(fill="x", ipady=3)
        Placeholder(self.search_entry, self.var_search, "Find someone…")
        self.counter_body = self._scroll_area(self.order_form)
        return page

    def _build_welcome(self, page: ttk.Frame) -> None:
        # Shown instead of the queue until a product exists.
        border, hero = self.card(page, padding=(40, 36, 40, 40))
        self.need_product = border
        ttk.Label(hero, text="GET STARTED", style="CardCaption.TLabel").pack(anchor="w")
        ttk.Label(hero, text="Set up what you sell.", style="HeroTitle.TLabel").pack(
            anchor="w", pady=(6, 8)
        )
        ttk.Label(
            hero,
            text="A product is whatever you hand out: a jar of honey, a box of "
                 "candles, a pound of coffee. Once one exists you can log who "
                 "bought how many, and tick them off as they collect.",
            style="CardHint.TLabel", wraplength=560,
        ).pack(anchor="w")
        steps = ttk.Frame(hero, style="Card.TFrame")
        steps.pack(anchor="w", pady=(24, 26))
        for number, text in enumerate(
            ("Establish a product", "Log who bought it", "Hand it over"), start=1
        ):
            ttk.Label(steps, text=f" {number} ", style="PillOk.TLabel").pack(side="left")
            ttk.Label(steps, text=text, style="CardStrong.TLabel").pack(
                side="left", padx=(8, 22)
            )
        ttk.Button(hero, text="Establish a product", style="Primary.TButton",
                   command=self.open_wizard).pack(anchor="w")

    def _render_counter(self, focus: tuple[int, str] | None = None) -> None:
        """Rebuild the queue: Waiting, oldest first, then Collected folded."""
        if not hasattr(self, "counter_body"):
            return
        body = self.counter_body
        for child in body.winfo_children():
            child.destroy()
        self._painted = [(w, t) for w, t in self._painted if w.winfo_exists()]
        self._card_widgets = {}
        if not self.tracker.list_products():
            return
        search = self.var_search.get().strip() or None
        orders = self.tracker.list_orders(search=search)
        waiting = sorted((o for o in orders if not o.fulfilled), key=lambda o: o.created_at)
        done = [o for o in orders if o.fulfilled]
        if self._open_card is not None and all(o.id != self._open_card for o in orders):
            self._open_card = None

        if not orders:
            border, empty = self.card(body, padding=(28, 32, 28, 32))
            border.pack(fill="x")
            title = (f"Nothing matches “{search}”" if search else "No one is waiting")
            hint = ("Try a shorter name, or clear the search." if search else
                    "Log a sale and the person appears here until everything is handed over.")
            ttk.Label(empty, text=title, style="CardTitle.TLabel").pack()
            ttk.Label(empty, text=hint, style="CardHint.TLabel", wraplength=420,
                      justify="center").pack(pady=(6, 0))
        else:
            head = ttk.Frame(body, style="Page.TFrame")
            head.pack(fill="x", pady=(0, 8))
            ttk.Label(head, text=f"WAITING  ·  {len(waiting)}",
                      style="PageCaption.TLabel").pack(side="left")
            ttk.Label(head, text="Oldest first", style="PageHint.TLabel").pack(side="right")
            if waiting:
                for order in waiting:
                    self._render_card(body, order)
            else:
                ttk.Label(body, text="Everyone has collected. Nice.",
                          style="PageSub.TLabel").pack(anchor="w", pady=(4, 12))
            if done:
                arrow = "▾" if self._show_collected else "▸"
                toggle = ttk.Button(
                    body, text=f"{arrow}  COLLECTED  ·  {len(done)}",
                    style="SideLink.TButton", command=self._toggle_collected,
                )
                toggle.configure(style="Fold.TButton")
                toggle.pack(anchor="w", pady=(10, 8))
                if self._show_collected:
                    for order in done:
                        self._render_card(body, order)
        self._tag_scroll(body, body.scroll_tag)
        if focus and focus[0] in self._card_widgets:
            widget = self._card_widgets[focus[0]].get(focus[1])
            if widget is not None:
                widget.focus_set()
                if isinstance(widget, ttk.Entry):
                    widget.select_range(0, "end")

    def _toggle_collected(self) -> None:
        self._show_collected = not self._show_collected
        self._render_counter()

    def _render_card(self, body: ttk.Frame, order: Order) -> None:
        border, card = self.card(body, padding=(18, 14, 18, 14))
        border.pack(fill="x", pady=(0, 10))
        widgets: dict[str, tk.Misc] = {}
        self._card_widgets[order.id] = widgets
        unit = "" if order.product_unit == "each" else f" {order.product_unit}"

        top = ttk.Frame(card, style="Card.TFrame")
        top.pack(fill="x")
        text = ttk.Frame(top, style="Card.TFrame")
        text.pack(side="left", fill="x", expand=True)
        ttk.Label(text, text=order.purchaser, style="CardTitle.TLabel").pack(anchor="w")
        meta = (f"{order.product_name}  ·  paid by "
                f"{format_payment_method(order.payment_method)}  ·  "
                f"{friendly_stamp(order.created_at)}")
        if not order.fulfilled:
            meta += f"  ·  {format_money(order.uncollected)} still owed"
        ttk.Label(text, text=meta, style="CardHint.TLabel", wraplength=520).pack(anchor="w")
        count = ttk.Frame(top, style="Card.TFrame")
        count.pack(side="right", anchor="n")
        figures = ttk.Frame(count, style="Card.TFrame")
        figures.pack(anchor="e")
        ttk.Label(figures, text=format_qty(order.quantity_received),
                  style="CardFigureBig.TLabel").pack(side="left")
        ttk.Label(figures, text=f" / {format_qty(order.quantity_ordered)}",
                  style="CardFigureMutedBig.TLabel").pack(side="left")
        hint = (f"All {format_qty(order.quantity_ordered)}{unit} handed over"
                if order.fulfilled else
                f"{format_qty(order.remaining)}{unit} still to hand over")
        ttk.Label(count, text=hint, style="CardHint.TLabel").pack(anchor="e")

        bar = tk.Canvas(card, height=8, bd=0, highlightthickness=0)
        self.paint(bar, bg="CARD")
        bar.ratio = float(order.quantity_received / order.quantity_ordered)
        bar.pack(fill="x", pady=(10, 12))
        bar.bind("<Configure>", lambda _e, c=bar: self._draw_bar(c))
        self._bars.append(bar)

        if self._open_card == order.id:
            box = ttk.Frame(card, style="Subtle.TFrame", padding=12)
            box.pack(fill="x")
            row = ttk.Frame(box, style="Subtle.TFrame")
            row.pack(fill="x")
            minus = ttk.Button(row, text="−", style="SubtleStep.TButton",
                               command=lambda: self.step_received(-1))
            minus.pack(side="left")
            entry = ttk.Entry(row, textvariable=self.var_got, style="Ticket.TEntry",
                              width=6, justify="center", font=self.font_big_figure)
            entry.pack(side="left", padx=8, ipady=2)
            entry.bind("<Return>", lambda _e: self.update_received())
            entry.bind("<Escape>", lambda _e: self.close_card())
            plus = ttk.Button(row, text="+", style="SubtleStep.TButton",
                              command=lambda: self.step_received(1))
            plus.pack(side="left")
            all_btn = ttk.Button(row, text=f"All {format_qty(order.quantity_ordered)}",
                                 style="Primary.TButton", command=self.mark_all_received)
            all_btn.pack(side="left", padx=(10, 0))
            ttk.Button(row, text="Done", style="Ghost.TButton",
                       command=self.close_card).pack(side="right")
            if order.quantity_received <= 0:
                minus.state(["disabled"])
            if order.fulfilled:
                plus.state(["disabled"])
                all_btn.state(["disabled"])
            error = ttk.Label(box, text=self._card_error, style="CounterError.TLabel",
                              wraplength=520)
            if self._card_error:
                error.pack(anchor="w", pady=(6, 0))
                entry.configure(style="Bad.TEntry")
            widgets.update(minus=minus, entry=entry, plus=plus, all=all_btn)
        else:
            actions = ttk.Frame(card, style="Card.TFrame")
            actions.pack(fill="x")
            label, style = ("Adjust", "Ghost.TButton") if order.fulfilled else \
                ("Hand over", "Primary.TButton")
            open_btn = ttk.Button(actions, text=label, style=style,
                                  command=lambda i=order.id: self.open_card(i))
            open_btn.pack(side="left")
            edit_btn = ttk.Button(actions, text="Edit", style="Ghost.TButton",
                                  command=lambda i=order.id: self._edit_card(i))
            edit_btn.pack(side="left", padx=(8, 0))
            widgets.update(open=open_btn, edit=edit_btn)

    def open_card(self, order_id: int) -> None:
        """Unfold the hand-over stepper on one card; only one is open at a time."""
        try:
            order = self.tracker.get_order(order_id)
        except TrackerError:
            return
        self._open_card = order_id
        self.selected_order_id = order_id
        self._card_error = ""
        self.var_got.set(format_qty(order.quantity_received))
        self._render_counter(focus=(order_id, "plus"))

    def close_card(self) -> None:
        closing = self._open_card
        self._open_card = None
        self._card_error = ""
        self._render_counter(focus=(closing, "open") if closing is not None else None)

    def _edit_card(self, order_id: int) -> None:
        self.selected_order_id = order_id
        self.open_order_editor()

    # ---------------------------------------------------------- details page

    def _build_details_page(self) -> ttk.Frame:
        page, _head = self._page_frame("details", "Details")

        border, card = self.card(page, padding=(14, 12, 14, 10))
        border.pack(fill="x", pady=(0, 12))
        line = ttk.Frame(card, style="Card.TFrame")
        line.pack(fill="x")
        ttk.Label(line, text="LOG", style="CardCaption.TLabel").pack(side="left", padx=(0, 10))
        self.cmd_entry = ttk.Entry(line, textvariable=self.var_cmd, style="Ticket.TEntry",
                                   font=self.font_figures)
        self.cmd_entry.pack(side="left", fill="x", expand=True, ipady=1)
        self.cmd_entry.bind("<Return>", lambda _e: self.run_cmd())
        self.cmd_entry.bind("<Escape>", lambda _e: self.var_cmd.set(""))
        Placeholder(self.cmd_entry, self.var_cmd,
                    "Jim Carter, 10, Honey, venmo   (product and paid-by optional)")
        ttk.Button(line, text="Log", style="Primary.TButton",
                   command=self.run_cmd).pack(side="left", padx=(10, 0))
        self.cmd_preview = ttk.Label(card, textvariable=self.var_cmd_preview,
                                     style="CardHint.TLabel", font=self.font_figures)
        self.cmd_preview.pack(anchor="w", pady=(6, 0))

        wrap = ttk.Frame(page, style="Page.TFrame")
        wrap.pack(fill="both", expand=True)
        border, card = self.card(wrap, padding=0)
        border.pack(fill="both", expand=True)
        bar = ttk.Frame(card, style="Card.TFrame", padding=(14, 12, 14, 12))
        bar.pack(fill="x")
        self._pills: dict[str, ttk.Radiobutton] = {}
        for value in ("all", "outstanding", "received"):
            pill = ttk.Radiobutton(bar, value=value, variable=self.var_filter,
                                   style="Pill.TRadiobutton", command=self.refresh,
                                   takefocus=True)
            pill.pack(side="left", padx=(0, 6))
            self._pills[value] = pill
        self.product_filter = ttk.Combobox(
            bar, textvariable=self.var_product_filter, state="readonly",
            style="Ticket.TCombobox", width=16,
        )
        self.product_filter.pack(side="left", padx=(12, 0))
        self.product_filter.bind("<<ComboboxSelected>>", lambda _e: self.refresh())
        self.details_search = ttk.Entry(bar, textvariable=self.var_search,
                                        style="Search.TEntry", width=26)
        self.details_search.pack(side="right")
        Placeholder(self.details_search, self.var_search, "Search  /")
        self._hairline(card)

        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=self.COLUMNS, show="headings",
                                 style="Ledger.Treeview", selectmode="browse")
        for key, (title, width, anchor) in self.HEADINGS.items():
            self.tree.heading(key, text=title, anchor=anchor,
                              command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview,
                               style="Page.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self._tag_rows()
        self._fit_columns(self.tree, {k: v[1] for k, v in self.HEADINGS.items()})
        bind_wheel_scroll(self.tree)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self.begin_edit)
        self.tree.bind("<Return>", self.begin_edit)
        for sequence in ("<plus>", "<equal>", "<KP_Add>"):
            self.tree.bind(sequence, lambda _e: self.step_received(1))
        for sequence in ("<minus>", "<KP_Subtract>"):
            self.tree.bind(sequence, lambda _e: self.step_received(-1))
        self.tree.bind("<Key-a>", lambda _e: self.mark_all_received())
        self.tree.bind("<Key-e>", lambda _e: self.open_order_editor())
        self.tree.bind("<Key-n>", lambda _e: self.cmd_entry.focus_set())
        self.tree.bind("<Key-slash>", lambda _e: self._focus_search())
        self.empty_label = ttk.Label(table, style="Empty.TLabel", justify="center")

        foot = ttk.Frame(card, style="Card.TFrame", padding=(14, 8, 14, 10))
        foot.pack(fill="x")
        ttk.Button(foot, text="All received", style="Ghost.TButton",
                   command=self.mark_all_received).pack(side="left")
        ttk.Button(foot, text="Edit order", style="Ghost.TButton",
                   command=self.open_order_editor).pack(side="left", padx=(8, 0))
        ttk.Label(foot, text="PAID BY", style="CardCaption.TLabel").pack(
            side="left", padx=(18, 6)
        )
        self.row_method = tk.StringVar()
        self.row_method_combo = ttk.Combobox(
            foot, textvariable=self.row_method, state="readonly",
            style="Ticket.TCombobox", width=7,
            values=[format_payment_method(m) for m in PAYMENT_METHODS],
        )
        self.row_method_combo.pack(side="left")
        self.row_method_combo.bind("<<ComboboxSelected>>", lambda _e: self._change_method())
        self.status_label = ttk.Label(foot, textvariable=self.var_status,
                                      style="CardHint.TLabel")
        self.status_label.pack(side="right")
        return page

    def _say(self, message: str, bad: bool = False) -> None:
        """The Details status line: what just happened, or why it did not."""
        self.var_status.set(message)
        self.status_label.configure(style="CardError.TLabel" if bad else "CardHint.TLabel")

    @staticmethod
    def _fit_columns(tree: ttk.Treeview, widths: dict[str, int]) -> None:
        """Share the list's width between its columns in fixed proportions.

        A Treeview does not shrink its columns to fit: narrower than their
        sum, it simply cuts off the last ones.
        """
        total = sum(widths.values())

        def fit(event: tk.Event) -> None:
            room = max(event.width - 4, 200)
            for key, width in widths.items():
                tree.column(key, width=int(width * room / total))

        tree.bind("<Configure>", fit, add="+")

    def _tag_rows(self) -> None:
        self.tree.tag_configure("done", background=RECEIVED_BG, foreground=MUTED)

    # The command line: "purchaser, quantity, product, paid by".

    def _parse_cmd(self) -> dict[str, str]:
        parts = [part.strip() for part in self.var_cmd.get().split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise TrackerError(
                "Need at least a purchaser and a quantity, separated by a comma."
            )
        return {
            "purchaser": parts[0], "quantity": parts[1],
            "product": parts[2] if len(parts) > 2 and parts[2] else None,
            "payment_method": parts[3] if len(parts) > 3 and parts[3] else CASH,
        }

    def _preview_cmd(self) -> None:
        if not hasattr(self, "cmd_preview"):
            return
        text = self.var_cmd.get().strip()
        style = "CardHint.TLabel"
        if not text:
            message = ("Type a purchaser, quantity, product and paid-by separated "
                       "by commas, then press Enter.")
        else:
            try:
                fields = self._parse_cmd()
                products = self.tracker.list_products()
                if fields["product"] is None:
                    if len(products) != 1:
                        raise TrackerError(
                            "Choose which product: "
                            + ", ".join(p.name for p in products)
                        )
                    product = products[0]
                else:
                    product = self.tracker.find_product(fields["product"])
                qty = parse_quantity(fields["quantity"])
                method = parse_payment_method(fields["payment_method"])
                message = (f"→ {fields['purchaser']}  ·  {format_qty(qty)} {product.unit} "
                           f"{product.name}  ·  {format_payment_method(method)}  ·  "
                           f"{format_money(qty * product.unit_price)}    Enter to log")
                style = "Good.TLabel"
            except TrackerError as exc:
                message = str(exc)
                style = "CardError.TLabel"
        self.var_cmd_preview.set(message)
        # Good.TLabel sits on PANEL; the preview sits on a card.
        self.cmd_preview.configure(
            style={"Good.TLabel": "CardGood.TLabel"}.get(style, style)
        )

    def run_cmd(self) -> None:
        try:
            fields = self._parse_cmd()
            order = self.tracker.add_order(**fields)
        except TrackerError as exc:
            self._say(str(exc), bad=True)
            return
        self.var_cmd.set("")
        self.var_filter.set("all")
        self.var_product_filter.set(self.EVERY_PRODUCT)
        self.var_search.set("")
        self._say(f"Logged #{order.id} {order.purchaser}  ·  "
                  f"{format_qty(order.quantity_ordered)} {order.product_unit} "
                  f"{order.product_name}  ·  {format_payment_method(order.payment_method)}")
        self._flash(f"Logged {order.purchaser}")
        self.refresh(select_id=order.id)

    BAR_CELLS = 6
    # Tk paints one foreground per row, so the bar must read monochrome.
    # U+25A0/U+25A1 are equal-width and present in Segoe UI for the Windows exe.
    BAR_FULL = "■"
    BAR_EMPTY = "□"

    @classmethod
    def _bar(cls, received: Decimal, ordered: Decimal) -> str:
        """Fixed-width text progress bar for the Buyers grid."""
        if ordered <= 0:
            return cls.BAR_EMPTY * cls.BAR_CELLS
        filled = int((received / ordered) * cls.BAR_CELLS)
        filled = max(0, min(cls.BAR_CELLS, filled))
        if filled == 0 and received > 0:
            filled = 1
        return cls.BAR_FULL * filled + cls.BAR_EMPTY * (cls.BAR_CELLS - filled)

    def _draw_bar(self, canvas: tk.Canvas) -> None:
        """A rounded progress bar sized to the canvas; ratio is kept on it."""
        canvas.delete("all")
        width = max(canvas.winfo_width(), 20)
        height = int(canvas.cget("height"))
        mid = height // 2
        radius = height // 2
        canvas.create_line(radius, mid, width - radius, mid, width=height,
                           capstyle="round", fill=LINE)
        ratio = max(0.0, min(1.0, getattr(canvas, "ratio", 0.0)))
        if ratio > 0:
            end = radius + (width - 2 * radius) * ratio
            canvas.create_line(radius, mid, max(end, radius + 1), mid, width=height,
                               capstyle="round", fill=ACCENT)

    def _redraw_bars(self) -> None:
        self._bars = [bar for bar in self._bars if bar.winfo_exists()]
        for bar in self._bars:
            self._draw_bar(bar)

    def step_received(self, delta: int) -> str:
        """Hand over one more (or take one back) on the selected order."""
        order_id = self._selected_id()
        if order_id is None:
            return "break"
        order = self.tracker.get_order(order_id)
        target = order.quantity_received + delta
        target = max(Decimal("0"), min(order.quantity_ordered, target))
        if target == order.quantity_received:
            return "break"
        self.var_got.set(format_qty(target))
        self.update_received(refocus="plus" if delta > 0 else "minus")
        return "break"

    def _change_method(self) -> None:
        order_id = self._selected_id()
        if order_id is None:
            self._say("Select a row first.", bad=True)
            return
        try:
            order = self.tracker.set_payment_method(order_id, self.row_method.get())
        except TrackerError as exc:
            self._say(str(exc), bad=True)
            return
        self._say(f"{order.purchaser} now paid by "
                  f"{format_payment_method(order.payment_method)}.")
        self.refresh(select_id=order.id)

    # ------------------------------------------------------------ buyers page

    def _build_buyers_page(self) -> ttk.Frame:
        page, head = self._page_frame("buyers", "Buyers")
        border, card = self.card(page, padding=0)
        border.pack(fill="both", expand=True)
        bar = ttk.Frame(card, style="Card.TFrame", padding=(14, 12, 14, 12))
        bar.pack(fill="x")
        ttk.Label(bar, text="Everyone who has bought, with what they still owe.",
                  style="CardHint.TLabel").pack(side="left")
        self.buyer_search_entry = ttk.Entry(bar, textvariable=self.var_buyer_search,
                                            style="Search.TEntry", width=28)
        self.buyer_search_entry.pack(side="right")
        Placeholder(self.buyer_search_entry, self.var_buyer_search, "Find a buyer")
        self._hairline(card)

        table = ttk.Frame(card, style="Card.TFrame")
        table.pack(fill="both", expand=True)
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ("product", "progress", "owed", "status", "method")
        headings = {
            "product": ("Orders", 150, "w"),
            "progress": ("Received / ordered", 170, "w"),
            "owed": ("Still owed", 95, "e"),
            "status": ("Status", 105, "w"),
            "method": ("Paid by", 80, "w"),
        }
        self.buyers_tree = ttk.Treeview(table, columns=columns, show="tree headings",
                                        style="List.Treeview", selectmode="browse")
        self.buyers_tree.heading("#0", text="Buyer", anchor="w")
        self.buyers_tree.column("#0", width=210, minwidth=120, stretch=True)
        for key in columns:
            title, width, anchor = headings[key]
            self.buyers_tree.heading(key, text=title, anchor=anchor)
            self.buyers_tree.column(key, width=width, minwidth=60, anchor=anchor,
                                    stretch=True)
        scroll = ttk.Scrollbar(table, orient="vertical",
                               command=self.buyers_tree.yview,
                               style="Page.Vertical.TScrollbar")
        self.buyers_tree.configure(yscrollcommand=scroll.set)
        self.buyers_tree.grid(row=0, column=0, sticky="nsew")
        self._fit_columns(self.buyers_tree, {
            "#0": 210, **{k: headings[k][1] for k in columns}
        })
        scroll.grid(row=0, column=1, sticky="ns")
        bind_wheel_scroll(self.buyers_tree)
        self.buyers_tree.bind("<Double-1>", self._open_from_buyers)
        self.buyers_tree.bind("<Return>", self._open_from_buyers)
        self.buyers_empty = ttk.Label(table, style="Empty.TLabel", justify="center")
        return page

    def _render_buyers(self) -> None:
        tree = self.buyers_tree
        tree.tag_configure("group", background=SUBTLE, font=self.font_button)
        self._tag_buyer_rows()
        tree.delete(*tree.get_children())
        needle = self.var_buyer_search.get().strip().casefold()
        groups: dict[str, list] = {}
        for order in self.tracker.list_orders():
            groups.setdefault(order.purchaser.casefold(), []).append(order)
        shown = 0
        for key in sorted(groups):
            orders = groups[key]
            name = orders[0].purchaser
            if needle and needle not in key:
                continue
            shown += 1
            owed = sum((o.uncollected for o in orders), Decimal("0"))
            waiting = sum(1 for o in orders if not o.fulfilled)
            parent = tree.insert(
                "", "end", iid=f"buyer:{key}", text=f"  {name}", open=True,
                tags=("group",),
                values=(
                    f"{len(orders)} order(s)",
                    f"{len(orders) - waiting} of {len(orders)} received",
                    format_money(owed) if owed else "—",
                    f"{waiting} outstanding" if waiting else "all received",
                    "",
                ),
            )
            for order in orders:
                done = order.fulfilled
                tree.insert(
                    parent, "end", iid=str(order.id), text="",
                    tags=("done",) if done else (),
                    values=(
                        order.product_name,
                        f"{self._bar(order.quantity_received, order.quantity_ordered)}  "
                        f"{format_qty(order.quantity_received)} / "
                        f"{format_qty(order.quantity_ordered)}",
                        "—" if done else format_money(order.uncollected),
                        "received" if done else "outstanding",
                        format_payment_method(order.payment_method),
                    ),
                )
        if shown:
            self.buyers_empty.place_forget()
        else:
            self.buyers_empty.configure(
                text="Nobody matches that name." if needle else
                "No buyers yet. Log a sale on the Orders page."
            )
            self.buyers_empty.place(relx=0.5, rely=0.4, anchor="center")
        self.var_subs["buyers"].set(
            f"{len(groups)} people  ·  "
            f"{format_money(self.tracker.financials().total_uncollected)} still owed"
        )

    def _tag_buyer_rows(self) -> None:
        self.buyers_tree.tag_configure("done", background=RECEIVED_BG, foreground=MUTED)

    def _open_from_buyers(self, _event: object = None) -> str:
        selection = self.buyers_tree.selection()
        if not selection or not selection[0].isdigit():
            return "break"
        order_id = int(selection[0])
        self.var_filter.set("all")
        self.var_product_filter.set(self.EVERY_PRODUCT)
        self.var_search.set("")
        self.show_page("details")
        self.refresh(select_id=order_id)
        self.tree.focus_set()
        return "break"

    # ---------------------------------------------------------- products page

    def _build_products_page(self) -> ttk.Frame:
        page, head = self._page_frame("products", "Products")
        ttk.Button(head, text="Add a product", style="Primary.TButton",
                   command=self.open_wizard).pack(side="right", anchor="s")
        self.products_body = self._scroll_area(page)
        return page

    def _scroll_area(self, page: ttk.Frame) -> ttk.Frame:
        """A vertically scrolling region on the page background."""
        shell = ttk.Frame(page, style="Page.TFrame")
        shell.pack(fill="both", expand=True)
        canvas = tk.Canvas(shell, bd=0, highlightthickness=0)
        self.paint(canvas, bg="PAGE")
        vsb = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview,
                            style="Page.Vertical.TScrollbar")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas, style="Page.TFrame")
        inner = canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(inner, width=e.width - 12))
        body.bind("<Configure>",
                  lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        # One class tag carries the wheel for every widget in the region, so
        # cards rebuilt later scroll as soon as they are tagged.
        tag = f"Scroll{id(canvas)}"
        handler = make_wheel_handler(canvas)
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind_class(tag, sequence, handler)
        body.scroll_tag = tag
        self._tag_scroll(canvas, tag)
        self._tag_scroll(body, tag)
        return body

    @staticmethod
    def _tag_scroll(widget: tk.Misc, tag: str) -> None:
        stack = [widget]
        while stack:
            node = stack.pop()
            if isinstance(node, SELF_SCROLLING):
                continue
            tags = node.bindtags()
            if tag not in tags:
                node.bindtags((tag, *tags))
            stack.extend(node.winfo_children())

    def _render_products(self) -> None:
        body = self.products_body
        for child in body.winfo_children():
            child.destroy()
        self._painted = [(w, t) for w, t in self._painted if w.winfo_exists()]
        products = self.tracker.list_products()
        orders = self.tracker.list_orders()
        columns = 3
        for column in range(columns):
            body.columnconfigure(column, weight=1, uniform="product")
        for index, product in enumerate(products):
            mine = [o for o in orders if o.product_id == product.id]
            border, card = self.card(body, padding=(20, 18, 20, 16))
            border.grid(row=index // columns, column=index % columns, sticky="nsew",
                        padx=(0 if index % columns == 0 else 8,
                              0 if index % columns == columns - 1 else 8),
                        pady=(0, 16))
            ttk.Label(card, text=product.name, style="CardTitle.TLabel",
                      wraplength=240).pack(anchor="w")
            ttk.Label(card, text=f"{format_money(product.unit_price)} per {product.unit}",
                      style="CardFigure.TLabel").pack(anchor="w", pady=(2, 0))
            chips = ttk.Frame(card, style="Card.TFrame")
            chips.pack(anchor="w", pady=(8, 0))
            if product.sku:
                ttk.Label(chips, text=f"SKU {product.sku}", style="Chip.TLabel").pack(
                    side="left", padx=(0, 6)
                )
            waiting = sum(1 for o in mine if not o.fulfilled)
            ttk.Label(
                chips,
                text=f"{waiting} waiting" if waiting else "nothing waiting",
                style="PillWarn.TLabel" if waiting else "PillOk.TLabel",
            ).pack(side="left")
            if product.notes:
                ttk.Label(card, text=product.notes, style="CardHint.TLabel",
                          wraplength=240).pack(anchor="w", pady=(8, 0))

            ordered = sum((o.quantity_ordered for o in mine), Decimal("0"))
            received = sum((o.quantity_received for o in mine), Decimal("0"))
            bar = tk.Canvas(card, height=8, bd=0, highlightthickness=0)
            self.paint(bar, bg="CARD")
            bar.ratio = float(received / ordered) if ordered else 0.0
            bar.pack(fill="x", pady=(14, 10))
            bar.bind("<Configure>", lambda _e, c=bar: self._draw_bar(c))
            self._bars.append(bar)

            stats = ttk.Frame(card, style="Card.TFrame")
            stats.pack(fill="x")
            owed = sum((o.uncollected for o in mine), Decimal("0"))
            for cell, (caption, value) in enumerate((
                ("ORDERS", str(len(mine))),
                ("SOLD", f"{format_qty(ordered)} {product.unit}"),
                ("HANDED OVER", format_qty(received)),
                ("STILL OWED", format_money(owed)),
            )):
                stats.columnconfigure(cell % 2, weight=1)
                ttk.Label(stats, text=caption, style="CardCaption.TLabel").grid(
                    row=(cell // 2) * 2, column=cell % 2, sticky="w",
                    pady=(6 if cell > 1 else 0, 0)
                )
                ttk.Label(stats, text=value, style="CardFigure.TLabel").grid(
                    row=(cell // 2) * 2 + 1, column=cell % 2, sticky="w"
                )

            actions = ttk.Frame(card, style="Card.TFrame")
            actions.pack(fill="x", pady=(14, 0))
            ttk.Button(actions, text="Edit", style="Ghost.TButton",
                       command=lambda pid=product.id: ProductEditor(
                           self, self.tracker, pid, on_saved=self._on_product_edited
                       )).pack(side="left")
            ttk.Button(actions, text="Log a sale", style="Ghost.TButton",
                       command=lambda name=product.name: self._sell(name)).pack(
                side="left", padx=(8, 0)
            )
        if not products:
            ttk.Label(body, text="No products yet. Add one to start logging sales.",
                      style="PageSub.TLabel").grid(row=0, column=0, columnspan=3,
                                                   pady=60)
        self._tag_scroll(body, body.scroll_tag)

    def _sell(self, product_name: str) -> None:
        self.show_page("orders")
        self.var_product.set(product_name)
        self.purchaser_entry.focus_set()

    # ------------------------------------------------------------- money page

    def _build_money_page(self) -> ttk.Frame:
        page, _head = self._page_frame("money", "Money")
        self.var_subs["money"].set(
            "What the ledger expects, against what is really in the drawer."
        )
        body = self._scroll_area(page)
        self.money_panel = MoneyPanel(body, self)
        self.money_panel.pack(fill="both", expand=True)
        self._tag_scroll(body, body.scroll_tag)
        return page

    # ---------------------------------------------------------------- binding

    def _binds(self) -> None:
        self.bind("<Control-s>", lambda _e: self.open_log_dialog())
        self.bind("<Control-n>", lambda _e: self.open_wizard())
        self.bind("<Control-comma>", lambda _e: self.open_settings())
        self.bind("<Control-e>", lambda _e: self.open_order_editor())
        self.bind("<Control-f>", lambda _e: self._focus_search())
        for number, (key, _title) in enumerate(self.PAGES, start=1):
            self.bind(f"<Control-Key-{number}>", lambda _e, k=key: self.show_page(k))

    def _focus_search(self) -> None:
        if self._page == "buyers":
            self.buyer_search_entry.focus_set()
        elif self._page == "details":
            self.details_search.focus_set()
        else:
            self.show_page("counter")
            self.search_entry.focus_set()

    # ------------------------------------------------------------- transitions

    def _flash(self, message: str) -> None:
        self.var_ok.set(message)
        if self._flash_job is not None:
            self.after_cancel(self._flash_job)
            self._flash_job = None
        if message:
            self._flash_job = self.after(5000, lambda: self.var_ok.set(""))

    def _maybe_prompt_product(self) -> None:
        if not self.tracker.list_products():
            self.open_wizard()

    def open_wizard(self) -> None:
        ProductWizard(self, self.tracker, on_saved=self._on_product_saved)

    def open_settings(self) -> None:
        SettingsDialog(self, self.tracker, on_change=self.refresh)

    def check_updates_quietly(self, *, sync: bool = False) -> None:
        """Look for a newer release; say so in the status bar if there is one."""
        def done(kind, value):
            if kind == "ok" and self.updates.available:
                self.var_notice.set(
                    f"Version {value.version} is available  \u00b7  Settings \u2192 Updates"
                )
        self.updates.check(done, sync=sync)

    def open_money(self) -> None:
        self.show_page("money")

    def open_log_dialog(self) -> LogSaleDialog | None:
        if not self.tracker.list_products():
            self.var_error.set("Establish a product first.")
            return None
        if self.log_dialog is not None and self.log_dialog.winfo_exists():
            self.log_dialog.lift()
            return self.log_dialog
        self.var_error.set("")
        self.log_dialog = LogSaleDialog(self, self.tracker)
        self.product_combo = self.log_dialog.product_combo
        self.method_combo = self.log_dialog.method_combo
        return self.log_dialog

    def open_order_editor(self) -> OrderEditor | None:
        order_id = self._selected_id()
        if order_id is None:
            message = "Select an order on the list first."
            self.var_error.set(message)
            if self._page == "details":
                self._say(message, bad=True)
            return None
        self.var_error.set("")
        return OrderEditor(self, self.tracker, order_id, on_saved=self._on_order_edited)

    def open_product_editor(self) -> ProductEditor | None:
        products = self.tracker.list_products()
        if not products:
            self.var_error.set("Establish a product first.")
            return None
        self.var_error.set("")
        # Start on the product the operator is most likely looking at: the
        # selected order's, then the one picked in the order form.
        start = products[0].id
        by_name = {product.name: product.id for product in products}
        order_id = self._selected_id()
        if order_id is not None:
            start = self.tracker.get_order(order_id).product_id
        elif self.var_product.get() in by_name:
            start = by_name[self.var_product.get()]
        return ProductEditor(self, self.tracker, start, on_saved=self._on_product_edited)

    def export_csv(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self, title="Export orders and totals",
            defaultextension=".csv", initialfile="sales.csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            written = self.tracker.export_csv(target)
        except (TrackerError, OSError) as exc:
            messagebox.showerror("Export", str(exc), parent=self)
            return
        count = len(self.tracker.list_orders())
        self._flash(f"Exported {count} order(s) to {written.name}")

    def _on_product_saved(self, product: Product) -> None:
        self.refresh(select_product=product.name)
        self.var_error.set("")
        self._flash(f"Added {product.name}")

    def _on_order_edited(self, order: Order) -> None:
        self.refresh(select_id=order.id)
        self._flash(f"Updated {order.purchaser}'s order")

    def _on_product_edited(self, product: Product, previous_name: str) -> None:
        # Keep the order form on a renamed product rather than letting it fall
        # back to whichever product sorts first.
        follow = product.name if self.var_product.get() == previous_name else None
        self.refresh(select_product=follow)
        self._flash(f"Updated {product.name}")

    def _sync_qty_label(self) -> None:
        name = self.var_product.get().strip()
        unit = "units"
        for product in self.tracker.list_products():
            if product.name == name:
                unit = product.unit
                break
        # "2 × Beeswax candle" reads better than "2 each of Beeswax candle".
        self.var_qty_label.set("×" if unit == "each" else f"{unit} of")

    def _reload_products(self, select_name: str | None = None) -> list[Product]:
        products = self.tracker.list_products()
        names = [product.name for product in products]
        if self.product_combo is not None and self.product_combo.winfo_exists():
            self.product_combo["values"] = names
        if select_name and select_name in names:
            self.var_product.set(select_name)
        elif names and self.var_product.get() not in names:
            self.var_product.set(names[0])
        self.product_filter["values"] = [self.EVERY_PRODUCT] + names
        if self.var_product_filter.get() not in self.product_filter["values"]:
            self.var_product_filter.set(self.EVERY_PRODUCT)
        if products:
            self.need_product.pack_forget()
            self.log_button.pack(side="right", anchor="s")
            self.order_form.pack(fill="both", expand=True)
        else:
            self.order_form.pack_forget()
            self.log_button.pack_forget()
            self.need_product.pack(fill="x")
            self.var_product.set("")
        self.cmd_entry.state(["!disabled"] if products else ["disabled"])
        self._sync_qty_label()
        return products

    # ------------------------------------------------------------------ orders

    def log_order(self) -> None:
        """Log a sale from the app's form variables (the Log a sale dialog)."""
        self.var_error.set("")
        try:
            order = self.tracker.add_order(
                purchaser=self.var_purchaser.get(),
                quantity=self.var_qty.get(),
                product=self.var_product.get() or None,
                payment_method=self.var_method.get().strip().lower() or CASH,
            )
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        self.var_purchaser.set("")
        self.var_qty.set("")
        if self.log_dialog is not None and self.log_dialog.winfo_exists():
            self.log_dialog.destroy()
        self._flash(
            f"Logged {order.purchaser} — {format_qty(order.quantity_ordered)} "
            f"{order.product_unit} of {order.product_name}"
        )
        self.var_search.set("")
        self.selected_order_id = order.id
        self.refresh(select_id=order.id)

    def _selected_id(self) -> int | None:
        """The order the operator is working on: the open card, or the grid row."""
        if self.selected_order_id is None:
            return None
        try:
            self.tracker.get_order(self.selected_order_id)
        except TrackerError:
            self.selected_order_id = None
        return self.selected_order_id

    def _on_select(self, _event: object = None) -> None:
        if self._refreshing:
            return
        selection = self.tree.selection()
        self.selected_order_id = int(selection[0]) if selection else None
        self._cancel_cell_editor()
        order = self._selected_id()
        if order is not None:
            self.row_method.set(format_payment_method(self.tracker.get_order(order).payment_method))

    def begin_edit(self, _event: object = None) -> str | None:
        """Type a received figure: the card's box on Counter, the cell on Details."""
        order_id = self._selected_id()
        if order_id is None:
            return None
        if self._page != "details":
            self.open_card(order_id)
            self._render_counter(focus=(order_id, "entry"))
            return "break"
        self._cancel_cell_editor()
        box = self.tree.bbox(str(order_id), "received")
        if not box:
            return None
        x, y, width, height = box
        self.var_got.set(format_qty(self.tracker.get_order(order_id).quantity_received))
        editor = ttk.Entry(self.tree, textvariable=self.var_got, style="Cell.TEntry",
                           font=self.font_figures, justify="right")
        editor.place(x=x + 2, y=y + 2, width=width - 4, height=height - 4)
        editor.focus_set()
        editor.select_range(0, "end")
        editor.bind("<Return>", lambda _e: self.update_received())
        editor.bind("<Escape>", lambda _e: self._cancel_cell_editor())
        self._editor = editor
        return "break"

    def _cancel_cell_editor(self) -> None:
        if self._editor is not None and self._editor.winfo_exists():
            self._editor.destroy()
        self._editor = None

    def update_received(self, refocus: str = "entry") -> None:
        """Commit ``var_got`` for the selected order.

        A rejected figure stays where it was typed with the reason beside
        it: on the open card, or in the grid's status line.
        """
        order_id = self._selected_id()
        if order_id is None:
            self.var_error.set("Select a purchaser on the list first.")
            return
        try:
            order = self.tracker.set_received(order_id, self.var_got.get())
        except TrackerError as exc:
            if self._open_card == order_id:
                self._card_error = str(exc)
                self._render_counter(focus=(order_id, "entry"))
            else:
                self._say(str(exc), bad=True)
                if self._editor is not None and self._editor.winfo_exists():
                    self._editor.configure(style="Bad.TEntry")
                    self._editor.focus_set()
                    self._editor.select_range(0, "end")
            return
        self.var_error.set("")
        self._card_error = ""
        message = (f"{order.purchaser} — recorded {format_qty(order.quantity_received)} "
                   f"of {format_qty(order.quantity_ordered)}")
        self._flash(message)
        self._say(message)
        self.refresh(select_id=order.id, focus=refocus)

    def mark_all_received(self) -> None:
        order_id = self._selected_id()
        if order_id is None:
            self._say("Select a row first.", bad=True)
            return
        try:
            order = self.tracker.mark_received(order_id)
        except TrackerError as exc:
            self._say(str(exc), bad=True)
            return
        self._card_error = ""
        self._flash(f"{order.purchaser} — marked fully received")
        self._say(f"{order.purchaser} marked fully received.")
        self.refresh(select_id=order.id, focus="all")

    # ------------------------------------------------------------------ render

    def sort_by(self, column: str) -> None:
        """Sort the grid on a column; a second click on it reverses."""
        if self._sort and self._sort[0] == column:
            self._sort = (column, not self._sort[1])
        else:
            self._sort = (column, False)
        self.refresh()

    def _sync_headings(self) -> None:
        for key, (title, _width, _anchor) in self.HEADINGS.items():
            arrow = ""
            if self._sort and self._sort[0] == key:
                arrow = "  ▼" if self._sort[1] else "  ▲"
            self.tree.heading(key, text=title + arrow)

    def refresh(
        self,
        select_id: int | None = None,
        select_product: str | None = None,
        focus: str | None = None,
    ) -> None:
        self._cancel_cell_editor()
        products = self._reload_products(select_product)

        search = self.var_search.get().strip() or None
        status = self.var_filter.get() or "all"
        try:
            orders = self.tracker.list_orders(search=search, status=status)
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        chosen = self.var_product_filter.get()
        if chosen and chosen != self.EVERY_PRODUCT:
            orders = [o for o in orders if o.product_name == chosen]
        if self._sort:
            column, reverse = self._sort
            orders.sort(key=self.SORT_KEYS[column], reverse=reverse)
        self._sync_headings()

        if select_id is not None:
            self.selected_order_id = select_id
        keep = self._selected_id()
        self._refreshing = True
        try:
            self.tree.delete(*self.tree.get_children())
            for order in orders:
                done = order.fulfilled
                self.tree.insert(
                    "", "end", iid=str(order.id),
                    values=(
                        order.id,
                        order.purchaser,
                        order.product_name,
                        f"{format_qty(order.quantity_ordered)} {order.product_unit}",
                        format_qty(order.quantity_received),
                        "—" if done else format_qty(order.remaining),
                        "—" if done else format_money(order.uncollected),
                        "received" if done else "outstanding",
                        format_payment_method(order.payment_method),
                        friendly_stamp(order.created_at).split(",")[0],
                    ),
                    tags=("done",) if done else (),
                )
            if keep is not None and self.tree.exists(str(keep)):
                self.tree.selection_set(str(keep))
                self.tree.see(str(keep))
            else:
                self.tree.selection_set(())
        finally:
            self._refreshing = False
        if keep is not None:
            self.row_method.set(
                format_payment_method(self.tracker.get_order(keep).payment_method)
            )
        else:
            self.row_method.set("")

        if orders:
            self.empty_label.place_forget()
        else:
            if not products:
                text = "No products yet. Establish one to get started."
            elif search or status != "all" or chosen != self.EVERY_PRODUCT:
                text = "Nothing matches this view."
            else:
                text = "No orders yet.\nLog one on the line above."
            self.empty_label.configure(text=text)
            self.empty_label.place(relx=0.5, rely=0.42, anchor="center")

        self._update_stats(products)
        card_focus = (keep, focus) if focus and keep is not None else None
        self._render_counter(focus=card_focus)
        if self._page not in ("counter", "details"):
            self._render_page(self._page)

    def _update_stats(self, products: list[Product]) -> None:
        summary = self.tracker.summary()
        money = self.tracker.financials()
        self.var_count.set(str(summary.order_count))
        self.var_outstanding.set(str(summary.outstanding_count))
        self.var_received.set(str(summary.received_count))
        self.var_due.set(format_qty(summary.units_remaining))
        for value, label, count in (
            ("all", "All", summary.order_count),
            ("outstanding", "Outstanding", summary.outstanding_count),
            ("received", "Received", summary.received_count),
        ):
            self._pills[value].configure(text=f"{label}   {count}")
        buyers = {o.purchaser.casefold() for o in self.tracker.list_orders()}
        for key, badge in (
            ("counter", summary.outstanding_count or ""),
            ("details", ""),
            ("buyers", len(buyers) or ""),
            ("products", len(products) or ""),
            ("money", ""),
        ):
            self._nav[key]["badge"].configure(text=str(badge))
        self.var_subs["counter"].set(
            f"{summary.outstanding_count} waiting  ·  "
            f"{format_money(money.total_uncollected)} still owed"
            if products else "Nothing to sell yet"
        )
        self.var_subs["details"].set(
            f"{summary.order_count} orders  ·  {format_qty(summary.units_remaining)} "
            f"units to hand out  ·  {format_money(money.total_uncollected)} owed  ·  "
            f"{format_money(money.cash_collected)} cash collected"
        )
        self.var_subs["products"].set(
            f"{len(products)} on file" if products else "Nothing on file yet"
        )

    def destroy(self) -> None:
        # Both pending timers have to go here rather than in _on_close: the
        # window can also be torn down directly, and a callback that fires
        # after the widgets are gone raises.
        for attr in ("_flash_job", "_theme_job"):
            job = getattr(self, attr, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, attr, None)
        super().destroy()

    def _on_close(self) -> None:
        self.tracker.close()
        self.destroy()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sales Tracker desktop ledger.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    args = parser.parse_args(argv)
    app = SalesApp(args.db)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
