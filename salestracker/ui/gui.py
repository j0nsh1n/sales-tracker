#!/usr/bin/env python3
"""Desktop ledger for Sales Tracker.

Design tokens (ledger / cash-register):
  bg        #E6EDE8   cool mint paper
  panel     #F7FBF8   ticket surface
  ink       #14201C   forest ink
  muted     #4A5C56
  accent    #0B6E4F   register green
  brass     #C9A227   stamped total rule
  danger    #A61B1B
  line      #C5D4CC
  display   Inter Display / Space Grotesk
  body      Inter
  figures   JetBrains Mono / Source Code Pro
"""

from __future__ import annotations

import argparse
import tkinter as tk
import tkinter.font as tkfont
from decimal import Decimal
from tkinter import messagebox, ttk

from salestracker import (
    CASH,
    DEFAULT_DB,
    DENOMINATIONS,
    PAYMENT_METHODS,
    UNITS,
    Product,
    SalesTracker,
    TrackerError,
    count_cash,
    format_money,
    format_payment_method,
    format_qty,
    reconcile,
)
from tkinter import filedialog

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


def _descendant_canvases(widget: tk.Misc) -> list[tk.Canvas]:
    """Every Canvas under widget. These hold a colour ttk styles cannot set."""
    found: list[tk.Canvas] = []
    stack = list(widget.winfo_children())
    while stack:
        child = stack.pop()
        if isinstance(child, tk.Canvas):
            found.append(child)
        stack.extend(child.winfo_children())
    return found


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

    def _wheel(event: tk.Event) -> None:
        if _owns_scroll(getattr(event, "widget", None)):
            return
        if getattr(event, "delta", 0):
            canvas.yview_scroll(int(-event.delta / 120), "units")
        elif getattr(event, "num", None) == 4:
            canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            canvas.yview_scroll(1, "units")

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


class MoneyDialog(tk.Toplevel):
    """Expected money on the left, an independent bill count on the right.

    The ledger figure and the drawer count are arrived at separately; the
    comparison at the bottom is the whole point of the page.
    """

    def __init__(self, master: tk.Tk, tracker: SalesTracker) -> None:
        super().__init__(master)
        self.tracker = tracker
        self.title("Money")
        self.configure(bg=PANEL)
        self.transient(master)
        self.minsize(760, 560)
        # Tall enough for the whole page at the default font size; the
        # scrolling body covers anything shorter.
        self.geometry("820x720")

        self.money = tracker.financials()
        self.var_counts: dict[int, tk.StringVar] = {}
        self.var_subtotals: dict[int, tk.StringVar] = {}
        self.var_counted = tk.StringVar(value=format_money(Decimal("0.00")))
        self.var_expected = tk.StringVar(
            value=format_money(self.money.cash_collected)
        )
        self.var_verdict = tk.StringVar(value="Enter your bill counts to compare.")
        self.var_note = tk.StringVar(value="")

        pad = scrollable_body(self)
        pad.columnconfigure(0, weight=1)
        pad.columnconfigure(1, weight=1)
        pad.rowconfigure(1, weight=1)

        ttk.Label(pad, text="Money", style="Section.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            pad,
            text="The ledger works out what should be in the drawer. Count your "
                 "bills yourself and compare — if the two disagree, something "
                 "was mislogged.",
            style="Hint.TLabel", wraplength=740,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 16))

        self._build_expected(pad)
        self._build_count(pad)
        self._build_verdict(pad)

        ttk.Button(pad, text="Close", style="Ghost.TButton",
                   command=self.destroy).grid(row=4, column=0, columnspan=2,
                                              sticky="ew", pady=(14, 0))
        self.bind("<Escape>", lambda _e: self.destroy())
        self.grab_set()
        self._recount()
        center_on_parent(self, master)

    # ------------------------------------------------------------- expected

    def _build_expected(self, parent: ttk.Frame) -> None:
        box = ttk.Frame(parent, style="Panel.TFrame")
        box.grid(row=2, column=0, sticky="nsew", padx=(0, 18))
        ttk.Label(box, text="EXPECTED FROM THE LEDGER",
                  style="Field.TLabel").pack(anchor="w", pady=(0, 8))

        money = self.money
        rows = (
            ("Cash collected", money.cash_collected, True),
            ("Cash still to collect", money.cash_uncollected, False),
            ("Venmo / other collected", money.other_collected, False),
            ("Venmo / other still to collect", money.other_uncollected, False),
            (None, None, False),
            ("Total collected", money.total_collected, False),
            ("Total still to collect", money.total_uncollected, False),
            ("Full order value", money.book_value, False),
        )
        for label, amount, emphasis in rows:
            if label is None:
                tk.Frame(box, bg=LINE, height=1).pack(fill="x", pady=8)
                continue
            line = ttk.Frame(box, style="Panel.TFrame")
            line.pack(fill="x", pady=2)
            ttk.Label(line, text=label,
                      style="Field.TLabel" if emphasis else "Hint.TLabel").pack(side="left")
            ttk.Label(line, text=format_money(amount),
                      style="Figure.TLabel" if emphasis else "FigureMuted.TLabel").pack(
                side="right"
            )

        ttk.Label(
            box,
            text="Only cash reaches the drawer. Venmo and other payments are "
                 "excluded from the comparison below.",
            style="Hint.TLabel", wraplength=330,
        ).pack(anchor="w", pady=(14, 0))

    # ---------------------------------------------------------------- count

    def _build_count(self, parent: ttk.Frame) -> None:
        box = ttk.Frame(parent, style="Panel.TFrame")
        box.grid(row=2, column=1, sticky="nsew")
        ttk.Label(box, text="YOUR BILL COUNT", style="Field.TLabel").pack(
            anchor="w", pady=(0, 8)
        )

        grid = ttk.Frame(box, style="Panel.TFrame")
        grid.pack(fill="x")
        grid.columnconfigure(2, weight=1)
        for row, denomination in enumerate(DENOMINATIONS):
            ttk.Label(grid, text=f"${denomination}", style="Figure.TLabel").grid(
                row=row, column=0, sticky="w", pady=3
            )
            var = tk.StringVar(value="")
            self.var_counts[denomination] = var
            entry = ttk.Entry(grid, textvariable=var, style="Ticket.TEntry",
                              width=6, justify="right")
            entry.grid(row=row, column=1, sticky="w", padx=(12, 10), pady=3)
            var.trace_add("write", lambda *_: self._recount())
            sub = tk.StringVar(value=format_money(Decimal("0.00")))
            self.var_subtotals[denomination] = sub
            ttk.Label(grid, textvariable=sub, style="FigureMuted.TLabel").grid(
                row=row, column=2, sticky="e", pady=3
            )

        tk.Frame(box, bg=LINE, height=1).pack(fill="x", pady=10)
        total = ttk.Frame(box, style="Panel.TFrame")
        total.pack(fill="x")
        ttk.Label(total, text="COUNTED", style="Field.TLabel").pack(side="left")
        ttk.Label(total, textvariable=self.var_counted,
                  style="Figure.TLabel").pack(side="right")
        ttk.Button(box, text="Clear counts", style="Ghost.TButton",
                   command=self._clear).pack(anchor="e", pady=(10, 0))

    # -------------------------------------------------------------- verdict

    def _build_verdict(self, parent: ttk.Frame) -> None:
        box = ttk.Frame(parent, style="Panel.TFrame")
        box.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(18, 0))
        tk.Frame(box, bg=LINE, height=1).pack(fill="x", pady=(0, 12))

        line = ttk.Frame(box, style="Panel.TFrame")
        line.pack(fill="x")
        for caption, var in (("COUNTED", self.var_counted),
                             ("EXPECTED CASH", self.var_expected)):
            cell = ttk.Frame(line, style="Panel.TFrame")
            cell.pack(side="left", padx=(0, 40))
            ttk.Label(cell, text=caption, style="Field.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=var, style="Figure.TLabel").pack(anchor="w")

        self.verdict_label = ttk.Label(box, textvariable=self.var_verdict,
                                       style="Hint.TLabel", wraplength=740)
        self.verdict_label.pack(anchor="w", pady=(12, 0))
        ttk.Label(box, textvariable=self.var_note, style="Hint.TLabel",
                  wraplength=740).pack(anchor="w", pady=(2, 0))

    # ------------------------------------------------------------ behaviour

    def _clear(self) -> None:
        for var in self.var_counts.values():
            var.set("")

    def _recount(self) -> None:
        raw = {d: v.get() for d, v in self.var_counts.items()}
        try:
            counted = count_cash(raw)
        except TrackerError as exc:
            self.var_counted.set("—")
            self.var_verdict.set(str(exc))
            self.verdict_label.configure(style="Error.TLabel")
            self.var_note.set("")
            return

        for denomination, var in self.var_subtotals.items():
            text = str(raw.get(denomination, "")).strip()
            number = int(text) if text.isdigit() else 0
            var.set(format_money(Decimal(denomination) * number))

        self.var_counted.set(format_money(counted))
        if not any(str(v).strip() for v in raw.values()):
            self.var_verdict.set("Enter your bill counts to compare.")
            self.verdict_label.configure(style="Hint.TLabel")
            self.var_note.set("")
            return

        result = reconcile(self.money.cash_collected, raw)
        self.var_verdict.set(result.headline)
        self.verdict_label.configure(
            style="Good.TLabel" if result.balanced else "Error.TLabel"
        )
        self.var_note.set(
            "" if result.balanced else
            "Check for a mislogged quantity, an order paid by Venmo but "
            "recorded as cash, or change given from the drawer."
        )


class SalesApp(tk.Tk):
    """Main window. The order list is the primary surface; entry is a top bar."""

    COLUMNS = ("purchaser", "product", "progress", "due", "status", "method")
    HEADINGS = {
        "purchaser": ("Purchaser", 180, "w"),
        "product": ("Product", 140, "w"),
        "progress": ("Received / ordered", 205, "w"),
        "due": ("Still due", 85, "e"),
        "status": ("Status", 110, "w"),
        "method": ("Paid by", 90, "w"),
    }
    BAR_CELLS = 6
    # Tk paints one foreground per row, so the bar must read monochrome.
    # U+25A0/U+25A1 are equal-width and present in Segoe UI for the Windows exe.
    BAR_FULL = "\u25a0"
    BAR_EMPTY = "\u25a1"

    def __init__(self, db_path: str | None = None, *, auto_setup: bool = True) -> None:
        super().__init__()
        self.tracker = SalesTracker(db_path)
        self.selected_order_id: int | None = None
        self._auto_setup = auto_setup
        self._editor: ttk.Entry | None = None
        self._row_error: ttk.Label | None = None
        self._flash_job: str | None = None
        self._theme_job: str | None = None
        self._rules: list[tk.Frame] = []

        # Paint before any widget is built, so nothing is created in the
        # outgoing theme's colours.
        self.theme_choice = theme.normalize_choice(
            self.tracker.get_setting(THEME_SETTING, theme.DEFAULT_CHOICE)
        )
        self.theme_painted = apply_palette(self.theme_choice)

        self.title("Sales Tracker")
        self.minsize(940, 580)
        # 700 left the footer 20px short of its natural height, so the totals
        # row was clipped at the default size.
        self.geometry("1080x730")
        self.configure(bg=BG)

        self._fonts()
        self._style()
        self._vars()
        self._build_menu()
        self._build()
        self._binds()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._watch_os_theme()
        if self._auto_setup:
            self.after(200, self._maybe_prompt_product)

    # ------------------------------------------------------------------- theme

    def set_theme(self, choice: str) -> str:
        """Remember the operator's pick and repaint. Returns what it painted."""
        self.theme_choice = theme.normalize_choice(choice)
        self.tracker.set_setting(THEME_SETTING, self.theme_choice)
        return self._repaint()

    def _repaint(self) -> str:
        self.theme_painted = apply_palette(self.theme_choice)
        self._style()
        self.configure(bg=BG)
        # ttk styles cover most of the app, but a handful of plain Tk widgets
        # and one row tag hold their own colours and have to be told again.
        self.tree.tag_configure("done", background=RECEIVED_BG, foreground=MUTED)
        for rule in self._rules:
            rule.configure(bg=LINE)
        for window in self.winfo_children():
            if isinstance(window, tk.Toplevel):
                window.configure(bg=PANEL)
            for canvas in _descendant_canvases(window):
                canvas.configure(bg=PANEL)
        return self.theme_painted

    def _watch_os_theme(self) -> None:
        """Re-check the desktop theme while "System" is selected.

        Tk has no notification for this, so it is a poll. Four seconds is far
        below what anyone notices and the check is a single registry read.
        """
        if self.theme_choice == theme.SYSTEM:
            if theme.detect_os_theme() != self.theme_painted:
                self._repaint()
        self._theme_job = self.after(4000, self._watch_os_theme)

    # ------------------------------------------------------------------ chrome

    def _fonts(self) -> None:
        available = set(tkfont.families())

        def pick(*names: str, fallback: str = "TkDefaultFont") -> str:
            for name in names:
                if name in available:
                    return name
            return fallback

        body = pick("Inter", "Adwaita Sans", "Cantarell", "Noto Sans")
        figures = pick(
            "JetBrainsMono Nerd Font", "JetBrains Mono", "Source Code Pro",
            "Hack", "Liberation Mono",
        )

        # Names read by ProductWizard; keep them.
        self.font_title = tkfont.Font(family=body, size=15, weight="bold")
        self.font_body = tkfont.Font(family=body, size=11)
        self.font_muted = tkfont.Font(family=body, size=10)
        self.font_label = tkfont.Font(family=body, size=9, weight="bold")
        self.font_button = tkfont.Font(family=body, size=11, weight="bold")

        self.font_row = tkfont.Font(family=body, size=11)
        self.font_figures = tkfont.Font(family=figures, size=11)
        self.font_stat = tkfont.Font(family=figures, size=17, weight="bold")

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure("App.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Bar.TFrame", background=PANEL)

        style.configure("Title.TLabel", background=BG, foreground=INK, font=self.font_title)
        style.configure("Meta.TLabel", background=BG, foreground=MUTED, font=self.font_muted)
        style.configure("Section.TLabel", background=PANEL, foreground=INK, font=self.font_title)
        style.configure("Field.TLabel", background=PANEL, foreground=MUTED, font=self.font_label)
        style.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=self.font_muted)
        style.configure("Error.TLabel", background=PANEL, foreground=DANGER, font=self.font_muted)
        style.configure("StatCaption.TLabel", background=BG, foreground=MUTED, font=self.font_label)
        style.configure("StatValue.TLabel", background=BG, foreground=ACCENT, font=self.font_stat)
        style.configure("Ok.TLabel", background=BG, foreground=ACCENT, font=self.font_muted)
        style.configure("Foot.TLabel", background=BG, foreground=MUTED, font=self.font_muted)
        style.configure("Empty.TLabel", background=BG, foreground=MUTED, font=self.font_muted)
        style.configure("RowErr.TLabel", background=ACCENT_SOFT, foreground=DANGER,
                        font=self.font_muted)
        style.configure("Figure.TLabel", background=PANEL, foreground=INK,
                        font=self.font_figures)
        style.configure("FigureMuted.TLabel", background=PANEL, foreground=MUTED,
                        font=self.font_figures)
        style.configure("Good.TLabel", background=PANEL, foreground=ACCENT,
                        font=self.font_muted)

        for name in ("Ticket.TEntry", "Cell.TEntry"):
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
        # A rejected inline edit carries its own state, on the row.
        style.configure(
            "Bad.TEntry", fieldbackground=FIELD, foreground=DANGER, insertcolor=DANGER,
            bordercolor=DANGER, lightcolor=DANGER, darkcolor=DANGER,
            padding=6, font=self.font_body,
        )

        style.configure("Ticket.TCombobox", fieldbackground=FIELD, foreground=INK,
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

        style.configure("Ghost.TButton", background=PANEL, foreground=INK,
                        bordercolor=LINE, focusthickness=3, focuscolor=FOCUS,
                        padding=(11, 7), font=self.font_body)
        style.map("Ghost.TButton", background=[("active", SUBTLE)])

        style.configure("Danger.TButton", background=PANEL, foreground=DANGER,
                        bordercolor=LINE, focusthickness=3, focuscolor=DANGER,
                        padding=(11, 7), font=self.font_body)
        style.map("Danger.TButton", background=[("active", DANGER_SOFT)])

        style.configure("Filter.TRadiobutton", background=BG, foreground=INK,
                        font=self.font_muted, focuscolor=FOCUS)
        # Same control, but sitting on a dialog panel rather than the page.
        style.configure("Panel.TRadiobutton", background=PANEL, foreground=INK,
                        font=self.font_muted, focuscolor=FOCUS)
        style.map("Panel.TRadiobutton", background=[("active", PANEL)])

        style.configure("Ledger.Treeview", background=FIELD, fieldbackground=FIELD,
                        foreground=INK, rowheight=34, font=self.font_row,
                        bordercolor=LINE)
        style.configure("Ledger.Treeview.Heading", background=BG, foreground=MUTED,
                        font=self.font_label, relief="flat", padding=(8, 9))
        style.map("Ledger.Treeview",
                  background=[("selected", ACCENT_SOFT)],
                  foreground=[("selected", INK)])

        style.configure("Ledger.Vertical.TScrollbar", background=PANEL,
                        troughcolor=SUBTLE, bordercolor=LINE, arrowcolor=MUTED)

    def _vars(self) -> None:
        self.var_meta = tk.StringVar(value="Nothing to sell yet")
        self.var_purchaser = tk.StringVar()
        self.var_product = tk.StringVar()
        self.var_qty = tk.StringVar()
        self.var_qty_label = tk.StringVar(value="HOW MANY")
        # Holds the display form ("Cash"); parse_payment_method lowercases it
        # again on the way into the ledger.
        self.var_method = tk.StringVar(value=format_payment_method(CASH))
        self.var_error = tk.StringVar()
        self.var_ok = tk.StringVar()
        self.var_search = tk.StringVar()
        self.var_filter = tk.StringVar(value="all")
        self.var_count = tk.StringVar(value="0")
        self.var_outstanding = tk.StringVar(value="0")
        self.var_received = tk.StringVar(value="0")
        self.var_due = tk.StringVar(value="0")
        self.var_got = tk.StringVar()
        self.var_search.trace_add("write", lambda *_: self.refresh())
        self.var_product.trace_add("write", lambda *_: self._sync_qty_label())

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        ledger = tk.Menu(menu, tearoff=0)
        ledger.add_command(label="New product…", command=self.open_wizard)
        ledger.add_command(label="Money…", command=self.open_money)
        ledger.add_command(label="Export CSV…", command=self.export_csv)
        ledger.add_command(label="Settings…", command=self.open_settings)
        ledger.add_separator()
        ledger.add_command(label="Quit", command=self._on_close)
        menu.add_cascade(label="Ledger", menu=ledger)
        self.config(menu=menu)

    def _rule(self) -> None:
        rule = tk.Frame(self, bg=LINE, height=1)
        rule.pack(fill="x")
        self._rules.append(rule)

    def _build(self) -> None:
        self._build_header()
        self._rule()
        self._build_entry()
        self._rule()
        self._build_stats()
        self._build_filters()
        self._build_table()
        self._build_footer()

    def _build_header(self) -> None:
        head = ttk.Frame(self, style="App.TFrame", padding=(20, 14, 20, 12))
        head.pack(fill="x")
        ttk.Label(head, text="Sales Tracker", style="Title.TLabel").pack(side="left")
        ttk.Label(head, textvariable=self.var_meta, style="Meta.TLabel").pack(
            side="left", padx=(12, 0), pady=(6, 0)
        )
        ttk.Button(head, text="Settings", style="Ghost.TButton",
                   command=self.open_settings).pack(side="right")
        ttk.Button(head, text="Export CSV", style="Ghost.TButton",
                   command=self.export_csv).pack(side="right", padx=(0, 8))
        ttk.Button(head, text="Money", style="Primary.TButton",
                   command=self.open_money).pack(side="right", padx=(0, 8))

    def _build_entry(self) -> None:
        bar = ttk.Frame(self, style="Bar.TFrame", padding=(20, 12, 20, 10))
        bar.pack(fill="x")

        # Shown instead of the form until a product exists.
        self.need_product = ttk.Frame(bar, style="Bar.TFrame")
        ttk.Label(self.need_product,
                  text="Set up a product before you log the first order.",
                  style="Hint.TLabel").pack(side="left", pady=(2, 0))
        ttk.Button(self.need_product, text="Establish a product",
                   style="Primary.TButton",
                   command=self.open_wizard).pack(side="left", padx=(12, 0))

        self.order_form = ttk.Frame(bar, style="Bar.TFrame")
        row = ttk.Frame(self.order_form, style="Bar.TFrame")
        row.pack(fill="x")

        self.purchaser_entry = self._field(row, "PURCHASER", self.var_purchaser, 24)
        self._field_label(row, "PRODUCT")
        self.product_combo = ttk.Combobox(
            self._last_box, textvariable=self.var_product, state="readonly",
            style="Ticket.TCombobox", width=18,
        )
        self.product_combo.pack(anchor="w", ipady=2)
        self.qty_entry = self._field(row, "HOW MANY", self.var_qty, 8,
                                     label_var=self.var_qty_label)
        self._field_label(row, "PAID BY")
        self.method_combo = ttk.Combobox(
            self._last_box, textvariable=self.var_method, state="readonly",
            style="Ticket.TCombobox", width=8,
            values=[format_payment_method(m) for m in PAYMENT_METHODS],
        )
        self.method_combo.pack(anchor="w", ipady=2)

        actions = ttk.Frame(row, style="Bar.TFrame")
        actions.pack(side="left", padx=(4, 0))
        ttk.Label(actions, text=" ", style="Field.TLabel").pack(anchor="w")
        ttk.Button(actions, text="Log order", style="Primary.TButton",
                   command=self.log_order).pack(anchor="w")

        hint = ttk.Frame(row, style="Bar.TFrame")
        hint.pack(side="left", padx=(12, 0))
        ttk.Label(hint, text=" ", style="Field.TLabel").pack(anchor="w")
        ttk.Label(hint, text="Enter submits  ·  Ctrl+N new product",
                  style="Hint.TLabel").pack(anchor="w", pady=(6, 0))

        # The order form's error belongs under the order form.
        ttk.Label(bar, textvariable=self.var_error, style="Error.TLabel").pack(
            anchor="w", pady=(8, 0)
        )

    def _field_label(self, parent: ttk.Frame, text: str,
                     label_var: tk.StringVar | None = None) -> None:
        box = ttk.Frame(parent, style="Bar.TFrame")
        box.pack(side="left", padx=(0, 10))
        if label_var is None:
            ttk.Label(box, text=text, style="Field.TLabel").pack(anchor="w", pady=(0, 3))
        else:
            ttk.Label(box, textvariable=label_var, style="Field.TLabel").pack(
                anchor="w", pady=(0, 3)
            )
        self._last_box = box

    def _field(self, parent: ttk.Frame, text: str, variable: tk.StringVar,
               width: int, label_var: tk.StringVar | None = None) -> ttk.Entry:
        self._field_label(parent, text, label_var)
        entry = ttk.Entry(self._last_box, textvariable=variable,
                          style="Ticket.TEntry", width=width)
        entry.pack(anchor="w", ipady=2)
        return entry

    def _build_stats(self) -> None:
        stats = ttk.Frame(self, style="App.TFrame", padding=(20, 12, 20, 4))
        stats.pack(fill="x")
        for caption, var in (
            ("ON THE LIST", self.var_count),
            ("OUTSTANDING", self.var_outstanding),
            ("RECEIVED", self.var_received),
            ("UNITS STILL DUE", self.var_due),
        ):
            cell = ttk.Frame(stats, style="App.TFrame")
            cell.pack(side="left", padx=(0, 36))
            ttk.Label(cell, text=caption, style="StatCaption.TLabel").pack(anchor="w")
            ttk.Label(cell, textvariable=var, style="StatValue.TLabel").pack(anchor="w")

    def _build_filters(self) -> None:
        row = ttk.Frame(self, style="App.TFrame", padding=(20, 6, 20, 8))
        row.pack(fill="x")
        for value, label in (("all", "All"), ("outstanding", "Outstanding"),
                             ("received", "Received")):
            ttk.Radiobutton(row, text=label, value=value, variable=self.var_filter,
                            style="Filter.TRadiobutton",
                            command=self.refresh).pack(side="left", padx=(0, 12))
        ttk.Entry(row, textvariable=self.var_search, style="Ticket.TEntry",
                  width=22).pack(side="right", ipady=2)
        ttk.Label(row, text="SEARCH", style="StatCaption.TLabel").pack(
            side="right", padx=(0, 8)
        )

    def _build_table(self) -> None:
        wrap = ttk.Frame(self, style="App.TFrame", padding=(20, 0, 20, 0))
        wrap.pack(fill="both", expand=True)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(wrap, columns=self.COLUMNS, show="headings",
                                 style="Ledger.Treeview", selectmode="browse")
        for key, (title, width, anchor) in self.HEADINGS.items():
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=True)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview,
                               style="Ledger.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

        self.tree.tag_configure("done", background=RECEIVED_BG, foreground=MUTED)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self.begin_edit)
        self.tree.bind("<Return>", self.begin_edit)

        self.empty_label = ttk.Label(wrap, style="Empty.TLabel", justify="center")

    def _build_footer(self) -> None:
        self._rule()
        foot = ttk.Frame(self, style="App.TFrame", padding=(20, 8, 20, 10))
        foot.pack(fill="x")
        ttk.Label(foot, textvariable=self.var_ok, style="Ok.TLabel").pack(side="left")
        ttk.Label(
            foot,
            text="Double-click a row (or press Enter) to record what they received.",
            style="Foot.TLabel",
        ).pack(side="right")

    def _binds(self) -> None:
        self.bind("<Control-s>", lambda _e: self.log_order())
        self.bind("<Control-n>", lambda _e: self.open_wizard())
        self.bind("<Control-comma>", lambda _e: self.open_settings())
        for widget in (self.purchaser_entry, self.qty_entry, self.product_combo):
            widget.bind("<Return>", lambda _e: self.log_order())

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

    def open_money(self) -> None:
        MoneyDialog(self, self.tracker)

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

    def _sync_qty_label(self) -> None:
        name = self.var_product.get().strip()
        unit = "units"
        for product in self.tracker.list_products():
            if product.name == name:
                unit = product.unit
                break
        self.var_qty_label.set(f"HOW MANY {unit.upper()}")

    def _reload_products(self, select_name: str | None = None) -> list[Product]:
        products = self.tracker.list_products()
        names = [product.name for product in products]
        self.product_combo["values"] = names
        if select_name and select_name in names:
            self.var_product.set(select_name)
        elif names and self.var_product.get() not in names:
            self.var_product.set(names[0])
        if products:
            self.need_product.pack_forget()
            self.order_form.pack(fill="x")
        else:
            self.order_form.pack_forget()
            self.need_product.pack(fill="x")
            self.var_product.set("")
        self._sync_qty_label()
        return products

    # ------------------------------------------------------------------ orders

    def log_order(self) -> None:
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
        self._flash(
            f"Logged {order.purchaser} — {format_qty(order.quantity_ordered)} "
            f"{order.product_unit} of {order.product_name}"
        )
        self.refresh(select_id=order.id)
        self.purchaser_entry.focus_set()

    def _selected_id(self) -> int | None:
        selection = self.tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _on_select(self, _event: object = None) -> None:
        order_id = self._selected_id()
        self.selected_order_id = order_id
        if order_id is None:
            self.var_got.set("")
            return
        try:
            order = self.tracker.get_order(order_id)
        except TrackerError:
            return
        self.var_got.set(format_qty(order.quantity_received))

    # ------------------------------------------------- inline received editing

    def begin_edit(self, _event: object = None) -> str | None:
        """Open an editor over the selected row's received figure."""
        order_id = self._selected_id()
        if order_id is None:
            return None
        self._cancel_edit()
        box = self.tree.bbox(str(order_id), "progress")
        if not box:
            return None
        x, y, width, height = box
        try:
            order = self.tracker.get_order(order_id)
        except TrackerError:
            return None
        self.var_got.set(format_qty(order.quantity_received))
        editor = ttk.Entry(self.tree, textvariable=self.var_got,
                           style="Cell.TEntry", font=self.font_figures)
        editor.place(x=x + 4, y=y + 3, width=76, height=height - 6)
        editor.focus_set()
        editor.select_range(0, "end")
        self._editor = editor
        self._edit_geometry = (x + 4, y + 3, 76, height - 6)
        editor.bind("<Return>", lambda _e: self.update_received())
        editor.bind("<Escape>", lambda _e: self._cancel_edit())
        return "break"

    def _cancel_edit(self) -> None:
        if self._row_error is not None:
            self._row_error.destroy()
            self._row_error = None
        if self._editor is not None:
            self._editor.destroy()
            self._editor = None

    def _reject_edit(self, message: str) -> None:
        """Keep the editor open, on the row, with the reason beside it."""
        if self._editor is None:
            self.var_error.set(message)
            return
        x, y, width, height = self._edit_geometry
        self._editor.configure(style="Bad.TEntry")
        if self._row_error is None:
            self._row_error = ttk.Label(self.tree, style="RowErr.TLabel")
        self._row_error.configure(text=f"  {message}")
        self._row_error.place(x=x + width + 8, y=y, height=height)
        self._editor.focus_set()
        self._editor.select_range(0, "end")

    def update_received(self) -> None:
        """Commit the received figure for the selected row.

        Works whether the inline editor is open or the value was set
        programmatically through ``var_got``.
        """
        order_id = self._selected_id()
        if order_id is None:
            self.var_error.set("Select a purchaser on the list first.")
            return
        try:
            order = self.tracker.set_received(order_id, self.var_got.get())
        except TrackerError as exc:
            self._reject_edit(str(exc))
            return
        self.var_error.set("")
        self._cancel_edit()
        self._flash(
            f"{order.purchaser} — recorded {format_qty(order.quantity_received)} "
            f"of {format_qty(order.quantity_ordered)}"
        )
        self.refresh(select_id=order.id)

    def mark_all_received(self) -> None:
        order_id = self._selected_id()
        if order_id is None:
            return
        try:
            order = self.tracker.mark_received(order_id)
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        self._cancel_edit()
        self._flash(f"{order.purchaser} — marked fully received")
        self.refresh(select_id=order.id)

    # ------------------------------------------------------------------ render

    @classmethod
    def _bar(cls, received: Decimal, ordered: Decimal) -> str:
        """Fixed-width progress bar. Block glyphs share one advance width."""
        if ordered <= 0:
            return cls.BAR_EMPTY * cls.BAR_CELLS
        filled = int((received / ordered) * cls.BAR_CELLS)
        filled = max(0, min(cls.BAR_CELLS, filled))
        if filled == 0 and received > 0:
            filled = 1
        return cls.BAR_FULL * filled + cls.BAR_EMPTY * (cls.BAR_CELLS - filled)

    def refresh(
        self,
        select_id: int | None = None,
        select_product: str | None = None,
    ) -> None:
        self._cancel_edit()
        products = self._reload_products(select_product)

        search = self.var_search.get().strip() or None
        status = self.var_filter.get() or "all"
        try:
            orders = self.tracker.list_orders(search=search, status=status)
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return

        keep = select_id if select_id is not None else self._selected_id()
        self.tree.delete(*self.tree.get_children())
        for order in orders:
            done = order.fulfilled
            self.tree.insert(
                "", "end", iid=str(order.id),
                values=(
                    order.purchaser,
                    order.product_name,
                    f"{self._bar(order.quantity_received, order.quantity_ordered)}  "
                    f"{format_qty(order.quantity_received)} / "
                    f"{format_qty(order.quantity_ordered)}",
                    "—" if done else format_qty(order.remaining),
                    "received" if done else "outstanding",
                    format_payment_method(order.payment_method),
                ),
                tags=("done",) if done else (),
            )

        if orders:
            self.empty_label.place_forget()
        else:
            if not products:
                text = "No products yet. Establish one above to get started."
            elif search or status != "all":
                text = "Nothing matches this filter."
            else:
                text = "No orders yet. Log a purchaser above."
            self.empty_label.configure(text=text)
            self.empty_label.place(relx=0.5, rely=0.42, anchor="center")

        if keep is not None and self.tree.exists(str(keep)):
            self.tree.selection_set(str(keep))
            self.tree.see(str(keep))
            self._on_select()
        elif not orders:
            self.selected_order_id = None
            self.var_got.set("")

        summary = self.tracker.summary()
        self.var_count.set(str(summary.order_count))
        self.var_outstanding.set(str(summary.outstanding_count))
        self.var_received.set(str(summary.received_count))
        self.var_due.set(format_qty(summary.units_remaining))
        if products:
            names = ", ".join(product.name for product in products[:3])
            extra = "" if len(products) <= 3 else f" +{len(products) - 3}"
            self.var_meta.set(
                f"{names}{extra}  ·  {format_money(summary.revenue)} ordered"
            )
        else:
            self.var_meta.set("Nothing to sell yet")

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
