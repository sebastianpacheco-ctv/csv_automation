# CSV Automation — Seedtag CTV Team

## ⛔ REGLA ABSOLUTA — NO NEGOCIABLE
**Claude NUNCA borra nada de Jira ni de Studio Seedtag.** Ni vídeos, ni creatives, ni tickets, ni comentarios, ni nada. Si detecta algo que conviene eliminar (artefactos de prueba, duplicados, errores), únicamente lo **advierte al usuario** con los IDs/URLs y deja que el usuario lo haga manualmente. Esta regla aplica incluso a artefactos que Claude mismo haya creado durante pruebas.

## ⛔ HARD RULE — Cola de Jira
**La ÚNICA cola que mira el bot es la 1597.**
URL: https://seedtag.atlassian.net/jira/servicedesk/projects/SDS/queues/custom/1597/board/2463
No mirar la cola 162 ni ninguna otra cola.

## Qué hace este proyecto
Bot de automatización para el equipo CTV de Seedtag Design Studio (SDS).
Detecta tickets de Jira con formato Standard Video (CSV/COV), convierte el vídeo al formato Seedtag y lo sube a Filestage y Studio Seedtag.

## Flujo completo (actualizado 2026-05-27 tras burn-in)
1. **Polling Jira** cada 60s — cola **1597** (URL en HARD RULE arriba). Procesa los tickets SECUENCIALMENTE (uno termina antes de empezar el siguiente).
2. **Detecta tickets CSV/COV** — primero el `requestType.id == "1916"` del formulario nuevo, fallback por keywords. La cola 1597 es CSV-only por diseño.
3. **QR check** — si el ticket tiene el campo "Advertiser's website for QR" relleno (vive en Atlassian Forms, NO es un customfield estándar — ver sección QR): el bot transiciona a To Build y **NO procesa el video** (avisa en Slack para QR manual).
4. **Descarga TODOS los adjuntos de video** del cliente (multi-mp4 → N creatives). Ignora los adjuntos que el propio bot subió (`_STANDARD_VIDEO_CONVERTED`, `_CTV_CSV[_Vn]`).
5. **Notifica en Slack** `#csv-tickets` con el **plan**: por cada video, nombre canónico + duración + bitrate + tamaño estimado.
6. **Espera confirmación** — `ok` / `no` en el hilo. `no` → cancela (reactivable con `reactivar SDS-XXX`).
7. **Transición Triage → To Build** vía `Send to Operations`.
8. **Por cada video** (loop):
   - **Convierte con FFmpeg** — H.264, 1920×1080, 29.97fps, AAC 256kbps, -24 LUFS. ≤30s → 30 Mbps; >30s → 15 Mbps.
   - **Renombra** al nombre canónico (`<summary_sanitizado>[_CTV_CSV][_Vn]`).
   - **Sube a Studio** bajo el bot `design_automations@seedtag.com`, pipeline **`ctv-base`** (⚠️ el SELECTOR_NAME string, NO el id hex — ver sección Studio).
   - **Espera procesado**: 10s → check → 10s × 15 (Studio completa en ~10-30s). Si tarda más → `pending_studio.json` 2da pasada.
   - **Crea creative** CSV-CTV con `createCovCreative`, y luego **setea country/category/configuration a nivel creative** con `set_creative_dimensions` (ver gotcha abajo).
   - **Adjunta el .mp4** al ticket Jira (pre-check 150MB: si supera, pregunta en Slack si recomprime).
9. **Comentario en Jira** (ADF clickable) con los preview links de todos los creatives + nota de borrar originales. **Los mismos links de preview de Studio se AGREGAN también al campo `description`** del ticket (al final, preservando el contenido existente; idempotente por href — ver `JiraClient.append_to_description` + `_build_studio_links_description`).
10. **Setea customfield_15826** (Seedtag Specs, id 27743) vía PUT, luego **transición To Build → Building** vía `Start Building`.
11. **Cleanup** de los .mp4 en `tmp/<TICKET>/` (conserva sidecars).

**Filestage: ELIMINADO del flujo** (26-may-2026). El equipo no lo usa para review; será reemplazado por GCS al migrar.

Todos los mensajes de progreso de un ticket van al **hilo** de su notificación inicial (canal principal limpio).

## Estructura
```
csv-automation/
├── src/
│   ├── main.py            # Orquestador, detección, loop multi-video, comandos Slack
│   ├── jira_client.py     # API Jira (POST /search/jql cola 1597, forms.cloud para QR, transiciones)
│   ├── slack_client.py    # Bot Slack — notificación con plan, wait_for_ticket_response, reactivar
│   ├── converter.py       # FFmpeg — bitrate adaptativo + override para recompresión
│   ├── studio_api.py      # Cliente GraphQL Studio (pipeline selector_name, retry name-exists)
│   ├── uploader.py        # FilestageUploader — DESACTIVADO del flujo (se conserva por si acaso)
│   └── test_real_ticket.py # Script de integración end-to-end
├── deploy/
│   ├── launchd/com.seedtag.csv-automation.plist  # servicio macOS para burn-in
│   └── encoding-preset/   # Mezzanine_TradeDesk_29.97.epr (fuente de verdad del encoding) + README
├── scripts/
│   ├── preflight.py       # valida ffmpeg/deps/.env/Slack/Filestage/Studio/Jira
│   └── launchd.sh         # install/uninstall/restart/status/logs del servicio
├── requirements.txt
├── .env                   # Credenciales (NO subir a git)
└── CLAUDE.md              # Este archivo
```

## Credenciales necesarias (.env)
```
JIRA_BASE_URL=https://seedtag.atlassian.net
JIRA_EMAIL=sebastianpacheco@seedtag.com
JIRA_API_TOKEN=...           # id.atlassian.net → Security → API tokens
JIRA_PROJECT_KEY=SDS

SLACK_BOT_TOKEN=xoxb-...     # App "CSV CTV" en api.slack.com/apps
SLACK_CHANNEL=csv-tickets
SLACK_CHANNEL_ID=C0B2ATE790B  # ID fijo — no requiere channels:read

FILESTAGE_SESSION_COOKIE=... # registeredSessionId de app.filestage.io (expira)
FILESTAGE_API_KEY=FSTG-...   # Backup

# Studio Seedtag — JWT del bot design_automations@seedtag.com (rolling 30 días por
# llamada, ver sección "Studio Seedtag — API GraphQL"). Se extrae manualmente
# desde DevTools → Application → Cookies → seedtag_jwt.
STUDIO_JWT_COOKIE=eyJ...

# Login del bot Studio — usado SOLO como fallback de Filestage para renovar su
# cookie (Playwright). Para el upload a Studio NO se usa, se usa el JWT.
STUDIO_EMAIL=design_automations@seedtag.com
STUDIO_PASSWORD=...

BITRATE_SHORT=30
BITRATE_LONG=15
DURATION_THRESHOLD=30

TMP_DIR=./tmp
LOGS_DIR=./logs
```

## Jira — datos clave
- **CloudId:** `f27c696c-ab8c-4c73-896e-079ad4bb1763`
- **Proyecto:** SDS (Seedtag Design Studio)
- **Cola del bot:** **1597** (ver HARD RULE arriba), `servicedeskId` 10
- **Endpoint Service Desk para listar la cola:** `GET /rest/servicedeskapi/servicedesk/10/queue/1597/issue` (devuelve un campo `size` con el total + `values[]` con los issues; cada issue trae key, summary, status, reporter, created)
- **Endpoint búsqueda con JQL:** `POST /rest/api/3/search/jql` (el `GET /search` antiguo devuelve 410 Gone desde 8 mayo)
- **JQL correcta para tickets CTV** (cuando jira_client se migre):
  ```jql
  "Request Type" in (
    "Omniscreen Video (CTV and In-Stream)",
    "[Deprecated] CTV - Standard",
    "[Deprecated] CTV - Aura: Creative Intelligence"
  )
  ```
  Nota: es `"Request Type"`, no `"Customer Request Type"`.
- **Paginación:** usar `nextPageToken`; `isLast: true` señala última página.
- **Detección por formulario nuevo:** `customfield_10800.requestType.id == "1916"` (formulario dedicado CSV/COV)
- **Campos relevantes:**
  - `customfield_14324` → Operator Entity (US, CA, MX, BR, ROLA, ES, FR, DE, IT, UK, BNL, AND, MENA, EMEA, EU)
  - `customfield_11531` → Ticket Type (CAMP/PROP)
  - `customfield_15827` → CSV quantity total
  - `customfield_15865` → Standard Video (CTV) qty
  - `customfield_15866` → Standard Display (Open Web) qty
  - `customfield_15867` → Formato adicional qty
  - `customfield_15831` → Industry → mapea a category de Studio
  - `customfield_15826` → Seedtag Specs (requerido al transicionar a "Start Building", valor id `"27743"`)
  - `customfield_11300` → Deadline
  - `customfield_10800` → Request type del formulario
- **Close date proxy:** `statuscategorychangedate` (`resolutiondate` siempre es null)

## Keywords de detección CSV/COV
```python
CSV_KEYWORDS = ["standard video", "csv", "cov", "csv-ctv", "cov-ctv"]
```
El bot busca estas keywords en título, descripción, todos los customfields de texto y comentarios.

## Detección de QR (Atlassian Forms)
- El campo **"Advertiser's website for QR"** NO es un customfield estándar de Jira. Vive dentro de **Atlassian Forms** y solo se accede vía la API externa `https://api.atlassian.com/jira/forms/cloud/{cloudId}/issue/{key}/form` + `.../form/{form_id}`.
- `JiraClient.get_form_answers(ticket_key)` lista las forms del ticket, cruza `design.questions[id].label` con `state.answers[id].text` y devuelve `{label: text}`.
- Si algún label contiene "advertiser" + "qr" con valor no vacío → el bot transiciona a To Build y **NO procesa el video** (avisa en Slack para QR manual).

## Workflow Jira (transiciones)
```
Triage  --[Send to Operations id=16]-->  To Build  --[Start Building id=5]-->  Building
```
- Al confirmar `ok`: `Send to Operations` (Triage → To Build).
- Al terminar: `Start Building` (To Build → Building). Atajo desde Triage directo: `Send to Building`. El bot prueba ambos.
- ⚠️ **`Start Building`/`Send to Building` exigen `customfield_15826` (Seedtag Specs) rellenado**, pero el field NO está en el screen de la transición. Hay que setearlo con **PUT `/issue/{key}`** ANTES de la transición (no en el body de `/transitions`, que devuelve "cannot be set, not on appropriate screen"). `JiraClient.set_fields()` hace el PUT.
- ⛔ El bot **NUNCA** usa `Send back to Brand` ni vuelve a Triage. Si algo falla, deja el ticket donde esté + avisa.

## Comandos Slack en `#csv-tickets`
- `ok` / `si` / `dale` → procesar el ticket activo (con el plan mostrado).
- `no` / `cancel` → cancelar; queda en `tmp/.canceled_tickets.json`.
- `reactivar SDS-XXXXX` → re-habilita un ticket cancelado (lo saca de canceled + seen_tickets).
- `status` → resumen de la cola.
- Recompresión >150MB: el bot pregunta en el hilo `si`/`no`.

## Filestage — DESACTIVADO del flujo (26-may-2026)
El equipo CTV no usa Filestage para review (comentarios del cliente van a Jira) y `s3-complete` fallaba con 400. La clase `FilestageUploader` sigue en `src/uploader.py` por si se reactiva, pero `main.py` no la llama. Será reemplazado por GCS en la migración. Datos por si se reactiva:
- **Team ID:** `e16f96c4de9a0c1b11bbebab1ac09104` · **User ID:** `b1cd742149aa51b33b01fec0e3b93663` · **API:** `https://api.filestage.io`
- Auth cookie `registeredSessionId` (expira con la sesión). `s3-complete` necesita `uploadId` + `parts` con ETags (bug nunca arreglado porque se sacó del flujo).

## Slack — datos clave
- **App:** "CSV CTV" (App ID: A0B2ARUN8FM reemplazada por nueva)
- **Canal:** `#csv-tickets` (ID: `C0B2ATE790B`)
- **Scopes aprobados:** `channels:history`, `chat:write`, `reactions:read`
- **Confirmación:** responder "ok" (o "si", "yes", "dale") en el hilo del mensaje
- **Restricción de seguridad:** el bot solo lee mensajes del canal `C0B2ATE790B`

## Equipo CTV
| Nombre | Account ID Jira |
|---|---|
| Sebastián Pacheco | `712020:1e830ca9-09b5-47f6-b10c-0c153b657896` |
| Leonardo Maya | `712020:cf45456a-1d79-4857-9db7-dcf0faa58212` |
| Víctor Fariñas | `712020:b7c45140-f8c5-4153-bee2-1509bcc18760` |
| Beatriz Luis Enríquez | `712020:f54bd75c-4d5b-4fe2-a6af-e9035bd70532` |

## Specs de conversión (preset Mezzanine_TradeDesk)
- Codec: H.264 (avc1) / Contenedor: MP4
- Resolución: 1920×1080 / FPS: 29.97 (30000/1001)
- Bitrate: 30 Mbps (≤30s) / 15 Mbps (>30s) — límite 200MB en Studio
- Audio: AAC 256kbps, 48kHz, Stereo
- Loudness: -24 LUFS, True Peak -2 dBTP

## Studio Seedtag — API GraphQL (cliente actual)

El antiguo `StudioUploader` con Playwright fue eliminado (Studio es 100% divs/SVG, no había botones HTML estables). La integración nueva vive en `src/studio_api.py` y usa la API GraphQL real de Studio.

### Identidad y permisos
- **Bot:** `design_automations@seedtag.com` — `_id: 6a0f0dfe30342d001a0e969b`
- **Permisos (9):** Resources_view, User_edit, Creative_edit, Creative_skip_verification_publish, Creative_preset_edit, Creative_readonly_edit, **Creative_cov_edit** (necesario para CTV), Adtag_edit, PublisherPanel

### Auth — JWT rolling
- **Endpoint:** `POST https://studio.seedtag.com/g` (GraphQL endpoint estilo Apollo)
- **Cookie:** `seedtag_jwt` con domain `.seedtag.com`
- **Comportamiento:** cada llamada a Studio devuelve `set-cookie: seedtag_jwt=<nuevo JWT>` con `iat`/`exp` actualizados (+30 días). Mientras el bot esté haciendo llamadas, **la cookie nunca expira**.
- **Persistencia (4 capas, ✅ implementadas):** (1) cookie jar en `requests.Session()`; (2) sidecar `.studio_jwt` (gitignored, chmod 600) leído al arrancar y escrito tras cada call; (3) heartbeat 24h (`studio.heartbeat()` en el loop, dispara en arranque y cada 24h); (4) `StudioJWTExpiredError` en 401/403 capturado → aviso a `#csv-tickets`.

### Constantes clave
- **Pipeline CTV:** ⚠️ usar el **selector_name** `"ctv-base"`, **NO el id hex** `"68d10800680fb2e148f30961"`. BUG CRÍTICO descubierto el 26-may-2026: aunque `getVideoTemplates` devuelve ambos identifiers para el mismo template, los videos subidos con el ID hex quedan **PROGRESSING para siempre**. Con el selector_name string el upload completa en ~10-30s. La constante `VIDEO_PIPELINE_CTV_BASE = "ctv-base"` en `studio_api.py`. Si se omite, Studio usa `"legacy"` (open-web ≤960×540, NO CTV).
- **Estados del vídeo:** `PROGRESSING` → `COMPLETED`. Estados de error: `ERROR`, `FAILED`.
- **Timing real del procesado CTV:** **~10-30 segundos** (con selector_name correcto). La nota vieja de ">15-20 minutos" era el síntoma del bug del ID hex, no el comportamiento real.
- **`getVideoTemplates`** lista los 8 pipelines: `ctv-base` (CTV 720p/1080p), `omniscreen` (8 formatos incl. 1080p), `open-web-*` (varios), `legacy` (default rápido baja calidad). El bot usa `ctv-base`.
- **Nombre duplicado:** Studio rechaza con `"The name already exists"`. `upload_video` reintenta UNA vez con sufijo `_RYYYYMMDDHHMM` para crear nombre único (no se puede borrar el viejo — regla absoluta).

### Patrón de espera tras upload (lo que ejecuta `wait_video_ready`)
1. Subir el vídeo (`uploadVideo`)
2. **Esperar 10s** (initial_wait)
3. Llamar `getVideoById` — si `state == COMPLETED` → seguir
4. Si no, **esperar 10s** y reintentar, hasta **15 retries** (~2.7 min total)
5. Si `COMPLETED` → crear creative; si no → lanzar `StudioVideoNotReadyError`
6. El orquestador captura esa excepción, postea en el hilo del ticket y guarda el `video_id` en `tmp/.pending_studio.json`. **Segunda pasada**: el loop principal revisa pending_studio cada 60s; cuando el video llega a COMPLETED crea el creative y postea un segundo comentario en Jira con el link. Cap de 2h.

### Gotcha — country/category/config no aparecen en la LISTA (28-may-2026)
Studio guarda country/category/configuration en **dos lugares**:
1. Dentro del `creativeTree.props` del creative — lo que `createCovCreative`
   rellena (vía `build_csv_ctv_ad_template`). Esto es lo que se ve al **EDITAR**
   el creative (panel "Dimensions" de Studio pro).
2. Campos **top-level del modelo del creative** (`creative.country`,
   `.category`, `.configuration`) — lo que muestran las **columnas de la LISTA**
   en Studio Manager.
`createCovCreative` solo rellena (1), NO (2). Por eso los creatives del bot se
veían con esas columnas vacías en la lista aunque al editar estaban bien.
**Fix:** tras crear, llamar `set_creative_dimensions(creative_id, country,
category, configuration="animation")` (hace getCreativeById + updateCreative con
los campos top-level). El bot lo hace dentro de `process_video_to_creative`, en
try/except no fatal (el creative ya existe; si falla solo faltan los tags de la
columna). Validado: updateCreative acepta `configuration` además de country/category.

### Validaciones del servidor
- `uploadVideo.filename` solo acepta `[A-Z0-9_]+`, sin guiones ni extensión. La sanitización está en `StudioAPIClient._sanitize_video_filename()`. Ej: `'SDS-21644 Foo.mp4'` → `'_SDS_21644_FOO'`.
- El campo `name` del creative SÍ permite minúsculas y espacios (validación distinta del filename).
- `AdTemplateInputType` requeridos: `name: String!`, `size: JSON!` (string `"WxH"`), `productFamily: String!` (`"ctv"` minúsculas), `shortCode: String!` (`"CSV-CTV"`), `manifest: JSON!`, `creativeTree: JSON!`. Toda la forma se construye con `build_csv_ctv_ad_template()`.

### Endpoints útiles y rotos
- **Funciona:** `getVideoById`, `uploadVideo`, `createCovCreative`, `updateCreative`, `getCreativeById`, `getUser`, `getCreativeDimensions`, `getVideoTemplates`.
- **`getVideosByQuery`:** estaba marcado como roto en commits viejos, pero con el JWT fresco del bot funciona. Aun así el flujo no depende de él (se reintenta por nombre con sufijo en vez de buscar).

### URLs finales
- **VAST URL** (para Trade Desk / DSP): `https://creatives.seedtag.com/vasts/{video_id}.xml`
- **Preview URL** (lo que se pega en Jira): `https://preview.seedtag.com/creative/{creative_id}`

### Schema completo
24 operaciones extraídas del bundle JS de Studio. El cliente actual solo usa las 7 necesarias para el flujo CSV-CTV. El resto del schema (incluyendo `getVideosByQuery`, `getVideoTemplates`, `createCreative`, `publishCreative`, `uploadResource`, etc.) no está en el cliente pero está documentado en commits anteriores si se necesita.

⛔ **NUNCA incluidos en el cliente:** `removeVideo` ni `removeCreative` — por la regla absoluta de no-borrado.

## Plan operacional (acordado 2026-05-26)

**Fase 1 — Burn-in local (esta semana laboral, ~5 días):**
- macOS con launchd plist (`com.seedtag.csv-automation.plist`, `RunAtLoad: true`, auto-restart al crashear).
- Portátil despierto en horario laboral; sleep nocturno y fin de semana aceptados.
- Procesa los tickets reales que entren en la cola 1597 (CSV-only por diseño).
- Sidecars de persistencia en `tmp/`: `.studio_jwt`, `.studio_video_id`, `.seen_tickets.json`.
- Cleanup automático tras procesado exitoso: borra `.mp4` raw + convertido, conserva `.studio_video_id`.
- Aviso Slack a los 30 días sobre carpetas viejas en `tmp/` (sin borrar nada, regla absoluta).
- Hardening de `_refresh_cookie()` Filestage: try/except + alerta Slack tras N fallos.
- `RotatingFileHandler` (10MB × 5 archivos) en `logs/automation.log`.

**Fase 2 — Fix de bugs encontrados durante burn-in.**

**Fase 3 — Migración a GCP:**
- VM `e2-micro` (us-central1 free tier) en proyecto `decoded-theme-461808-d3`, Ubuntu 22.04.
- systemd unit + Google Cloud Ops Agent → Logs Explorer + Dashboards.
- Secretos vía Secret Manager o `.env` chmod 600 en la VM.
- **GCS bucket reemplaza Filestage como primary** (signed URL 90 días en el comentario de Jira). Filestage queda como toggle backup configurable.
- Adiós a Playwright en producción.

## Pendientes

### 1. ✅ Migrar `jira_client.py` (cerrado 2026-05-22)
- `GET /rest/api/3/search` reemplazado por `POST /rest/api/3/search/jql` (enriquecimiento batched: 1 sola llamada para N tickets, ya no N+1).
- Apuntando a cola **1597** vía Service Desk + JQL `issuekey in (...)`.
- Añadidos `get_transitions()` y `transition()` que faltaban en `JiraClient`.
- `FIELDS` alineado con lo que `main.py` realmente lee: añadidos `15867` (Formato adicional qty) y `15831` (Industry); quitados `14196`/`14197` (no se leían).

### 2. ✅ Flujo end-to-end validado en burn-in (26-27 may 2026)
- Validado en vivo con SDS-21709 y SDS-21715: Triage → To Build → convert → Studio (COMPLETED ~20-30s) → creative → comentario clickable → Building → cleanup.
- **Bug crítico resuelto:** el `videoPipelineId` debía ser el selector_name `"ctv-base"`, no el id hex (con hex los videos quedaban PROGRESSING para siempre).
- launchd auto-restart al login + persistencia JWT/seen_tickets sobrevivieron la noche.

### 3. ✅ Persistencia del JWT del bot (cerrado 2026-05-22, 4 capas)
Ver sección "Auth — JWT rolling" arriba.

### 4. ✅ Filestage sacado del flujo (26-may). Pendiente Fase 3: reemplazar por GCS.

### 5. Despliegue a GCP (Fase 3) — PRÓXIMO GRAN PASO
- Proyecto `decoded-theme-461808-d3` en GCP.
- Compute Engine `e2-micro` (us-central1, free tier) + systemd unit (equivalente al launchd actual).
- Google Cloud Ops Agent → Logs Explorer + Monitoring Dashboards + Error Reporting + Alerting.
- GCS bucket para vídeos procesados (signed URL ~90 días) reemplazando Filestage. Service account = auth sin cookies.
- Requiere instalar ffmpeg + venv en la VM. Playwright YA NO es necesario (Filestage fuera).

### Mejoras futuras (no bloqueantes)
- #35: bajar videos desde más tipos de links (Drive/WeTransfer/Dropbox con auth/bypass). Pendiente definir qué servicios priorizar.
- Soporte para Standard Display (JPG/PNG vía Pillow).
- Procesamiento paralelo de tickets (hoy es serial; suficiente para 3-5/día).
- Service Desk queue endpoint hardcodea `limit=50` y no pagina. OK con volumen actual.

## Cómo arrancar / operar (burn-in local con launchd)
```bash
cd /Users/sebastianpacheco/csv-automation
source venv/bin/activate
python3 scripts/preflight.py          # valida todo el stack (7 checks)
./scripts/launchd.sh install          # arranca el servicio (auto-start al login)
./scripts/launchd.sh status           # PID + último exit
./scripts/launchd.sh logs             # tail -f logs/automation.log
./scripts/launchd.sh restart          # tras cambios de código
./scripts/launchd.sh uninstall        # parar y quitar del autostart
```
**Prerequisito único:** `STUDIO_JWT_COOKIE` fresco en `.env` (DevTools → Cookies → `seedtag_jwt` en studio.seedtag.com con el bot logueado). El sidecar `.studio_jwt` lo mantiene fresco después.

## Estado verificado el 2026-05-27 (burn-in en curso)
Ejecutar `python3 scripts/preflight.py` para revalidar.
- ✅ Flujo completo end-to-end funcionando (Studio COMPLETED en ~20-30s con `ctv-base`).
- ✅ QR skip, multi-mp4, threading Slack, reactivar, recompresión >150MB, retry name-exists.
- ✅ launchd sobrevivió sleep nocturno (auto-restart al login).
- ✅ Repo sincronizado en `github.com/sebastianpacheco-ctv/csv_automation`.

### Características del plist launchd
- `RunAtLoad: true` → arranca al hacer login en la Mac.
- `KeepAlive { Crashed: true, SuccessfulExit: false }` → auto-restart si crashea, no si sale limpio.
- `ThrottleInterval: 30` → no relanza más rápido que cada 30s (protege contra crash-loops).
- `ProcessType: Background` + `EnvironmentVariables.PATH` con `/opt/homebrew/bin` (para ffmpeg/ffprobe, que launchd no hereda del shell).
- **Rutas hardcodeadas:** el plist usa `/Users/sebastianpacheco/csv-automation/...`. En otro Mac, sustituir antes de copiar.
- Logs del proceso: `logs/automation.log` (rota a 10MB×5). stderr de launchd: `logs/launchd-stderr.log`.

### Arranque manual (sin launchd, para debug)
```bash
cd csv-automation && source venv/bin/activate && python3 src/main.py
```

### Test aislado del flujo de Studio
```bash
export STUDIO_JWT_COOKIE='eyJ...'
python3 src/test_real_ticket.py
```
Descarga el adjunto de SDS-21631, convierte, sube a Studio bajo el bot, crea el creative. Idempotente vía `tmp/SDS-21631/.studio_video_id`.

## Sidecars en TMP_DIR
`TMP_DIR` apunta a `/Users/sebastianpacheco/Documents/CSV_automations/tmp/` (fuera del repo). Sidecars que mantiene el bot:
- `.studio_jwt` — JWT del bot (chmod 600), se refresca tras cada call.
- `.seen_tickets.json` — claves de tickets ya procesados (evita re-spam al reiniciar).
- `.canceled_tickets.json` — tickets cancelados con `no` (reactivables).
- `.pending_studio.json` — videos esperando COMPLETED (segunda pasada).
- `.last_tmp_check` — timestamp del último aviso de carpetas viejas (>30 días).
- `<TICKET>/.studio_video_id` — idempotencia del upload por ticket.
