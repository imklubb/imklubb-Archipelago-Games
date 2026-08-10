"""
map_view.py -- the maps, inside the client.

The same eleven maps and 195 pins the PopTracker pack draws, for players who
would rather not run a second program. It follows Taz between maps the same way
the tracker does, but from the client's own reading rather than a server key --
the client already knows which level he is in.

Everything here is guarded. A client that cannot build the view should still
work perfectly as a client, so every failure path ends in "no map tab" rather
than an exception.
"""

from typing import Dict, List, Optional

from . import _imports

# Kivy only exists when the GUI is running. A headless client, or one whose
# Kivy differs from what this expects, simply gets no map tab.
try:
    from kivy.core.image import Image as CoreImage      # noqa: F401
    from kivy.graphics import Color, Ellipse, Rectangle
    from kivy.uix.image import Image
    from kivy.uix.label import Label
    from kivy.uix.tabbedpanel import TabbedPanelItem
    from kivy.uix.widget import Widget
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.scrollview import ScrollView
    KIVY = True
except Exception:                                       # pragma: no cover
    KIVY = False
    Widget = object
    TabbedPanelItem = object


# Three states, and only three. Colouring by TYPE told a player what a pin was
# -- which the map already shows -- rather than the one thing they need at a
# glance: can I get this now?
IN_LOGIC = (0.16, 0.70, 0.24)       # green: reachable with what you have
OUT_OF_LOGIC = (0.80, 0.16, 0.16)   # red: something is still missing
CHECKED = (0.13, 0.13, 0.13)        # done
BOSS_LOCATION = {
    "Elephant Pong":  "BOSS 1: Gossamer Defeated",
    "Gladiatoons":    "BOSS 2: Daffy Defeated",
    "Dodge City":     "BOSS 3: Sam Defeated (Dodge City)",
    "Disco Volcano":  "BOSS 4: Sam Defeated (Disco Volcano)",
    "The Hindenbird": "BOSS 5: Tweety Defeated",
}

DONE_COLOUR = (0.22, 0.22, 0.22)
PIN_RADIUS = 7


def load_maps() -> Optional[dict]:
    """The map and pin data, or None if it is not bundled."""
    return _imports.data("taz_maps.json")


class MapCanvas(Widget):
    """One map, with its pins drawn on top.

    Positions are percentages of the image, so the drawing survives any window
    size -- which is the same reason the pin placer exported them that way.
    """

    def __init__(self, image_path: str, pins: List[dict], **kw):
        super().__init__(**kw)
        self.image_path = image_path
        self.pins = pins
        self.done = set()          # fully checked
        self.logic = set()         # reachable with what the player has
        self.hidden = set()        # not used by this seed
        self.on_pick = None        # called with a pin when one is clicked
        self.bind(pos=lambda *_: self.redraw(),
                  size=lambda *_: self.redraw())

    def set_state(self, done, hidden, logic=()):
        self.done, self.hidden = set(done), set(hidden)
        self.logic = set(logic)
        self.redraw()

    def on_touch_down(self, touch):
        """Clicking a pin says what is on it.

        Hovering would need a Kivy hover behaviour that this client does not
        set up, and a click is unambiguous on a touch screen too.
        """
        if not self.collide_point(*touch.pos) or not self.on_pick:
            return super().on_touch_down(touch)
        x, y, w, h = self._fit()
        if w <= 0:
            return super().on_touch_down(touch)
        best, best_d = None, PIN_RADIUS * 2.5
        for i, pin in enumerate(self.pins):
            if i in self.hidden:
                continue
            px = x + (pin["x"] / 100.0) * w
            py = y + h - (pin["y"] / 100.0) * h
            d = ((touch.x - px) ** 2 + (touch.y - py) ** 2) ** 0.5
            if d < best_d:
                best, best_d = i, d
        if best is not None:
            self.on_pick(self.pins[best], best in self.done,
                         best in self.logic)
            return True
        return super().on_touch_down(touch)

    def _fit(self):
        """The image rectangle, letterboxed to keep its aspect ratio.

        Stretching the map to the widget would put every pin in the wrong
        place, since their coordinates are relative to the image.
        """
        iw, ih = 1600.0, 1100.0
        if self.width <= 0 or self.height <= 0:
            return self.x, self.y, 0, 0
        scale = min(self.width / iw, self.height / ih)
        w, h = iw * scale, ih * scale
        return (self.x + (self.width - w) / 2,
                self.y + (self.height - h) / 2, w, h)

    def redraw(self):
        if not KIVY:
            return
        self.canvas.clear()
        x, y, w, h = self._fit()
        if w <= 0:
            return
        with self.canvas:
            Color(1, 1, 1, 1)
            Rectangle(source=self.image_path, pos=(x, y), size=(w, h))
            for i, pin in enumerate(self.pins):
                if i in self.hidden:
                    continue
                # Kivy's origin is bottom-left and the pin data is top-left,
                # so the vertical axis is flipped here rather than in the data.
                px = x + (pin["x"] / 100.0) * w
                py = y + h - (pin["y"] / 100.0) * h
                # The border first, then the fill on top of it.
                Color(1, 1, 1, 0.9)
                # Squares with a border, matching the tracker's markers --
                # circles read as decoration rather than as checks.
                Rectangle(pos=(px - PIN_RADIUS - 1, py - PIN_RADIUS - 1),
                          size=(PIN_RADIUS * 2 + 2, PIN_RADIUS * 2 + 2))
                if i in self.done:
                    Color(*CHECKED, 1)
                elif i in self.logic:
                    Color(*IN_LOGIC, 1)
                else:
                    Color(*OUT_OF_LOGIC, 1)
                Rectangle(pos=(px - PIN_RADIUS, py - PIN_RADIUS),
                          size=(PIN_RADIUS * 2, PIN_RADIUS * 2))


class MapTab:
    """The maps, as one client tab.

    Built with GameManager.add_client_tab, which is the client's own API for
    this -- the earlier attempt drove a TabbedPanel directly and threw, because
    this client's tab bar is an MDNavigationBar and nothing like one.

    The maps sit in a Spinner-and-canvas pair rather than nested tabs, since
    eleven more tabs inside a tab bar would be unreadable.
    """

    def __init__(self, data: dict):
        self.data = data
        self.canvases = {}
        self.canvas_area = None
        self.spinner = None
        self.current = None
        self.detail = None
        # What each pin covers, and what has been sent -- kept from the last
        # refresh so a click can answer without recomputing everything.
        self._last_names = {}
        self._last_sent = set()

    def build(self, image_dir: str):
        if not KIVY:
            return None
        from kivy.uix.spinner import Spinner

        names = [m["name"] for m in self.data["maps"]]
        root = BoxLayout(orientation="vertical")

        self.spinner = Spinner(text=names[0], values=names,
                               size_hint_y=None, height=36)
        self.spinner.bind(text=lambda _s, v: self.show(v, by_hand=True))
        root.add_widget(self.spinner)

        for m in self.data["maps"]:
            pins = self.data["pins"].get(m["name"], [])
            self.canvases[m["name"]] = MapCanvas(
                f"{image_dir}/{m['image']}", pins)

        self.canvas_area = BoxLayout()
        root.add_widget(self.canvas_area)

        self.detail = Label(text="Click a pin to see what is on it.",
                            size_hint_y=None, height=28)
        root.add_widget(self.detail)
        for c in self.canvases.values():
            c.on_pick = self._picked

        self.show(names[0])
        return root

    def _picked(self, pin, done, in_logic):
        """Say what is actually left on this pin.

        "Sandwiches -- in logic" is true and useless: the pin holds twenty
        checks and the player wants to know which. So the outstanding ones are
        named, with a count when there are too many to list.
        """
        state = "checked" if done else ("in logic" if in_logic
                                        else "NOT in logic")
        left = [n for n in self._last_names.get(id(pin), [])
                if n not in self._last_sent]
        if not left:
            self.detail.text = f"{pin['name']}  --  {state}"
            return

        # Strip the level prefix -- the map already says which level this is.
        short = [n.split(" - ", 1)[-1] for n in left]
        if len(short) == 1:
            what = short[0]
        elif len(short) <= 3:
            what = ", ".join(short)
        else:
            what = f"{len(short)} left, next: {short[0]}"
        self.detail.text = f"{pin['name']}  --  {state}  --  {what}"

    def show(self, name: str, by_hand: bool = False):
        """Swap which map is drawn. Called by the spinner and by the client
        when Taz changes level."""
        if not self.canvas_area or name not in self.canvases:
            return
        if name == self.current:
            return
        self.current = name
        self.canvas_area.clear_widgets()
        self.canvas_area.add_widget(self.canvases[name])
        if not by_hand and self.spinner and self.spinner.text != name:
            self.spinner.text = name

    def refresh(self, sent_names, opt):
        """Recolour every pin from what has been checked.

        A pin covers several locations -- a poster pin holds all seven -- so it
        only goes dark once every location under it is done. Sandwich and
        destruction pins follow the seed's settings, exactly as the tracker
        does: a threshold this seed never uses is not a check the player is
        missing, so it is hidden rather than shown as outstanding.
        """
        if not self.canvases:
            return
        sand = int(opt.get("sandwich_checks", 100) or 0)
        dest = int(opt.get("destruction_checks", 50) or 0)
        start_s = int(opt.get("starting_sandwiches", 0) or 0)
        start_d = int(opt.get("starting_destruction", 0) or 0)
        goal = {"standard": 50, "advanced": 75,
                "expert": 100}.get(opt.get("difficulty", "standard"), 50)

        owned = set(opt.get("_owned_items", ()))
        self._last_sent = set(sent_names)
        for name, canvas in self.canvases.items():
            done, hidden, logic = set(), set(), set()
            for i, pin in enumerate(canvas.pins):
                kind = pin["type"]
                if kind == "sandwich" and sand <= 0:
                    hidden.add(i)
                    continue
                if kind == "destruction" and dest <= 0:
                    hidden.add(i)
                    continue
                names = self._locations_for(name, pin, sand, dest,
                                            start_s, start_d, goal)
                self._last_names[id(pin)] = names
                if names and all(n in sent_names for n in names):
                    done.add(i)
                elif not names:
                    hidden.add(i)
                elif self._reachable(name, pin, owned, opt):
                    logic.add(i)
            canvas.set_state(done, hidden, logic)

    def _reachable(self, map_name, pin, owned, opt=None):
        """Can the player get to this pin right now?

        Mode matters, and getting it wrong is not subtle: Linear has no level
        unlock items at all, so asking for one there marks every pin red
        forever. It gates on the poster count instead, exactly as the world
        does -- a hub opens when the boss before it is within reach.
        """
        from . import logic as L
        opt = opt or {}
        linear = opt.get("mode") == "linear" or \
            opt.get("game_mode") == "linear"
        posters = int(opt.get("_posters", 0) or 0)

        level = map_name
        if map_name == "Main Map":
            level = pin["name"].split(" - ")[0]
        level = _canon(level)
        lid = {n: l for l, n in L.LEVELS}.get(level)

        # A bonus game booth stands OUTSIDE its level's entrance, in the hub,
        # so in Open mode the level's own unlock is irrelevant -- the bonus
        # unlock is the whole condition. Checked before the level gate rather
        # than after it, or the level test would refuse it first.
        if pin["type"] == "bonus" and not linear:
            return f"{level} Bonus Game Unlock" in owned

        # Getting INTO the level.
        if level in BOSS_LOCATION:
            if linear:
                gate = {"Elephant Pong": "gate_elephant_pong",
                        "Gladiatoons": "gate_gladiatoons",
                        "Dodge City": "gate_dodge_city",
                        "Disco Volcano": "gate_disco_volcano",
                        "The Hindenbird": "gate_disco_volcano"}[level]
                if posters < int(opt.get(gate, 100) or 0):
                    return False
            elif f"{level} Unlock" not in owned:
                return False
        elif level == "Yosemite Zoo":
            pass                       # the starting hub
        elif linear:
            # Which hub a level is in decides which gate opens it.
            need = {4: 0, 5: 0, 6: 0,
                    9: "gate_elephant_pong", 10: "gate_elephant_pong",
                    11: "gate_elephant_pong",
                    14: "gate_gladiatoons", 15: "gate_gladiatoons",
                    16: "gate_gladiatoons",
                    18: "gate_dodge_city"}.get(lid, 0)
            if need and posters < int(opt.get(need, 100) or 0):
                return False
        elif f"{level} Unlock" not in owned:
            return False

        # And what the pin itself needs on top of that.
        kind = pin["type"]
        if kind == "bonus":
            return f"{level} Bonus Game Unlock" in owned
        if kind == "catcher" or (kind == "group" and any(
                m["type"] == "catcher" for m in pin.get("members", []))):
            costume = L.LEVEL_COSTUME_NAME.get(lid if lid else 3)
            return costume in owned if costume else True
        return True

    def _locations_for(self, map_name, pin, sand, dest,
                       start_s, start_d, goal):
        """Every Archipelago location a pin stands for, for THIS seed."""
        from . import logic as L
        level = map_name
        if map_name == "Main Map":
            level = pin["name"].split(" - ")[0]
        level = _canon(level)
        kind = pin["type"]

        if kind == "sandwich":
            return [f"{level} - {v} Sandwiches"
                    for v in range(sand, 101, sand) if v > start_s]
        if kind == "destruction":
            return [f"{level} - {v}% Destruction"
                    for v in range(dest, goal + 1, dest) if v > start_d]
        if kind == "poster":
            if map_name == "Main Map":
                return [f"{level} - Poster - {n}"
                        for n in L.POSTER_NAMES.get(level, [])]
            return [f"{level} - Poster - {pin['name']}"]
        # The five boss pins on the main map. Their location is named for the
        # boss defeated, not for the arena -- "Elephant Pong - Level Complete"
        # is not a location that exists.
        if kind == "complete" and level in BOSS_LOCATION:
            return [BOSS_LOCATION[level]]

        if kind == "statue":
            return [f"{level} - Golden Sam Statue"]
        if kind == "complete":
            return [f"{level} - Level Complete"]
        if kind == "bonus":
            return [f"{level} - Bonus Game Completed"]

        if kind == "catcher":
            if map_name == "Main Map":
                cat = _imports.data("taz_catchers.json") or {}
                lid = {n: l for l, n in L.LEVELS}.get(level)
                if lid is None and level == "Yosemite Zoo":
                    lid = 3
                rec = (cat.get(str(lid)) or {}).get("catchers", [])
                return [f"{level} - Catcher - {c['name']}" for c in rec]
            return [f"{level} - Catcher - {pin['name']}"]

        if kind == "combined":
            # The main map's Extras pin: sandwiches, destruction, the statue
            # and the level completion, all under one marker.
            out = [f"{level} - {v} Sandwiches"
                   for v in range(sand, 101, sand) if v > start_s] if sand else []
            out += [f"{level} - {v}% Destruction"
                    for v in range(dest, goal + 1, dest)
                    if v > start_d] if dest else []
            out += [f"{level} - Golden Sam Statue",
                    f"{level} - Level Complete"]
            return out

        if kind == "group":
            # A pin holding several things at one spot.
            out = []
            for m in pin.get("members", []):
                out += self._locations_for(
                    map_name, {"type": m["type"], "name": m["name"]},
                    sand, dest, start_s, start_d, goal)
            return out

        return []


def _canon(name: str) -> str:
    import re
    from . import logic as L
    sq = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    table = {sq(n): n for _, n in L.LEVELS}
    table.update({sq(n): n for n in L.LEVEL_IDS.values()})
    return table.get(sq(name), name)
