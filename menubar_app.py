"""
menubar_app.py

A native macOS menu bar controller for your LG soundbar -- sliders live
right in the menu bar dropdown, no browser needed. Built with `rumps`
(a thin wrapper around AppKit's NSStatusItem).

Talks to the soundbar directly using lg_soundbar.py (same protocol client
used by server.py) -- this doesn't need server.py running at all, it's a
separate, self-contained way to control the same device.

Setup:
    pip3 install rumps
    (pycryptodome should already be installed from the web version)

Usage:
    python3 menubar_app.py 192.168.1.42
    (the IP is remembered after the first run in ~/.lg_soundbar_controller.json,
    so afterwards you can just run: python3 menubar_app.py)
"""

import json
import os
import sys

import rumps

from lg_soundbar import LGSoundbar

CONFIG_PATH = os.path.expanduser("~/.lg_soundbar_controller.json")

# Ranges reported by the device's own SETTING_VIEW_INFO response.
TRIM_CHANNELS = {
    # ui label      : (device field prefix, min, max)
    "Center":        ("center", -6, 6),
    "Dialogue":      ("dialog", 0, 6),
    "Subwoofer":     ("woofer", -15, 6),
    "Rear":          ("rear", -6, 6),
    "Height / Top":  ("top", -6, 6),
}

SETTERS = {
    "center": lambda bar, v: bar.set_center_level(v),
    "dialog": lambda bar, v: bar.set_dialog_level(v),
    "woofer": lambda bar, v: bar.set_subwoofer_level(v),
    "rear": lambda bar, v: bar.set_rear_level(v),
    "top": lambda bar, v: bar.set_top_level(v),
}


def load_saved_ip():
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f).get("ip")
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_ip(ip):
    with open(CONFIG_PATH, "w") as f:
        json.dump({"ip": ip}, f)


def hide_dock_icon():
    """Make this a menu-bar-only app (no Dock icon, no Cmd+Tab entry)."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception:
        pass  # cosmetic only -- app still works fine without this


class SoundbarMenuBarApp(rumps.App):
    def __init__(self, ip):
        super().__init__("\U0001F50A", quit_button=None)  # speaker emoji
        self.ip = ip

        self.bar = LGSoundbar(ip)
        self.bar.refresh_all()

        # --- Build the menu ---
        self.master_slider = rumps.SliderMenuItem(
            value=50, min_value=0, max_value=100,
            callback=self.on_master_slide, dimensions=(200, 15),
        )
        self.mute_item = rumps.MenuItem("Mute", callback=self.on_mute_click)
        self.rear_enable_item = rumps.MenuItem("Rear Speakers On", callback=self.on_rear_enable_click)
        self.refresh_item = rumps.MenuItem("Sync Now", callback=self.on_refresh_click)
        self.quit_item = rumps.MenuItem("Quit", callback=rumps.quit_application)

        self.trim_sliders = {}  # field -> SliderMenuItem

        menu_items = [
            rumps.MenuItem("Front / Master"),
            self.master_slider,
            self.mute_item,
            None,
        ]
        for label, (field, vmin, vmax) in TRIM_CHANNELS.items():
            slider = rumps.SliderMenuItem(
                value=0, min_value=vmin, max_value=vmax,
                callback=self._make_trim_callback(field), dimensions=(200, 15),
            )
            self.trim_sliders[field] = slider
            menu_items += [rumps.MenuItem(label), slider]

        menu_items += [
            None,
            self.rear_enable_item,
            None,
            self.refresh_item,
            self.quit_item,
        ]
        self.menu = menu_items

        # Give the initial get requests a moment to land, then paint the UI.
        rumps.Timer(self._initial_sync, 0.6).start()

        # Periodic sync so changes made in LG ThinQ (or the web mixer, if
        # it's also running) show up here too.
        self.sync_timer = rumps.Timer(self.periodic_sync, 2)
        self.sync_timer.start()

    def _initial_sync(self, timer):
        timer.stop()
        self.sync_from_device()

    def periodic_sync(self, _timer):
        self.sync_from_device()

    def sync_from_device(self):
        spk = self.bar.latest("SPK_LIST_VIEW_INFO")
        settings = self.bar.latest("SETTING_VIEW_INFO")

        if spk.get("i_vol") is not None:
            self.master_slider.value = spk["i_vol"]
        self.mute_item.state = 1 if spk.get("b_mute") else 0
        self.rear_enable_item.state = 1 if settings.get("b_rear", True) else 0

        for field, slider in self.trim_sliders.items():
            raw = settings.get(f"i_{field}_level")
            vmin = settings.get(f"i_{field}_min")
            if raw is not None and vmin is not None:
                slider.value = vmin + raw

    def _make_trim_callback(self, field):
        vmin = next(v for (f, v, _mx) in TRIM_CHANNELS.values() if f == field)

        def callback(sender):
            display_value = int(sender.value)
            raw_value = display_value - vmin
            SETTERS[field](self.bar, raw_value)

        return callback

    def on_master_slide(self, sender):
        self.bar.set_master_volume(int(sender.value))

    def on_mute_click(self, sender):
        sender.state = 0 if sender.state else 1
        self.bar.set_mute(bool(sender.state))

    def on_rear_enable_click(self, sender):
        sender.state = 0 if sender.state else 1
        self.bar.set_rear_enabled(bool(sender.state))

    def on_refresh_click(self, _sender):
        self.bar.refresh_all()
        rumps.Timer(self._initial_sync, 0.4).start()


def main():
    ip = None
    if len(sys.argv) > 1:
        ip = sys.argv[1]
        save_ip(ip)
    else:
        ip = load_saved_ip()

    if not ip:
        print("Usage: python3 menubar_app.py <soundbar-ip>")
        print("  (only needed the first time -- it's saved after that)")
        sys.exit(1)

    hide_dock_icon()
    print(f"Connecting to {ip} ... look for the speaker icon in your menu bar.")
    SoundbarMenuBarApp(ip).run()


if __name__ == "__main__":
    main()