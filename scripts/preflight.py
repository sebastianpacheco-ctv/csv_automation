#!/usr/bin/env python3
"""Pre-flight para arrancar el burn-in.

Verifica que todo el stack (ffmpeg, deps, credenciales, conectividad)
está listo antes de cargar el launchd plist.

Exit 0 si todo OK, 1 si algo falla.

Uso:
    source venv/bin/activate
    python3 scripts/preflight.py
"""
import os
import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

OK = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"

errors = 0
warnings = 0


def check(name, fn, gating=True):
    """Corre un chequeo. Si `gating=False`, un fallo se muestra como warning
    amarillo pero NO bloquea el arranque (no suma a `errors`). Se usa para
    dependencias que ya no están en el flujo (p.ej. Filestage)."""
    global errors, warnings
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, f"excepcion: {type(e).__name__}: {e}"
    sigil = OK if ok else (FAIL if gating else WARN)
    print(f"  {sigil} {name}" + (f"  —  {detail}" if detail else ""))
    if not ok:
        if gating:
            errors += 1
        else:
            warnings += 1


print("\n=== PRE-FLIGHT CSV AUTOMATION ===\n")


def check_ffmpeg():
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True)
    if r.returncode == 0:
        return True, r.stdout.split("\n", 1)[0]
    return False, "no en PATH"


def check_deps():
    import requests  # noqa: F401
    import slack_sdk  # noqa: F401
    import dotenv  # noqa: F401
    # Extras NO críticos para el core del bot: gdown (carpetas de Drive; si falta
    # degrada con aviso en Slack), flask (dashboard), playwright (legacy Filestage,
    # fuera del flujo). No bloquean el burn-in.
    have, missing = [], []
    for mod in ("gdown", "flask", "playwright"):
        try:
            __import__(mod)
            have.append(mod)
        except ImportError:
            missing.append(mod)
    detail = "requests, slack_sdk, dotenv" + (" + " + ", ".join(have) if have else "")
    if missing:
        detail += f"  ·  opcionales sin instalar: {', '.join(missing)}"
    return True, detail


def check_env():
    # FILESTAGE_SESSION_COOKIE ya NO es requerida: Filestage está fuera del
    # flujo. Su cookie caduca con la sesión del navegador; no debe bloquear.
    needed = [
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT_KEY",
        "SLACK_BOT_TOKEN", "SLACK_CHANNEL_ID",
        "STUDIO_JWT_COOKIE",
    ]
    missing = [k for k in needed if not os.getenv(k) or os.getenv(k) == "..."]
    if missing:
        return False, f"falta(n): {', '.join(missing)}"
    return True, f"{len(needed)} keys presentes"


def check_slack():
    from slack_sdk import WebClient
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        return False, "SLACK_BOT_TOKEN vacio"
    r = WebClient(token=token).auth_test()
    if not r.get("ok"):
        return False, f"auth_test fallo: {r}"
    return True, f"bot @{r['user']} en team {r['team']}"


def check_filestage():
    import requests
    cookie = os.getenv("FILESTAGE_SESSION_COOKIE")
    if not cookie or cookie == "...":
        return False, "FILESTAGE_SESSION_COOKIE vacio"
    r = requests.get(
        "https://api.filestage.io/projects",
        cookies={"registeredSessionId": cookie},
        params={"team_id": "e16f96c4de9a0c1b11bbebab1ac09104", "viewArchived": "false"},
        headers={"Accept": "*/*", "Origin": "https://app.filestage.io",
                 "Referer": "https://app.filestage.io/"},
        timeout=15,
    )
    if r.status_code == 200:
        try:
            n = len(r.json())
        except Exception:
            n = "?"
        return True, f"HTTP 200, {n} folders accesibles"
    return False, f"HTTP {r.status_code} — cookie caducada (ignorable, fuera del flujo)"


def check_studio():
    from studio_api import StudioAPIClient
    jwt = os.getenv("STUDIO_JWT_COOKIE")
    if not jwt:
        return False, "STUDIO_JWT_COOKIE vacio — extraer de DevTools"
    sidecar = Path(os.getenv("TMP_DIR", "./tmp")) / ".studio_jwt"
    client = StudioAPIClient(jwt_cookie=jwt, sidecar_path=sidecar)
    user = client.ping()
    expected = "design_automations@seedtag.com"
    if user.get("email") != expected:
        return False, f"identidad incorrecta: {user.get('email')!r} (esperaba {expected!r})"
    return True, f"bot {user['email']}"


def check_jira():
    from jira_client import JiraClient
    j = JiraClient(
        base_url=os.getenv("JIRA_BASE_URL"),
        email=os.getenv("JIRA_EMAIL"),
        api_token=os.getenv("JIRA_API_TOKEN"),
        project_key=os.getenv("JIRA_PROJECT_KEY"),
    )
    issues = j.get_omniscreen_video_issues()
    if not issues:
        return True, "0 tickets abiertos en cola 1597 (la cola esta vacia ahora, OK)"
    sample = issues[0]
    key = sample.get("key", "?")
    fields = sample.get("fields", {})
    has_cf = bool(fields.get("customfield_14324") or fields.get("customfield_15865"))
    if not has_cf:
        return False, f"{len(issues)} tickets pero customfields no llegan poblados — JQL roto?"
    return True, f"{len(issues)} tickets; sample {key} con customfields OK"


check("ffmpeg disponible", check_ffmpeg)
check("Python deps", check_deps)
check(".env completo", check_env)
check("Slack bot autenticado", check_slack)
check("Filestage cookie (informativo, fuera del flujo)", check_filestage, gating=False)
check("Studio JWT bajo el bot", check_studio)
check("Jira polling + customfields", check_jira)

print()
if errors:
    print(f"\033[31m{errors} fallo(s). Resuelve antes de cargar launchd.\033[0m\n")
    sys.exit(1)
if warnings:
    print(f"\033[33m{warnings} warning(s) no bloqueante(s) (fuera del flujo, ignorable). "
          f"Listo para arrancar burn-in:\033[0m")
else:
    print("\033[32mTodo verde. Listo para arrancar burn-in:\033[0m")
print("  ./scripts/launchd.sh install\n")
sys.exit(0)
