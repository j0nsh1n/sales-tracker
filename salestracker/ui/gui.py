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
from tkinter import messagebox, ttk

from salestracker import (
    DEFAULT_DB,
    UNITS,
    Order,
    Product,
    SalesTracker,
    TrackerError,
    format_money,
    format_qty,
)

BG = "#E6EDE8"
PANEL = "#F7FBF8"
INK = "#14201C"
MUTED = "#4A5C56"
ACCENT = "#0B6E4F"
ACCENT_DARK = "#085541"
BRASS = "#C9A227"
DANGER = "#A61B1B"
LINE = "#C5D4CC"
ROW_ALT = "#EEF5F1"
FOCUS = "#0B6E4F"
WHITE = "#FFFFFF"
RECEIVED_BG = "#E3F2EA"


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
                from sales_tracker import parse_money

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
        self.tracker = tracker
        self.on_change = on_change
        self.title("Settings")
        self.configure(bg=PANEL)
        self.transient(master)
        self.grab_set()
        self.minsize(620, 700)
        self.geometry("660x780")

        pad = ttk.Frame(self, style="Panel.TFrame", padding=24)
        pad.pack(fill="both", expand=True)

        ttk.Label(pad, text="Settings", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            pad,
            text="Nothing on the main list can be deleted by accident. "
            "Removing rows happens only here.",
            style="Hint.TLabel",
            wraplength=580,
        ).pack(anchor="w", pady=(6, 14))

        ttk.Label(pad, text="LEDGER FILE", style="Field.TLabel").pack(anchor="w")
        ttk.Label(pad, text=str(tracker.db_path), style="Hint.TLabel", wraplength=580).pack(
            anchor="w", pady=(2, 16)
        )

        ttk.Label(pad, text="UNLOCK", style="Field.TLabel").pack(anchor="w")
        ttk.Label(
            pad,
            text="Type RESET to enable every destructive action below. "
            "None of them can be undone.",
            style="Hint.TLabel",
            wraplength=580,
        ).pack(anchor="w", pady=(4, 6))
        self.var_confirm = tk.StringVar()
        ttk.Entry(pad, textvariable=self.var_confirm, style="Ticket.TEntry").pack(
            fill="x", ipady=4, pady=(0, 16)
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
        self.products_tree = self._picker(
            pad,
            "DELETE A PRODUCT",
            "A product with orders on the list cannot be removed until those "
            "orders are deleted first.",
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
            height=5,
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


class SalesApp(tk.Tk):
    def __init__(self, db_path: str | None = None, *, auto_setup: bool = True) -> None:
        super().__init__()
        self.tracker = SalesTracker(db_path)
        self.selected_order_id: int | None = None
        self._status_filter = "all"
        self._auto_setup = auto_setup

        self.title("Ledger — Sales Tracker")
        self.minsize(1100, 700)
        self.geometry("1220x760")
        self.configure(bg=BG)

        self._fonts()
        self._style()
        self._vars()
        self._build_menu()
        self._build()
        self._binds()
        self.refresh()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self._auto_setup:
            self.after(200, self._maybe_prompt_product)

    def _fonts(self) -> None:
        available = set(tkfont.families())

        def pick(*names: str, fallback: str = "TkDefaultFont") -> str:
            for name in names:
                if name in available:
                    return name
            return fallback

        display = pick("Inter Display", "Space Grotesk", "Inter", "Cantarell")
        body = pick("Inter", "Adwaita Sans", "Cantarell", "Noto Sans")
        figures = pick(
            "JetBrainsMono Nerd Font", "Source Code Pro", "Hack", "Liberation Mono"
        )

        self.font_kicker = tkfont.Font(family=body, size=10, weight="bold")
        self.font_title = tkfont.Font(family=display, size=24, weight="bold")
        self.font_body = tkfont.Font(family=body, size=12)
        self.font_label = tkfont.Font(family=body, size=10, weight="bold")
        self.font_muted = tkfont.Font(family=body, size=11)
        self.font_button = tkfont.Font(family=body, size=12, weight="bold")
        self.font_figures = tkfont.Font(family=figures, size=26, weight="bold")
        self.font_stat = tkfont.Font(family=figures, size=16, weight="bold")
        self.font_table = tkfont.Font(family=body, size=11)
        self.font_heading = tkfont.Font(family=body, size=10, weight="bold")

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("App.TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Header.TFrame", background=INK)
        style.configure("Kicker.TLabel", background=INK, foreground=BRASS, font=self.font_kicker)
        style.configure("Title.TLabel", background=INK, foreground=WHITE, font=self.font_title)
        style.configure(
            "HeaderMuted.TLabel", background=INK, foreground="#A8C4B8", font=self.font_muted
        )
        style.configure("Today.TLabel", background=INK, foreground=WHITE, font=self.font_figures)
        style.configure("Section.TLabel", background=PANEL, foreground=INK, font=self.font_title)
        style.configure("Field.TLabel", background=PANEL, foreground=MUTED, font=self.font_label)
        style.configure("Hint.TLabel", background=PANEL, foreground=MUTED, font=self.font_muted)
        style.configure("Error.TLabel", background=PANEL, foreground=DANGER, font=self.font_muted)
        style.configure(
            "StatCaption.TLabel", background=PANEL, foreground=MUTED, font=self.font_label
        )
        style.configure("StatValue.TLabel", background=PANEL, foreground=ACCENT, font=self.font_stat)
        style.configure("Empty.TLabel", background=PANEL, foreground=MUTED, font=self.font_muted)
        style.configure(
            "Ticket.TEntry",
            fieldbackground=WHITE,
            foreground=INK,
            insertcolor=INK,
            bordercolor=LINE,
            lightcolor=LINE,
            darkcolor=LINE,
            padding=8,
            font=self.font_body,
        )
        style.map(
            "Ticket.TEntry",
            bordercolor=[("focus", FOCUS)],
            lightcolor=[("focus", FOCUS)],
            darkcolor=[("focus", FOCUS)],
        )
        style.configure(
            "Ticket.TCombobox",
            fieldbackground=WHITE,
            foreground=INK,
            bordercolor=LINE,
            padding=6,
            font=self.font_body,
        )
        style.configure(
            "Primary.TButton",
            background=ACCENT,
            foreground=WHITE,
            bordercolor=ACCENT,
            focusthickness=3,
            focuscolor=BRASS,
            padding=(16, 10),
            font=self.font_button,
        )
        style.map(
            "Primary.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#8AA89A")],
            foreground=[("disabled", WHITE)],
        )
        style.configure(
            "Ghost.TButton",
            background=PANEL,
            foreground=INK,
            bordercolor=LINE,
            focusthickness=3,
            focuscolor=FOCUS,
            padding=(12, 8),
            font=self.font_body,
        )
        style.map("Ghost.TButton", background=[("active", ROW_ALT)])
        style.configure(
            "Danger.TButton",
            background=PANEL,
            foreground=DANGER,
            bordercolor=LINE,
            focusthickness=3,
            focuscolor=DANGER,
            padding=(12, 8),
            font=self.font_body,
        )
        style.map("Danger.TButton", background=[("active", "#F8E8E8")])
        style.configure(
            "Filter.TRadiobutton",
            background=PANEL,
            foreground=INK,
            font=self.font_body,
            focuscolor=FOCUS,
        )
        style.configure(
            "Ledger.Treeview",
            background=WHITE,
            fieldbackground=WHITE,
            foreground=INK,
            rowheight=36,
            font=self.font_table,
            bordercolor=LINE,
        )
        style.configure(
            "Ledger.Treeview.Heading",
            background=ROW_ALT,
            foreground=MUTED,
            font=self.font_heading,
            relief="flat",
            padding=(8, 8),
        )
        style.map(
            "Ledger.Treeview",
            background=[("selected", ACCENT)],
            foreground=[("selected", WHITE)],
        )
        style.configure(
            "Ledger.Vertical.TScrollbar",
            background=PANEL,
            troughcolor=ROW_ALT,
            bordercolor=LINE,
            arrowcolor=INK,
        )
        style.configure(
            "Fill.Horizontal.TProgressbar",
            troughcolor=ROW_ALT,
            background=ACCENT,
            bordercolor=LINE,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )

    def _vars(self) -> None:
        self.var_headline = tk.StringVar(value="Establish a product")
        self.var_meta = tk.StringVar(value="Nothing to sell yet")
        self.var_due = tk.StringVar(value="0")
        self.var_purchaser = tk.StringVar()
        self.var_product = tk.StringVar()
        self.var_qty = tk.StringVar()
        self.var_qty_label = tk.StringVar(value="HOW MANY")
        self.var_error = tk.StringVar()
        self.var_search = tk.StringVar()
        self.var_filter = tk.StringVar(value="all")
        self.var_count = tk.StringVar(value="0 on the list")
        self.var_outstanding = tk.StringVar(value="0")
        self.var_received = tk.StringVar(value="0")
        self.var_detail = tk.StringVar(value="Select a name to update what they have received.")
        self.var_got = tk.StringVar()
        self.var_got_hint = tk.StringVar(value="")
        self.var_search.trace_add("write", lambda *_: self.refresh())
        self.var_product.trace_add("write", lambda *_: self._sync_qty_label())

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        ledger = tk.Menu(menu, tearoff=0)
        ledger.add_command(label="New product…", command=self.open_wizard)
        ledger.add_command(label="Settings…", command=self.open_settings)
        ledger.add_separator()
        ledger.add_command(label="Quit", command=self._on_close)
        menu.add_cascade(label="Ledger", menu=ledger)
        self.config(menu=menu)

    def _build(self) -> None:
        header = ttk.Frame(self, style="Header.TFrame", padding=(28, 18, 28, 16))
        header.pack(fill="x")
        left = ttk.Frame(header, style="Header.TFrame")
        left.pack(side="left", fill="y")
        ttk.Label(left, text="LEDGER  ·  SALES TRACKER", style="Kicker.TLabel").pack(anchor="w")
        ttk.Label(left, textvariable=self.var_headline, style="Title.TLabel").pack(
            anchor="w", pady=(4, 0)
        )
        ttk.Label(left, textvariable=self.var_meta, style="HeaderMuted.TLabel").pack(
            anchor="w", pady=(2, 0)
        )
        right = ttk.Frame(header, style="Header.TFrame")
        right.pack(side="right")
        ttk.Label(right, text="STILL TO HAND OUT", style="Kicker.TLabel").pack(anchor="e")
        ttk.Label(right, textvariable=self.var_due, style="Today.TLabel").pack(anchor="e")
        brass = tk.Frame(right, bg=BRASS, height=4)
        brass.pack(fill="x", pady=(8, 0))
        ttk.Button(right, text="Settings", style="Ghost.TButton", command=self.open_settings).pack(
            anchor="e", pady=(10, 0)
        )

        body = ttk.Frame(self, style="App.TFrame", padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        self._build_ticket(body)
        self._build_register(body)

        status = tk.Frame(self, bg=INK)
        status.pack(fill="x", side="bottom")
        tk.Label(
            status,
            text="Orders stay on this list until you reset them in Settings.  "
            "Checking someone off only records what they have received.",
            bg=INK,
            fg="#A8C4B8",
            font=self.font_muted,
            anchor="w",
            padx=20,
            pady=8,
        ).pack(fill="x")

    def _build_ticket(self, parent: ttk.Frame) -> None:
        ticket = ttk.Frame(parent, style="Panel.TFrame", padding=22)
        ticket.grid(row=0, column=0, sticky="nsw", padx=(0, 16))
        tk.Frame(ticket, bg=ACCENT, width=6).place(x=0, y=0, relheight=1)

        ttk.Label(ticket, text="New order", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            ticket,
            text="Name of the purchaser, then how many they bought.",
            style="Hint.TLabel",
            wraplength=280,
        ).pack(anchor="w", pady=(2, 16))

        self.need_product = ttk.Frame(ticket, style="Panel.TFrame")
        ttk.Label(
            self.need_product,
            text="Set up a product before you log the first order.",
            style="Hint.TLabel",
            wraplength=280,
        ).pack(anchor="w", pady=(0, 10))
        ttk.Button(
            self.need_product,
            text="Establish a product",
            style="Primary.TButton",
            command=self.open_wizard,
        ).pack(fill="x")

        self.order_form = ttk.Frame(ticket, style="Panel.TFrame")
        self.purchaser_entry = self._labeled(self.order_form, "PURCHASER", self.var_purchaser)
        ttk.Label(self.order_form, text="PRODUCT", style="Field.TLabel").pack(
            anchor="w", pady=(0, 4)
        )
        self.product_combo = ttk.Combobox(
            self.order_form,
            textvariable=self.var_product,
            state="readonly",
            style="Ticket.TCombobox",
        )
        self.product_combo.pack(fill="x", ipady=4, pady=(0, 10))
        ttk.Button(
            self.order_form,
            text="New product…",
            style="Ghost.TButton",
            command=self.open_wizard,
        ).pack(fill="x", pady=(0, 12))
        self.qty_entry = self._labeled(self.order_form, "HOW MANY", self.var_qty, self.var_qty_label)
        ttk.Label(self.order_form, textvariable=self.var_error, style="Error.TLabel").pack(
            anchor="w", pady=(0, 10)
        )
        ttk.Button(
            self.order_form,
            text="Log order",
            style="Primary.TButton",
            command=self.log_order,
        ).pack(fill="x")

    def _labeled(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        label_var: tk.StringVar | None = None,
    ) -> ttk.Entry:
        wrap = ttk.Frame(parent, style="Panel.TFrame")
        wrap.pack(fill="x", pady=(0, 10))
        if label_var is None:
            ttk.Label(wrap, text=label, style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        else:
            ttk.Label(wrap, textvariable=label_var, style="Field.TLabel").pack(
                anchor="w", pady=(0, 4)
            )
        entry = ttk.Entry(wrap, textvariable=variable, style="Ticket.TEntry")
        entry.pack(fill="x", ipady=4)
        return entry

    def _build_register(self, parent: ttk.Frame) -> None:
        register = ttk.Frame(parent, style="Panel.TFrame", padding=18)
        register.grid(row=0, column=1, sticky="nsew")
        register.columnconfigure(0, weight=1)
        register.rowconfigure(2, weight=1)

        stats = ttk.Frame(register, style="Panel.TFrame")
        stats.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        for i in range(3):
            stats.columnconfigure(i, weight=1)
        self._stat(stats, 0, "ON THE LIST", self.var_count)
        self._stat(stats, 1, "OUTSTANDING", self.var_outstanding)
        self._stat(stats, 2, "RECEIVED", self.var_received)

        filters = ttk.Frame(register, style="Panel.TFrame")
        filters.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(filters, text="SEARCH", style="Field.TLabel").pack(side="left")
        search = ttk.Entry(filters, textvariable=self.var_search, style="Ticket.TEntry", width=20)
        search.pack(side="left", padx=(8, 16), ipady=3)
        for value, label in (
            ("all", "All"),
            ("outstanding", "Outstanding"),
            ("received", "Received"),
        ):
            ttk.Radiobutton(
                filters,
                text=label,
                value=value,
                variable=self.var_filter,
                style="Filter.TRadiobutton",
                command=self.refresh,
            ).pack(side="left", padx=(0, 8))

        table_wrap = ttk.Frame(register, style="Panel.TFrame")
        table_wrap.grid(row=2, column=0, sticky="nsew")
        table_wrap.columnconfigure(0, weight=1)
        table_wrap.rowconfigure(0, weight=1)

        columns = ("purchaser", "product", "progress", "remaining", "status")
        self.tree = ttk.Treeview(
            table_wrap,
            columns=columns,
            show="headings",
            style="Ledger.Treeview",
            selectmode="browse",
        )
        headings = {
            "purchaser": ("Purchaser", 180, "w"),
            "product": ("Product", 150, "w"),
            "progress": ("Received / ordered", 150, "e"),
            "remaining": ("Still due", 90, "e"),
            "status": ("Status", 120, "w"),
        }
        for key, (title, width, anchor) in headings.items():
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=True)
        scroll = ttk.Scrollbar(
            table_wrap,
            orient="vertical",
            command=self.tree.yview,
            style="Ledger.Vertical.TScrollbar",
        )
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.tag_configure("odd", background=WHITE)
        self.tree.tag_configure("even", background=ROW_ALT)
        self.tree.tag_configure("done", background=RECEIVED_BG)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.empty_label = ttk.Label(
            table_wrap,
            text="No orders yet. Log a purchaser on the left.",
            style="Empty.TLabel",
            justify="center",
        )

        detail = ttk.Frame(register, style="Panel.TFrame")
        detail.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        ttk.Label(detail, text="HANDED OUT SO FAR", style="Field.TLabel").pack(anchor="w")
        ttk.Label(detail, textvariable=self.var_detail, style="Hint.TLabel", wraplength=640).pack(
            anchor="w", pady=(2, 8)
        )
        row = ttk.Frame(detail, style="Panel.TFrame")
        row.pack(fill="x")
        ttk.Label(row, textvariable=self.var_got_hint, style="Field.TLabel").pack(side="left")
        self.got_entry = ttk.Entry(row, textvariable=self.var_got, style="Ticket.TEntry", width=10)
        self.got_entry.pack(side="left", padx=(8, 8), ipady=4)
        ttk.Button(row, text="Update received", style="Primary.TButton", command=self.update_received).pack(
            side="left"
        )
        ttk.Button(
            row,
            text="Mark all received",
            style="Ghost.TButton",
            command=self.mark_all_received,
        ).pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(
            detail, style="Fill.Horizontal.TProgressbar", maximum=100, mode="determinate"
        )
        self.progress.pack(fill="x", pady=(10, 0))

    def _stat(self, parent: ttk.Frame, column: int, caption: str, variable: tk.StringVar) -> None:
        cell = ttk.Frame(parent, style="Panel.TFrame", padding=(8, 4))
        cell.grid(row=0, column=column, sticky="ew")
        ttk.Label(cell, text=caption, style="StatCaption.TLabel").pack(anchor="w")
        ttk.Label(cell, textvariable=variable, style="StatValue.TLabel").pack(anchor="w")

    def _binds(self) -> None:
        self.bind("<Control-s>", lambda _e: self.log_order())
        self.bind("<Control-n>", lambda _e: self.open_wizard())
        self.bind("<Control-comma>", lambda _e: self.open_settings())
        self.got_entry.bind("<Return>", lambda _e: self.update_received())

    def _maybe_prompt_product(self) -> None:
        if not self.tracker.list_products():
            self.open_wizard()

    def open_wizard(self) -> None:
        ProductWizard(self, self.tracker, on_saved=self._on_product_saved)

    def open_settings(self) -> None:
        SettingsDialog(self, self.tracker, on_change=self.refresh)

    def _on_product_saved(self, product: Product) -> None:
        self.refresh(select_product=product.name)
        self.var_error.set("")

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

    def log_order(self) -> None:
        self.var_error.set("")
        try:
            order = self.tracker.add_order(
                purchaser=self.var_purchaser.get(),
                quantity=self.var_qty.get(),
                product=self.var_product.get() or None,
            )
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        self.var_purchaser.set("")
        self.var_qty.set("")
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
            self.var_detail.set("Select a name to update what they have received.")
            self.var_got.set("")
            self.var_got_hint.set("")
            self.progress["value"] = 0
            return
        try:
            order = self.tracker.get_order(order_id)
        except TrackerError:
            return
        self._fill_detail(order)

    def _fill_detail(self, order: Order) -> None:
        unit = order.product_unit
        self.var_detail.set(
            f"{order.purchaser} ordered {format_qty(order.quantity_ordered)} {unit} "
            f"of {order.product_name}."
        )
        self.var_got.set(format_qty(order.quantity_received))
        self.var_got_hint.set(f"RECEIVED SO FAR (of {format_qty(order.quantity_ordered)})")
        if order.quantity_ordered > 0:
            pct = float(order.quantity_received / order.quantity_ordered * 100)
        else:
            pct = 0.0
        self.progress["value"] = min(100.0, pct)

    def update_received(self) -> None:
        order_id = self._selected_id()
        if order_id is None:
            self.var_error.set("Select a purchaser on the list first.")
            return
        try:
            order = self.tracker.set_received(order_id, self.var_got.get())
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return
        self.var_error.set("")
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
        self.refresh(select_id=order.id)

    def refresh(
        self,
        select_id: int | None = None,
        select_product: str | None = None,
    ) -> None:
        products = self._reload_products(select_product)
        if products:
            names = ", ".join(product.name for product in products[:3])
            extra = "" if len(products) <= 3 else f" +{len(products) - 3}"
            self.var_headline.set(names + extra)
        else:
            self.var_headline.set("Establish a product")

        search = self.var_search.get().strip() or None
        status = self.var_filter.get() or "all"
        try:
            orders = self.tracker.list_orders(search=search, status=status)
        except TrackerError as exc:
            self.var_error.set(str(exc))
            return

        keep = select_id if select_id is not None else self._selected_id()
        self.tree.delete(*self.tree.get_children())
        for index, order in enumerate(orders):
            if order.fulfilled:
                tag = "done"
                remaining = "—"
                status_text = "received"
            else:
                tag = "even" if index % 2 == 0 else "odd"
                remaining = format_qty(order.remaining)
                status_text = "outstanding"
            self.tree.insert(
                "",
                "end",
                iid=str(order.id),
                values=(
                    order.purchaser,
                    order.product_name,
                    f"{format_qty(order.quantity_received)} / {format_qty(order.quantity_ordered)}",
                    remaining,
                    status_text,
                ),
                tags=(tag,),
            )

        if orders:
            self.empty_label.place_forget()
        else:
            self.empty_label.place(relx=0.5, rely=0.45, anchor="center")

        if keep is not None and self.tree.exists(str(keep)):
            self.tree.selection_set(str(keep))
            self.tree.see(str(keep))
            self._on_select()
        elif not orders:
            self.selected_order_id = None
            self.var_detail.set("Select a name to update what they have received.")
            self.var_got.set("")
            self.progress["value"] = 0

        summary = self.tracker.summary()
        self.var_due.set(format_qty(summary.units_remaining))
        self.var_count.set(str(summary.order_count))
        self.var_outstanding.set(str(summary.outstanding_count))
        self.var_received.set(str(summary.received_count))
        if products:
            first = products[0]
            self.var_meta.set(
                f"{summary.outstanding_count} outstanding  ·  "
                f"{format_qty(summary.units_remaining)} {first.unit} still due  ·  "
                f"{format_money(summary.revenue)} ordered"
            )
        else:
            self.var_meta.set("Nothing to sell yet")

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
