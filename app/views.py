# Eine Dateiansicht (Pane): Liste (Spalten) oder Raster (Kacheln mit Vorschaubildern), dieselbe Auswahl.
# Sortieren/Filtern passiert in Python auf einer einfachen Liste, danach ein einziges splice() ins Modell –
# das ist bei tausenden Einträgen viel schneller als GTK-Sortierer mit Python-Vergleichsfunktionen.
import fnmatch, os, threading, time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

import icons
import model
import swipe as swipe_mod
import thumbs

ZOOMS = [64, 96, 128, 176, 240]          # Kachelbreiten im Raster


def _bitset_iter(bs):
    ok, it, pos = Gtk.BitsetIter.init_first(bs)
    while ok:
        yield pos
        ok, pos = it.next()


class Pane(Gtk.Box):
    """Ein Ordner mit Verlauf. Die Fenster-Ebene reagiert über die on_*-Rückrufe."""

    def __init__(self, win, uri, prefs):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("fw-pane")
        self.win = win
        self.prefs = prefs
        self.uri = None
        self.back, self.fwd = [], []
        self.all = []                  # alle Einträge des Ordners
        self.shown = []                # nach Filter/Versteckt/Sortierung
        self.filter = ""
        self.sort_field = prefs.get("sort", "name")
        self.sort_desc = prefs.get("sort_desc", False)
        self.show_hidden = prefs.get("hidden", False)
        self.view = prefs.get("view", "list")
        self.zoom = prefs.get("zoom", 2)
        self.loading = False
        self.error = None
        self.search = None             # {"query", "root", "gen"} während einer rekursiven Suche
        self.extra_filter = None       # z. B. Dateityp-Filter im Datei-Dialog
        self.loader = model.Loader()
        self.monitor = None
        self.reload_id = None
        self.select_after = None       # nach dem Laden auswählen (z. B. Ordner, aus dem man kam)
        self.typed, self.typed_at = "", 0
        self.on_location = self.on_selection = self.on_activate = self.on_context = None
        self.on_loaded = self.on_focus = self.on_middle = self.on_zoom = None

        self.store = Gio.ListStore(item_type=model.FileItem)
        self.sel = Gtk.MultiSelection(model=self.store)
        self.sel.connect("selection-changed", lambda *_: self.on_selection and self.on_selection(self))

        self.list = self._build_list()
        self.grid = self._build_grid()
        self.stack = Gtk.Stack(vexpand=True, hexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=90)
        for name, view in (("list", self.list), ("grid", self.grid)):
            sc = Gtk.ScrolledWindow(child=view, hexpand=True, vexpand=True)
            sc.add_css_class("fw-scroll")
            self._attach_background(sc)
            self.stack.add_named(sc, name)
        self.empty = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER, vexpand=True)
        self.empty.add_css_class("fw-empty")
        self.stack.add_named(self.empty, "empty")
        # Laden/Verbinden (Netzlaufwerk, VPN): Drehkreis + Text statt leerer Fläche
        self.busy_spin = Gtk.Spinner(halign=Gtk.Align.CENTER, width_request=34, height_request=34)
        self.busy_lbl = Gtk.Label(label="", wrap=True, justify=Gtk.Justification.CENTER)
        self.busy_lbl.add_css_class("fw-empty")
        busy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        busy.add_css_class("fw-busy")
        busy.append(self.busy_spin)
        busy.append(self.busy_lbl)
        self.busy_cancel_btn = Gtk.Button(label="abbrechen", halign=Gtk.Align.CENTER, focus_on_click=False,
                                          visible=False)
        self.busy_cancel_btn.connect("clicked", lambda *_: self.cancel_busy())
        busy.append(self.busy_cancel_btn)
        self._busy_cancel = None
        self.stack.add_named(busy, "busy")
        self._busy_id = None
        self.search_error = None
        self.edit_after = None         # (uri, start(item)) nach dem nächsten Laden
        self.editing = None            # Inline-Umbenennen läuft: {"item", "text", "cell", "done"}
        # 2-Finger-Zurück/Vor im -ist-Design (swipe.py, Seitenstapel mit Vorschau der Zielseite)
        self.swipe = swipe_mod.SwipeBack(self.stack, self._can_back, self.go_back, where=self._sw_where,
                                         target=self._sw_back, can_fwd=self._can_fwd, fwd=self.go_forward,
                                         fwd_target=self._sw_fwd, mouse=False)
        self.append(self.swipe.widget)
        self._pinch = 1.0

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("enter", lambda *_: self.on_focus and self.on_focus(self))
        self.add_controller(focus)
        click = Gtk.GestureClick(button=0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._any_click)
        self.add_controller(click)
        # Touchpad: zwei Finger spreizen = zoomen (waagrecht wischen macht swipe.py)
        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._pinch_begin)
        pinch.connect("scale-changed", self._pinch_changed)
        self.add_controller(pinch)

        self.set_zoom(self.zoom)
        if uri:
            self.go(uri, record=False)

    # ================================================================ Aufbau
    def _build_list(self):
        cv = Gtk.ColumnView(model=self.sel, show_row_separators=False, show_column_separators=False,
                            enable_rubberband=True, reorderable=False, tab_behavior=Gtk.ListTabBehavior.ITEM)
        cv.add_css_class("fw-list")
        self.columns = {}
        for key, title, expand, width in (("name", "name", True, 280), ("size", "größe", False, 92),
                                          ("mtime", "geändert", False, 132), ("type", "typ", False, 150),
                                          ("where", "ordner", False, 220), ("deleted", "gelöscht", False, 132)):
            f = Gtk.SignalListItemFactory()
            f.connect("setup", self._cell_setup, key)
            f.connect("bind", self._cell_bind, key)
            f.connect("unbind", self._cell_unbind, key)
            col = Gtk.ColumnViewColumn(title=title, factory=f, expand=expand, resizable=True)
            if not expand:
                col.set_fixed_width(width)
            if key in ("name", "size", "mtime", "type", "deleted"):
                col.set_sorter(Gtk.CustomSorter.new(lambda *a: 0))
            col._key = key
            cv.append_column(col)
            self.columns[key] = col
        self.columns["where"].set_visible(False)
        self.columns["deleted"].set_visible(False)
        cv.get_sorter().connect("changed", self._header_sort)
        cv.connect("activate", lambda v, pos: self._activate_pos(pos))
        return cv

    def _build_grid(self):
        f = Gtk.SignalListItemFactory()
        f.connect("setup", self._tile_setup)
        f.connect("bind", self._tile_bind)
        f.connect("unbind", self._tile_unbind)
        gv = Gtk.GridView(model=self.sel, factory=f, enable_rubberband=True, max_columns=40, min_columns=1,
                          tab_behavior=Gtk.ListTabBehavior.ITEM)
        gv.add_css_class("fw-grid")
        gv.connect("activate", lambda v, pos: self._activate_pos(pos))
        return gv

    # ----- Listenzellen -----
    def _cell_setup(self, _f, li, key):
        if key == "name":
            box = Gtk.Box(spacing=8)
            g = Gtk.Label(xalign=0.5)
            g.add_css_class("fglyph")
            n = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.MIDDLE)
            n.add_css_class("fname")
            box.append(g)
            box.append(n)
            box._g, box._n = g, n
            w = box
        else:
            w = Gtk.Label(xalign=1 if key == "size" else 0, ellipsize=Pango.EllipsizeMode.END)
            w.add_css_class("fmeta")
        w._item = None
        self._attach_item_controllers(w)
        li.set_child(w)

    def _is_cut(self, it):
        return it.uri in self.win.app.cut

    def _name_classes(self, it, link=True):
        return (["fname"] + (["fhidden"] if it.hidden else []) + (["flink"] if link and it.is_link else [])
                + (["fcut"] if self._is_cut(it) else []))

    def refresh_cut(self):
        """Ausgeschnittene Einträge (Strg+X) blass + kursiv zeigen – nach Ausschneiden/Einfügen/Abbrechen."""
        for it in self.shown:
            cut = self._is_cut(it)
            c = it.extra.get("_name_cell")
            if c is not None:
                c._n.set_css_classes(self._name_classes(it))
                c._g.set_css_classes(["fglyph", "g-" + it.group] + (["fcut"] if cut else []))
            t = it.extra.get("_tile_cell")
            if t is not None:
                t._n.set_css_classes(self._name_classes(it, link=False))
                t._g.set_css_classes(["tglyph", "g-" + it.group] + (["fcut"] if cut else []))
                t._pic.set_opacity(0.4 if cut else 1.0)

    def _cell_bind(self, _f, li, key):
        w, it = li.get_child(), li.get_item()
        w._item = it
        if key == "name":
            w._g.set_label(it.glyph)
            w._g.set_css_classes(["fglyph", "g-" + it.group])
            w._n.set_label(it.display)
            w._n.set_css_classes(self._name_classes(it))
            w._g.set_css_classes(["fglyph", "g-" + it.group] + (["fcut"] if self._is_cut(it) else []))
            w.set_tooltip_text(it.display if len(it.display) > 40 else None)
            it.extra["_name_cell"] = w
        elif key == "size":
            w.set_label(it.size_text())
            it.extra["_size_lbl"] = w
        elif key == "mtime":
            w.set_label(it.date_text())
        elif key == "type":
            w.set_label(it.type_text)
        elif key == "where":
            src = it.extra.get("orig_path") or it.path
            w.set_label(icons.home_relative(os.path.dirname(src)) if src else "")
        elif key == "deleted":
            d = it.extra.get("deleted", "")
            w.set_label(d.replace("T", " ")[:16])

    def _cell_unbind(self, _f, li, key):
        w = li.get_child()
        if key == "size" and w._item is not None and w._item.extra.get("_size_lbl") is w:
            w._item.extra.pop("_size_lbl", None)
        if key == "name" and w._item is not None and w._item.extra.get("_name_cell") is w:
            w._item.extra.pop("_name_cell", None)
        w._item = None

    # ----- Rasterkacheln -----
    def _tile_setup(self, _f, li):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("ftile")
        pic = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True, halign=Gtk.Align.CENTER,
                          valign=Gtk.Align.CENTER)
        pic.add_css_class("fthumb")
        g = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        frame = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        frame.add_named(g, "glyph")
        frame.add_named(pic, "pic")
        n = Gtk.Label(justify=Gtk.Justification.CENTER, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2,
                      ellipsize=Pango.EllipsizeMode.MIDDLE, max_width_chars=1)
        n.add_css_class("fname")
        box.append(frame)
        box.append(n)
        box._g, box._pic, box._frame, box._n = g, pic, frame, n
        box._item = None
        self._attach_item_controllers(box)
        li.set_child(box)

    def _tile_bind(self, _f, li):
        box, it = li.get_child(), li.get_item()
        box._item = it
        size = ZOOMS[self.zoom]
        box._frame.set_size_request(size, int(size * 0.78))
        box._g.set_label(it.glyph)
        box._g.set_css_classes(["tglyph", "g-" + it.group])
        box._n.set_label(it.display)
        box._n.set_css_classes(self._name_classes(it, link=False))
        box._g.set_css_classes(["tglyph", "g-" + it.group] + (["fcut"] if self._is_cut(it) else []))
        box._pic.set_opacity(0.4 if self._is_cut(it) else 1.0)
        box.set_tooltip_text(it.display)
        it.extra["_tile_cell"] = box
        box._frame.set_visible_child_name("glyph")
        if it.thumb is not None:
            box._pic.set_paintable(it.thumb)
            box._frame.set_visible_child_name("pic")
        elif self.prefs.get("thumbs", True) and it.group in icons.THUMBABLE:
            def got(tex, box=box, it=it):
                it.thumb = tex
                if box._item is it:
                    box._pic.set_paintable(tex)
                    box._frame.set_visible_child_name("pic")
            thumbs.get().get(it, got, "large" if size > 128 else "normal")

    def _tile_unbind(self, _f, li):
        box = li.get_child()
        if box._item is not None and box._item.extra.get("_tile_cell") is box:
            box._item.extra.pop("_tile_cell", None)
        box._item = None
        box._pic.set_paintable(None)

    # ----- Maus / Ziehen auf Einträgen -----
    def _attach_item_controllers(self, w):
        # Die Zelle nie als Extra-Argument/Standardwert mitgeben, sondern über ctrl.get_widget() holen:
        # Zelle → Controller → Handler → Zelle wäre ein Kreis über C, den Pythons GC nicht sieht – jede Zeile,
        # die GTK verwirft (Ordnerwechsel, Fenster zu), bliebe sonst samt Pane für immer im Speicher.
        click = Gtk.GestureClick(button=0)
        click.connect("pressed", lambda g, n, x, y: self._item_pressed(g, n, x, y, g.get_widget()))
        w.add_controller(click)
        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK)
        drag.connect("prepare", lambda src, x, y: self._drag_prepare(src, x, y, src.get_widget()))
        drag.connect("drag-begin", lambda src, d: self._drag_begin(src, d, src.get_widget()))
        w.add_controller(drag)
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK)
        drop.set_preload(True)

        def accept(t, d):
            c = t.get_widget()
            return bool(c is not None and c._item and c._item.is_dir and c._item.path)
        drop.connect("accept", accept)
        drop.connect("enter", lambda t, x, y: self._drop_motion(t, x, y, t.get_widget()))
        drop.connect("motion", lambda t, x, y: self._drop_motion(t, x, y, t.get_widget()))
        drop.connect("drop", lambda t, v, x, y: self._drop(t, v, x, y, t.get_widget()))
        w.add_controller(drop)

    def _attach_background(self, sc):
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK)
        drop.set_preload(True)
        drop.connect("enter", self._drop_motion, None)
        drop.connect("motion", self._drop_motion, None)
        drop.connect("drop", self._drop, None)
        sc.add_controller(drop)
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", self._bg_menu)
        sc.add_controller(right)
        left = Gtk.GestureClick(button=1, propagation_phase=Gtk.PropagationPhase.CAPTURE)
        left.connect("pressed", self._bg_click)
        sc.add_controller(left)

    def _bg_click(self, g, n, x, y):
        """Klick auf freie Fläche (nicht auf einen Eintrag) hebt die Auswahl auf – außer mit Strg/Umschalt."""
        if g.get_current_event_state() & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK):
            return
        w = g.get_widget().pick(x, y, Gtk.PickFlags.DEFAULT)
        if w is None or w.get_css_name() in ("listview", "gridview", "columnview", "scrolledwindow", "viewport"):
            self.sel.unselect_all()

    def _any_click(self, g, n, x, y):
        if self.on_focus:
            self.on_focus(self)

    def position_of(self, item):
        found, pos = self.store.find(item)
        return pos if found else None

    def _item_pressed(self, g, n, x, y, w):
        it = w._item
        if it is None:
            return
        btn = g.get_current_button()
        pos = self.position_of(it)
        if pos is None:
            return
        if btn == 3:
            if not self.sel.is_selected(pos):
                self.sel.select_item(pos, True)
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            if self.on_context:
                ok, pt = w.compute_point(self, Graphene_point(x, y))
                self.on_context(self, self.selected(), pt.x if ok else x, pt.y if ok else y)
        elif btn == 2:
            g.set_state(Gtk.EventSequenceState.CLAIMED)
            if self.on_middle:
                self.on_middle(self, it)

    def _bg_menu(self, g, n, x, y):
        if self.on_context:
            ok, pt = g.get_widget().compute_point(self, Graphene_point(x, y))
            self.sel.unselect_all()
            self.on_context(self, [], pt.x if ok else x, pt.y if ok else y)

    def _drag_prepare(self, src, x, y, w):
        it = w._item
        if it is None:
            return None
        pos = self.position_of(it)
        if pos is not None and not self.sel.is_selected(pos):
            self.sel.select_item(pos, True)
        items = self.selected() or [it]
        return content_for(items)

    def _drag_begin(self, src, drag, w):
        icon = Gtk.WidgetPaintable.new(w)
        src.set_icon(icon, 10, 10)

    def _drop_dest(self, w):
        if w is not None and w._item is not None and w._item.is_dir:
            return w._item.uri
        return self.uri

    def _drop_motion(self, target, x, y, w):
        dest = self._drop_dest(w)
        if not dest or not model.is_local(dest) and not dest.startswith(("smb:", "sftp:", "ftp:", "dav")):
            return 0
        state = target.get_current_event_state()
        if state & Gdk.ModifierType.CONTROL_MASK and state & Gdk.ModifierType.SHIFT_MASK:
            return Gdk.DragAction.LINK
        if state & Gdk.ModifierType.CONTROL_MASK:
            return Gdk.DragAction.COPY
        if state & Gdk.ModifierType.SHIFT_MASK:
            return Gdk.DragAction.MOVE
        val = target.get_value()
        files = val.get_files() if val else []
        if files and same_device(files[0].get_path(), model.path_of(dest)):
            return Gdk.DragAction.MOVE
        return Gdk.DragAction.COPY

    def _drop(self, target, value, x, y, w):
        files = value.get_files() if value else []
        dest = self._drop_dest(w)
        if not files or not dest:
            return False
        action = self._drop_motion(target, x, y, w) or Gdk.DragAction.COPY
        uris = [f.get_uri() for f in files]
        if all(Gio.File.new_for_uri(u).get_parent() and Gio.File.new_for_uri(u).get_parent().get_uri() == dest
               for u in uris) and action == Gdk.DragAction.MOVE:
            return False                # auf den eigenen Ordner gezogen: nichts tun
        self.win.drop_files(uris, dest, action)
        return True

    # ================================================================ Touchpad-Gesten
    def _can_back(self):
        return bool(self.back) and not self.search

    def _can_fwd(self):
        return bool(self.fwd) and not self.search

    @staticmethod
    def _sw_title(uri):
        if not uri:
            return "zurück"
        if uri.startswith("trash:"):
            return "papierkorb"
        f = Gio.File.new_for_uri(uri)
        p = f.get_path()
        if p and os.path.abspath(p) == os.path.expanduser("~"):
            return "home"
        return (os.path.basename(p.rstrip("/")) if p else f.get_basename()) or "/"

    def _sw_where(self):
        return self.uri, self._sw_title(self.uri)

    def _sw_back(self):
        return (self.back[-1], self._sw_title(self.back[-1])) if self.back else None

    def _sw_fwd(self):
        return (self.fwd[-1], self._sw_title(self.fwd[-1])) if self.fwd else None

    def _pinch_begin(self, g, seq):
        self._pinch = 1.0

    def _pinch_changed(self, g, scale):
        """Spreizen/Zusammenziehen: je ~30 % eine Zoomstufe (Liste ↔ Raster wie Strg+Plus/Minus)."""
        if scale >= self._pinch * 1.3:
            self._pinch = scale
            if self.on_zoom:
                self.on_zoom(self, 1)
        elif scale <= self._pinch / 1.3:
            self._pinch = scale
            if self.on_zoom:
                self.on_zoom(self, -1)

    def check_width(self):
        """Vom Fenster nach Größenänderungen aufgerufen."""
        width = self.get_width()
        if width <= 1:
            return
        tier = 2 if width >= 700 else (1 if width >= 470 else 0)
        if tier != getattr(self, "_tier", None):
            self._tier = tier
            # Spalten erst nach dem Größenwechsel (GTK stürzt ab, wenn Spalten mitten im Ziehen umschalten)
            if getattr(self, "_tier_id", None):
                GLib.source_remove(self._tier_id)
            self._tier_id = GLib.timeout_add(180, self._apply_tier_later)

    def _apply_tier_later(self):
        self._tier_id = None
        self._apply_tier()
        return False

    def _apply_tier(self, tier=None):
        """Schmale Ansicht: erst „typ“, dann „geändert“ ausblenden (statt abzuschneiden).
        Papierkorb: „gelöscht“ und „ursprung“ statt „geändert“ und „typ“."""
        tier = getattr(self, "_tier", 2) if tier is None else tier
        trash = bool(self.uri and self.uri.startswith("trash:"))
        want = {"type": tier >= 2 and not trash, "mtime": tier >= 1 and not trash,
                "deleted": trash and tier >= 1, "where": bool(self.search) or (trash and tier >= 2)}
        for k, v in want.items():
            if self.columns[k].get_visible() != v:
                self.columns[k].set_visible(v)
        self.columns["where"].set_title("ursprung" if trash else "ordner")
        return False

    # ================================================================ Navigation
    def go(self, uri, record=True, select=None):
        self.cancel_edit()
        uri = normalize(uri)
        if record and self.uri and uri != self.uri:
            self.back.append(self.uri)
            del self.back[:-100]
            self.fwd.clear()
        self.end_search(reload=False)
        self.select_after = select
        self.uri = uri
        self.filter = ""
        self._apply_tier()
        self._watch()
        self.reload()
        if self.on_location:
            self.on_location(self)

    def go_back(self):
        if self.back:
            self.fwd.append(self.uri)
            prev = self.back.pop()
            self.go(prev, record=False, select=self.uri)

    def go_forward(self):
        if self.fwd:
            self.back.append(self.uri)
            self.go(self.fwd.pop(), record=False)

    def go_up(self):
        f = Gio.File.new_for_uri(self.uri)
        parent = f.get_parent()
        if parent is not None:
            self.go(parent.get_uri(), select=self.uri)

    def reload(self, keep=True):
        if self.search:
            return
        self.loading = True
        keep_sel = {it.uri for it in self.selected()} if keep else set()
        if not model.is_local(self.uri) and not self.uri.startswith(("trash:", "recent:")) and not self._busy_id:
            def slow():
                self._busy_id = None
                if self.loading:
                    import urllib.parse
                    name = location_name(self.uri)
                    if name in ("/", ""):
                        name = urllib.parse.urlsplit(self.uri).hostname or name
                    self.show_busy(f"\U000F0318  {name} wird geladen …", self._abort_load)
                return False
            self._busy_id = GLib.timeout_add(250, slow)
        self.loader.load(self.uri, lambda items, err: self._loaded(items, err, keep_sel))

    def _abort_load(self):
        """Langsames Laden (Netz) abbrechen: Ergebnis verwerfen, Fehlerseite statt endlosem Spinner."""
        self.loader.gen += 1
        self.loading = False
        self.error = GLib.Error("abgebrochen")
        self.all = []
        self._apply()
        self._show_empty("\U000F0349\nabgebrochen\nF5 versucht es nochmal")

    def _loaded(self, items, err, keep_sel):
        self.loading = False
        self.error = err
        if err is not None:
            self.all = []
            self._apply()
            msg = err.message if isinstance(err, GLib.Error) else (err.strerror or str(err))
            self._show_empty("\U000F0029\n" + msg)
        else:
            old = {it.uri: it for it in self.all}
            for it in items:                    # Vorschaubilder/Ordnergrößen behalten
                o = old.get(it.uri)
                if o is not None and o.mtime == it.mtime:
                    it.thumb, it.dir_count = o.thumb, o.dir_count
            self.all = items
            self._apply(keep=keep_sel)
            self._count_dirs()
        if self.on_loaded:
            self.on_loaded(self)

    def _watch(self):
        if self.monitor is not None:
            self.monitor.cancel()
            self.monitor = None
        try:
            f = Gio.File.new_for_uri(self.uri)
            self.monitor = f.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self.monitor.set_rate_limit(200)
            self.monitor.connect("changed", self._changed)
        except GLib.Error:
            self.monitor = None

    def _changed(self, mon, f, other, ev):
        # Inhalt wächst (Download): höchstens alle 2 s neu lesen; Anlegen/Löschen/Umbenennen fast sofort
        slow = ev == Gio.FileMonitorEvent.CHANGED
        if self.reload_id is None:
            self.reload_id = GLib.timeout_add(2000 if slow else 180, self._do_reload)

    def _do_reload(self):
        if self.editing:                # beim Umbenennen in der Zeile nicht neu aufbauen – später nachholen
            return True
        self.reload_id = None
        self.reload()
        return False

    def _count_dirs(self):
        """Anzahl der Einträge je Unterordner nachladen (für die Größenspalte), im Hintergrund."""
        dirs = [it for it in self.all if it.is_dir and it.path and it.dir_count is None][:800]
        if not dirs:
            return
        gen = self.loader.gen

        def work():
            res = []
            for it in dirs:
                try:
                    with os.scandir(it.path) as sc:
                        n = sum(1 for e in sc if self.show_hidden or not icons.is_hidden(e.name))
                except OSError:
                    n = None
                res.append((it, n))
            GLib.idle_add(lambda: self._counted(gen, res) and False)
        threading.Thread(target=work, daemon=True).start()

    def _counted(self, gen, res):
        if gen != self.loader.gen:
            return False
        for it, n in res:
            it.dir_count = n
            lbl = it.extra.get("_size_lbl")
            if lbl is not None and lbl._item is it:
                lbl.set_label(it.size_text())
        return False

    # ================================================================ Anzeige
    def _apply(self, keep=None):
        """Filter + Sortierung auf self.all anwenden und das Modell in einem Rutsch ersetzen."""
        if self.editing:                # Zeilen werden neu gebunden → Bearbeitung vorher abschließen
            self.finish_edit(True)
        items = self.all
        if not self.show_hidden and not self.search:
            items = [i for i in items if not i.hidden]
        if self.extra_filter is not None:
            items = [i for i in items if self.extra_filter(i)]
        if self.filter:
            q = self.filter.casefold()
            if any(c in q for c in "*?["):
                items = [i for i in items if fnmatch.fnmatch(i.display.casefold(), q)]
            else:
                items = [i for i in items if q in i.display.casefold()]
        items = model.sort_items(list(items), self.sort_field, self.sort_desc,
                                 self.prefs.get("folders_first", True))
        keep = keep if keep is not None else {it.uri for it in self.selected()}
        old = self.shown if len(self.shown) == self.store.get_n_items() else []
        same = lambda x, y: (x.uri, x.mtime, x.size, x.is_dir) == (y.uri, y.mtime, y.size, y.is_dir)
        n = min(len(old), len(items))
        p = 0
        while p < n and same(old[p], items[p]):
            p += 1
        s = 0
        while s < n - p and same(old[-1 - s], items[-1 - s]):
            s += 1
        # nur den geänderten Mittelteil austauschen: Bildlauf, Fokus und Menüs bleiben beim Neuladen
        # (z. B. wachsende Download-Datei) unberührt
        self.shown = old[:p] + items[p:len(items) - s] + old[len(old) - s:]
        self.store.splice(p, len(old) - p - s, items[p:len(items) - s])
        if self.select_after or keep:
            want = {self.select_after} if self.select_after else keep
            positions = [i for i, it in enumerate(items) if it.uri in want]
            if positions:
                bs = Gtk.Bitset.new_empty()
                for p in positions:
                    bs.add(p)
                self.sel.set_selection(bs, Gtk.Bitset.new_range(0, len(items)))
                if self.select_after:
                    GLib.idle_add(lambda p=positions[0]: self.scroll_to(p) and False)
            self.select_after = None
        self._update_empty()
        self._sync_header()
        if self.edit_after:               # gerade angelegt (neuer Ordner …) → gleich in der Zeile benennen
            uri, start = self.edit_after
            it = next((i for i in items if i.uri == uri), None)
            if it is not None:
                self.edit_after = None
                GLib.idle_add(lambda: (start(it), False)[1])

    def show_busy(self, text, cancel=None):
        """Spinner-Seite. cancel: Rückruf für den „abbrechen“-Knopf (None = Knopf bleibt weg)."""
        self.busy_lbl.set_label(text)
        self._busy_cancel = cancel
        self.busy_cancel_btn.set_visible(cancel is not None)
        self.busy_spin.start()
        self.stack.set_visible_child_name("busy")

    def cancel_busy(self):
        cb, self._busy_cancel = self._busy_cancel, None
        self.hide_busy()
        if cb:
            cb()

    def hide_busy(self):
        self._busy_cancel = None
        self.busy_cancel_btn.set_visible(False)
        if self._busy_id:
            GLib.source_remove(self._busy_id)
            self._busy_id = None
        if self.stack.get_visible_child_name() == "busy":
            self.busy_spin.stop()
            self._update_empty()

    def _update_empty(self):
        if self._busy_id:
            GLib.source_remove(self._busy_id)
            self._busy_id = None
        self.busy_spin.stop()
        if self.store.get_n_items():
            self.stack.set_visible_child_name(self.view)
        elif self.error is None and not self.loading:
            if self.search:
                txt = "\U000F0349\nsuche läuft …" if self.search.get("running") else "\U000F0349\nnichts gefunden"
            elif self.filter:
                txt = f"\U000F0349\nnichts passt zu „{self.filter}“"
            elif self.all and not self.show_hidden:
                txt = f"\U000F0209\nnur versteckte dateien ({len(self.all)})\nstrg+h zeigt sie"
            else:
                txt = "\U000F0770\nordner ist leer"
            self._show_empty(txt)

    def _show_empty(self, text):
        self.empty.set_label(text)
        self.stack.set_visible_child_name("empty")

    def set_view(self, view):
        self.finish_edit(True)
        self.view = view
        self._update_empty()

    def set_zoom(self, z):
        self.finish_edit(True)
        self.zoom = max(0, min(len(ZOOMS) - 1, z))
        for c in list(self.grid.get_css_classes()):
            if c.startswith("z"):
                self.grid.remove_css_class(c)
        self.grid.add_css_class(f"z{self.zoom}")
        if self.store.get_n_items():   # neu binden, damit die Größe greift
            items = list(self.shown)
            keep = {it.uri for it in self.selected()}
            self.store.splice(0, self.store.get_n_items(), items)
            self._restore(keep)

    def _restore(self, keep):
        bs = Gtk.Bitset.new_empty()
        for i, it in enumerate(self.shown):
            if it.uri in keep:
                bs.add(i)
        self.sel.set_selection(bs, Gtk.Bitset.new_range(0, len(self.shown)))

    def set_hidden(self, on):
        self.show_hidden = on
        self._apply()

    def set_filter(self, text):
        self.filter = text
        self._apply()

    def set_sort(self, field, desc):
        self.sort_field, self.sort_desc = field, desc
        self._apply()

    def _sync_header(self):
        cv = self.list
        col = self.columns.get(self.sort_field)
        cur = cv.get_sorter().get_primary_sort_column()
        order = Gtk.SortType.DESCENDING if self.sort_desc else Gtk.SortType.ASCENDING
        if col is not None and (cur is not col or cv.get_sorter().get_primary_sort_order() != order):
            self._syncing = True
            cv.sort_by_column(col, order)
            self._syncing = False

    def _header_sort(self, sorter, *_):
        if getattr(self, "_syncing", False):
            return
        col = sorter.get_primary_sort_column()
        if col is None:
            return
        desc = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        if col._key != self.sort_field or desc != self.sort_desc:
            self.sort_field, self.sort_desc = col._key, desc
            self.prefs["sort"], self.prefs["sort_desc"] = col._key, desc
            self._apply()

    def scroll_to(self, pos):
        view = self.list if self.view == "list" else self.grid
        try:
            view.scroll_to(pos, None, Gtk.ListScrollFlags.FOCUS, None) if self.view == "list" else \
                view.scroll_to(pos, Gtk.ListScrollFlags.FOCUS, None)
        except (TypeError, AttributeError):
            pass
        return False

    def focus_view(self):
        (self.list if self.view == "list" else self.grid).grab_focus()

    # ================================================================ Auswahl
    def selected(self):
        bs = self.sel.get_selection()
        return [self.store.get_item(p) for p in _bitset_iter(bs)]

    def select_all(self):
        self.sel.select_all()

    def select_none(self):
        self.sel.unselect_all()

    def invert(self):
        n = self.store.get_n_items()
        cur = self.sel.get_selection()
        bs = Gtk.Bitset.new_range(0, n)
        bs.difference(cur)
        self.sel.set_selection(bs, Gtk.Bitset.new_range(0, n))

    def select_pattern(self, pattern):
        bs = Gtk.Bitset.new_empty()
        p = pattern.casefold()
        for i, it in enumerate(self.shown):
            if fnmatch.fnmatch(it.display.casefold(), p if any(c in p for c in "*?[") else f"*{p}*"):
                bs.add(i)
        self.sel.set_selection(bs, Gtk.Bitset.new_range(0, len(self.shown)))
        return bs.get_size()

    def select_uris(self, uris):
        self.select_after = None
        want = set(uris)
        bs = Gtk.Bitset.new_empty()
        first = None
        for i, it in enumerate(self.shown):
            if it.uri in want:
                bs.add(i)
                first = i if first is None else first
        self.sel.set_selection(bs, Gtk.Bitset.new_range(0, len(self.shown)))
        if first is not None:
            self.scroll_to(first)

    def cursor_item(self):
        sel = self.selected()
        return sel[0] if sel else None

    def _activate_pos(self, pos):
        it = self.store.get_item(pos)
        if it is not None and self.on_activate:
            sel = self.selected()
            self.on_activate(self, sel if it in sel and len(sel) > 1 else [it])

    # ================================================================ Umbenennen in der Zeile
    def edit_name(self, item, done, text=None, select_stem=True):
        """Namen direkt in der Zeile/Kachel bearbeiten (wie im Windows-Explorer).
        done(neuer_name) bei Enter oder Klick daneben, done(None) bei Esc."""
        self.cancel_edit()
        pos = self.position_of(item)
        if pos is None:
            return False
        self.scroll_to(pos)
        self.editing = {"item": item, "text": text if text is not None else item.name, "done": done,
                        "stem": select_stem, "tries": 0}
        GLib.idle_add(self._edit_attach)
        return True

    def _edit_attach(self):
        ed = self.editing
        if not ed or ed.get("cell"):
            return False
        it = ed["item"]
        cell = it.extra.get("_name_cell" if self.view == "list" else "_tile_cell")
        if cell is None or cell._item is not it:
            ed["tries"] += 1
            if ed["tries"] > 40:              # Zeile kam nie in Sicht → abbrechen
                self.editing = None
                ed["done"](None)
                return False
            return True                       # nach dem Scrollen noch nicht gebunden: nochmal versuchen
        t = Gtk.Text(hexpand=True, text=ed["text"], propagate_text_width=False)
        t.add_css_class("fw-inline")
        if self.view != "list":
            t.set_alignment(0.5)
            t.add_css_class("tile")
        lbl = cell._n
        lbl.set_visible(False)
        if self.view == "list":
            cell.append(t)
        else:
            cell.insert_child_after(t, cell._frame)
        ed["cell"], ed["widget"] = cell, t
        t.connect("activate", lambda *_: self.finish_edit(True))
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._edit_key)
        t.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: GLib.idle_add(self._edit_left))
        t.add_controller(focus)
        t.grab_focus()
        name = ed["text"]
        dot = name.rfind(".") if ed["stem"] and not it.is_dir else -1
        t.select_region(0, dot if dot > 0 else -1)
        return False

    def _edit_key(self, ctrl, kv, _code, state):
        if kv == Gdk.KEY_Escape:
            self.finish_edit(False)
            return True
        if kv in (Gdk.KEY_Up, Gdk.KEY_Down, Gdk.KEY_Page_Up, Gdk.KEY_Page_Down):
            return True                       # nicht in der Liste weiterwandern, solange man tippt
        return False

    def _edit_left(self):
        ed = self.editing
        if ed and ed.get("widget") is not None and not ed["widget"].has_focus():
            self.finish_edit(True)
        return False

    def finish_edit(self, ok):
        ed = self.editing
        if not ed:
            return
        self.editing = None
        t, cell = ed.get("widget"), ed.get("cell")
        new = t.get_text().strip() if (ok and t is not None) else None
        if t is not None and cell is not None:
            had_focus = t.has_focus()
            cell.remove(t)
            cell._n.set_visible(True)
            if had_focus:
                self.focus_view()
        ed["done"](new)

    def cancel_edit(self):
        if self.editing:
            self.finish_edit(False)

    # ================================================================ Tastatur
    def _on_key(self, _c, kv, _code, state):
        mods = state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SUPER_MASK)
        if mods:
            return False
        if kv == Gdk.KEY_BackSpace:
            if self.search:
                self.end_search()
            else:
                self.go_up()
            return True
        if kv == Gdk.KEY_Escape and (self.filter or self.search):
            self.win.clear_filter(self)
            return True
        ch = Gdk.keyval_to_unicode(kv)
        if ch and chr(ch).isprintable() and chr(ch) != " ":
            now = time.monotonic()
            self.typed = (self.typed if now - self.typed_at < 0.9 else "") + chr(ch).casefold()
            self.typed_at = now
            for i, it in enumerate(self.shown):
                if it.display.casefold().startswith(self.typed):
                    self.sel.select_item(i, True)
                    self.scroll_to(i)
                    break
            return True
        return False

    # ================================================================ rekursive Suche
    def start_search(self, query):
        base = model.path_of(self.uri)
        if not base or not query.strip():
            return
        q, test, err = model.parse_query(query)
        if err:
            self.search_error = err
            return
        self.search_error = None
        gen = (self.search or {}).get("gen", 0) + 1
        self.search = {"query": query, "root": base, "gen": gen, "running": True, "count": 0}
        self.all, self.filter = [], ""
        self.columns["where"].set_visible(True)
        self._apply()
        glob = any(c in q for c in "*?[")
        hidden = self.show_hidden
        batch, lock = [], threading.Lock()

        def flush():
            with lock:
                items = list(batch)
                batch.clear()
            if not self.search or self.search["gen"] != gen:
                return False
            if items:
                built = [model.FileItem(n, u, p, d, l, s, m, mo, c, meta=x) for n, u, p, d, l, s, m, mo, c, x in items]
                self.all.extend(built)
                self._apply()
            return False

        def work():
            count, last = 0, time.monotonic()
            for root, dirs, files in os.walk(base):
                if not self.search or self.search["gen"] != gen:
                    return
                if not hidden:
                    dirs[:] = [d for d in dirs if not icons.is_hidden(d)]
                dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".cache")]
                for name in dirs + files:
                    n = name.casefold()
                    if (not q or (fnmatch.fnmatch(n, q) if glob else q in n)) and (hidden or not icons.is_hidden(name)):
                        full = os.path.join(root, name)
                        try:
                            st = os.stat(full)
                        except OSError:
                            continue
                        is_dir = os.path.isdir(full)
                        ctype = "inode/directory" if is_dir else Gio.content_type_guess(name, None)[0]
                        if test is not None and not test(name, is_dir, st.st_size, st.st_mtime, ctype):
                            continue
                        with lock:
                            batch.append((name, GLib.filename_to_uri(full, None), full, is_dir, os.path.islink(full),
                                          st.st_size, st.st_mtime, st.st_mode, ctype,
                                          model.item_meta(name, name, is_dir, False, st.st_mode)))
                        count += 1
                        if count >= 5000:
                            break
                if count >= 5000:
                    break
                if time.monotonic() - last > 0.15:
                    last = time.monotonic()
                    GLib.idle_add(flush)
            GLib.idle_add(flush)
            GLib.idle_add(lambda: self._search_done(gen, count) and False)
        threading.Thread(target=work, daemon=True).start()
        if self.on_location:
            self.on_location(self)

    def _search_done(self, gen, count):
        if self.search and self.search["gen"] == gen:
            self.search["running"] = False
            self.search["count"] = count
            self._update_empty()
            if self.on_loaded:
                self.on_loaded(self)

    def end_search(self, reload=True):
        if self.search is None:
            return
        self.search = None
        self.columns["where"].set_visible(False)
        if reload:
            self.reload(keep=False)
            if self.on_location:
                self.on_location(self)

    # ================================================================ Infos
    def title(self):
        if self.search:
            return f"suche „{self.search['query']}“"
        return location_name(self.uri)

    def is_trash(self):
        return self.uri.startswith("trash:")

    def local_path(self):
        return model.path_of(self.uri)


# ---------------------------------------------------------------- Hilfen
def Graphene_point(x, y):
    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene
    p = Graphene.Point()
    p.init(x, y)
    return p


def normalize(uri):
    if "://" not in uri and not uri.startswith(("trash:", "recent:")):
        uri = model.uri_for(os.path.abspath(os.path.expanduser(uri)))
    if uri in ("trash:", "trash://"):
        uri = "trash:///"
    if uri.startswith("file://") and len(uri) > 8 and uri.endswith("/"):
        uri = uri.rstrip("/") or "file:///"
    return uri


def location_name(uri):
    if uri.startswith("trash:"):
        return "papierkorb"
    if uri.startswith("recent:"):
        return "zuletzt benutzt"
    p = model.path_of(uri)
    if p:
        if p == "/":
            return "/"
        if p == os.path.expanduser("~"):
            return "~"
        return os.path.basename(p)
    f = Gio.File.new_for_uri(uri)
    return f.get_basename() or uri


def same_device(a, b):
    try:
        return a and b and os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def content_for(items):
    """Inhalt für Ziehen/Zwischenablage: Dateiliste + text/uri-list + Pfade als Text."""
    files = [Gio.File.new_for_uri(it.uri) for it in items]
    uris = "\r\n".join(it.uri for it in items) + "\r\n"
    paths = "\n".join(it.path or it.uri for it in items)
    providers = [
        Gdk.ContentProvider.new_for_value(GObject.Value(Gdk.FileList, Gdk.FileList.new_from_list(files))),
        Gdk.ContentProvider.new_for_bytes("text/uri-list", GLib.Bytes.new(uris.encode())),
        Gdk.ContentProvider.new_for_bytes("text/plain;charset=utf-8", GLib.Bytes.new(paths.encode())),
    ]
    return Gdk.ContentProvider.new_union(providers)
