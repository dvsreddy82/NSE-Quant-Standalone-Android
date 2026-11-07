"""Entry points the Kotlin app calls through Chaquopy.

    start(home)            set up and start the engine (idempotent)
    get(path) -> str       the same JSON the companion app fetched from the Mac (/api/...)
    act(name, json) -> str actions: shoonya settings / sign-in / disconnect, refresh, direction call
All return JSON strings; errors come back as {"error": "..."}.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback

_engine = None


def start(home: str) -> str:
    global _engine
    if _engine is not None:
        return "already running"
    os.environ.setdefault("NSE_APP_HOME", home)
    os.environ.setdefault("NSE_YAHOO_LITE", "1")
    os.environ.setdefault("MONGO_TIMEOUT_MS", "1")
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import engine
    _engine = engine.Engine(home)
    _engine.start()
    return "started"


def _dump(o) -> str:
    return json.dumps(o, ensure_ascii=False, default=str)


def get(path: str) -> str:
    if _engine is None:
        return _dump({"error": "engine not started"})
    try:
        return _dump(_engine.routes.get(path))
    except KeyError:
        return _dump({"error": f"unknown endpoint {path}"})
    except Exception as exc:
        logging.getLogger("phone_api").warning("%s failed: %s\n%s", path, exc, traceback.format_exc())
        return _dump({"error": str(exc)})


def act(name: str, payload: str = "{}") -> str:
    import engine
    e = _engine
    try:
        args = json.loads(payload or "{}")
        if name == "shoonya_settings":
            out = engine.shoonya_settings_view()
            out["state"], out["text"] = e.broker.state()[:2]
            out["error"] = e.broker.error
            out["waiting"] = e.broker.catcher is not None
            return _dump(out)
        if name == "shoonya_save":
            engine.shoonya_save(args)
            return _dump({"ok": True})
        if name == "shoonya_signin":                   # returns the URL the app opens in the browser
            return _dump({"url": e.broker.begin_signin()})
        if name == "shoonya_code":                     # pasted redirect address / code
            code = engine.code_from(args.get("text", ""))
            if not code:
                return _dump({"error": "No code found in that text"})
            e.broker.cancel_signin()
            if not engine.in_session():
                e.broker.manual_after_hours = True
            e.run("connect", lambda: e.broker.connect_with(code=code), e._connected, force=True)
            return _dump({"ok": True})
        if name == "shoonya_disconnect":
            e.broker.disconnect()
            return _dump({"ok": True})
        if name == "refresh":
            e.need(args.get("what", ""))
            return _dump({"ok": True})
        if name == "direction_now":
            e.make_calls(manual=True)
            return _dump({"ok": True})
        if name == "events":
            return _dump({"events": list(reversed(e.events))[:30], "busy": sorted(e.busy)})
        return _dump({"error": f"unknown action {name}"})
    except Exception as exc:
        return _dump({"error": str(exc)})
