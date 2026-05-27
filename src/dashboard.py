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
JWT_PATH = TMP / ".studio_jwt"
POLL = getattr(bot, "POLL_INTERVAL", 60)
LAUNCHD = ROOT / "scripts" / "launchd.sh"

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
    """Vivo si el último poll fue hace menos de ~3 ciclos."""
    lp = status.get("last_poll")
    if not lp:
        return False
    try:
        dt = datetime.fromisoformat(lp)
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
            "queue_count": status.get("queue_count"),
            "seen": len(seen),
            "pending_studio": len(pending),
            "pending_commands": len(control.get("commands", [])),
        },
        "queue": rows,
        "pending_studio": pending,
        "canceled": sorted(canceled),
        "jwt_days": _jwt_days_left(_read_text(JWT_PATH)),
        "log": _log_tail(60),
        "now": datetime.now(timezone.utc).isoformat(),
    }


# ── API ─────────────────────────────────────────────────────────────────────
@app.get("/api/state")
def api_state():
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
  :root{--coral:#FF6B7C;--black:#000;--white:#fff;--cream:#EBE6E4;--g1:#D4D0CE;--g3:#8D8A89;--g4:#5E5C5B;--g5:#2F2E2E;
        --sans:'Instrument Sans',sans-serif;--serif:'Instrument Serif',serif;}
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:var(--sans);background:var(--cream);color:var(--g5);padding:22px 26px;font-size:14px}
  h1{font-weight:700;font-size:1.5rem;color:var(--black);letter-spacing:-.01em}
  h1 .em{font-family:var(--serif);font-style:italic;font-weight:400;color:var(--coral)}
  .top{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:18px}
  .statusbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .dot{width:11px;height:11px;border-radius:50%;display:inline-block;margin-right:7px}
  .on{background:#2bbf6a}.off{background:var(--coral)}.paused{background:#e6a700}
  .chip{background:var(--white);border:1px solid var(--g1);border-radius:999px;padding:7px 14px;font-weight:600}
  .btn{font-family:var(--sans);font-weight:600;border:none;border-radius:9px;padding:8px 14px;cursor:pointer;font-size:.85rem}
  .btn-coral{background:var(--coral);color:#fff}.btn-dark{background:var(--black);color:#fff}
  .btn-ghost{background:var(--white);color:var(--g5);border:1px solid var(--g1)}
  .btn:active{transform:translateY(1px)}
  .cards{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-bottom:20px}
  .card{background:var(--white);border:1px solid var(--g1);border-radius:16px;padding:16px 18px}
  .card .k{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--g3);font-weight:600}
  .card .v{font-size:1.7rem;font-weight:700;color:var(--black);margin-top:4px;line-height:1}
  .card .v.coral{color:var(--coral)}
  .panel{background:var(--white);border:1px solid var(--g1);border-radius:16px;padding:18px 20px;margin-bottom:18px}
  .panel h2{font-size:1.05rem;font-weight:700;color:var(--black);margin-bottom:12px}
  table{width:100%;border-collapse:collapse;font-size:.86rem}
  th{text-align:left;color:var(--g3);font-weight:600;text-transform:uppercase;font-size:.7rem;letter-spacing:.05em;padding:6px 10px;border-bottom:1px solid var(--g1)}
  td{padding:9px 10px;border-bottom:1px solid #f1edeb;vertical-align:middle}
  tr:last-child td{border-bottom:none}
  .key{font-weight:700;color:var(--black)}
  .badge{font-size:.7rem;font-weight:700;padding:3px 9px;border-radius:999px;white-space:nowrap}
  .b-new{background:#fff0e6;color:#b35a00}.b-seen{background:#eef3f6;color:var(--g4)}.b-cancel{background:#fdeaec;color:#c0344a}
  .b-csv{background:#e9f8f0;color:#178a54}.b-nocsv{background:#f3f0ee;color:var(--g3)}
  .acts{display:flex;gap:6px;justify-content:flex-end}
  .mini{font-size:.78rem;padding:5px 10px;border-radius:7px}
  .log{background:#0e1116;color:#cfe3d8;font-family:ui-monospace,Menlo,monospace;font-size:11.5px;line-height:1.5;
       border-radius:12px;padding:14px;max-height:300px;overflow:auto;white-space:pre-wrap}
  .log .err{color:#ff8a9a}.log .warn{color:#e6c66a}
  .muted{color:var(--g3)}
  .wordmark{font-weight:600;letter-spacing:.25em;color:var(--black);font-size:.78rem}
  .toast{position:fixed;bottom:20px;right:20px;background:var(--black);color:#fff;padding:12px 18px;border-radius:10px;
         font-weight:600;opacity:0;transition:.25s;pointer-events:none}
  .toast.show{opacity:1}
  @media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}
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

  <div class="cards" id="cards"></div>

  <div class="panel">
    <h2>Mantenimiento</h2>
    <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px">
      <button class="btn btn-dark" onclick="restartBot()">↻ Reiniciar bot</button>
      <button class="btn btn-ghost" onclick="runHealth()">🩺 Health checks</button>
      <span id="health" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center"></span>
    </div>
    <div style="display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">
      <textarea id="jwt" placeholder="Pegá el JWT nuevo de Studio (seedtag_jwt de DevTools → Cookies)…"
        style="flex:1;min-width:300px;height:58px;border:1px solid var(--g1);border-radius:9px;padding:8px;font-family:ui-monospace,monospace;font-size:11px;resize:vertical"></textarea>
      <button class="btn btn-coral" onclick="renewJwt()">Renovar JWT + reiniciar</button>
    </div>
  </div>

  <div class="panel">
    <h2>Cola 1597 — Video Operations</h2>
    <table><thead><tr><th>Ticket</th><th>Resumen</th><th>Estado Jira</th><th>CSV</th><th>En el bot</th><th></th></tr></thead>
    <tbody id="queue"><tr><td colspan="6" class="muted">cargando…</td></tr></tbody></table>
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

  <div class="wordmark">SEEDTAG · Design Studio</div>
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
  if(b.alive&&b.paused){dot='paused';txt='en pausa';}
  else if(b.alive){dot='on';txt='corriendo';}
  document.getElementById('botstate').innerHTML=`<span class="dot ${dot}"></span> ${txt}`+(b.pid?` · PID ${b.pid}`:'');
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
function toast(m){ const t=document.getElementById('toast'); t.textContent=m; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3200); }
refresh(); setInterval(refresh,5000);
</script>
</body></html>
"""


if __name__ == "__main__":
    host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.getenv("DASHBOARD_PORT", "8787"))
    print(f"\n  Dashboard CSV CTV → http://{host}:{port}\n")
    app.run(host=host, port=port, debug=False)
