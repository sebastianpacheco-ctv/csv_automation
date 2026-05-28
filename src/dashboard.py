"""
Dashboard de administración del bot CSV CTV — Seedtag Design Studio.

Web liviana (Flask) para VER el estado del bot y CONTROLARLO sin tocar archivos
ni terminal. Lee los sidecars de TMP_DIR + la cola 1597 de Jira + el log, y
escribe comandos en tmp/.bot_control.json que el bot ejecuta en su próximo loop.

NO toca el bot directamente: el acoplamiento es solo vía archivos (control +
status). Si el dashboard no corre, el bot funciona igual; si el bot no corre,
el dashboard muestra "offline".

Correr local:
    source venv/bin/activate
    python3 src/dashboard.py            # → http://127.0.0.1:8787

En GCP: mismo archivo detrás de gunicorn/uvicorn + auth (IAP o login básico).
"""
import os
import sys
import json
import time
import base64
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from dotenv import load_dotenv
from flask import Flask, jsonify, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from jira_client import JiraClient          # noqa: E402
from studio_api import StudioAPIClient      # noqa: E402
import main as bot                          # noqa: E402  (seguro: main usa __main__ guard)

# main.py configura logging (RotatingFileHandler sobre automation.log) al
# importarse. Limpiamos los handlers EN ESTE proceso para que el dashboard no
# escriba ni rote el log del bot (evita conflicto de rotación entre procesos).
# El proceso del bot conserva sus handlers intactos.
import logging  # noqa: E402
for _h in list(logging.getLogger().handlers):
    logging.getLogger().removeHandler(_h)
    try:
        _h.close()
    except Exception:
        pass

TMP = Path(os.getenv("TMP_DIR", "./tmp"))
LOGS = Path(os.getenv("LOGS_DIR", "./logs"))
LOG_FILE = LOGS / "automation.log"
SEEN_PATH = TMP / ".seen_tickets.json"
CANCELED_PATH = TMP / ".canceled_tickets.json"
PENDING_PATH = TMP / ".pending_studio.json"
STATUS_PATH = TMP / ".bot_status.json"
CONTROL_PATH = TMP / ".bot_control.json"
HISTORY_PATH = TMP / ".bot_history.json"
APPROVAL_PATH = TMP / ".bot_approval.json"
JWT_PATH = TMP / ".studio_jwt"
POLL = getattr(bot, "POLL_INTERVAL", 60)
LAUNCHD = ROOT / "scripts" / "launchd.sh"
EDITABLE_CONFIG = ("BITRATE_SHORT", "BITRATE_LONG", "DURATION_THRESHOLD")
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mxf"}


def _set_env_var(key: str, value: str):
    """Setea KEY=value en el .env (reemplaza la línea o la agrega)."""
    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.strip().startswith(f"{key}="):
            out.append(f"{key}={value}"); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    env.write_text("\n".join(out) + "\n")


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except Exception:
                pass
    return total

VALID_ACTIONS = {"reprocess", "cancel", "reactivate", "pause", "resume"}

app = Flask(__name__)
_jira = None
_queue_cache = {"ts": 0.0, "data": []}


# ── Helpers de lectura ──────────────────────────────────────────────────────
def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _read_text(path: Path) -> str:
    try:
        return path.read_text().strip()
    except Exception:
        return ""


def _jira_client() -> JiraClient:
    global _jira
    if _jira is None:
        _jira = JiraClient(
            base_url=os.getenv("JIRA_BASE_URL"),
            email=os.getenv("JIRA_EMAIL"),
            api_token=os.getenv("JIRA_API_TOKEN"),
            project_key=os.getenv("JIRA_PROJECT_KEY"),
        )
    return _jira


def _get_queue_cached(ttl: int = 25):
    """Cola 1597 cacheada (evita martillar Jira en cada refresh del dashboard)."""
    now = time.time()
    if now - _queue_cache["ts"] < ttl:
        return _queue_cache["data"]
    try:
        data = _jira_client().get_omniscreen_video_issues()
    except Exception as e:
        app.logger.warning(f"No se pudo leer la cola: {e}")
        data = _queue_cache["data"]  # mantener lo último conocido
    _queue_cache["ts"] = now
    _queue_cache["data"] = data
    return data


def _jwt_days_left(jwt: str):
    """Días hasta que caduque el JWT de Studio (decodifica el payload, sin verificar)."""
    if not jwt or jwt.count(".") < 2:
        return None
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        exp = data.get("exp")
        if not exp:
            return None
        return round((exp - time.time()) / 86400, 1)
    except Exception:
        return None


def _is_alive(status: dict) -> bool:
    """Vivo si el último write de estado (updated_at, que refresca tanto el loop
    como el heartbeat _set_activity) fue hace menos de ~3 ciclos."""
    ts = status.get("updated_at") or status.get("last_poll")
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return age < POLL * 3 + 30
    except Exception:
        return False


def _status_name(fields: dict):
    st = fields.get("status")
    if isinstance(st, dict):
        return st.get("name")
    return st


def _log_tail(n: int = 60) -> list:
    try:
        lines = LOG_FILE.read_text(errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _build_state() -> dict:
    status = _read_json(STATUS_PATH, {})
    seen = set(_read_json(SEEN_PATH, []))
    canceled = set(_read_json(CANCELED_PATH, []))
    pending = _read_json(PENDING_PATH, [])
    control = _read_json(CONTROL_PATH, {"paused": False, "commands": []})

    rows = []
    for it in _get_queue_cached():
        k = it.get("key")
        if not k:
            continue
        f = it.get("fields", {}) or {}
        try:
            is_csv = bot.is_csv_ticket(it)
        except Exception:
            is_csv = None
        if k in canceled:
            state = "cancelado"
        elif k in seen:
            state = "procesado/visto"
        else:
            state = "nuevo (pendiente)"
        rows.append({
            "key": k,
            "summary": (f.get("summary") or "")[:90],
            "status": _status_name(f),
            "is_csv": is_csv,
            "state": state,
        })

    return {
        "bot": {
            "alive": _is_alive(status),
            "paused": bool(control.get("paused", status.get("paused", False))),
            "pid": status.get("pid"),
            "started_at": status.get("started_at"),
            "last_poll": status.get("last_poll"),
            "updated_at": status.get("updated_at"),
            "current": status.get("current"),
            "awaiting": status.get("awaiting"),
            "queue_count": status.get("queue_count"),
            "seen": len(seen),
            "pending_studio": len(pending),
            "pending_commands": len(control.get("commands", [])),
        },
        "queue": rows,
        "pending_studio": pending,
        "canceled": sorted(canceled),
        "history": list(reversed(_read_json(HISTORY_PATH, [])))[:25],
        "jwt_days": _jwt_days_left(_read_text(JWT_PATH)),
        "log": _log_tail(60),
        "now": datetime.now(timezone.utc).isoformat(),
    }


# ── API ─────────────────────────────────────────────────────────────────────
@app.get("/api/state")
def api_state():
    """Estado completo del bot que consume el frontend del dashboard: bot
    status (alive/paused/PID/current activity), cola 1597 (cacheada 25s),
    sidecars (seen/canceled/pending_studio), JWT days left, log tail, historial."""
    return jsonify(_build_state())


@app.post("/api/command")
def api_command():
    body = request.get_json(force=True, silent=True) or {}
    action = (body.get("action") or "").lower()
    key = (body.get("key") or "").upper().strip()
    if action not in VALID_ACTIONS:
        return jsonify({"ok": False, "error": f"acción inválida: {action}"}), 400
    if action in {"reprocess", "cancel", "reactivate"} and not key:
        return jsonify({"ok": False, "error": "falta el ticket key"}), 400

    ctrl = _read_json(CONTROL_PATH, {"paused": False, "commands": []})
    ctrl.setdefault("commands", [])
    if action == "pause":
        ctrl["paused"] = True
    elif action == "resume":
        ctrl["paused"] = False
    else:
        ctrl["commands"].append({
            "id": f"{int(time.time()*1000)}",
            "action": action, "key": key,
            "ts": datetime.now(timezone.utc).isoformat(),
        })
    try:
        CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTROL_PATH.write_text(json.dumps(ctrl, indent=2))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "queued": action, "key": key,
                    "note": f"el bot lo aplica en su próximo loop (≤{POLL}s)"})


def _run_launchd(verb: str) -> dict:
    try:
        r = subprocess.run(["bash", str(LAUNCHD), verb], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=60)
        return {"ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()[-400:]}
    except Exception as e:
        return {"ok": False, "out": str(e)}


@app.post("/api/restart")
def api_restart():
    res = _run_launchd("restart")
    return jsonify(res), (200 if res["ok"] else 500)


@app.post("/api/jwt")
def api_jwt():
    """Renueva el JWT de Studio: valida el token, lo guarda en el sidecar
    (.studio_jwt, que tiene preferencia) y reinicia el bot para que lo tome."""
    body = request.get_json(force=True, silent=True) or {}
    token = (body.get("token") or "").strip()
    if token.lower().startswith("seedtag_jwt="):
        token = token.split("=", 1)[1].strip()
    if token.count(".") < 2:
        return jsonify({"ok": False, "error": "no parece un JWT (faltan los 3 segmentos)"}), 400
    days = _jwt_days_left(token)
    if days is None:
        return jsonify({"ok": False, "error": "no pude decodificar el exp del JWT"}), 400
    if days <= 0:
        return jsonify({"ok": False, "error": f"ese JWT ya está caducado"}), 400
    try:
        JWT_PATH.parent.mkdir(parents=True, exist_ok=True)
        JWT_PATH.write_text(token)
        os.chmod(JWT_PATH, 0o600)
    except Exception as e:
        return jsonify({"ok": False, "error": f"no pude guardar el sidecar: {e}"}), 500
    restart = _run_launchd("restart")
    return jsonify({"ok": True, "days": days, "restarted": restart["ok"],
                    "note": f"JWT guardado (caduca en {days} d)" +
                            (" y bot reiniciado" if restart["ok"] else " — reiniciá el bot a mano")})


@app.get("/api/health")
def api_health():
    """Health checks en vivo: ffmpeg, Slack, Jira, Studio, JWT."""
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:80]})

    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        add("ffmpeg", r.returncode == 0,
            (r.stdout.splitlines() or ["?"])[0] if r.returncode == 0 else "no en PATH")
    except Exception as e:
        add("ffmpeg", False, e)
    try:
        import ssl, certifi
        from slack_sdk import WebClient
        a = WebClient(token=os.getenv("SLACK_BOT_TOKEN"),
                      ssl=ssl.create_default_context(cafile=certifi.where())).auth_test()
        add("Slack", a.get("ok"), f"@{a.get('user', '?')}")
    except Exception as e:
        add("Slack", False, e)
    try:
        import requests as _rq
        r = _rq.get(f"{os.getenv('JIRA_BASE_URL')}/rest/api/3/myself",
                    auth=(os.getenv("JIRA_EMAIL"), os.getenv("JIRA_API_TOKEN")),
                    headers={"Accept": "application/json"}, timeout=15)
        add("Jira", r.status_code == 200,
            r.json().get("emailAddress", "ok") if r.status_code == 200 else f"HTTP {r.status_code}")
    except Exception as e:
        add("Jira", False, e)
    try:
        jwt = _read_text(JWT_PATH) or os.getenv("STUDIO_JWT_COOKIE")
        u = StudioAPIClient(jwt_cookie=jwt, sidecar_path=None).ping()
        add("Studio", True, u.get("email", "ok"))
    except Exception as e:
        add("Studio", False, e)
    d = _jwt_days_left(_read_text(JWT_PATH))
    add("JWT Studio", d is not None and d > 2, f"{d} d" if d is not None else "?")
    return jsonify({"checks": checks, "now": datetime.now(timezone.utc).isoformat()})


@app.get("/api/disk")
def api_disk():
    import shutil
    try:
        free_gb = shutil.disk_usage(str(TMP)).free / (1024 ** 3)
    except Exception:
        free_gb = None
    folders, tmp_total = [], 0
    try:
        for d in TMP.iterdir():
            if d.is_dir() and d.name.upper().startswith("SDS-"):
                sz = _dir_size(d)
                tmp_total += sz
                folders.append({"name": d.name, "mb": round(sz / 1024 / 1024, 1)})
    except Exception:
        pass
    folders.sort(key=lambda x: -x["mb"])
    return jsonify({"tmp_mb": round(tmp_total / 1024 / 1024, 1),
                    "free_gb": round(free_gb, 1) if free_gb is not None else None,
                    "folders": folders[:20]})


@app.post("/api/cleanup")
def api_cleanup():
    """Borra los videos (.mp4, etc.) de las carpetas SDS-* en TMP, conservando
    sidecars (mismo criterio que el bot). Libera espacio; no toca Jira ni Studio."""
    freed, n = 0, 0
    try:
        for d in TMP.iterdir():
            if d.is_dir() and d.name.upper().startswith("SDS-"):
                for f in d.glob("*"):
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                        try:
                            freed += f.stat().st_size
                            f.unlink()
                            n += 1
                        except Exception:
                            pass
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "files": n, "freed_mb": round(freed / 1024 / 1024, 1)})


@app.get("/api/config")
def api_config_get():
    return jsonify({
        "editable": {k: os.getenv(k) for k in EDITABLE_CONFIG},
        "readonly": {"POLL_INTERVAL": POLL,
                     "MAX_JIRA_ATTACH_MB": getattr(bot, "MAX_JIRA_ATTACH_MB", None)},
    })


@app.post("/api/config")
def api_config_set():
    body = request.get_json(force=True, silent=True) or {}
    changed = {}
    for k in EDITABLE_CONFIG:
        if k not in body:
            continue
        sv = str(body[k]).strip()
        if not sv.isdigit() or int(sv) <= 0:
            return jsonify({"ok": False, "error": f"{k} debe ser un entero > 0"}), 400
        _set_env_var(k, sv)
        changed[k] = sv
    if not changed:
        return jsonify({"ok": False, "error": "nada para cambiar"}), 400
    restart = _run_launchd("restart")
    return jsonify({"ok": True, "changed": changed, "restarted": restart["ok"],
                    "note": "config guardada" + (" y bot reiniciado" if restart["ok"] else "")})


@app.post("/api/approve")
def api_approve():
    """Aprueba/rechaza el ticket en espera escribiendo .bot_approval.json, que
    el bot consume dentro de wait_for_ticket_response (no se puede postear 'ok'
    como bot porque el propio bot ignora mensajes de bots)."""
    body = request.get_json(force=True, silent=True) or {}
    decision = (body.get("decision") or "").lower()
    if decision not in {"ok", "no"}:
        return jsonify({"ok": False, "error": "decision debe ser 'ok' o 'no'"}), 400
    aw = _read_json(STATUS_PATH, {}).get("awaiting")
    if not aw or not aw.get("ts"):
        return jsonify({"ok": False, "error": "no hay ningún ticket esperando confirmación"}), 409
    try:
        APPROVAL_PATH.write_text(json.dumps({"ts": aw["ts"], "decision": decision}))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "decision": decision, "ticket": aw.get("ticket"),
                    "note": f"el bot lo aplica en ≤15s"})


@app.get("/")
def index():
    return HTML


# ── HTML (branding Seedtag) ───────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>CSV CTV — Panel del bot · Seedtag</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=Instrument+Serif:ital@1&display=swap" rel="stylesheet">
<style>
  :root{
    --coral:#FF6B7C; --white:#fff;
    --bg:#0e0e10; --surface:#1a1a1c; --surface2:#232327; --line:#34343b;
    --text:#EBE6E4; --text2:#b8b4b2; --muted:#8d8a89;
    --g1:#34343b; --g3:#8d8a89; --g4:#9a9694; --g5:#2f2e2e;
    --sans:'Instrument Sans',sans-serif; --serif:'Instrument Serif',serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:var(--sans);background:var(--bg);color:var(--text);padding:22px 26px;font-size:14px}
  h1{font-weight:700;font-size:1.5rem;color:#fff;letter-spacing:-.01em}
  h1 .em{font-family:var(--serif);font-style:italic;font-weight:400;color:var(--coral)}
  .top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
  .statusbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .dot{width:11px;height:11px;border-radius:50%;display:inline-block;margin-right:7px}
  .on{background:#2bbf6a}.off{background:var(--coral)}.paused{background:#e6a700}
  .chip{background:var(--surface2);border:1px solid var(--line);border-radius:999px;padding:7px 14px;font-weight:600;color:var(--text)}
  .btn{font-family:var(--sans);font-weight:600;border:none;border-radius:9px;padding:8px 14px;cursor:pointer;font-size:.85rem}
  .btn-coral{background:var(--coral);color:#fff}
  .btn-dark{background:var(--surface2);color:#fff;border:1px solid var(--line)}
  .btn-ghost{background:var(--surface2);color:var(--text);border:1px solid var(--line)}
  .btn-white{background:#fff;color:var(--coral)}
  .btn:active{transform:translateY(1px)}
  .cards{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:20px}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:16px 18px}
  .card .k{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:600}
  .card .v{font-size:1.7rem;font-weight:700;color:#fff;margin-top:4px;line-height:1}
  .card .v.coral{color:var(--coral)}
  .panel{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin-bottom:18px}
  .panel h2{font-size:1.05rem;font-weight:700;color:#fff;margin-bottom:12px}
  table{width:100%;border-collapse:collapse;font-size:.86rem}
  th{text-align:left;color:var(--muted);font-weight:600;text-transform:uppercase;font-size:.7rem;letter-spacing:.05em;padding:6px 10px;border-bottom:1px solid var(--line)}
  td{padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .key{font-weight:700;color:#fff}
  .badge{font-size:.7rem;font-weight:700;padding:3px 9px;border-radius:999px;white-space:nowrap}
  .b-new{background:rgba(230,167,0,.18);color:#f2c14e}
  .b-seen{background:rgba(255,255,255,.08);color:var(--text2)}
  .b-cancel{background:rgba(255,107,124,.20);color:#ff8a9a}
  .b-csv{background:rgba(43,191,106,.18);color:#57d98a}
  .b-nocsv{background:rgba(255,255,255,.06);color:var(--muted)}
  .acts{display:flex;gap:6px;justify-content:flex-end}
  .mini{font-size:.78rem;padding:5px 10px;border-radius:7px}
  .log{background:#08080a;color:#cfe3d8;border:1px solid var(--line);font-family:ui-monospace,Menlo,monospace;font-size:11.5px;line-height:1.5;
       border-radius:12px;padding:14px;max-height:300px;overflow:auto;white-space:pre-wrap}
  .log .err{color:#ff8a9a}.log .warn{color:#e6c66a}
  .muted{color:var(--muted)}
  .wordmark{font-weight:600;letter-spacing:.25em;color:var(--text2);font-size:.78rem}
  .toast{position:fixed;bottom:20px;right:20px;background:#fff;color:#000;padding:12px 18px;border-radius:10px;
         font-weight:600;opacity:0;transition:.25s;pointer-events:none}
  .toast.show{opacity:1}
  .cfg{display:flex;flex-direction:column;font-size:.72rem;color:var(--muted);font-weight:600;gap:4px;text-transform:uppercase;letter-spacing:.04em}
  .cfg input{width:130px;border:1px solid var(--line);border-radius:8px;padding:7px 9px;font-family:var(--sans);font-size:.95rem;background:var(--surface2);color:var(--text)}
  textarea{background:var(--surface2);color:var(--text)}
  .mrow{display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-top:14px;padding-top:14px;border-top:1px solid var(--line)}
  .approvebar{background:var(--coral);color:#fff;border-radius:14px;padding:14px 20px;margin-bottom:18px;display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
  .approvebar b{font-weight:700}

  /* ── A1: layout grid 2/3 + 1/3 ── */
  main.grid{display:grid;grid-template-columns:2fr 1fr;gap:18px;align-items:start;margin-top:0}
  main.grid > .col-main,
  main.grid > .col-side{display:flex;flex-direction:column;gap:18px}
  main.grid .panel{margin-bottom:0}  /* el gap del flex maneja la separación */
  @media(max-width:1100px){main.grid{grid-template-columns:1fr}}
  @media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}

  /* Banners / alerts top-of-page (sidecar D3, onboarding F4) — placeholders */
  #sidecar-alert,#onboarding-banner{margin-bottom:14px}
  #sidecar-alert[hidden],#onboarding-banner[hidden],
  #metrics[hidden],#help-panel[hidden]{display:none !important}
</style></head>
<body>
  <div class="top">
    <div>
      <h1>Panel del bot <span class="em">CSV CTV</span></h1>
      <div class="muted" id="subtitle" style="margin-top:4px">cargando…</div>
    </div>
    <div class="statusbar">
      <span class="chip" id="botstate">—</span>
      <button class="btn btn-dark" id="pausebtn" onclick="togglePause()">Pausar</button>
      <button class="btn btn-ghost" onclick="refresh()">↻ Refrescar</button>
    </div>
  </div>

  <!-- Banners top-of-content. Quedan ocultos hasta que el bloque correspondiente los active. -->
  <div id="onboarding-banner" hidden></div>     <!-- F4: hint primera vez (Guía rápida) -->
  <div id="sidecar-alert" hidden></div>         <!-- D3: aviso si .seen/.history/.pending son JSON corrupto -->

  <div id="approvebar"></div>                   <!-- Tanda 3: aprobar/rechazar (activo) -->

  <section class="metrics" id="metrics" hidden></section>   <!-- E3: 5 cards de métricas 7d -->

  <div class="cards" id="cards"></div>          <!-- cards de estado básico (activo) -->

  <!-- ── A1: layout 2/3 + 1/3 ── -->
  <main class="grid">
    <div class="col-main">

      <div class="panel">
        <h2>Cola 1597 — Video Operations</h2>
        <table><thead><tr><th>Ticket</th><th>Resumen</th><th>Estado Jira</th><th>CSV</th><th>En el bot</th><th></th></tr></thead>
        <tbody id="queue"><tr><td colspan="6" class="muted">cargando…</td></tr></tbody></table>
      </div>

      <div class="panel">
        <h2>Historial de procesado</h2>
        <table><thead><tr><th>Ticket</th><th>Resultado</th><th>Detalle</th><th>Cuándo</th><th></th></tr></thead>
        <tbody id="history"><tr><td colspan="5" class="muted">cargando…</td></tr></tbody></table>
      </div>

    </div>
    <aside class="col-side">

      <div class="panel">
        <h2>Mantenimiento</h2>
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">
          <button class="btn btn-dark" onclick="restartBot()">↻ Reiniciar bot</button>
          <button class="btn btn-ghost" onclick="runHealth()">🩺 Health checks</button>
          <span id="health" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"></span>
        </div>
        <div style="display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap" class="mrow">
          <textarea id="jwt" placeholder="Pegá el JWT nuevo de Studio (seedtag_jwt de DevTools → Cookies)…"
            style="flex:1;min-width:240px;height:58px;border:1px solid var(--g1);border-radius:9px;padding:8px;font-family:ui-monospace,monospace;font-size:11px;resize:vertical"></textarea>
          <button class="btn btn-coral" onclick="renewJwt()">Renovar JWT + reiniciar</button>
        </div>
        <div class="mrow">
          <button class="btn btn-ghost" onclick="loadDisk()">💾 Disco</button>
          <span id="disk" class="muted"></span>
          <button class="btn btn-ghost" onclick="cleanup()">🧹 Limpiar videos de TMP</button>
        </div>
        <div class="mrow" style="align-items:flex-end">
          <label class="cfg">Bitrate ≤30s (Mbps)<input id="BITRATE_SHORT" type="number" min="1"></label>
          <label class="cfg">Bitrate &gt;30s (Mbps)<input id="BITRATE_LONG" type="number" min="1"></label>
          <label class="cfg">Umbral duración (s)<input id="DURATION_THRESHOLD" type="number" min="1"></label>
          <button class="btn btn-coral" onclick="saveConfig()">Guardar config + reiniciar</button>
          <span id="cfgnote" class="muted"></span>
        </div>
      </div>

      <div class="panel">
        <h2>Pendientes de Studio (2ª pasada)</h2>
        <tbody><table id="pending"></table></tbody>
        <div id="pending-empty" class="muted">—</div>
      </div>

      <div class="panel">
        <h2>Log reciente</h2>
        <div class="log" id="log">—</div>
      </div>

    </aside>
  </main>

  <div class="wordmark">SEEDTAG · Design Studio</div>

  <section id="help-panel" hidden></section>    <!-- F2: Guía rápida (collapsible) -->

  <div class="toast" id="toast"></div>

<script>
let STATE=null;
async function refresh(){
  try{
    const r=await fetch('/api/state'); STATE=await r.json(); render(STATE);
  }catch(e){ document.getElementById('botstate').innerHTML='<span class="dot off"></span> sin conexión'; }
}
function fmtAge(iso){ if(!iso)return '—'; const s=(Date.now()-new Date(iso))/1000;
  if(s<60)return Math.round(s)+'s'; if(s<3600)return Math.round(s/60)+'m'; return Math.round(s/3600)+'h'; }
function render(s){
  const b=s.bot;
  let dot='off',txt='offline';
  const age=b.updated_at?(Date.now()-new Date(b.updated_at))/1000:1e9;
  if(b.current){
    if(age>1800){dot='paused';txt='ocupado (¿colgado?): '+b.current;}
    else{dot='on';txt='ocupado · '+b.current;}
  } else if(b.alive&&b.paused){dot='paused';txt='en pausa';}
  else if(b.alive){dot='on';txt='corriendo';}
  document.getElementById('botstate').innerHTML=`<span class="dot ${dot}"></span> ${txt}`+(b.pid?` · PID ${b.pid}`:'');
  const ab=document.getElementById('approvebar');
  if(b.awaiting&&b.awaiting.ticket){
    ab.className='approvebar';
    ab.innerHTML=`<span>⏳ <b>${b.awaiting.ticket}</b> espera tu confirmación</span>`+
      `<span style="display:flex;gap:8px"><button class="btn btn-white" onclick="approve('ok')">Aprobar</button>`+
      `<button class="btn btn-ghost" onclick="approve('no')">Rechazar</button></span>`;
  } else { ab.className=''; ab.innerHTML=''; }
  document.getElementById('pausebtn').textContent=b.paused?'Reanudar':'Pausar';
  document.getElementById('pausebtn').className='btn '+(b.paused?'btn-coral':'btn-dark');
  document.getElementById('subtitle').textContent=
    `último poll hace ${fmtAge(b.last_poll)} · ${b.pending_commands} comando(s) en cola`;
  const jwt=s.jwt_days==null?'—':(s.jwt_days+' d');
  const cards=[
    ['Cola 1597', b.queue_count==null?'—':b.queue_count,''],
    ['Procesados', b.seen,''],
    ['Pend. Studio', b.pending_studio, b.pending_studio>0?'coral':''],
    ['Cancelados', s.canceled.length,''],
    ['JWT Studio', jwt, (s.jwt_days!=null&&s.jwt_days<3)?'coral':''],
  ];
  document.getElementById('cards').innerHTML=cards.map(c=>
    `<div class="card"><div class="k">${c[0]}</div><div class="v ${c[2]}">${c[1]}</div></div>`).join('');

  document.getElementById('queue').innerHTML = s.queue.length? s.queue.map(t=>{
    const csv = t.is_csv ? '<span class="badge b-csv">CSV</span>' : '<span class="badge b-nocsv">no</span>';
    let st='b-seen', lbl=t.state;
    if(t.state.startsWith('nuevo'))st='b-new'; else if(t.state==='cancelado')st='b-cancel';
    let acts='';
    if(t.state==='cancelado') acts=`<button class="btn btn-coral mini" onclick="cmd('reactivate','${t.key}')">Reactivar</button>`;
    else acts=`<button class="btn btn-ghost mini" onclick="cmd('reprocess','${t.key}')">Reprocesar</button>`
            +`<button class="btn btn-ghost mini" onclick="cmd('cancel','${t.key}')">Cancelar</button>`;
    return `<tr><td class="key">${t.key}</td><td>${t.summary||''}</td><td>${t.status||'—'}</td>
      <td>${csv}</td><td><span class="badge ${st}">${lbl}</span></td><td><div class="acts">${acts}</div></td></tr>`;
  }).join('') : '<tr><td colspan="6" class="muted">cola vacía</td></tr>';

  const pe=document.getElementById('pending-empty'), pt=document.getElementById('pending');
  if(s.pending_studio.length){ pe.style.display='none';
    pt.innerHTML='<thead><tr><th>Ticket</th><th>video_id</th><th>desde</th></tr></thead><tbody>'+
      s.pending_studio.map(p=>`<tr><td class="key">${p.ticket_key||'?'}</td><td>${p.video_id||''}</td><td>${(p.created_at||'').slice(0,16)}</td></tr>`).join('')+'</tbody>';
  } else { pe.style.display='block'; pt.innerHTML=''; }

  const RES={ok:['b-csv','OK'],partial:['b-new','Parcial'],error:['b-cancel','Error'],
    no_video:['b-cancel','Sin video'],timeout:['b-new','Timeout'],canceled:['b-seen','Cancelado'],
    skipped_qr:['b-seen','QR manual']};
  document.getElementById('history').innerHTML = (s.history&&s.history.length)? s.history.map(h=>{
    const r=RES[h.result]||['b-seen',h.result];
    let detail='';
    if(h.creatives){ const ok=h.creatives.filter(c=>c.url).length;
      detail=`${ok}/${h.creatives.length} creatives`;
      const na=h.creatives.filter(c=>c.url&&!c.attached).length; if(na)detail+=` · ${na} sin adjuntar`;
    } else if(h.error){ detail=h.error; }
    const failed=['error','partial','no_video','timeout'].includes(h.result);
    const act=failed?`<button class="btn btn-coral mini" onclick="cmd('reprocess','${h.key}')">Reintentar</button>`:'';
    return `<tr><td class="key">${h.key}</td><td><span class="badge ${r[0]}">${r[1]}</span></td>
      <td class="muted" title="${(detail||'').replace(/"/g,'&quot;')}">${(detail||'—').slice(0,80)}</td>
      <td class="muted">${fmtAge(h.at)}</td><td><div class="acts">${act}</div></td></tr>`;
  }).join('') : '<tr><td colspan="5" class="muted">sin historial aún</td></tr>';

  document.getElementById('log').innerHTML = s.log.map(l=>{
    const e=l.replace(/&/g,'&amp;').replace(/</g,'&lt;');
    if(/ERROR|🚨|❌/.test(l))return `<span class="err">${e}</span>`;
    if(/WARNING|⚠️/.test(l))return `<span class="warn">${e}</span>`;
    return e;
  }).join('\n');
}
async function cmd(action,key){
  const r=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({action,key})});
  const j=await r.json();
  toast(j.ok?`✓ ${action} ${key||''} — ${j.note||''}`:`✗ ${j.error}`);
  setTimeout(refresh,600);
}
function togglePause(){ cmd(STATE&&STATE.bot.paused?'resume':'pause',''); }
async function restartBot(){
  if(!confirm('¿Reiniciar el bot? Solo hacelo si no hay un ticket procesándose ahora.'))return;
  toast('reiniciando bot…');
  const r=await fetch('/api/restart',{method:'POST'}); const j=await r.json();
  toast(j.ok?'✓ bot reiniciado':'✗ '+(j.out||'error')); setTimeout(refresh,3500);
}
async function runHealth(){
  const h=document.getElementById('health'); h.innerHTML='<span class="muted">chequeando…</span>';
  try{ const r=await fetch('/api/health'); const j=await r.json();
    h.innerHTML=j.checks.map(c=>`<span class="badge ${c.ok?'b-csv':'b-cancel'}" title="${(c.detail||'').replace(/"/g,'&quot;')}">${c.ok?'✓':'✗'} ${c.name}</span>`).join('');
  }catch(e){ h.innerHTML='<span class="badge b-cancel">✗ error</span>'; }
}
async function renewJwt(){
  const t=document.getElementById('jwt').value.trim();
  if(!t){toast('pegá el JWT primero');return;}
  toast('guardando JWT…');
  const r=await fetch('/api/jwt',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t})});
  const j=await r.json();
  toast(j.ok?('✓ '+j.note):'✗ '+j.error);
  if(j.ok){ document.getElementById('jwt').value=''; setTimeout(refresh,3500); }
}
async function approve(d){
  const r=await fetch('/api/approve',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({decision:d})});
  const j=await r.json(); toast(j.ok?`✓ ${d==='ok'?'aprobado':'rechazado'} ${j.ticket||''} — ${j.note||''}`:'✗ '+j.error);
  setTimeout(refresh,1800);
}
async function loadDisk(){
  document.getElementById('disk').textContent='…';
  try{ const j=await (await fetch('/api/disk')).json();
    document.getElementById('disk').textContent=`TMP ${j.tmp_mb} MB · libre ${j.free_gb} GB`+(j.folders.length?` · ${j.folders.length} carpeta(s)`:'');
  }catch(e){ document.getElementById('disk').textContent='error'; }
}
async function cleanup(){
  if(!confirm('¿Borrar los .mp4 de las carpetas SDS-* en TMP? Conserva sidecars; no toca Jira ni Studio.'))return;
  const j=await (await fetch('/api/cleanup',{method:'POST'})).json();
  toast(j.ok?`✓ ${j.files} archivo(s), ${j.freed_mb} MB liberados`:'✗ '+j.error); loadDisk();
}
async function loadConfig(){
  try{ const j=await (await fetch('/api/config')).json();
    for(const k in j.editable){const el=document.getElementById(k); if(el)el.value=j.editable[k]||'';}
  }catch(e){}
}
async function saveConfig(){
  if(!confirm('¿Guardar la config y reiniciar el bot?'))return;
  const body={}; ['BITRATE_SHORT','BITRATE_LONG','DURATION_THRESHOLD'].forEach(k=>{body[k]=document.getElementById(k).value;});
  const j=await (await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
  document.getElementById('cfgnote').textContent=j.ok?('✓ '+(j.note||'')):'✗ '+j.error;
  if(j.ok)setTimeout(()=>{refresh();loadConfig();},3500);
}
function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3200); }
refresh(); loadConfig(); loadDisk(); setInterval(refresh,5000);
</script>
</body></html>
"""


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    print(f"\n  Dashboard CSV CTV → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)
