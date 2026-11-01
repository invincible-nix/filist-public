# Vorschaubilder nach freedesktop-Standard: erst der gemeinsame Cache (~/.cache/thumbnails, den auch andere
# Programme füllen), fehlende bestellt Tumbler über D-Bus (Bilder, Videos, PDF, Office …). Ohne Tumbler
# rechnet filist Bilder selbst herunter. Laden und Skalieren passiert in einem kleinen Thread-Pool.
import concurrent.futures, hashlib, os

import gi
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib

CACHE = os.path.join(GLib.get_user_cache_dir(), "thumbnails")
FAIL_MAX = 5 * 1024 * 1024 * 1024
PIXBUF_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tif", "tiff", "ico", "svg", "avif", "heic", "heif", "jxl",
              "tga", "xpm", "pnm", "ppm"}


def _thumb_path(uri, flavor):
    return os.path.join(CACHE, flavor, hashlib.md5(uri.encode()).hexdigest() + ".png")


class Thumbs:
    MEM_LIMIT = 48 << 20

    def drop(self):
        """Alle Vorschauen im Speicher verwerfen (Fenster zu); die Dateien im Cache bleiben."""
        self.memory.clear()
        self.bytes = 0
        self.failed.clear()

    def __init__(self):
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="thumb")
        self.memory = {}           # (uri, mtime, flavor) → Texture
        self.bytes = 0             # Pixelgröße des Speichers (statt 3000 Stück: 256-px-Bilder summierten sich auf Hunderte MB)
        self.failed = set()
        self.waiting = {}          # uri → [(item, flavor, cb)]
        self.batch = {}            # uri → mime, gesammelt für Tumbler
        self.batch_id = None
        self.handles = {}          # tumbler handle → [uris]
        self.proxy = None
        self.supported = None
        self.enabled = True
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION, Gio.DBusProxyFlags.NONE, None, "org.freedesktop.thumbnails.Thumbnailer1",
                "/org/freedesktop/thumbnails/Thumbnailer1", "org.freedesktop.thumbnails.Thumbnailer1", None)
            self.proxy.connect("g-signal", self._on_signal)
            # sofort (wenige ms) – sonst gehen die ersten Anfragen an Tumbler vorbei
            _schemes, mimes = self.proxy.call_sync("GetSupported", None, Gio.DBusCallFlags.NONE, 1500,
                                                   None).unpack()
            self.supported = set(mimes)
        except GLib.Error:
            self.proxy = None
        import shutil
        self.ffmpeg = shutil.which("ffmpeg")

    def can(self, item):
        if not self.enabled or item.is_dir:
            return False
        if self.supported is not None and item.ctype in self.supported:
            return True
        if self.ffmpeg and item.ctype.startswith("video/") and item.path:
            return True
        from icons import ext_of
        return ext_of(item.name) in PIXBUF_EXT

    def get(self, item, cb, flavor="normal"):
        """Ruft cb(texture) im GTK-Thread auf, sobald das Bild da ist (sofort, wenn schon im Speicher)."""
        if not self.can(item):
            return
        key = (item.uri, item.mtime, flavor)
        tex = self.memory.get(key)
        if tex is not None:
            cb(tex)
            return
        if key in self.failed:
            return
        self.pool.submit(self._from_cache, item, flavor, cb, True)

    # ----- im Thread -----
    def _from_cache(self, item, flavor, cb, may_queue):
        path = _thumb_path(item.uri, flavor)
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file(path)
            mt = pb.get_option("tEXt::Thumb::MTime")
            if mt is None or int(float(mt)) == int(item.mtime):
                self._deliver(item, flavor, cb, Gdk.Texture.new_for_pixbuf(pb))
                return
        except (GLib.Error, ValueError):
            pass
        if may_queue:
            GLib.idle_add(lambda: self._queue(item, flavor, cb) and False)

    def _render_self(self, item, flavor, cb):
        size = 256 if flavor == "large" else 128
        if item.ctype.startswith("video/") and self.ffmpeg and item.path:
            self._render_video(item, flavor, cb, size)
            return
        try:
            if not item.path or os.path.getsize(item.path) > 80 * 1024 * 1024:
                raise GLib.Error("zu groß")
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(item.path, size, size, True)
            pb = pb.apply_embedded_orientation() or pb
            self._deliver(item, flavor, cb, Gdk.Texture.new_for_pixbuf(pb))
        except (GLib.Error, OSError):
            GLib.idle_add(lambda: self.failed.add((item.uri, item.mtime, flavor)) and False)

    def _render_video(self, item, flavor, cb, size):
        """Einzelbild nach ~10 % der Länge per ffmpeg; landet normgerecht im gemeinsamen Cache."""
        import subprocess
        try:
            dur = 0.0
            try:
                out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                      item.path], capture_output=True, text=True, timeout=10).stdout.strip()
                dur = float(out or 0)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
            png = subprocess.run([self.ffmpeg, "-v", "error", "-ss", f"{min(dur * 0.1, 30):.2f}", "-i", item.path,
                                  "-frames:v", "1", "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                                  "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=30).stdout
            if not png:
                raise OSError("kein Bild")
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(png)
            loader.close()
            pb = loader.get_pixbuf()
            path = _thumb_path(item.uri, flavor)
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            tmp = path + f".{os.getpid()}.tmp"
            pb.savev(tmp, "png", ["tEXt::Thumb::URI", "tEXt::Thumb::MTime"], [item.uri, str(int(item.mtime))])
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
            self._deliver(item, flavor, cb, Gdk.Texture.new_for_pixbuf(pb))
        except (GLib.Error, OSError, subprocess.SubprocessError):
            GLib.idle_add(lambda: self.failed.add((item.uri, item.mtime, flavor)) and False)

    def _deliver(self, item, flavor, cb, tex):
        def main():
            key = (item.uri, item.mtime, flavor)
            old = self.memory.pop(key, None)
            if old is not None:
                self.bytes -= old.get_width() * old.get_height() * 4
            self.memory[key] = tex
            self.bytes += tex.get_width() * tex.get_height() * 4
            while self.bytes > self.MEM_LIMIT and len(self.memory) > 1:      # älteste zuerst
                k = next(iter(self.memory))
                t = self.memory.pop(k)
                self.bytes -= t.get_width() * t.get_height() * 4
            cb(tex)
            return False
        GLib.idle_add(main)

    # ----- Tumbler -----
    def _queue(self, item, flavor, cb):
        if self.proxy is None or (self.supported is not None and item.ctype not in self.supported):
            self.pool.submit(self._render_self, item, flavor, cb)
            return
        self.waiting.setdefault(item.uri, []).append((item, flavor, cb))
        self.batch.setdefault(flavor, {})[item.uri] = item.ctype
        if self.batch_id is None:
            self.batch_id = GLib.timeout_add(60, self._flush)

    def _flush(self):
        self.batch_id = None
        for flavor, items in self.batch.items():
            uris, mimes = list(items), list(items.values())
            self.proxy.call("Queue", GLib.Variant("(asasssu)", (uris, mimes, flavor, "foreground", 0)),
                            Gio.DBusCallFlags.NONE, 5000, None, self._queued, (uris, flavor))
        self.batch = {}
        return False

    def _queued(self, proxy, res, data):
        uris, flavor = data
        try:
            (handle,) = proxy.call_finish(res).unpack()
            self.handles[handle] = uris
        except GLib.Error:
            self._fallback(uris)

    def _on_signal(self, proxy, sender, signal, params):
        if signal == "Ready":
            _h, uris = params.unpack()
            for u in uris:
                for item, flavor, cb in self.waiting.pop(u, []):
                    self.pool.submit(self._from_cache, item, flavor, cb, False)
        elif signal == "Error":
            _h, uris, _code, _msg = params.unpack()
            self._fallback(uris)
        elif signal == "Finished":
            (h,) = params.unpack()
            left = [u for u in self.handles.pop(h, []) if u in self.waiting]
            self._fallback(left)

    def _fallback(self, uris):
        from icons import ext_of
        for u in uris:
            for item, flavor, cb in self.waiting.pop(u, []):
                if ext_of(item.name) in PIXBUF_EXT:
                    self.pool.submit(self._render_self, item, flavor, cb)
                else:
                    self.failed.add((item.uri, item.mtime, flavor))


THUMBS = None


def get():
    global THUMBS
    if THUMBS is None:
        THUMBS = Thumbs()
    return THUMBS
