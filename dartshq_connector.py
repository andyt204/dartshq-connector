"""
Standalone DartsHQ ↔ Autodarts bridge. Pack with PyInstaller on Windows (--onedir).
Dependencies: requests, tkinter (stdlib).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

AUTODARTS_URL = "http://localhost:3180/api/state"
AUTODARTS_TIMEOUT = 0.4
POLL_INTERVAL_S = 0.2
WARN_THROTTLE_S = 1.5
DARTSHQ_WARN_THROTTLE_S = 2.0
CONNECTOR_API_URL = "https://dartshq-connector-api-production.up.railway.app"


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def config_path() -> Path:
    return base_dir() / "config.json"


def parse_autodarts_state(data: dict) -> tuple[int, list[dict]]:
    raw = data.get("state", data.get("status", 0))
    if isinstance(raw, dict):
        raw = raw.get("value", 0)
    try:
        board_status = int(raw)
    except (TypeError, ValueError):
        board_status = 0

    raw_throws = data.get("throws") or []
    valid_throws = [
        t
        for t in raw_throws
        if isinstance(t, dict)
        and t.get("segment")
        and isinstance(t.get("segment"), dict)
        and "name" in t["segment"]
    ]
    return board_status, valid_throws


def throw_to_segment_coords(dart_data: dict) -> tuple[str, float, float]:
    name = dart_data.get("segment", {}).get("name", "Miss")
    coords = dart_data.get("coords") or {}
    if isinstance(coords, dict):
        x = float(coords.get("x", 0.0))
        y = float(coords.get("y", 0.0))
    else:
        x, y = 0.0, 0.0
    return str(name), x, y


def post_dart(
    session: requests.Session,
    secret: str,
    segment_name: str,
    x_coord: float,
    y_coord: float,
    board_status: int,
    visit_cleared: bool,
) -> bool:
    url = urljoin(CONNECTOR_API_URL.rstrip("/") + "/", "api/dart")
    payload = {
        "segment_name": segment_name,
        "x_coord": x_coord,
        "y_coord": y_coord,
        "board_status": board_status,
        "visit_cleared": visit_cleared,
    }
    try:
        r = session.post(
            url,
            json=payload,
            headers={
                "X-DartsHQ-Key": secret,
                "Content-Type": "application/json",
            },
            timeout=10.0,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def run_setup_gui(path: Path) -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("DartsHQ Connector Setup")
    root.resizable(False, False)

    main = ttk.Frame(root, padding=12)
    main.grid(row=0, column=0, sticky="nsew")

    ttk.Label(main, text="Connector Secret Key").grid(row=0, column=0, sticky="w", pady=(0, 4))
    secret_e = ttk.Entry(main, width=40, show="*")
    secret_e.grid(row=1, column=0, sticky="ew", pady=(0, 12))

    status_var = tk.StringVar(value="")

    def on_save():
        secret = secret_e.get()
        if not secret or not str(secret).strip():
            messagebox.showerror("DartsHQ Connector Setup", "Please enter your Connector Secret Key.")
            return
        secret = str(secret).strip()
        try:
            cfg = {"secret": secret}
            path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
            root.destroy()
        except OSError as e:
            status_var.set("")
            messagebox.showerror("DartsHQ Connector Setup", f"Could not save config:\n{e}")

    ttk.Button(main, text="Save & Connect", command=on_save).grid(row=2, column=0, sticky="ew")
    ttk.Label(main, textvariable=status_var).grid(row=3, column=0, pady=(8, 0))

    def on_close():
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

    if not path.is_file():
        sys.exit(0)


def load_config(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    cfg = json.loads(raw)
    if not isinstance(cfg, dict):
        raise ValueError("config.json must be a JSON object")
    if "secret" not in cfg or not str(cfg["secret"]).strip():
        raise ValueError("config.json missing or empty: secret")
    return cfg


def run_poll_loop(cfg: dict) -> None:
    secret = str(cfg["secret"])

    session = requests.Session()
    last_count = 0
    autodarts_ok = False
    last_autodarts_warn = 0.0
    last_dartshq_warn = 0.0

    print("DartsHQ Connector — monitoring Autodarts and DartsHQ.", flush=True)

    while True:
        try:
            r = session.get(AUTODARTS_URL, timeout=AUTODARTS_TIMEOUT)
            if r.status_code != 200:
                raise requests.HTTPError(response=r)
            data = r.json()
            if not isinstance(data, dict):
                raise ValueError("Autodarts state is not a JSON object")

            if not autodarts_ok:
                print("✅ Connected to Autodarts", flush=True)
                autodarts_ok = True

            board_status, valid_throws = parse_autodarts_state(data)
            n = len(valid_throws)

            if n == 0 and last_count > 0:
                if post_dart(
                    session,
                    secret,
                    "",
                    0.0,
                    0.0,
                    board_status,
                    True,
                ):
                    last_count = 0
                else:
                    now = time.time()
                    if now - last_dartshq_warn >= DARTSHQ_WARN_THROTTLE_S:
                        print(
                            "🔴 Cannot reach DartsHQ — check your connection",
                            flush=True,
                        )
                        last_dartshq_warn = now
            elif n < last_count:
                last_count = n
            elif n > last_count:
                for i in range(last_count, n):
                    dart_data = valid_throws[i]
                    seg, x, y = throw_to_segment_coords(dart_data)
                    if post_dart(
                        session,
                        secret,
                        seg,
                        x,
                        y,
                        board_status,
                        False,
                    ):
                        print(f"🎯 Dart sent: {seg}", flush=True)
                        last_count = i + 1
                    else:
                        now = time.time()
                        if now - last_dartshq_warn >= DARTSHQ_WARN_THROTTLE_S:
                            print(
                                "🔴 Cannot reach DartsHQ — check your connection",
                                flush=True,
                            )
                            last_dartshq_warn = now
                        break

        except (requests.RequestException, ValueError, json.JSONDecodeError):
            autodarts_ok = False
            now = time.time()
            if now - last_autodarts_warn >= WARN_THROTTLE_S:
                print("⚠️ Autodarts not found — retrying...", flush=True)
                last_autodarts_warn = now

        time.sleep(POLL_INTERVAL_S)


def main() -> None:
    path = config_path()
    if not path.is_file():
        run_setup_gui(path)

    try:
        cfg = load_config(path)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"Invalid config.json: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    try:
        run_poll_loop(cfg)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
