"""
MLB Edge Alpha Bot — Web Dashboard
Ejecuta el análisis en background y hace streaming del output al browser via SSE.
"""

import os
import sys
import json
import time
import threading
import importlib.util
from flask import Flask, render_template, Response, jsonify

app = Flask(__name__)

# ── Estado global ─────────────────────────────────────────────────────────────

_state = {
    "running":    False,
    "log_lines":  [],
    "error":      None,
    "completed":  False,
}
_lock = threading.Lock()


# ── Captura stdout ────────────────────────────────────────────────────────────

class _StreamCapture:
    def __init__(self, original):
        self._original = original

    def write(self, text):
        self._original.write(text)
        self._original.flush()
        if text.strip():
            with _lock:
                _state["log_lines"].append(text.rstrip("\n"))

    def flush(self):
        self._original.flush()

    def isatty(self):
        return False


# ── Ejecutar análisis en thread ───────────────────────────────────────────────

def _run_analysis():
    with _lock:
        _state["running"]   = True
        _state["log_lines"] = []
        _state["error"]     = None
        _state["completed"] = False

    old_stdout = sys.stdout
    sys.stdout = _StreamCapture(old_stdout)

    try:
        # Importar y ejecutar MLB-AI dinámicamente
        script_path = os.path.join(os.path.dirname(__file__), "MLB-AI.py")
        spec = importlib.util.spec_from_file_location("mlb_ai", script_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.main()
    except Exception as e:
        with _lock:
            _state["error"] = str(e)
        print(f"❌ Error: {e}")
    finally:
        sys.stdout = old_stdout
        with _lock:
            _state["running"]   = False
            _state["completed"] = True


# ── Rutas Flask ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/run", methods=["POST"])
def run():
    with _lock:
        if _state["running"]:
            return jsonify({"status": "already_running"}), 409
    t = threading.Thread(target=_run_analysis, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/stream")
def stream():
    """Server-Sent Events — transmite líneas de log al browser."""
    sent = 0

    def generate():
        nonlocal sent
        while True:
            with _lock:
                lines   = _state["log_lines"]
                running = _state["running"]
                done    = _state["completed"]

            while sent < len(lines):
                line = lines[sent]
                yield f"data: {json.dumps(line)}\n\n"
                sent += 1

            if done and sent >= len(lines):
                yield "data: __DONE__\n\n"
                break

            time.sleep(0.15)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})


@app.route("/resultados")
def resultados():
    path = os.path.join(os.path.dirname(__file__), "resultados.json")
    if not os.path.exists(path):
        return jsonify([])
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/status")
def status():
    with _lock:
        return jsonify({
            "running":   _state["running"],
            "completed": _state["completed"],
            "error":     _state["error"],
            "lines":     len(_state["log_lines"]),
        })


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
