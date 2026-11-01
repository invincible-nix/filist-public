# Das filist-Fenster: Kopfzeile mit Tabs (wie datist/musicalist), Navigationsleiste, Seitenleiste,
# eine oder zwei Ansichten je Tab, Vorschau, Aufgaben, Statuszeile. Im Auswahl-Modus (Datei-Dialog anderer
# Programme über das Portal) zusätzlich unten eine Leiste mit Name, Filter und „öffnen“/„speichern“.
import fnmatch, os, shutil, subprocess, sys, time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

import dialogs
import glass
import icons
import model
import ops as opsmod
import panels
import places
import flyout
import share
import vpn
import tui
import views

_NEWS_CHECKED = False   # Changelog-Hinweis nur einmal pro Prozess
SIDE_MIN = 120    # schmalste Seitenleiste (kleine Fenster)
LIST_MIN = 100    # so viel Breite behält die Dateiliste mindestens, wenn Leisten eingepasst werden
NARROW_W = 1000   # darunter gelten eigene (gemerkte) Leisten-Breiten

TERMINALS = ["kitty", "foot", "alacritty", "wezterm", "ghostty", "gnome-terminal", "konsole", "xfce4-terminal",
             "xterm"]


class Tab:
    def __init__(self, win, uri):
        self.win = win
        self.panes = []
        self.panels = []
        self.active = 0
        self.box = Gtk.Box(spacing=8, homogeneous=True, hexpand=True, vexpand=True)
        self.add_pane(uri)
        self.button = None

    @property
    def pane(self):
        return self.panes[self.active]

    def add_pane(self, uri):
        p = views.Pane(self.win, uri, self.win.prefs)
        panel = tui.Panel("", p)
        panel.body.set_vexpand(True)
        panel.body.add_css_class("fw-panebody")
        p.panel = panel
        self.panes.append(p)
        self.panels.append(panel)
        self.box.append(panel)
        self.win.wire(self, p)
        return p

    def remove_pane(self, i):
        p, panel = self.panes.pop(i), self.panels.pop(i)
        if p.monitor:
            p.monitor.cancel()
        self.box.remove(panel)
        self.active = 0


class FwWindow(Gtk.ApplicationWindow):
    def __init__(self, app, uri=None, mode="browse", chooser=None):
        super().__init__(application=app, title="filist")
        self.app = app
        self.prefs = app.prefs
        self.ops = app.ops
        self.mode = mode                     # browse | open | save | dir
        self.chooser = chooser or {}
        self.set_default_size(*getattr(glass, "fit_size", lambda w, h: (w, h))(
            *((1180, 760) if mode == "browse" else (980, 640))))      # passt auf kleine Bildschirme
        getattr(glass, "frame_window", lambda w: w.set_decorated(False))(self)   # Kachel-WM: rahmenlos
        self.add_css_class("pb-window")
        self.add_css_class("tui")
        self.add_css_class("fw")
        self.theme = tui.Theme.get()
        self.tabs = []
        self.cur = None
        self._status_id = None
        self.cut_uris = set()

        root = tui.vbox(spacing=0)
        self.set_child(root)
        root.append(self._build_top())
        root.append(self._build_nav())
        self.body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, shrink_start_child=False,
                              resize_start_child=False, wide_handle=False)
        self.body.add_css_class("fw-body")
        self.places = places.Places(self)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.places, vexpand=True)
        self.side_panel = tui.Panel("orte", sc)
        self.side_panel.body.set_vexpand(True)
        self.side_panel.set_size_request(SIDE_MIN, -1)
        self.body.set_start_child(self.side_panel)
        self.body.set_position(max(SIDE_MIN, int(self.prefs.get("side_width", 210))))
        self.body.connect("notify::position", self._side_moved)
        self.side_panel.set_visible(self.prefs.get("sidebar", True))
        self.tab_stack = Gtk.Stack(hexpand=True, vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE,
                                   transition_duration=80, hhomogeneous=False)
        self.preview = panels.Preview()
        psc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=self.preview, vexpand=True)
        self.prev_panel = tui.Panel("vorschau", psc)
        self.prev_panel.body.set_vexpand(True)
        self.prev_panel.set_size_request(panels.PREV_MIN, -1)
        self.prev_panel.set_visible(self.prefs.get("preview", False))
        # Vorschau-Leiste mit Griff: Breite per Ziehen änderbar, wird gemerkt (prefs["preview_width"])
        self.prev_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, start_child=self.tab_stack,
                                    end_child=self.prev_panel, resize_start_child=True, resize_end_child=False,
                                    shrink_start_child=False, shrink_end_child=False, wide_handle=False)
        self.prev_split.add_css_class("fw-prevsplit")
        self._placing = False           # True, während fit_panels() die Griffe selbst setzt
        self._save_id = None
        self.prev_split.connect("notify::position", self._prev_moved)
        self._pressed = False           # Maus/Finger gedrückt → Positionsänderung = Ziehen am Griff
        leg = Gtk.EventControllerLegacy(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        leg.connect("event", self._body_event)
        self.body.add_controller(leg)
        self.body.set_end_child(self.prev_split)
        root.append(self.body)
        self.jobs = panels.Jobs(self.ops)
        self.jobs_panel = tui.Panel("aufgaben", self.jobs)
        self.jobs_panel.add_css_class("fw-jobspanel")
        self.jobs_panel.set_visible(False)
        self.jobs.connect("notify::visible", lambda *_: self.jobs_panel.set_visible(self.jobs.get_visible()))
        root.append(self.jobs_panel)
        if mode != "browse":
            root.append(self._build_chooser_bar())
        root.append(self._build_status())

        self._actions()
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        mouse = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        mouse.connect("pressed", self._mouse_nav)
        self.add_controller(mouse)
        self.connect("close-request", self._on_close)

        self.new_tab(uri or self.prefs.get("start_uri") or model.uri_for(os.path.expanduser("~")))
        if mode == "browse":
            self.set_title("filist")

    # ================================================================ Aufbau
    def _build_top(self):
        top = tui.hbox(spacing=2)
        top.add_css_class("topbar")
        brand = tui.label("filist", "brand")
        self.brand = brand
        top.append(brand)
        self.tab_box = tui.hbox(spacing=2)
        top.append(self.tab_box)
        plus = Gtk.Button(label="+", tooltip_text="Neuer Tab (Strg+T)", focus_on_click=False)
        plus.add_css_class("tab")
        plus.connect("clicked", lambda *_: self.new_tab(self.pane.uri if self.cur else None))
        self.plus = plus
        top.append(plus)
        top.append(tui.label("", hexpand=True))
        self.mode_label = tui.label("", "accent")
        top.append(self.mode_label)
        top.append(self._changelog_btn())
        self.menu_slot = tui.hbox(spacing=0)               # Zahnrad (Ansicht & Einstellungen) – füllt _build_nav
        self.menu_slot.set_margin_start(6)
        top.append(self.menu_slot)
        gt = glass.GlassToggle(self)
        gt.set_margin_start(6)
        top.append(gt)
        close = tui.Button("✕", self.close, tooltip="Schließen (Strg+Q)")
        close.add_css_class("close")
        close.set_margin_start(4)
        top.append(close)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                propagate_natural_height=True, child=top)
        return Gtk.WindowHandle(child=sc)

    def _changelog_btn(self):
        """„v1.2.0“ oben rechts → Changelog. Nach einem Update einmal hervorgehoben (nur im ersten Fenster)."""
        here = os.path.dirname(os.path.realpath(__file__))
        try:
            version = open(os.path.join(here, "VERSION")).read().strip()
        except OSError:
            version = ""
        b = Gtk.Button(label=f"v{version}", tooltip_text="changelog – was ist neu?", focus_on_click=False,
                       valign=Gtk.Align.CENTER, margin_start=6)
        b.add_css_class("flat")
        b.add_css_class("off")

        def show(*_):
            import changelog
            changelog.open_window(self, "filist", os.path.join(here, "CHANGELOG.md"), key="filist", version=version)
            b.remove_css_class("news")
        b.connect("clicked", show)
        global _NEWS_CHECKED
        if not _NEWS_CHECKED and version and self.mode == "browse":
            _NEWS_CHECKED = True
            try:
                import changelog
                if changelog.check_new("filist", version):
                    b.add_css_class("news")
                    b.remove_css_class("off")
                    b.set_label(f"󰋚 v{version}")
                    GLib.timeout_add(600, lambda: (self.say(f"neu in v{version} – klick oben rechts auf "
                                                            f"„v{version}“ für den changelog", seconds=12), False)[1])
            except Exception as e:
                print("changelog:", e, file=sys.stderr)
        try:                      # neuere Version auf GitHub? → Knopf „󰚰 v1.2.0 → v1.3.0“
            import changelog
            tell = self.mode == "browse"

            def has_update(latest):
                if tell and not getattr(FwWindow, "_update_told", None) == latest:
                    FwWindow._update_told = latest
                    GLib.timeout_add(900, lambda: (self.say(changelog.update_text(latest), seconds=12), False)[1])
            getattr(changelog, "check_update", lambda *a: None)("filist", version, has_update)
        except Exception as e:
            print("update-check:", e, file=sys.stderr)
        box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        box.append(b)
        try:                      # 󰚰 update neben der Version (lädt + installiert)
            import changelog
            upd = changelog.update_button("filist", version, notify=self.say, classes=("flat", "news"))
            upd.set_margin_start(2)
            box.append(upd)
        except Exception as e:
            print("update-button:", e, file=sys.stderr)
        return box

    def _build_nav(self):
        nav = tui.hbox(spacing=4)
        nav.add_css_class("fw-nav")

        def flat(glyph, tip, cb):
            b = Gtk.Button(label=glyph, tooltip_text=tip, focus_on_click=False, valign=Gtk.Align.CENTER)
            b.add_css_class("flat")
            b.add_css_class("fw-navbtn")
            b.connect("clicked", lambda *_: cb())
            nav.append(b)
            return b
        self.btn_back = flat("\U000F0141", "Zurück (Alt+←)", lambda: self.pane.go_back())
        self.btn_fwd = flat("\U000F0142", "Vor (Alt+→)", lambda: self.pane.go_forward())
        self.btn_up = flat("\U000F0143", "Übergeordneter Ordner (Alt+↑)", lambda: self.pane.go_up())
        flat("\U000F0450", "Neu laden (Strg+R)", lambda: self.pane.reload())
        self.path = PathBar(self)
        nav.append(self.path)
        self.filter = tui.Entry("filtern · enter = überall suchen", width=24)
        self.filter.set_hexpand(False)
        self.filter.set_tooltip_text("enter = in allen Unterordnern suchen. Filter dazu:\n"
                                     "typ:bild|video|audio|dokument|archiv|code|ordner   ext:pdf,docx\n"
                                     "größe:>10M   größe:1k-5M   geändert:<7d (neuer)   geändert:>1y (älter)   geändert:heute")
        self.filter.connect("changed", self._filter_changed)
        self.filter.connect("activate", lambda *_: self._search())
        fk = Gtk.EventControllerKey()
        fk.connect("key-pressed", self._filter_key)
        self.filter.add_controller(fk)
        nav.append(self.filter)
        self.view_btns = {}
        for v, glyph, tip in (("list", "\U000F0572", "Liste (Strg+1)"), ("grid", "\U000F0570", "Raster (Strg+2)")):
            b = Gtk.ToggleButton(label=glyph, tooltip_text=tip, focus_on_click=False, valign=Gtk.Align.CENTER)
            b.add_css_class("tab")
            b.add_css_class("fw-viewbtn")
            if self.view_btns:
                b.set_group(next(iter(self.view_btns.values())))
            b.connect("toggled", lambda btn, v=v: btn.get_active() and self.set_view(v))
            self.view_btns[v] = b
            nav.append(b)
        self.menu_btn = Gtk.MenuButton(label="\U000F0493", tooltip_text="Einstellungen & Ansicht",
                                       valign=Gtk.Align.CENTER)
        self.menu_btn.add_css_class("tbtn")
        self.menu_btn.set_always_show_arrow(False)
        self.menu_btn.set_popover(self._settings_popover())
        self.menu_slot.append(self.menu_btn)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                propagate_natural_height=True, child=nav)
        return sc

    def _settings_popover(self):
        pop = Gtk.Popover(has_arrow=False)
        pop.add_css_class("tpop")
        box = tui.vbox(spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        self.chk = {}

        def check(key, text, default, cb):
            c = tui.Check(text, self.prefs.get(key, default), cb)
            self.chk[key] = c
            box.append(c)
        check("hidden", "versteckte dateien  (strg+h)", False, lambda v: self.set_hidden(v))
        check("folders_first", "ordner zuerst", True, lambda v: self._pref("folders_first", v, resort=True))
        check("thumbs", "vorschaubilder im raster", True, lambda v: self._pref("thumbs", v, rebind=True))
        check("preview", "vorschau-leiste  (leertaste)", False, lambda v: self.toggle_preview(v))
        check("sidebar", "seitenleiste  (f9)", True, lambda v: self.toggle_sidebar(v))
        check("split", "geteilte ansicht  (f3)", False, lambda v: self.toggle_split(v))
        box.append(tui.label("sortieren", "dim"))
        self.sort_sel = tui.Select(lambda f: self.set_sort(f, self.pane.sort_desc))
        self.sort_sel.set_options([("name", "name"), ("size", "größe"), ("mtime", "geändert"), ("type", "typ")],
                                  self.prefs.get("sort", "name"))
        box.append(self.sort_sel)
        check("sort_desc", "absteigend", False, lambda v: self.set_sort(self.pane.sort_field, v))
        box.append(tui.label("terminal", "dim"))
        term = tui.Entry(text=self.prefs.get("terminal", ""), placeholder="automatisch (kitty, foot …)", width=22)
        term.connect("changed", lambda e: self.prefs.__setitem__("terminal", e.get_text().strip()))
        box.append(term)
        box.append(tui.label("design", "dim"))
        box.append(tui.theme_button())
        pop.set_child(box)
        return pop

    def _build_status(self):
        self.status = tui.label("", "msg", hexpand=True, ellipsize=True)
        self.status.set_selectable(False)
        self.sel_info = tui.label("", "dim")
        self.hints = tui.hbox(spacing=12)
        hints = (("strg+t", "tab"), ("f3", "teilen"), ("leer", "vorschau"), ("f2", "umbenennen"),
                 ("strg+h", "versteckt"))
        if self.mode != "browse":
            hints = (("enter", "auswählen"), ("esc", "abbrechen"), ("strg+h", "versteckt"))
        for k, v in hints:
            self.hints.append(tui.hbox(tui.label(k, "accent"), tui.label(v, "dim"), spacing=5))
        bar = tui.hbox(self.status, self.sel_info, self.hints, spacing=14)
        bar.add_css_class("statusbar")
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                propagate_natural_height=True, child=bar)
        return sc

    def _build_chooser_bar(self):
        c = self.chooser
        bar = tui.hbox(spacing=8)
        bar.add_css_class("fw-choose")
        self.name_entry = None
        if self.mode == "save":
            self.name_entry = tui.Entry("dateiname", text=c.get("current_name", ""))
            self.name_entry.connect("activate", lambda *_: self.accept())
            bar.append(tui.label("name", "dim"))
            bar.append(self.name_entry)
        else:
            bar.append(tui.label("", hexpand=True))
        self.filters = c.get("filters") or []
        self.filter_idx = c.get("current_filter_index", 0)
        if self.filters:
            sel = tui.Select(self._choose_filter, max_chars=30)
            sel.set_options([(str(i), f[0]) for i, f in enumerate(self.filters)], str(self.filter_idx))
            bar.append(sel)
        cancel = tui.Button("abbrechen", lambda: self.finish(False))
        ok_text = c.get("accept_label") or {"open": "öffnen", "save": "speichern", "dir": "ordner wählen"}[self.mode]
        ok_text = ok_text.replace("_", "").lower()
        self.ok_btn = tui.Button(ok_text, self.accept)
        self.ok_btn.add_css_class("on")
        bar.append(cancel)
        bar.append(self.ok_btn)
        return bar

    # ================================================================ Breiten-Stufen
    def do_size_allocate(self, width, height, baseline):
        Gtk.ApplicationWindow.do_size_allocate(self, width, height, baseline)
        tier = 2 if width >= 760 else (1 if width >= 520 else 0)
        if tier != getattr(self, "_tier", None):
            self._tier = tier
            GLib.idle_add(self._apply_tier, tier)
        if not getattr(self, "_pane_check", None):
            self._pane_check = GLib.idle_add(self._check_panes)
        if width != getattr(self, "_alloc_w", None):
            self._alloc_w = width
            GLib.idle_add(self.fit_panels)
            if getattr(self, "_fit_id", None):          # nach dem Größenwechsel (Animation) noch einmal
                GLib.source_remove(self._fit_id)
            self._fit_id = GLib.timeout_add(150, self._refit)

    def _check_panes(self):
        """Spalten der Listen an die tatsächliche Breite anpassen (auch nach Teilen/Seitenleiste)."""
        self._pane_check = None
        for t in self.tabs:
            for p in t.panes:
                p.check_width()
        return False

    def _apply_tier(self, tier):
        """Schmal (z. B. gekachelt): Kopfzeile verschlanken. Seitenleiste und Vorschau bleiben, wie sie
        eingestellt sind – fit_panels() macht sie in kleinen Fenstern nur vorübergehend schmaler."""
        self.brand.set_visible(tier >= 1)
        self.filter.set_width_chars(24 if tier >= 2 else 12)
        self.hints.set_visible(tier >= 2)
        return False

    # ================================================================ Tabs
    def new_tab(self, uri=None, switch=True):
        tab = Tab(self, uri or model.uri_for(os.path.expanduser("~")))
        self.tabs.append(tab)
        self.tab_stack.add_child(tab.box)
        btn = Gtk.ToggleButton(focus_on_click=False)
        btn.add_css_class("tab")
        key = tui.label("", "key")
        name = tui.label("", ellipsize=True)
        name.set_max_width_chars(18)
        x = Gtk.Button(label="✕", focus_on_click=False, tooltip_text="Tab schließen (Strg+W)")
        x.add_css_class("fw-tabx")
        x.connect("clicked", lambda *_: self.close_tab(tab))
        btn.set_child(tui.hbox(key, name, x, spacing=3))
        btn._key, btn._name, btn._x = key, name, x
        if self.tabs[:-1]:
            btn.set_group(self.tabs[0].button)
        btn.connect("toggled", lambda b: b.get_active() and self.switch_tab(tab))
        mid = Gtk.GestureClick(button=2)
        mid.connect("pressed", lambda *_: self.close_tab(tab))
        btn.add_controller(mid)
        # Das ✕ steckt im Tab-Knopf, der den Klick sonst selbst schluckt → vorher abfangen
        xclick = Gtk.GestureClick(button=1, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        xclick.connect("pressed", self._tab_x_pressed, tab)
        btn.add_controller(xclick)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop.connect("enter", lambda *_: (self.switch_tab(tab), Gdk.DragAction.COPY)[1])
        btn.add_controller(drop)
        tab.button = btn
        self.tab_box.append(btn)
        self._number_tabs()
        if switch or self.cur is None:
            btn.set_active(True)
            self.switch_tab(tab)
        return tab

    def _tab_x_pressed(self, g, n, px, py, tab):
        btn = tab.button
        w = btn.pick(px, py, Gtk.PickFlags.DEFAULT)
        if btn._x.get_visible() and w is not None and (w is btn._x or w.is_ancestor(btn._x)):
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            GLib.idle_add(lambda: (tab in self.tabs and self.close_tab(tab), False)[1])

    def switch_tab(self, tab):
        if self.cur is tab and self.tab_stack.get_visible_child() is tab.box:
            return
        self.cur = tab
        self.tab_stack.set_visible_child(tab.box)
        if not tab.button.get_active():
            tab.button.set_active(True)
        self.chk_set("split", len(tab.panes) > 1)
        self._pane_changed(tab.pane)
        GLib.idle_add(lambda: tab.pane.focus_view() and False)

    def close_tab(self, tab=None):
        tab = tab or self.cur
        if len(self.tabs) == 1:
            self.close()
            return
        i = self.tabs.index(tab)
        for p in tab.panes:
            if p.monitor:
                p.monitor.cancel()
        self.tabs.remove(tab)
        self.tab_box.remove(tab.button)
        self.tab_stack.remove(tab.box)
        self._number_tabs()
        nxt = self.tabs[min(i, len(self.tabs) - 1)]
        nxt.button.set_active(True)
        self.switch_tab(nxt)

    def _number_tabs(self):
        many = len(self.tabs) > 1
        for i, t in enumerate(self.tabs):
            t.button._key.set_label(tui.SUP[i + 1] if i < 9 else "")
            t.button._x.set_visible(many)
        self.tab_box.set_visible(True)

    def cycle_tab(self, d):
        i = self.tabs.index(self.cur)
        t = self.tabs[(i + d) % len(self.tabs)]
        t.button.set_active(True)

    @property
    def pane(self):
        return self.cur.pane

    def current_uri(self):
        return self.pane.uri if self.cur else None

    def wire(self, tab, p):
        p.on_location = lambda pane: self._pane_changed(pane)
        p.on_loaded = lambda pane: (self._pane_changed(pane, loaded=True),
                                    pane.error is not None and self._load_failed(pane),
                                    pane.error is None and setattr(pane, "_reconnected", None))
        p.on_selection = lambda pane: self._selection_changed(pane)
        p.on_activate = lambda pane, items: self.activate_items(items, pane)
        p.on_context = lambda pane, items, x, y: self.context_menu(pane, items, x, y)
        p.on_focus = lambda pane: self._focus_pane(tab, pane)
        p.on_zoom = lambda pane, d: self.pinch_zoom(pane, d)
        p.on_middle = lambda pane, it: self.new_tab(it.uri, switch=False) if it.is_dir else self.activate_items([it],
                                                                                                                 pane)

    def _focus_pane(self, tab, pane):
        if pane in tab.panes and tab.panes.index(pane) != tab.active:
            tab.active = tab.panes.index(pane)
            self._pane_changed(pane)

    def _pane_changed(self, pane, loaded=False):
        tab = next((t for t in self.tabs if pane in t.panes), None)
        if tab is None:
            return
        for i, (p, panel) in enumerate(zip(tab.panes, tab.panels)):
            title = p.title()
            n = len(p.shown)
            extra = " · lädt …" if p.loading else (f" · {n}" if not p.search else
                                                     (f" · {n}" + (" …" if p.search.get("running") else "")))
            panel.set_title(f"{tui.esc(title)}<span alpha='60%'>{tui.esc(extra)}</span>", markup=True)
            panel.set_active(len(tab.panes) > 1 and i == tab.active)
        if tab.button:
            tab.button._name.set_label(tab.pane.title())
            tab.button.set_tooltip_text(model.path_of(tab.pane.uri) or tab.pane.uri)
        if tab is not self.cur or pane is not tab.pane:
            return
        self.path.set_uri(pane.uri, pane.search)
        self.btn_back.set_sensitive(bool(pane.back))
        self.btn_fwd.set_sensitive(bool(pane.fwd))
        self.btn_up.set_sensitive(Gio.File.new_for_uri(pane.uri).get_parent() is not None)
        self.view_btns[pane.view].set_active(True)
        if not pane.search and self.filter.get_text() != pane.filter:
            self._setting_filter = True
            self.filter.set_text(pane.filter)
            self._setting_filter = False
        self.places.highlight(pane.uri)
        if self.mode == "browse":
            self.set_title(f"filist – {model.path_of(pane.uri) or pane.uri}")
        self._selection_changed(pane)

    def _selection_changed(self, pane):
        if pane is not self.pane:
            return
        sel = pane.selected()
        if sel:
            files = [i for i in sel if not i.is_dir]
            size = sum(i.size or 0 for i in files)
            txt = f"{len(sel)} ausgewählt" + (f" · {model.human_size(size)}" if files else "")
        else:
            p = pane.local_path()
            free = model.disk_free(p)[0] if p else None
            txt = f"{len(pane.shown)} elemente" + (f" · {model.human_size(free)} frei" if free else "")
        self.sel_info.set_label(txt)
        if self.prev_panel.get_visible():
            self.preview.show_items(sel, pane)
        if self.mode == "save" and len(sel) == 1 and not sel[0].is_dir:
            self.name_entry.set_text(sel[0].name)

    # ================================================================ Navigation
    REMOTE = ("smb:", "sftp:", "ftp:", "dav:", "davs:", "afp:", "ssh:", "nfs:", "webdav:")

    def open_location(self, uri, pane=None):
        pane = pane or self.pane
        rule = vpn.rule_for(uri, self.prefs)
        if rule:                         # Ort braucht ein VPN → erst verbinden, dann einhängen/öffnen
            self._via_vpn(uri, rule, pane)
            return
        if uri.startswith(self.REMOTE) and not self._mounted(uri):
            self.mount_and_open(uri, pane)
            return
        pane.go(uri)
        pane.focus_view()
        if uri.startswith(self.REMOTE):
            self._watch_load(pane, uri)

    def _via_vpn(self, uri, rule, pane):
        name = rule["nm"]
        state = {"done": False}
        self.places.busy(uri, True)

        def cancel():
            state["done"] = True
            vpn.cancel(name)
            self.places.busy(uri, False)
            self.say("abgebrochen")

        def later():
            if not state["done"] and not state.get("step"):
                pane.show_busy(f"\U000F099D  vpn „{name}“ wird geprüft …", cancel)
            return False
        GLib.timeout_add(150, later)

        def step(text):
            if not state["done"]:
                state["step"] = True
                pane.show_busy("\U000F099D  " + text, cancel)
                self.say(text)

        def done(err, fresh=False):
            if state["done"]:                # vom Abbrechen-Knopf schon erledigt
                return
            state["done"] = True
            if err:
                self.places.busy(uri, False)
                pane.hide_busy()
                if err == "abgebrochen":
                    self.say("abgebrochen")
                else:
                    self.say(f"vpn „{name}“: {err}", error=True, seconds=12)
                return
            if fresh and uri.startswith(self.REMOTE) and self._mounted(uri):
                self._remount(uri, pane)     # VPN war weg → alte Verbindung zum Server ist tot
            elif uri.startswith(self.REMOTE) and not self._mounted(uri):
                self.mount_and_open(uri, pane)
            else:
                self.places.busy(uri, False)
                pane.go(uri)
                pane.focus_view()
                if uri.startswith(self.REMOTE):
                    self._watch_load(pane, uri)
        vpn.ensure(rule, done, step)

    def set_vpn(self, arg):
        uri, _, name = arg.partition("\n")
        rule = vpn.set_rule(self.prefs, uri, name or None)
        self.app.save_prefs()
        self.say(f"{views.location_name(uri)}: " + (f"vpn „{name}“ wird vorher automatisch verbunden" if rule
                                                      else "ohne vpn"))

    def _mounted(self, uri):
        try:
            Gio.File.new_for_uri(uri).find_enclosing_mount(None)
            return True
        except GLib.Error:
            return False

    def mount_and_open(self, uri, pane=None):
        pane = pane or self.pane
        f = Gio.File.new_for_uri(uri)
        op = Gtk.MountOperation.new(self)
        host = vpn.host_of(uri) or uri
        self.say(f"verbinde mit {host} …")
        cancellable = Gio.Cancellable()

        def cancel():
            cancellable.cancel()
            self.say("abgebrochen")
        pane.show_busy(f"\U000F0318  verbinde mit {host} …", cancel)
        self.places.busy(uri, True)

        def done(file, res):
            self.places.busy(uri, False)
            try:
                file.mount_enclosing_volume_finish(res)
            except GLib.Error as e:
                if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    return
                if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                    pane.hide_busy()
                    self.say(f"verbinden fehlgeschlagen: {self._mount_hint(e.message)}", error=True, seconds=12)
                    return
            self.say(f"verbunden: {host}")
            pane._reconnected = None
            pane.go(uri)
            pane.focus_view()
            self._watch_load(pane, uri)
        f.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, cancellable, done)

    @staticmethod
    def _mount_hint(msg):
        low = (msg or "").lower()
        if "timed out" in low or "timeout" in low or "zeitüberschreitung" in low:
            return "server antwortet nicht – vpn oder netz prüfen"
        if "no such host" in low or "could not resolve" in low or "name or service not known" in low:
            return "servername nicht gefunden – vpn oder netz prüfen"
        if "password" in low or "logon" in low or "denied" in low or "authentic" in low:
            return "anmeldung abgelehnt – benutzer/passwort stimmen?"
        return msg

    def _watch_load(self, pane, uri, seconds=15):
        """Netzlaufwerk lädt ewig (tote Verbindung)? Nach ein paar Sekunden einmal neu verbinden."""
        def check():
            if pane.uri == uri and pane.loading and getattr(pane, "_reconnected", None) != uri:
                pane.loader.gen += 1         # hängendes Laden verwerfen
                pane.loading = False
                pane.error = GLib.Error("zeitüberschreitung")
                self._load_failed(pane)
            return False
        GLib.timeout_add_seconds(seconds, check)

    def _load_failed(self, pane):
        """Netzlaufwerk antwortet nicht (VPN war weg, Verbindung veraltet): einmal neu verbinden und einhängen."""
        uri = pane.uri
        if not uri or not uri.startswith(self.REMOTE) or getattr(pane, "_reconnected", None) == uri:
            return
        pane._reconnected = uri
        rule = vpn.rule_for(uri, self.prefs)
        try:
            mount = Gio.File.new_for_uri(uri).find_enclosing_mount(None)
        except GLib.Error:
            mount = None

        def again(*_):
            if rule:
                self._via_vpn(uri, rule, pane)
            else:
                self.mount_and_open(uri, pane)
        alive = {"v": True}
        pane.show_busy("\U000F0318  verbindung wird erneuert …", lambda: alive.update(v=False))
        self._unmount_then(mount, lambda: alive["v"] and again())

    def _remount(self, uri, pane):
        try:
            mount = Gio.File.new_for_uri(uri).find_enclosing_mount(None)
        except GLib.Error:
            mount = None
        alive = {"v": True}
        pane.show_busy("\U000F0318  verbindung zum server wird erneuert …",
                       lambda: (alive.update(v=False), self.places.busy(uri, False)))
        self._unmount_then(mount, lambda: alive["v"] and self.mount_and_open(uri, pane))

    def _unmount_then(self, mount, then):
        if mount is not None and mount.can_unmount():
            mount.unmount_with_operation(Gio.MountUnmountFlags.FORCE, None, None,
                                         lambda m, res: (self._finish_unmount(m, res), then()))
        else:
            then()

    @staticmethod
    def _finish_unmount(m, res):
        try:
            m.unmount_with_operation_finish(res)
        except GLib.Error:
            pass

    def connect_server(self):
        dialogs.connect_server(self, lambda uri: (places.add_bookmark(uri), self.open_location(uri)))

    def set_view(self, v):
        self.pane.set_view(v)
        self.prefs["view"] = v
        self.app.save_prefs()

    def set_hidden(self, on):
        self.prefs["hidden"] = on
        for t in self.tabs:
            for p in t.panes:
                p.set_hidden(on)
        self.chk_set("hidden", on)
        self.app.save_prefs()
        self._pane_changed(self.pane)

    def set_sort(self, field, desc):
        self.prefs["sort"], self.prefs["sort_desc"] = field, desc
        for t in self.tabs:
            for p in t.panes:
                p.set_sort(field, desc)
        self.sort_sel.set_options(self.sort_sel.options, field)
        self.chk_set("sort_desc", desc)
        self.app.save_prefs()

    def _pref(self, key, value, resort=False, rebind=False):
        self.prefs[key] = value
        self.app.save_prefs()
        for t in self.tabs:
            for p in t.panes:
                if resort or rebind:
                    p._apply()

    def chk_set(self, key, value):
        c = getattr(self, "chk", {}).get(key)
        if c is not None:
            c.set_active(value)

    def _show_prev(self, on):
        if on == self.prev_panel.get_visible():
            return
        self.prev_panel.set_visible(on)
        if on:
            GLib.idle_add(self.fit_panels)

    def fit_panels(self):
        """Griffe so setzen, dass Seitenleiste und Vorschau ihre gemerkte Breite bekommen. Reicht der Platz
        nicht (halber Bildschirm o. ä.), werden beide anteilig schmaler – gemerkt bleibt die Wunschbreite."""
        w = self.body.get_width()
        if w <= 0:
            return False
        side_on, prev_on = self.side_panel.get_visible(), self.prev_panel.get_visible()
        side, prev = self._wish(w)
        side, prev = (side if side_on else 0), (prev if prev_on else 0)
        room = w - LIST_MIN - (8 if side_on else 0) - (8 if prev_on else 0)
        if side + prev > room and side + prev > 0:
            k = max(0.0, room) / (side + prev)
            side = max(SIDE_MIN, int(side * k)) if side_on else 0
            prev = max(panels.PREV_MIN, int(prev * k)) if prev_on else 0
        self._placing = True
        try:
            if side_on:
                self.body.set_position(side)
            if prev_on:
                pw = w - (side + 8 if side_on else 0)
                self.prev_split.set_position(max(0, pw - prev - 8))
        finally:
            self._placing = False
        return False

    def _body_event(self, _c, ev):
        if ev is None:            # GTK 4.14 (Ubuntu 24.04) liefert hier manchmal kein Ereignis
            return False
        t = ev.get_event_type()
        if t in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType.TOUCH_BEGIN):
            self._pressed = True
        elif t in (Gdk.EventType.BUTTON_RELEASE, Gdk.EventType.TOUCH_END, Gdk.EventType.TOUCH_CANCEL):
            self._pressed = False
        return False

    def _size_key(self):
        """Breiten werden getrennt gemerkt: kleines Fenster (halber Bildschirm o. ä.) und großes."""
        return "_narrow" if self.get_width() < NARROW_W else ""

    def _wish(self, w):
        """Gemerkte Wunschbreiten (Seitenleiste, Vorschau) für die aktuelle Fenstergröße."""
        p, k = self.prefs, self._size_key()
        side, prev = p.get("side_width", 210), p.get("preview_width", 320)
        if k and "preview_width" + k in p:
            side, prev = p.get("side_width" + k, side), p["preview_width" + k]
        elif k:           # schmal, noch nie gezogen: anteilig, die Liste behält gut 40 %
            room = w * 0.58 - 16
            if side + prev > room:
                f = room / (side + prev)
                side, prev = side * f, prev * f
        return max(SIDE_MIN, int(side)), max(panels.PREV_MIN, int(prev))

    def _refit(self):
        self._fit_id = None
        return self.fit_panels()

    def _user_drag(self):
        """Positionsänderung vom Ziehen am Griff? Sonst (Fenstergröße, fit_panels) bleibt die Wunschbreite."""
        return self._pressed and not self._placing

    def _side_moved(self, *_):
        if not self.side_panel.get_visible() or not self._user_drag():
            return
        self._save_widths()

    def _prev_moved(self, *_):
        if not self.prev_panel.get_visible() or not self._user_drag():
            return
        w = self.prev_split.get_width()
        if w <= 0:
            return
        self._save_widths()

    def _save_widths(self):
        if self._save_id:
            GLib.source_remove(self._save_id)

        def save():
            self._save_id = None
            k = self._size_key()
            side, prev = self._wish(self.body.get_width())
            if self.side_panel.get_visible():
                side = max(SIDE_MIN, self.body.get_position())
            pw = self.prev_split.get_width()
            if self.prev_panel.get_visible() and pw > 0:
                prev = max(panels.PREV_MIN, pw - self.prev_split.get_position() - 8)   # 8 = Griff
            self.prefs["side_width" + k], self.prefs["preview_width" + k] = int(side), int(prev)
            self.app.save_prefs()
            return False
        self._save_id = GLib.timeout_add(500, save)

    def toggle_preview(self, on=None):
        on = (not self.prev_panel.get_visible()) if on is None else on
        self._show_prev(on)
        self.prefs["preview"] = on
        self.chk_set("preview", on)
        if on:
            self.preview.show_items(self.pane.selected(), self.pane)
        self.app.save_prefs()

    def toggle_sidebar(self, on=None):
        on = (not self.side_panel.get_visible()) if on is None else on
        self.side_panel.set_visible(on)
        GLib.idle_add(self.fit_panels)
        self.prefs["sidebar"] = on
        self.chk_set("sidebar", on)
        self.app.save_prefs()

    def toggle_split(self, on=None):
        tab = self.cur
        on = (len(tab.panes) == 1) if on is None else on
        if on and len(tab.panes) == 1:
            p = tab.add_pane(tab.pane.uri)
            tab.active = 1
            GLib.idle_add(lambda: p.focus_view() and False)
        elif not on and len(tab.panes) > 1:
            tab.remove_pane(1 if tab.active == 0 else 0)
            tab.active = 0
        self.chk_set("split", len(tab.panes) > 1)
        self._pane_changed(tab.pane)

    def other_pane(self):
        tab = self.cur
        return tab.panes[1 - tab.active] if len(tab.panes) > 1 else None

    def _filter_changed(self, e):
        if getattr(self, "_setting_filter", False):
            return
        txt = e.get_text()
        if self.pane.search and not txt:
            self.pane.end_search()
            return
        if not self.pane.search:
            self.pane.set_filter(txt)
            self._pane_changed(self.pane)

    def _filter_key(self, _c, kv, _code, state):
        if kv == Gdk.KEY_Escape:
            self.clear_filter(self.pane)
            return True
        if kv in (Gdk.KEY_Down, Gdk.KEY_Tab):
            self.pane.focus_view()
            if self.pane.store.get_n_items() and not self.pane.selected():
                self.pane.sel.select_item(0, True)
            return True
        return False

    def _search(self):
        q = self.filter.get_text().strip()
        if q:
            self.pane.start_search(q)
            if self.pane.search_error:
                self.say(self.pane.search_error, error=True)
                return
            self.say(f"suche „{q}“ in {icons.home_relative(self.pane.search['root'])} …" if self.pane.search else
                     "suche geht nur in lokalen ordnern")

    def clear_filter(self, pane):
        self._setting_filter = True
        self.filter.set_text("")
        self._setting_filter = False
        if pane.search:
            pane.end_search()
        pane.set_filter("")
        pane.focus_view()
        self._pane_changed(pane)

    # ================================================================ Öffnen
    def activate_items(self, items, pane=None):
        pane = pane or self.pane
        dirs = [i for i in items if i.is_dir]
        files = [i for i in items if not i.is_dir]
        if self.mode != "browse":
            if files and self.mode == "open" and not dirs:
                self.accept()
                return
            if self.mode == "save" and files:
                self.name_entry.set_text(files[0].name)
                self.accept()
                return
        if dirs:
            if pane.search or pane.is_trash() and dirs[0].extra.get("gio_uri"):
                target = dirs[0].extra.get("gio_uri") if pane.is_trash() else dirs[0].uri
            else:
                target = dirs[0].uri
            pane.go(target)
            for d in dirs[1:]:
                self.new_tab(d.uri, switch=False)
            pane.focus_view()
        if self.mode == "browse":
            self.launch_default(files)

    def launch_default(self, files):
        if not files:
            return
        groups = {}
        for f in files:
            if f.uri.startswith("trash:") or f.extra.get("gio_uri", "").startswith("trash:"):
                self.say("dateien im papierkorb erst wiederherstellen", error=True)
                return
            groups.setdefault(f.ctype, []).append(f)
        for ctype, its in groups.items():
            app = Gio.AppInfo.get_default_for_type(ctype, False) if ctype else None
            if app is None:
                if opsmod.is_archive(its[0].name):
                    self.extract(its, here=True)
                    continue
                self.open_with(its)
                continue
            self.launch(app, its)

    def launch(self, app, items):
        ctx = self.get_display().get_app_launch_context()
        try:
            if app.supports_uris():
                app.launch_uris_async([i.uri for i in items], ctx, None, None, None)
            else:
                app.launch([Gio.File.new_for_uri(i.uri) for i in items], ctx)
            self.say(f"öffne {items[0].display if len(items) == 1 else f'{len(items)} dateien'} mit "
                     f"{app.get_display_name()}")
            recent = Gtk.RecentManager.get_default()
            for i in items[:20]:
                recent.add_item(i.uri)
        except GLib.Error as e:
            self.say(f"öffnen fehlgeschlagen: {e.message}", error=True)

    def open_with(self, items=None):
        items = [i for i in (items or self.pane.selected()) if not i.is_dir]
        if not items:
            return
        dialogs.OpenWith(self, items, self.launch, self.set_default_app).present()

    def set_default_app(self, ctype, app_id):
        app = next((a for a in Gio.AppInfo.get_all() if a.get_id() == app_id), None)
        if app is None:
            return
        try:
            app.set_as_default_for_type(ctype)
            self.say(f"{app.get_display_name()} ist jetzt standard für {model.type_description(ctype)}")
        except GLib.Error as e:
            self.say(e.message, error=True)

    def open_terminal(self, path=None):
        path = path or self.pane.local_path()
        sel = self.pane.selected()
        if sel and len(sel) == 1 and sel[0].is_dir and sel[0].path and path is None:
            path = sel[0].path
        if not path:
            self.say("terminal geht nur in lokalen ordnern", error=True)
            return
        term = self.prefs.get("terminal") or os.environ.get("TERMINAL") or next(
            (t for t in TERMINALS if shutil.which(t)), None)
        if not term:
            self.say("kein terminal gefunden", error=True)
            return
        args = term.split()
        name = os.path.basename(args[0])
        if name == "kitty":
            args += ["--directory", path]
        elif name in ("foot", "alacritty"):
            args += ["--working-directory", path]
        elif name == "wezterm":
            args += ["start", "--cwd", path]
        elif name in ("gnome-terminal", "xfce4-terminal"):
            args += [f"--working-directory={path}"]
        elif name == "konsole":
            args += ["--workdir", path]
        subprocess.Popen(args, cwd=path, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ================================================================ Operationen
    def targets(self):
        return self.pane.selected()

    def copy(self, cut=False):
        items = self.targets()
        if not items:
            return
        uris = [i.uri for i in items]
        special = ("cut" if cut else "copy") + "\n" + "\n".join(uris)
        prov = views.content_for(items)
        union = Gdk.ContentProvider.new_union([
            Gdk.ContentProvider.new_for_bytes("x-special/gnome-copied-files", GLib.Bytes.new(special.encode())),
            prov])
        self.get_clipboard().set_content(union)
        self.app.set_cut(uris if cut else ())
        self.say(("ausgeschnitten: " if cut else "kopiert: ") +
                 (items[0].display if len(items) == 1 else f"{len(items)} elemente"))

    def copy_path(self):
        items = self.targets()
        text = "\n".join(i.path or i.uri for i in items) if items else (self.pane.local_path() or self.pane.uri)
        self.get_clipboard().set(text)
        self.say("pfad kopiert")

    def paste(self, dest=None):
        dest = dest or self.pane.uri
        cb = self.get_clipboard()
        formats = cb.get_formats()
        if formats.contain_mime_type("x-special/gnome-copied-files"):
            cb.read_async(["x-special/gnome-copied-files"], GLib.PRIORITY_DEFAULT, None,
                          lambda c, res: self._paste_special(c, res, dest))
        elif formats.contain_gtype(Gdk.FileList):
            cb.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None,
                                lambda c, res: self._paste_files(c, res, dest))
        elif formats.contain_gtype(Gdk.Texture):
            cb.read_texture_async(None, lambda c, res: self._paste_image(c, res, dest))
        elif formats.contain_gtype(GObject.TYPE_STRING):
            cb.read_text_async(None, lambda c, res: self._paste_text(c, res, dest))
        else:
            self.say("nichts zum einfügen", error=True)

    def _paste_special(self, cb, res, dest):
        try:
            stream, _mime = cb.read_finish(res)
        except GLib.Error as e:
            self.say(e.message, error=True)
            return
        # asynchron lesen: kommt der Inhalt aus filist selbst, würde synchrones Lesen den eigenen
        # Hauptthread blockieren, der die Daten gerade liefern soll (Deadlock)
        out = Gio.MemoryOutputStream.new_resizable()
        out.splice_async(stream, Gio.OutputStreamSpliceFlags.CLOSE_SOURCE | Gio.OutputStreamSpliceFlags.CLOSE_TARGET,
                         GLib.PRIORITY_DEFAULT, None, lambda o, r: self._paste_special_data(o, r, dest))

    def _paste_special_data(self, out, res, dest):
        try:
            out.splice_finish(res)
            lines = out.steal_as_bytes().get_data().decode(errors="replace").splitlines()
        except GLib.Error as e:
            self.say(e.message, error=True)
            return
        if not lines:
            return
        cut = lines[0].strip() == "cut"
        uris = [l for l in lines[1:] if "://" in l]
        if uris:
            self.ops.transfer(uris, dest, move=cut, done=lambda j: self._after(j, select=True))
            if cut:
                self.get_clipboard().set_content(None)
                self.app.set_cut(())

    def _paste_files(self, cb, res, dest):
        try:
            fl = cb.read_value_finish(res)
            uris = [f.get_uri() for f in fl.get_files()]
        except GLib.Error as e:
            self.say(e.message, error=True)
            return
        if uris:
            self.ops.transfer(uris, dest, move=False, done=lambda j: self._after(j, select=True))

    def _paste_image(self, cb, res, dest):
        try:
            tex = cb.read_texture_finish(res)
        except GLib.Error as e:
            self.say(e.message, error=True)
            return
        folder = model.path_of(dest)
        if not folder:
            return
        path = opsmod._free_path(os.path.join(folder, time.strftime("eingefügt %Y-%m-%d %H.%M.%S.png")))
        tex.save_to_png(path)
        self.say(f"bild gespeichert: {os.path.basename(path)}")

    def _paste_text(self, cb, res, dest):
        try:
            text = cb.read_text_finish(res)
        except GLib.Error:
            return
        if not text:
            return
        cand = [l.strip() for l in text.splitlines() if l.strip()]
        uris = []
        for c in cand:
            if c.startswith("file://"):
                uris.append(c)
            elif c.startswith(("/", "~")) and os.path.exists(os.path.expanduser(c)):
                uris.append(model.uri_for(os.path.expanduser(c)))
        if uris and len(uris) == len(cand):
            self.ops.transfer(uris, dest, done=lambda j: self._after(j, select=True))
            return
        folder = model.path_of(dest)
        if folder:                            # Text als neue Datei einfügen
            path = opsmod._free_path(os.path.join(folder, "eingefügter text.txt"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self.say(f"text gespeichert: {os.path.basename(path)}")

    def drop_files(self, uris, dest, action):
        if dest.startswith("trash:"):
            self.ops.trash(uris, done=self._after)
            return True
        if action is None:
            src = model.path_of(uris[0])
            action = Gdk.DragAction.MOVE if views.same_device(src, model.path_of(dest)) else Gdk.DragAction.COPY
        if action == Gdk.DragAction.LINK:
            folder = model.path_of(dest)
            for u in uris:
                p = model.path_of(u)
                if p and folder:
                    try:
                        os.symlink(p, opsmod._free_path(os.path.join(folder, os.path.basename(p))))
                    except OSError as e:
                        self.say(f"verknüpfung: {e.strerror}", error=True)
            return True
        self.ops.transfer(uris, dest, move=action == Gdk.DragAction.MOVE, done=lambda j: self._after(j, select=True))
        return True

    DONE = {"copy": "kopiert", "move": "verschoben", "trash": "im papierkorb", "delete": "gelöscht",
            "extract": "entpackt", "compress": "gepackt", "restore": "wiederhergestellt"}

    def _after(self, job, select=False):
        if job.state == "fertig":
            subject = job.title.split(": ", 1)[1] if ": " in job.title else job.title.split(" ", 1)[-1]
            msg = f"{self.DONE.get(job.kind, 'fertig')}: {subject}"
            self.say(msg + ("  · strg+z macht es rückgängig" if job.undo else ""))
        for p in self.app.all_panes():
            if p.uri and not p.search:
                p._changed()

    def trash(self):
        items = self.targets()
        if not items:
            return
        if self.pane.is_trash():
            self.delete()
            return
        self.ops.trash([i.extra.get("gio_uri") or i.uri for i in items], done=self._after)

    def delete(self):
        items = self.targets()
        if not items:
            return
        n = len(items)
        what = f"„{items[0].display}“" if n == 1 else f"{n} Elemente"
        dialogs.confirm(self, "endgültig löschen", f"{what} endgültig löschen? Das lässt sich nicht rückgängig machen.",
                        "löschen", lambda: self.ops.delete([i.extra.get("gio_uri") or i.uri for i in items],
                                                           done=self._after))

    def restore(self):
        items = [i for i in self.targets() if i.extra.get("orig_path")]
        if items:
            self.ops.restore_items(items, done=self._after)

    def empty_trash(self):
        dialogs.confirm(self, "papierkorb leeren", "Alles im Papierkorb endgültig löschen?", "leeren",
                        lambda: self.ops.empty_trash(done=self._after))

    def undo(self):
        title = self.ops.undo()
        self.say(f"rückgängig: {title.lower()}" if title else "nichts zum rückgängig machen", error=not title)

    def rename(self):
        items = self.targets()
        folder = self.pane.local_path()
        if not items:
            return
        if len(items) > 1:
            dialogs.BatchRename(self, items, self._batch_rename).present()
            return
        it = items[0]
        if not it.path:
            self.say("umbenennen geht hier nicht", error=True)
            return
        self._inline_rename(self.pane, it)

    def _inline_rename(self, pane, it, text=None):
        """Name direkt in der Zeile ändern (Enter/Klick daneben = übernehmen, Esc = abbrechen)."""
        parent = os.path.dirname(it.path)

        def done(new):
            if not new or new == it.name:
                return
            err = dialogs.name_error(new, parent, it.name)
            if err:
                self.say(err, error=True)
                GLib.idle_add(lambda: (pane.edit_name(it, done, text=new, select_stem=False), False)[1])
                return
            self._rename_one(it, new, pane)
        if not pane.edit_name(it, done, text):
            dialogs.ask_text(self, "umbenennen", None, it.name, lambda new: self._rename_one(it, new, pane),
                             ok_text="umbenennen", validate=lambda v: dialogs.name_error(v, parent, it.name))

    def _rename_one(self, it, new, pane=None):
        if new == it.name:
            return
        src = it.path
        dst = os.path.join(os.path.dirname(src), new)
        try:
            if os.path.lexists(dst) and os.path.normcase(dst) != os.path.normcase(src):
                raise OSError(17, "Diesen Namen gibt es schon")
            os.rename(src, dst)
        except OSError as e:
            self.say(f"umbenennen fehlgeschlagen: {e.strerror}", error=True)
            return
        self.ops.record(f"Umbenennen von {new}", lambda: os.rename(dst, src))
        (pane or self.pane).select_after = model.uri_for(dst)
        self.say(f"umbenannt in „{new}“ · strg+z macht es rückgängig")

    def _batch_rename(self, pairs):
        done, errors = [], []
        # zweistufig über temporäre Namen, damit Tauschen (a→b, b→a) klappt
        temps = []
        for it, new in pairs:
            tmp = it.path + f".fw-tmp-{os.getpid()}"
            try:
                os.rename(it.path, tmp)
                temps.append((it, new, tmp))
            except OSError as e:
                errors.append(f"{it.name}: {e.strerror}")
        for it, new, tmp in temps:
            dst = os.path.join(os.path.dirname(it.path), new)
            try:
                if os.path.lexists(dst):
                    raise OSError(17, "existiert schon")
                os.rename(tmp, dst)
                done.append((it.path, dst))
            except OSError as e:
                os.rename(tmp, it.path)
                errors.append(f"{it.name}: {e.strerror}")
        if done:
            self.ops.record(f"Umbenennen von {len(done)} Elementen",
                            lambda: [os.rename(d, s) for s, d in reversed(done) if os.path.lexists(d)])
        self.say(f"{len(done)} umbenannt" + (f", {len(errors)} fehler: {errors[0]}" if errors else ""),
                 error=bool(errors))

    def new_folder(self):
        folder = self.pane.local_path()
        if folder:
            self._mk(folder, os.path.basename(opsmod._free_path(os.path.join(folder, "neuer ordner"))), True,
                     edit=True)

    def new_file(self):
        folder = self.pane.local_path()
        if folder:
            self._mk(folder, os.path.basename(opsmod._free_path(os.path.join(folder, "neue datei.txt"))), False,
                     edit=True)

    def _mk(self, folder, name, is_dir, edit=False):
        """Anlegen; mit edit=True danach gleich in der Zeile benennen (wie im Windows-Explorer)."""
        p = os.path.join(folder, name)
        pane = self.pane
        puri = Gio.File.new_for_uri(pane.uri).get_child(name).get_uri()     # bei smb:// der smb-URI, nicht der FUSE-Pfad
        try:
            if is_dir:
                os.mkdir(p)
            else:
                open(p, "x").close()
        except OSError as e:
            self.say(e.strerror, error=True)
            return
        self.ops.record(f"Anlegen von {name}", lambda: self.ops.trash([puri], record=False))
        pane.select_after = puri
        if edit:
            pane.edit_after = (puri, lambda it: self._inline_rename(pane, it))
            pane.reload()

    def duplicate(self):
        items = self.targets()
        if items:
            self.ops.transfer([i.uri for i in items], self.pane.uri, done=lambda j: self._after(j, True))

    def make_link(self):
        folder = self.pane.local_path()
        for it in self.targets():
            if it.path and folder:
                try:
                    os.symlink(it.path, opsmod._free_path(os.path.join(folder, f"{it.name} (verknüpfung)")))
                except OSError as e:
                    self.say(e.strerror, error=True)

    def compress(self):
        items = self.targets()
        folder = self.pane.local_path()
        if items and folder:
            dialogs.compress(self, items, folder, lambda out, fmt: self.ops.compress(
                [i.uri for i in items], out, fmt, done=lambda j: (self._after(j), setattr(
                    self.pane, "select_after", model.uri_for(out)))))

    def extract(self, items=None, here=True, folder=None):
        items = [i for i in (items or self.targets()) if opsmod.is_archive(i.name)]
        dest = folder or self.pane.uri
        if items:
            self.ops.extract([i.uri for i in items], dest, done=self._after)

    def properties(self):
        items = self.targets()
        if not items:
            p = self.pane.local_path()
            if not p:
                return
            info = os.stat(p)
            items = [model.FileItem(os.path.basename(p) or "/", self.pane.uri, p, True, False, info.st_size,
                                    info.st_mtime, info.st_mode, "inode/directory")]
        dialogs.Properties(self, items, self.set_default_app).present()

    def bookmark(self, uri=None):
        sel = self.targets()
        uri = uri or (sel[0].uri if len(sel) == 1 and sel[0].is_dir else self.pane.uri)
        if places.add_bookmark(uri):
            self.say(f"lesezeichen: {views.location_name(uri)}")
        else:
            places.remove_bookmark(uri)
            self.say(f"lesezeichen entfernt: {views.location_name(uri)}")

    def to_other_pane(self, move):
        other = self.other_pane()
        items = self.targets()
        if other is None or not items:
            self.say("f5/f6 kopieren/verschieben in die andere hälfte – erst mit f3 teilen", error=other is None)
            return
        self.ops.transfer([i.uri for i in items], other.uri, move=move, done=self._after)

    def select_pattern(self):
        dialogs.ask_text(self, "auswählen nach muster", "z. B. *.jpg oder urlaub", "*", lambda pat: self.say(
            f"{self.pane.select_pattern(pat)} ausgewählt"), select_stem=False, ok_text="auswählen",
            validate=lambda v: None if v else "Muster eingeben")

    # ================================================================ Kontextmenü
    def context_menu(self, pane, items, x, y):
        m = Gio.Menu()
        trash = pane.is_trash()
        if items:
            sec = Gio.Menu()
            if trash:
                sec.append("wiederherstellen", "win.restore")
                sec.append("endgültig löschen", "win.delete")
                m.append_section(None, sec)
            else:
                sec.append("öffnen", "win.open")
                files = [i for i in items if not i.is_dir]
                if files:
                    sub = Gio.Menu()
                    ctype = files[0].ctype
                    for app in (Gio.AppInfo.get_recommended_for_type(ctype) if ctype else [])[:8]:
                        sub.append(app.get_display_name(), f"win.open-app::{app.get_id()}")
                    sub.append("andere anwendung …", "win.open-with")
                    sec.append_submenu("öffnen mit", sub)
                if any(i.is_dir for i in items):
                    sec.append("in neuem tab", "win.open-tab")
                    sec.append("terminal hier", "win.terminal")
                m.append_section(None, sec)
                sec = Gio.Menu()
                sec.append("ausschneiden", "win.cut")
                sec.append("kopieren", "win.copy")
                sec.append("pfad kopieren", "win.copy-path")
                ways = share.ways([i.path for i in items])
                if ways:
                    sub = Gio.Menu()
                    for wid, text in ways:
                        sub.append(text, f"win.share::{wid}")
                    sec.append_submenu("teilen", sub)
                if self.other_pane():
                    sec.append("in andere hälfte kopieren", "win.copy-other")
                    sec.append("in andere hälfte verschieben", "win.move-other")
                m.append_section(None, sec)
                sec = Gio.Menu()
                sec.append("umbenennen" if len(items) == 1 else "mehrere umbenennen …", "win.rename")
                sec.append("duplizieren", "win.duplicate")
                sec.append("verknüpfung anlegen", "win.link")
                sec.append("packen …", "win.compress")
                if any(opsmod.is_archive(i.name) for i in items):
                    sec.append("hier entpacken", "win.extract")
                if len(items) == 1 and items[0].is_dir:
                    sec.append("als lesezeichen", "win.bookmark")
                m.append_section(None, sec)
                sec = Gio.Menu()
                sec.append("in den papierkorb", "win.trash")
                sec.append("endgültig löschen …", "win.delete")
                m.append_section(None, sec)
            sec = Gio.Menu()
            sec.append("eigenschaften", "win.properties")
            m.append_section(None, sec)
        else:
            sec = Gio.Menu()
            if trash:
                sec.append("papierkorb leeren", "win.empty-trash")
            else:
                sec.append("neuer ordner", "win.new-folder")
                sec.append("neue datei", "win.new-file")
                sec.append("einfügen", "win.paste")
                sec.append("terminal hier", "win.terminal")
            m.append_section(None, sec)
            sec = Gio.Menu()
            sec.append("alles auswählen", "win.select-all")
            sec.append("versteckte " + ("ausblenden" if pane.show_hidden else "anzeigen"), "win.hidden")
            sec.append("als lesezeichen", "win.bookmark")
            if not trash:
                sec.append("eigenschaften", "win.properties")
            m.append_section(None, sec)
        quick = items and not trash
        if quick:
            qi = Gio.MenuItem.new(None, None)
            qi.set_attribute_value("custom", GLib.Variant.new_string("quick"))
            qs = Gio.Menu()
            qs.append_item(qi)
            m.prepend_section(None, qs)
        fly = flyout.Fly(m, pane)
        if quick:
            class _P:                                    # _quick_bar ruft popdown()
                popdown = staticmethod(fly.close)
            fly.custom["quick"] = self._quick_bar(_P, len(items))
        fly.popup(pane, x, y)


    def _quick_bar(self, pop, n):
        """Zwei große Symbole oben im Rechtsklick-Menü: umbenennen und löschen (Papierkorb)."""
        def big(glyph, tip, action, danger=False):
            b = Gtk.Button(label=glyph, tooltip_text=tip, hexpand=True, focus_on_click=False)
            b.add_css_class("fw-qbtn")
            if danger:
                b.add_css_class("danger")
            def go(*_):
                pop.popdown()
                GLib.idle_add(lambda: (self.activate_action(action, None), False)[1])
            b.connect("clicked", go)
            return b
        box = Gtk.Box(spacing=6, homogeneous=True)
        box.add_css_class("fw-quick")
        box.append(big("\U000F03EB", "Umbenennen (F2)" if n == 1 else "Mehrere umbenennen (F2)", "win.rename"))
        box.append(big("\U000F0A7A", "In den Papierkorb (Entf)", "win.trash", danger=True))
        return box

    # ================================================================ Aktionen & Tasten
    def _actions(self):
        A = [
            ("new-tab", lambda: self.new_tab(self.pane.uri), ["<Control>t", "<Control>n"]),
            ("close-tab", lambda: self.close_tab(), ["<Control>w"]),
            ("next-tab", lambda: self.cycle_tab(1), ["<Control>Tab", "<Control>Page_Down"]),
            ("prev-tab", lambda: self.cycle_tab(-1), ["<Control><Shift>Tab", "<Control>Page_Up"]),
            ("back", lambda: self.pane.go_back(), ["<Alt>Left"]),
            ("forward", lambda: self.pane.go_forward(), ["<Alt>Right"]),
            ("up", lambda: self.pane.go_up(), ["<Alt>Up"]),
            ("home", lambda: self.open_location(model.uri_for(os.path.expanduser("~"))), ["<Alt>Home"]),
            ("reload", lambda: self.pane.reload(), ["<Control>r"]),
            ("location", lambda: self.path.edit(), ["<Control>l"]),
            ("find", lambda: (self.filter.grab_focus(), self.filter.select_region(0, -1)), ["<Control>f"]),
            ("hidden", lambda: self.set_hidden(not self.pane.show_hidden), ["<Control>h"]),
            ("view-list", lambda: self.view_btns["list"].set_active(True), ["<Control>1"]),
            ("view-grid", lambda: self.view_btns["grid"].set_active(True), ["<Control>2"]),
            ("zoom-in", lambda: self._zoom(1), ["<Control>plus", "<Control>equal", "<Control>KP_Add"]),
            ("zoom-out", lambda: self._zoom(-1), ["<Control>minus", "<Control>KP_Subtract"]),
            ("split", lambda: self.toggle_split(), ["F3"]),
            ("sidebar", lambda: self.toggle_sidebar(), ["F9"]),
            ("preview", lambda: self.toggle_preview(), []),
            ("open", lambda: self.activate_items(self.targets()), []),
            ("open-tab", lambda: [self.new_tab(i.uri, switch=False) for i in self.targets() if i.is_dir], []),
            ("open-with", lambda: self.open_with(), []),
            ("terminal", lambda: self.open_terminal(), ["F4", "<Control><Alt>t"]),
            ("copy", lambda: self.copy(), ["<Control>c"]),
            ("cut", lambda: self.copy(cut=True), ["<Control>x"]),
            ("paste", lambda: self.paste(), ["<Control>v"]),
            ("copy-path", lambda: self.copy_path(), ["<Control><Shift>c"]),
            ("copy-other", lambda: self.to_other_pane(False), ["F5"]),
            ("move-other", lambda: self.to_other_pane(True), ["F6"]),
            ("select-all", lambda: self.pane.select_all(), ["<Control>a"]),
            ("select-none", lambda: self.pane.select_none(), ["<Control><Shift>a"]),
            ("invert", lambda: self.pane.invert(), ["<Control>i"]),
            ("select-pattern", lambda: self.select_pattern(), ["<Control>s"]),
            ("rename", lambda: self.rename(), ["F2"]),
            ("new-folder", lambda: self.new_folder(), ["<Control><Shift>n", "F7"]),
            ("new-file", lambda: self.new_file(), ["<Control><Alt>n"]),
            ("duplicate", lambda: self.duplicate(), ["<Control><Shift>d"]),
            ("link", lambda: self.make_link(), []),
            ("trash", lambda: self.trash(), ["Delete", "KP_Delete"]),
            ("delete", lambda: self.delete(), ["<Shift>Delete", "<Shift>KP_Delete"]),
            ("restore", lambda: self.restore(), []),
            ("empty-trash", lambda: self.empty_trash(), []),
            ("undo", lambda: self.undo(), ["<Control>z"]),
            ("compress", lambda: self.compress(), []),
            ("extract", lambda: self.extract(), []),
            ("properties", lambda: self.properties(), ["<Alt>Return", "<Alt>KP_Enter"]),
            ("bookmark", lambda: self.bookmark(), ["<Control>d"]),
            ("quit", lambda: self.close(), ["<Control>q"]),
        ]
        app = self.get_application()
        for name, cb, keys in A:
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda a, p, cb=cb: cb())
            self.add_action(act)
            if keys:
                app.set_accels_for_action(f"win.{name}", keys)
        for i in range(1, 10):
            act = Gio.SimpleAction.new(f"tab{i}", None)
            act.connect("activate", lambda a, p, i=i: i <= len(self.tabs) and self.tabs[i - 1].button.set_active(True))
            self.add_action(act)
            app.set_accels_for_action(f"win.tab{i}", [f"<Alt>{i}"])
        for name, cb in (("open-app", self._open_app), ("go", lambda u: self.open_location(u)),
                         ("go-tab", lambda u: self.new_tab(u)), ("bookmark-add", lambda u: self.bookmark(u)),
                         ("bookmark-remove", lambda u: (places.remove_bookmark(u), self.say("lesezeichen entfernt"))),
                         ("eject", lambda u: self.places.eject_uri(u)),
                         ("share", lambda w: share.share(w, [i.path for i in self.targets()], self)),
                         ("vpn-set", lambda a: self.set_vpn(a)),
                         ):
            act = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            act.connect("activate", lambda a, p, cb=cb: cb(p.get_string()))
            self.add_action(act)

    def _open_app(self, app_id):
        app = next((a for a in Gio.AppInfo.get_all() if a.get_id() == app_id), None)
        items = [i for i in self.targets() if not i.is_dir]
        if app and items:
            self.launch(app, items)

    def _zoom(self, d):
        if self.pane.view != "grid":
            self.view_btns["grid"].set_active(True)
            return
        self.pane.set_zoom(self.pane.zoom + d)
        self.prefs["zoom"] = self.pane.zoom
        self.app.save_prefs()

    def pinch_zoom(self, pane, d):
        """Touchpad spreizen/zusammenziehen: Liste → Raster → größer, und zurück bis zur Liste."""
        if pane is not self.pane:
            return
        if pane.view != "grid":
            if d > 0:
                self.view_btns["grid"].set_active(True)
            return
        if d < 0 and pane.zoom == 0:
            self.view_btns["list"].set_active(True)
            return
        self._zoom(d)

    def _focus_in_text(self):
        f = self.get_focus()
        return isinstance(f, (Gtk.Text, Gtk.TextView, Gtk.Entry))

    def _text_key(self, kv, state):
        """Tippen in einem Textfeld (Filter, Pfad, Umbenennen in der Zeile): Tasten, die sonst Dateiaktionen
        auslösen (Entf, Strg+A/C/X/V/Z), gehören dem Textfeld – die App-Kürzel würden sie vorher abfangen."""
        f = self.get_focus()
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK)
        ctrl = Gdk.ModifierType.CONTROL_MASK
        try:
            if isinstance(f, Gtk.Text):
                if kv in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not mods:
                    f.emit("delete-from-cursor", Gtk.DeleteType.CHARS, 1)
                elif mods == ctrl and kv in (Gdk.KEY_a, Gdk.KEY_A):
                    f.select_region(0, -1)
                elif mods == ctrl and kv in (Gdk.KEY_c, Gdk.KEY_C):
                    f.emit("copy-clipboard")
                elif mods == ctrl and kv in (Gdk.KEY_x, Gdk.KEY_X):
                    f.emit("cut-clipboard")
                elif mods == ctrl and kv in (Gdk.KEY_v, Gdk.KEY_V):
                    f.emit("paste-clipboard")
                elif mods == ctrl and kv in (Gdk.KEY_z, Gdk.KEY_Z):
                    f.activate_action("text.undo", None)
                else:
                    return False
                return True
            if isinstance(f, Gtk.TextView) and mods == ctrl:
                if kv in (Gdk.KEY_a, Gdk.KEY_A):
                    f.emit("select-all", True)
                elif kv in (Gdk.KEY_c, Gdk.KEY_C):
                    f.emit("copy-clipboard")
                else:
                    return False
                return True
        except (TypeError, GLib.Error):
            pass
        return False

    def _key(self, _c, kv, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if kv == Gdk.KEY_Escape and self.mode != "browse" and not self._focus_in_text():
            self.finish(False)
            return True
        if self._focus_in_text():
            return self._text_key(kv, state)
        if self.mode == "save" and self.name_entry and not ctrl and not state & Gdk.ModifierType.ALT_MASK:
            ch = Gdk.keyval_to_unicode(kv)
            if ch >= 32 and ch != 127:            # Speichern-Dialog: einfach lostippen = Dateiname ersetzen
                name = self.name_entry.get_text()
                dot = name.rfind(".")
                tail = name[dot:] if dot > 0 else ""
                self.name_entry.grab_focus()
                self.name_entry.set_text(chr(ch) + tail)
                self.name_entry.set_position(1)
                return True
        if kv == Gdk.KEY_space and not ctrl:
            self.toggle_preview()
            return True
        if kv in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not ctrl and not state & Gdk.ModifierType.ALT_MASK:
            items = self.targets()
            if items:
                self.activate_items(items)
                return True
            if self.mode != "browse":
                self.accept()
                return True
        if kv == Gdk.KEY_Tab and len(self.cur.panes) > 1 and not ctrl:
            self.cur.active = 1 - self.cur.active
            self.pane.focus_view()
            self._pane_changed(self.pane)
            return True
        if kv == Gdk.KEY_slash and not ctrl:
            self.path.edit("/")
            return True
        if kv == Gdk.KEY_asciitilde:
            self.path.edit("~/")
            return True
        return False

    def _mouse_nav(self, g, n, x, y):
        b = g.get_current_button()
        if b == 8:
            self.pane.swipe.trigger(d=-1) or self.pane.go_back()
        elif b == 9:
            self.pane.swipe.trigger(d=1) or self.pane.go_forward()

    # ================================================================ Status
    def say(self, text, error=False, seconds=6):
        if error:
            seconds = max(seconds, 10)
        self.status.set_label(("✗ " if error else "▸ ") + str(text).strip().replace("\n", " "))
        (self.status.add_css_class if error else self.status.remove_css_class)("err")
        if self._status_id:
            GLib.source_remove(self._status_id)
        self._status_id = GLib.timeout_add_seconds(seconds, self._clear_status)

    def _clear_status(self):
        self._status_id = None
        st = getattr(self, "status", None)      # Fenster schon aufgeräumt (release_tree) → nichts mehr zu tun
        if st is not None:
            st.set_label("")
        return False

    def _on_close(self, *_):
        self.app.save_prefs()
        if self._status_id:
            GLib.source_remove(self._status_id)
            self._status_id = None
        for t in self.tabs:
            for p in t.panes:
                if p.monitor:
                    p.monitor.cancel()
        if self.mode != "browse" and not getattr(self, "_finished", False):
            self.finish(False, close=False)
        # Lambdas/Methoden der Kind-Widgets zeigen auf das Fenster zurück – ohne Aufräumen bliebe jedes
        # geschlossene Fenster für immer im Speicher (~5 MB pro Super+E). Etwas später, wenn GTK fertig ist.
        try:                                  # Jobs-Anzeige dieses Fensters nicht mehr benachrichtigen
            self.ops.listeners.remove(self.jobs.update)
        except (AttributeError, ValueError):
            pass
        if hasattr(tui, "release_tree"):
            GLib.timeout_add(1500, lambda: (tui.release_tree(self), self._drop_thumbs(), tui.trim_memory(), False)[-1])
        return False

    def _drop_thumbs(self):
        """Kein anderes Filist-Fenster offen: Vorschau-Texturen freigeben (der Daemon behält sonst Hunderte MB)."""
        if not any(isinstance(w, type(self)) and w is not self and w.get_visible() for w in Gtk.Window.list_toplevels()):
            import thumbs
            if thumbs.THUMBS is not None:
                thumbs.THUMBS.drop()

    # ================================================================ Auswahl-Modus (Portal)
    def _choose_filter(self, idx):
        self.filter_idx = int(idx)
        self.apply_chooser_filter()

    def apply_chooser_filter(self):
        if not self.filters:
            return
        _name, rules = self.filters[self.filter_idx]
        globs = [p.casefold() for kind, p in rules if kind == 0]
        mimes = [p for kind, p in rules if kind == 1]

        def ok(it):
            if it.is_dir:
                return True
            n = it.name.casefold()
            if any(fnmatch.fnmatch(n, g) for g in globs):
                return True
            return any(m == "*" or Gio.content_type_is_a(it.ctype, Gio.content_type_from_mime_type(m) or m) or
                       fnmatch.fnmatch(it.ctype, m) for m in mimes)
        for t in self.tabs:
            for p in t.panes:
                p.extra_filter = ok
                p._apply()

    def accept(self):
        pane = self.pane
        sel = pane.selected()
        if self.mode == "open":
            if self.chooser.get("directory"):
                uris = [i.uri for i in sel if i.is_dir] or [pane.uri]
            else:
                files = [i for i in sel if not i.is_dir]
                if not files:
                    if sel and sel[0].is_dir:
                        pane.go(sel[0].uri)
                    return
                if not self.chooser.get("multiple"):
                    files = files[:1]
                uris = [i.uri for i in files]
            self.finish(True, uris)
        elif self.mode == "dir":
            uris = [i.uri for i in sel if i.is_dir][:1] or [pane.uri]
            self.finish(True, uris)
        elif self.mode == "save":
            name = self.name_entry.get_text().strip()
            if not name:
                self.name_entry.grab_focus()
                return
            folder = pane.local_path()
            if not folder:
                self.say("bitte einen lokalen ordner wählen", error=True)
                return
            target = os.path.join(folder, name)
            if os.path.isdir(target):
                pane.go(model.uri_for(target))
                self.name_entry.set_text("")
                return
            uri = model.uri_for(target)         # file://-Pfad (bei smb: FUSE-Pfad, den auch andere Programme lesen)
            if os.path.exists(target):
                cd = getattr(self, "_replace_dlg", None)
                if cd is not None and cd.get_visible():       # zweites Enter im Namensfeld = trotzdem ersetzen
                    cd.close()
                    self.finish(True, [uri])
                    return
                self._replace_dlg = dialogs.confirm(self, "ersetzen?", f"„{name}“ gibt es schon. Ersetzen?",
                                                    "ersetzen", lambda: self.finish(True, [uri]))
                return
            self.finish(True, [uri])

    def finish(self, ok, uris=None, close=True):
        if getattr(self, "_finished", False):
            return
        self._finished = True
        cb = self.chooser.get("done")
        if cb:
            cb(ok, uris or [], self.filter_idx if self.filters else None)
        if close:
            self.close()


class PathBar(Gtk.Stack):
    """Pfad als klickbare Abschnitte; Klick daneben oder Strg+L → Eingabefeld mit Tab-Vervollständigung."""

    def __init__(self, win):
        super().__init__(hexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=80)
        self.add_css_class("fw-path")
        self.win = win
        self.crumbs = tui.hbox(spacing=0)
        self.crumbs.add_css_class("fw-crumbs")
        self.csc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.EXTERNAL,
                                      vscrollbar_policy=Gtk.PolicyType.NEVER, child=self.crumbs, hexpand=True)
        click = Gtk.GestureClick()
        click.connect("released", lambda g, n, x, y: self._bg_click(x, y))
        self.csc.add_controller(click)
        self.entry = tui.Entry("pfad oder adresse, z. B. ~/dokumente, sftp://server")
        self.entry.connect("activate", lambda *_: self._go())
        k = Gtk.EventControllerKey()
        k.connect("key-pressed", self._key)
        self.entry.add_controller(k)
        fc = Gtk.EventControllerFocus()
        fc.connect("leave", lambda *_: GLib.timeout_add(120, self._leave))
        self.entry.add_controller(fc)
        self.add_named(self.csc, "crumbs")
        self.add_named(self.entry, "entry")
        self.uri = None

    def set_uri(self, uri, search=None):
        self.uri = uri
        tui.clear(self.crumbs)
        path = model.path_of(uri)
        parts = []
        if path:
            home = os.path.expanduser("~")
            if path == home or path.startswith(home + "/"):
                parts.append((icons.HOME + " ~", home))
                rest = path[len(home):].strip("/")
                acc = home
            else:
                parts.append(("/", "/"))
                rest = path.strip("/")
                acc = ""
            for seg in [s for s in rest.split("/") if s]:
                acc = acc + "/" + seg
                parts.append((seg, acc))
            targets = [(t, model.uri_for(p)) for t, p in parts]
        else:
            f = Gio.File.new_for_uri(uri)
            chain = []
            while f is not None:
                chain.append(f)
                f = f.get_parent()
            chain.reverse()
            targets = []
            for i, c in enumerate(chain):
                name = c.get_basename() if i else (views.location_name(c.get_uri()) if uri.startswith(("trash:", "recent:"))
                                                    else c.get_uri().split("://")[0] + "://" + (c.get_uri().split("://")[1].split("/")[0]))
                targets.append((name or "/", c.get_uri()))
        for i, (text, target) in enumerate(targets):
            if i:
                sep = tui.label("/", "fw-sep")
                self.crumbs.append(sep)
            b = Gtk.Button(label=text, focus_on_click=False)
            b.add_css_class("fw-crumb")
            if i == len(targets) - 1:
                b.add_css_class("last")
            b.connect("clicked", lambda _b, t=target: self.win.open_location(t))
            drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
            drop.connect("drop", lambda t, v, x, y, tgt=target: self.win.drop_files(
                [f.get_uri() for f in v.get_files()], tgt, None))
            b.add_controller(drop)
            self.crumbs.append(b)
        if search:
            self.crumbs.append(tui.label(f"  \U000F0349 „{search['query']}“", "accent"))
        self.set_visible_child_name("crumbs")
        GLib.idle_add(self._scroll_end)

    def _scroll_end(self):
        adj = self.csc.get_hadjustment()
        adj.set_value(adj.get_upper())
        return False

    def _bg_click(self, x, y):
        pick = self.csc.pick(x, y, Gtk.PickFlags.DEFAULT)
        while pick is not None and pick is not self.csc:
            if isinstance(pick, Gtk.Button):
                return
            pick = pick.get_parent()
        self.edit()

    def edit(self, initial=None):
        p = model.path_of(self.uri or "") or self.uri or ""
        text = initial if initial is not None else (icons.home_relative(p) + ("/" if p and p != "/" else ""))
        self.entry.set_text(text)
        self.set_visible_child_name("entry")
        self.entry.grab_focus()
        self.entry.set_position(-1)

    def _leave(self):
        if not self.entry.has_focus():
            self.set_visible_child_name("crumbs")
        return False

    def _go(self):
        text = self.entry.get_text().strip()
        if not text:
            return
        if "://" in text or text.startswith(("trash:", "recent:")):
            uri = text
        else:
            p = os.path.expanduser(text)
            if not os.path.isabs(p):
                base = model.path_of(self.uri or "") or os.path.expanduser("~")
                p = os.path.join(base, p)
            p = os.path.normpath(p)
            if os.path.isfile(p):
                self.win.pane.go(model.uri_for(os.path.dirname(p)), select=model.uri_for(p))
                self.set_visible_child_name("crumbs")
                self.win.pane.focus_view()
                return
            if not os.path.isdir(p):
                self.win.say(f"„{text}“ gibt es nicht", error=True)
                return
            uri = model.uri_for(p)
        self.set_visible_child_name("crumbs")
        self.win.open_location(uri)

    def _text_key(self, kv, state):
        """Tippen in einem Textfeld (Filter, Pfad, Umbenennen in der Zeile): Tasten, die sonst Dateiaktionen
        auslösen (Entf, Strg+A/C/X/V/Z), gehören dem Textfeld – die App-Kürzel würden sie vorher abfangen."""
        f = self.get_focus()
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK | Gdk.ModifierType.ALT_MASK)
        ctrl = Gdk.ModifierType.CONTROL_MASK
        try:
            if isinstance(f, Gtk.Text):
                if kv in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not mods:
                    f.emit("delete-from-cursor", Gtk.DeleteType.CHARS, 1)
                elif mods == ctrl and kv in (Gdk.KEY_a, Gdk.KEY_A):
                    f.select_region(0, -1)
                elif mods == ctrl and kv in (Gdk.KEY_c, Gdk.KEY_C):
                    f.emit("copy-clipboard")
                elif mods == ctrl and kv in (Gdk.KEY_x, Gdk.KEY_X):
                    f.emit("cut-clipboard")
                elif mods == ctrl and kv in (Gdk.KEY_v, Gdk.KEY_V):
                    f.emit("paste-clipboard")
                elif mods == ctrl and kv in (Gdk.KEY_z, Gdk.KEY_Z):
                    f.activate_action("text.undo", None)
                else:
                    return False
                return True
            if isinstance(f, Gtk.TextView) and mods == ctrl:
                if kv in (Gdk.KEY_a, Gdk.KEY_A):
                    f.emit("select-all", True)
                elif kv in (Gdk.KEY_c, Gdk.KEY_C):
                    f.emit("copy-clipboard")
                else:
                    return False
                return True
        except (TypeError, GLib.Error):
            pass
        return False

    def _key(self, _c, kv, _code, state):
        if kv == Gdk.KEY_Escape:
            self.set_visible_child_name("crumbs")
            self.win.pane.focus_view()
            return True
        if kv == Gdk.KEY_Tab:
            self._complete()
            return True
        return False

    def _complete(self):
        text = self.entry.get_text()
        p = os.path.expanduser(text)
        folder, part = os.path.split(p)
        folder = folder or "/"
        try:
            names = sorted(e.name for e in os.scandir(folder) if e.is_dir() and
                           (part.startswith(".") or not e.name.startswith(".")))
        except OSError:
            return
        hits = [n for n in names if n.casefold().startswith(part.casefold())]
        if not hits:
            return
        common = os.path.commonprefix(hits) if len(hits) > 1 else hits[0] + "/"
        if len(hits) > 1 and len(common) <= len(part):
            self.win.say("  ".join(hits[:12]) + (" …" if len(hits) > 12 else ""))
            return
        base = text[:len(text) - len(part)] if part else text
        self.entry.set_text(base + common)
        self.entry.set_position(-1)


