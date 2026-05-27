# Traspaso del bot CSV CTV — guía para el nuevo responsable

Bot de automatización del equipo CTV (Seedtag Design Studio): detecta tickets de
video CSV/COV en la cola **1597** de Jira, convierte el video, lo sube a Studio
como creative, lo adjunta/comenta en Jira y mueve el estado. Incluye un
**dashboard web** para operarlo. Corre como servicio de macOS (launchd).

> El detalle completo del flujo, reglas y troubleshooting está en **`CLAUDE.md`**.
> Esta guía es solo el paso a paso para tomar la posta.

---

## 1. El código (GitHub)
- Pedí acceso al repo **`github.com/sebastianpacheco-ctv/csv_automation`**
  (Settings → Collaborators).
- Cloná:
  ```bash
  git clone https://github.com/sebastianpacheco-ctv/csv_automation.git
  cd csv_automation
  ```

## 2. Los secretos (`.env`) — por canal seguro 🔑
El `.env` **NO está en git** (tiene los tokens). Pedíselo al responsable saliente
por **1Password / Bitwarden / gestor del equipo o encriptado** — nunca por chat
plano ni git. Contiene: Jira API token, Slack bot token, **Studio JWT**, login del
bot, config de bitrate y rutas (`TMP_DIR`/`LOGS_DIR`).
- Poné el `.env` recibido en la raíz del repo.

## 3. Setup en tu Mac
```bash
brew install ffmpeg
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```
Ajustes obligatorios (rutas con el usuario viejo hardcodeadas):
- **`.env`**: cambiá `TMP_DIR` y `LOGS_DIR` a rutas tuyas
  (ej. `/Users/TU_USUARIO/Documents/CSV_automations/...`).
- **`deploy/launchd/com.seedtag.csv-automation.plist`** y
  **`com.seedtag.csv-dashboard.plist`**: reemplazá todas las rutas
  `/Users/sebastianpacheco/csv-automation/...` por la ruta donde clonaste.
- **JWT de Studio fresco**: el JWT del `.env` puede estar viejo. Renová desde el
  dashboard (botón "Renovar JWT") una vez arriba, o manualmente: DevTools →
  Application → Cookies → `seedtag_jwt` en studio.seedtag.com (logueado como el
  bot `design_automations@seedtag.com`) → pegalo en `.env` (`STUDIO_JWT_COOKIE`).

Validá y arrancá:
```bash
python3 scripts/preflight.py              # 7 checks — todo verde antes de seguir
./scripts/launchd.sh install              # arranca el BOT (auto-start al login)
./scripts/launchd.sh dashboard install    # arranca el DASHBOARD → http://127.0.0.1:8787
```

## 4. Operar el día a día
- **Dashboard:** `http://127.0.0.1:8787` — estado en vivo, cola, historial,
  reprocesar/cancelar/reintentar, pausar, reiniciar, renovar JWT, health checks,
  disco/limpieza, editar config, aprobar/rechazar tickets.
- **Comandos:**
  ```bash
  ./scripts/launchd.sh status            # estado del bot
  ./scripts/launchd.sh dashboard status  # estado del dashboard
  ./scripts/launchd.sh logs              # tail del log del bot
  ./scripts/launchd.sh restart           # reiniciar el bot tras cambios
  ```
- **Slack `#csv-tickets`:** el bot avisa cada ticket y espera `ok`/`no` en el hilo.
  Comandos: `ok`, `no`, `reactivar SDS-XXXXX`, `status`.

## ⚠️ 2 reglas críticas del traspaso
1. **UN SOLO bot a la vez.** Dos bots sobre la cola 1597 = doble procesamiento,
   creatives duplicados y race en Slack. En el cutover: el saliente para el suyo
   (`./scripts/launchd.sh uninstall` + `./scripts/launchd.sh dashboard uninstall`)
   y **recién ahí** vos arrancás el tuyo.
2. **Copiá el estado para un cutover limpio.** Pedí los sidecars
   `tmp/.seen_tickets.json` y `tmp/.canceled_tickets.json` y ponelos en tu
   `TMP_DIR`. Si arrancás con `seen_tickets` vacío, el bot trata como "nuevos"
   todos los tickets abiertos de la cola y los re-notifica.

## ⛔ Regla absoluta (no negociable)
El bot **NUNCA** borra nada de Jira ni de Studio. Si algo conviene eliminar, se
avisa y lo hace un humano. (Más reglas en `CLAUDE.md`.)

## Cuando algo falla — dónde mirar
1. **Dashboard** → health checks + historial de tickets (resultado + motivo del error).
2. **Log** → `./scripts/launchd.sh logs` (o `logs/automation.log`).
3. **`CLAUDE.md`** → flujo completo, gotchas conocidos y workflow de Jira/Studio.
