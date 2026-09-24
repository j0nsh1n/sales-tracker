/* Shared sample ledger and rules for the four design explorations.
 *
 * Every direction loads this file, so the data, the terminology and the
 * validation are identical across A, B, C and D. The rules mirror
 * salestracker/store.py: purchaser required, quantity > 0, received between
 * 0 and ordered, duplicate product names refused, bill counts whole numbers.
 * State lives in memory only; reloading the page restores the sample.
 */
(function (global) {
  "use strict";

  var SAMPLE = {
    products: [
      { id: 1, name: "Honey", unit: "jar", price: 12.5, sku: "H-01", notes: "Raw wildflower, 12 oz" },
      { id: 2, name: "Strawberry jam", unit: "jar", price: 6, sku: "", notes: "" },
      { id: 3, name: "Beeswax candle", unit: "each", price: 8, sku: "C-2", notes: "" }
    ],
    // id, purchaser, product id, ordered, received, paid by, logged
    orders: [
      [1, "Jim Carter", 1, 10, 5, "cash", "2026-09-12T10:04"],
      [2, "Ann Lee", 2, 4, 4, "venmo", "2026-09-14T11:30"],
      [3, "Ann Lee", 1, 2, 0, "venmo", "2026-09-20T09:12"],
      [4, "Marcus Hill", 3, 6, 6, "cash", "2026-09-15T15:45"],
      [5, "Priya Nair", 1, 3, 1, "other", "2026-09-18T13:20"],
      [6, "Jimena Ortiz", 2, 5, 0, "cash", "2026-09-21T10:50"],
      [7, "Jim Carter", 3, 2, 0, "cash", "2026-09-22T16:05"],
      [8, "Tom Becker", 1, 12, 12, "cash", "2026-09-10T09:00"],
      [9, "Sara Kim", 2, 2, 0, "venmo", "2026-09-23T12:15"]
    ]
  };

  var FIXTURES = {
    sample: SAMPLE,
    "no-orders": { products: SAMPLE.products, orders: [] },
    empty: { products: [], orders: [] }
  };
  var FIXTURE_LABELS = {
    sample: "Sample ledger (3 products, 9 orders)",
    "no-orders": "Products only, no orders yet",
    empty: "Nothing set up yet"
  };

  var METHODS = ["cash", "venmo", "other"];
  var DENOMINATIONS = [100, 50, 20, 10, 5, 2, 1];
  var UNITS = ["each", "jar", "box", "dozen", "lb", "bag", "case", "pack", "bottle"];

  function round2(n) { return Math.round(n * 100) / 100; }
  function round3(n) { return Math.round(n * 1000) / 1000; }

  function money(n) {
    var sign = n < 0 ? "−" : "";
    return sign + "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function qty(n) { return String(round3(n)); }
  function cap(s) { s = String(s || ""); return s ? s[0].toUpperCase() + s.slice(1) : ""; }
  function initials(name) {
    var words = String(name || "").replace(/-/g, " ").split(/\s+/).filter(Boolean);
    if (!words.length) return "?";
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0][0] + words[words.length - 1][0]).toUpperCase();
  }
  function when(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return String(iso || "");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  }
  function whenLong(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return String(iso || "");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + ", " +
      d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function plural(n, one, many) { return n === 1 ? one : (many || one + "s"); }

  function parseNumber(value, field) {
    var text = String(value == null ? "" : value).trim();
    if (text === "") throw new Error(field + " is required.");
    var n = Number(text);
    if (!isFinite(n)) throw new Error(field + " must be a number.");
    return n;
  }
  function parseQuantity(value) {
    var n = parseNumber(value, "quantity");
    if (n <= 0) throw new Error("quantity must be greater than zero.");
    return round3(n);
  }
  function parseReceived(value, max) {
    var n = parseNumber(value, "received");
    if (n < 0) throw new Error("received cannot be negative.");
    if (n > max + 1e-9) throw new Error("received cannot be more than " + qty(max) + ".");
    return round3(n);
  }
  function parseMoney(value) {
    var n = parseNumber(value, "price");
    if (n < 0) throw new Error("price cannot be negative.");
    return round2(n);
  }
  function parseMethod(value) {
    var m = String(value == null ? "" : value).trim().toLowerCase() || "cash";
    if (METHODS.indexOf(m) < 0) throw new Error("payment method must be one of: cash, venmo, other.");
    return m;
  }
  function parseBillCount(value, denomination) {
    var text = String(value == null ? "" : value).trim();
    if (text === "") return 0;
    var n = Number(text);
    if (!isFinite(n) || n !== Math.floor(n)) {
      throw new Error("count of $" + denomination + " bills must be a whole number.");
    }
    if (n < 0) throw new Error("count of $" + denomination + " bills cannot be negative.");
    return n;
  }

  function createStore() {
    var state = { products: [], orders: [], nextProduct: 1, nextOrder: 1, fixture: "sample" };
    var listeners = [];
    var now = function () { return new Date().toISOString().slice(0, 16); };

    function emit() { listeners.forEach(function (fn) { fn(); }); }

    function load(name) {
      var fx = FIXTURES[name] || FIXTURES.sample;
      state.fixture = FIXTURES[name] ? name : "sample";
      state.products = fx.products.map(function (p) { return Object.assign({}, p); });
      state.orders = fx.orders.map(function (row) {
        return { id: row[0], purchaser: row[1], productId: row[2], ordered: row[3],
                 received: row[4], method: row[5], created: row[6], updated: row[6] };
      });
      state.nextProduct = state.products.reduce(function (m, p) { return Math.max(m, p.id); }, 0) + 1;
      state.nextOrder = state.orders.reduce(function (m, o) { return Math.max(m, o.id); }, 0) + 1;
      emit();
    }

    function product(id) {
      var p = state.products.filter(function (x) { return x.id === Number(id); })[0];
      if (!p) throw new Error("No product with id " + id + ".");
      return p;
    }
    function findProduct(nameOrId) {
      var text = String(nameOrId == null ? "" : nameOrId).trim();
      var byName = state.products.filter(function (p) { return p.name.toLowerCase() === text.toLowerCase(); })[0];
      if (byName) return byName;
      if (/^\d+$/.test(text)) return product(Number(text));
      throw new Error("No product named “" + text + "”.");
    }
    function products() {
      return state.products.slice().sort(function (a, b) {
        return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1;
      });
    }

    function view(o) {
      var p = product(o.productId);
      var remaining = Math.max(0, round3(o.ordered - o.received));
      return {
        id: o.id, purchaser: o.purchaser, productId: p.id, product: p.name, unit: p.unit,
        price: p.price, ordered: o.ordered, received: o.received, method: o.method,
        created: o.created, updated: o.updated, remaining: remaining,
        fulfilled: o.received >= o.ordered,
        status: o.received >= o.ordered ? "received" : "outstanding",
        total: round2(o.ordered * p.price),
        collected: round2(o.received * p.price),
        uncollected: round2(remaining * p.price)
      };
    }
    function order(id) {
      var o = state.orders.filter(function (x) { return x.id === Number(id); })[0];
      if (!o) throw new Error("No order with id " + id + ".");
      return view(o);
    }
    function orders(opts) {
      opts = opts || {};
      var needle = String(opts.search || "").trim().toLowerCase();
      var status = opts.status || "all";
      return state.orders.map(view).filter(function (v) {
        if (needle && v.purchaser.toLowerCase().indexOf(needle) < 0 &&
            v.product.toLowerCase().indexOf(needle) < 0) return false;
        if (status === "outstanding" && v.fulfilled) return false;
        if (status === "received" && !v.fulfilled) return false;
        if (opts.productId && v.productId !== Number(opts.productId)) return false;
        return true;
      });
    }
    function purchasers() {
      var seen = {};
      state.orders.forEach(function (o) { seen[o.purchaser.toLowerCase()] = o.purchaser; });
      return Object.keys(seen).sort().map(function (k) { return seen[k]; });
    }

    function validatedProduct(fields) {
      var name = String(fields.name || "").trim();
      var unit = String(fields.unit || "").trim().toLowerCase() || "each";
      if (!name) throw new Error("product name is required.");
      if (name.length > 80) throw new Error("product name is too long.");
      return { name: name, unit: unit, price: parseMoney(fields.price == null || fields.price === "" ? "0" : fields.price),
               sku: String(fields.sku || "").trim(), notes: String(fields.notes || "").trim() };
    }
    function addProduct(fields) {
      var clean = validatedProduct(fields);
      if (state.products.some(function (p) { return p.name.toLowerCase() === clean.name.toLowerCase(); })) {
        throw new Error("A product named “" + clean.name + "” is already on file.");
      }
      clean.id = state.nextProduct++;
      state.products.push(clean);
      emit();
      return clean;
    }
    function editProduct(id, fields) {
      var current = product(id);
      var merged = Object.assign({}, current, fields);
      var clean = validatedProduct(merged);
      if (state.products.some(function (p) { return p.id !== current.id && p.name.toLowerCase() === clean.name.toLowerCase(); })) {
        throw new Error("A product named “" + clean.name + "” is already on file.");
      }
      Object.assign(current, clean);
      emit();
      return current;
    }
    function ordersFor(productId) {
      return state.orders.filter(function (o) { return o.productId === Number(productId); }).length;
    }
    function deleteProduct(id) {
      var p = product(id);
      var attached = ordersFor(id);
      if (attached) throw new Error(p.name + " still has " + attached + " order(s). Delete those orders first.");
      state.products = state.products.filter(function (x) { return x.id !== p.id; });
      emit();
      return p;
    }

    function addOrder(fields) {
      var list = products();
      if (!list.length) throw new Error("Establish a product first, then log who bought it.");
      var chosen;
      if (fields.product == null || fields.product === "") {
        if (list.length > 1) throw new Error("Choose which product this order is for.");
        chosen = list[0];
      } else {
        chosen = findProduct(fields.product);
      }
      var buyer = String(fields.purchaser || "").trim();
      if (!buyer) throw new Error("purchaser name is required.");
      var ordered = parseQuantity(fields.qty);
      var method = parseMethod(fields.method);
      var stamp = now();
      var o = { id: state.nextOrder++, purchaser: buyer, productId: chosen.id, ordered: ordered,
                received: 0, method: method, created: stamp, updated: stamp };
      state.orders.push(o);
      emit();
      return view(o);
    }
    function raw(id) {
      var o = state.orders.filter(function (x) { return x.id === Number(id); })[0];
      if (!o) throw new Error("No order with id " + id + ".");
      return o;
    }
    function setReceived(id, value) {
      var o = raw(id);
      o.received = parseReceived(value, o.ordered);
      o.updated = now();
      emit();
      return view(o);
    }
    function markReceived(id) { return setReceived(id, raw(id).ordered); }
    function setMethod(id, method) {
      var o = raw(id);
      o.method = parseMethod(method);
      o.updated = now();
      emit();
      return view(o);
    }
    function editOrder(id, fields) {
      var o = raw(id);
      var buyer = fields.purchaser == null ? o.purchaser : String(fields.purchaser).trim();
      if (!buyer) throw new Error("purchaser name is required.");
      var ordered = fields.qty == null || fields.qty === "" ? o.ordered : parseQuantity(fields.qty);
      if (ordered < o.received) {
        throw new Error("quantity cannot be less than the " + qty(o.received) + " already received.");
      }
      var productId = fields.product == null || fields.product === "" ? o.productId : findProduct(fields.product).id;
      var method = fields.method == null ? o.method : parseMethod(fields.method);
      o.purchaser = buyer; o.ordered = ordered; o.productId = productId; o.method = method; o.updated = now();
      emit();
      return view(o);
    }
    function deleteOrder(id) {
      var v = order(id);
      state.orders = state.orders.filter(function (x) { return x.id !== v.id; });
      emit();
      return v;
    }
    function resetOrders() {
      var n = state.orders.length;
      state.orders = [];
      emit();
      return n;
    }
    function resetAll() { state.orders = []; state.products = []; emit(); }

    function summary() {
      var all = state.orders.map(view);
      var s = { count: all.length, outstanding: 0, received: 0, unitsDue: 0, revenue: 0 };
      all.forEach(function (v) {
        if (v.fulfilled) s.received++; else s.outstanding++;
        s.unitsDue = round3(s.unitsDue + v.remaining);
        s.revenue = round2(s.revenue + v.total);
      });
      return s;
    }
    function financials() {
      var f = { cashCollected: 0, cashUncollected: 0, otherCollected: 0, otherUncollected: 0 };
      state.orders.map(view).forEach(function (v) {
        if (v.method === "cash") { f.cashCollected += v.collected; f.cashUncollected += v.uncollected; }
        else { f.otherCollected += v.collected; f.otherUncollected += v.uncollected; }
      });
      Object.keys(f).forEach(function (k) { f[k] = round2(f[k]); });
      f.totalCollected = round2(f.cashCollected + f.otherCollected);
      f.totalUncollected = round2(f.cashUncollected + f.otherUncollected);
      f.bookValue = round2(f.totalCollected + f.totalUncollected);
      return f;
    }
    function countCash(counts) {
      var total = 0;
      DENOMINATIONS.forEach(function (d) { total += d * parseBillCount(counts[d], d); });
      return round2(total);
    }
    function reconcile(counts) {
      var expected = financials().cashCollected;
      var counted = countCash(counts);
      var diff = round2(counted - expected);
      var r = { expected: expected, counted: counted, diff: diff, balanced: diff === 0 };
      if (r.balanced) r.headline = "Balanced — the drawer matches the ledger.";
      else if (diff > 0) r.headline = "Over by " + money(diff) + " — more cash than the ledger expects.";
      else r.headline = "Short by " + money(-diff) + " — less cash than the ledger expects.";
      r.note = r.balanced ? "" : "Check for a mislogged quantity, an order paid by Venmo but recorded as cash, or change given from the drawer.";
      return r;
    }

    function subscribe(fn) { listeners.push(fn); return function () { listeners = listeners.filter(function (f) { return f !== fn; }); }; }
    function attempt(fn) {
      try { return { ok: true, value: fn() }; }
      catch (e) { return { ok: false, error: e.message }; }
    }

    load("sample");
    return {
      load: load, subscribe: subscribe, attempt: attempt,
      get fixture() { return state.fixture; },
      products: products, product: product, findProduct: findProduct, ordersFor: ordersFor,
      addProduct: addProduct, editProduct: editProduct, deleteProduct: deleteProduct,
      orders: orders, order: order, purchasers: purchasers,
      addOrder: addOrder, setReceived: setReceived, markReceived: markReceived,
      setMethod: setMethod, editOrder: editOrder, deleteOrder: deleteOrder,
      resetOrders: resetOrders, resetAll: resetAll,
      summary: summary, financials: financials, countCash: countCash, reconcile: reconcile
    };
  }

  /* Simulated first paint: call render() once after a short delay so each
   * direction shows its loading state. Reduced-motion viewers skip it. */
  function boot(showLoading, render, ms) {
    var quick = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
    showLoading();
    setTimeout(render, quick ? 0 : (ms || 650));
  }

  global.ST = {
    createStore: createStore, boot: boot,
    FIXTURES: Object.keys(FIXTURES), FIXTURE_LABELS: FIXTURE_LABELS,
    METHODS: METHODS, DENOMINATIONS: DENOMINATIONS, UNITS: UNITS,
    money: money, qty: qty, cap: cap, initials: initials, when: when, whenLong: whenLong,
    esc: esc, plural: plural,
    parseQuantity: parseQuantity, parseReceived: parseReceived, parseMethod: parseMethod
  };
})(window);
