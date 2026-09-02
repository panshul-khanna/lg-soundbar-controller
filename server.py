"""
server.py

Local web server / mixer for your LG soundbar, live-synced with LG ThinQ.

The soundbar's "i_xxx_level" fields aren't the dB value itself -- they're
a zero-based count up from that channel's own minimum. The device reports
its true min/max for each channel alongside the level, e.g.:

    "i_woofer_level": 11, "i_woofer_min": -15, "i_woofer_max": 6

...meaning the real, ThinQ-matching dB value is: min + level (here: -4).

This server converts in both directions so the UI matches ThinQ's own
numbers and range, using the device's reported bounds rather than
hardcoded constants.

Run this on your Mac, then open http://localhost:8765

Usage:
    python3 server.py 192.168.1.42
"""

import json
import queue
import sys
import threading

from flask import Flask, jsonify, request, render_template, Response

from lg_soundbar import LGSoundbar

app = Flask(__name__)
bar = None  # set in main()
state_lock = threading.Lock()

# Maps our UI channel name -> the device's field prefix
TRIM_CHANNELS = {
    "subwoofer": "woofer",
    "rear": "rear",
    "top": "top",
    "center": "center",
    "dialog": "dialog",
}

_subscribers = []
_subscribers_lock = threading.Lock()


def extract_state():
    spk = bar.latest("SPK_LIST_VIEW_INFO")
    settings = bar.latest("SETTING_VIEW_INFO")

    state = {
        "master_volume": spk.get("i_vol"),
        "master_min": 0,
        "master_max": 100,
        "muted": spk.get("b_mute"),
        "rear_enabled": settings.get("b_rear"),
    }

    for ui_name, field in TRIM_CHANNELS.items():
        raw = settings.get(f"i_{field}_level")
        vmin = settings.get(f"i_{field}_min")
        vmax = settings.get(f"i_{field}_max")
        value = (vmin + raw) if (raw is not None and vmin is not None) else None
        state[f"{ui_name}_level"] = value
        state[f"{ui_name}_min"] = vmin
        state[f"{ui_name}_max"] = vmax

    return state


def broadcast_state():
    data = extract_state()
    with _subscribers_lock:
        for q in _subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass


def on_soundbar_update(_msg):
    broadcast_state()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    with state_lock:
        return jsonify(extract_state())


@app.route("/api/stream")
def api_stream():
    client_queue = queue.Queue(maxsize=10)
    with _subscribers_lock:
        _subscribers.append(client_queue)

    def generate():
        try:
            yield f"data: {json.dumps(extract_state())}\n\n"
            while True:
                data = client_queue.get()
                yield f"data: {json.dumps(data)}\n\n"
        finally:
            with _subscribers_lock:
                if client_queue in _subscribers:
                    _subscribers.remove(client_queue)

    return Response(generate(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    bar.refresh_all()
    return jsonify({"ok": True})


@app.route("/api/raw")
def api_raw():
    with state_lock:
        return jsonify({
            "SPK_LIST_VIEW_INFO": bar.latest("SPK_LIST_VIEW_INFO"),
            "SETTING_VIEW_INFO": bar.latest("SETTING_VIEW_INFO"),
        })


@app.route("/api/set/<channel>", methods=["POST"])
def api_set(channel):
    display_value = request.json.get("value")
    if display_value is None:
        return jsonify({"ok": False, "error": "missing value"}), 400
    display_value = int(display_value)

    if channel == "master":
        bar.set_master_volume(display_value)
        return jsonify({"ok": True})

    if channel not in TRIM_CHANNELS:
        return jsonify({"ok": False, "error": "unknown channel"}), 400

    field = TRIM_CHANNELS[channel]
    settings = bar.latest("SETTING_VIEW_INFO")
    vmin = settings.get(f"i_{field}_min")
    if vmin is None:
        return jsonify({
            "ok": False,
            "error": "haven't heard the device's real range yet -- try Sync"
        }), 409

    raw_level = display_value - vmin  # convert dB-style display back to raw

    setters = {
        "subwoofer": bar.set_subwoofer_level,
        "rear": bar.set_rear_level,
        "top": bar.set_top_level,
        "center": bar.set_center_level,
        "dialog": bar.set_dialog_level,
    }
    setters[channel](raw_level)
    return jsonify({"ok": True})


@app.route("/api/mute", methods=["POST"])
def api_mute():
    enable = bool(request.json.get("enable"))
    bar.set_mute(enable)
    return jsonify({"ok": True})


@app.route("/api/rear-enabled", methods=["POST"])
def api_rear_enabled():
    enable = bool(request.json.get("enable"))
    bar.set_rear_enabled(enable)
    return jsonify({"ok": True})


def main():
    global bar
    if len(sys.argv) < 2:
        print("Usage: python3 server.py <soundbar-ip>")
        print("  e.g. python3 server.py 192.168.1.42")
        sys.exit(1)

    host_ip = sys.argv[1]
    print(f"Connecting to LG soundbar at {host_ip}:9741 ...")
    bar = LGSoundbar(host_ip, on_update=on_soundbar_update)
    bar.refresh_all()
    print("Connected. Open http://localhost:8765 in your browser.")
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)


if __name__ == "__main__":
    main()