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

## Flujo completo
1. **Polling Jira** cada 60s — cola **1597** (URL en HARD RULE arriba)
2. **Detecta tickets CSV/COV** — primero el `requestType.id == "1916"` del formulario nuevo, fallback por keywords en título/descripción/customfields
3. **Notifica en Slack** `#csv-tickets` — bot "CSV CTV"
4. **Espera confirmación** — respuesta "ok" en el hilo del mensaje
5. **Descarga el vídeo** — adjunto directo o link (WeTransfer; Drive no funciona por Smart Links)
6. **Convierte con FFmpeg** — H.264, 1920x1080, 29.97fps, AAC 256kbps, -24 LUFS
   - ≤ 30s → 30 Mbps
   - > 30s → 15 Mbps (límite 200MB de Studio Seedtag)
7. **Sube a Filestage** — folder CTV, proyecto = título del ticket
8. **Sube a Studio Seedtag** vía API GraphQL bajo el bot `design_automations@seedtag.com`:
   - Upload con pipeline `ctv-base` (id `68d10800680fb2e148f30961`)
   - Espera procesado: **60s → check → 30s → check → Slack alert** si no llega
   - Crea creative CSV-CTV con `createCovCreative`
9. **Añade comentario en Jira** — link de Filestage + preview de Studio + specs técnicas

## Estructura
```
csv-automation/
├── src/
│   ├── main.py            # Orquestador + detección de tickets
│   ├── jira_client.py     # API Jira (❌ pendiente migración a POST /search/jql, cola 1597)
│   ├── slack_client.py    # Bot Slack — notificaciones + espera "ok"
│   ├── converter.py       # FFmpeg — bitrate adaptativo por duración
│   ├── studio_api.py      # ✅ NUEVO — cliente GraphQL para Studio Seedtag
│   ├── uploader.py        # FilestageUploader (S3 multipart) — el Studio Playwright fue eliminado
│   └── test_real_ticket.py # Script de integración end-to-end contra un ticket real
├── requirements.txt
├── .env                   # Credenciales (NO subir a git)
├── .env.example
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

## Filestage — datos clave
- **Team ID:** `e16f96c4de9a0c1b11bbebab1ac09104`
- **User ID:** `b1cd742149aa51b33b01fec0e3b93663`
- **Folder CTV ID:** `236e302dea2ac363db574559ac1ab4fb`
- **API base:** `https://api.filestage.io` (sin /v1)
- **Auth:** Cookie de sesión `registeredSessionId` (obtenida de DevTools en app.filestage.io)
- **Upload:** S3 multipart — flujo: `s3-create` → `s3-multipart-create-signedurl` (una llamada por parte) → `s3-complete`
- **⚠️ La cookie expira** con la sesión del navegador — hay que renovarla periódicamente

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
- **Persistencia:** `requests.Session()` mantiene el cookie jar en memoria. Para sobrevivir reinicios, persistir en sidecar `.studio_jwt` (gitignored). Pendiente implementar también heartbeat 24h + fallback Slack al recibir 401.

### Constantes clave
- **Pipeline CTV:** `videoPipelineId = "68d10800680fb2e148f30961"` (selectorName: `ctv-base`). Si no se pasa, Studio usa `"legacy"` por defecto y genera formatos open-web baja calidad (max 960x540), NO CTV.
- **Estados del vídeo:** `PROGRESSING` → `COMPLETED`. Estados de error: `ERROR`, `FAILED`. (NO son `ready`/`processing` como en otros sistemas.)
- **Lentitud del procesado CTV:** observado **>15-20 minutos** para un vídeo de 19s en el pipeline `ctv-base`. El pipeline `legacy` en cambio termina en ~30s. Esto es normal y no es bug del cliente.

### Patrón de espera tras upload (lo que ejecuta `wait_video_ready`)
1. Subir el vídeo (`uploadVideo`)
2. **Esperar 60s**
3. Llamar `getVideoById` — si `state == COMPLETED` → seguir
4. Si no, **esperar 30s** y llamar otra vez
5. Si `COMPLETED` → seguir; si no → lanzar `StudioVideoNotReadyError`
6. El orquestador captura esa excepción y postea en `#csv-tickets`. El vídeo queda subido en Studio; un humano completa el creative manualmente.

### Validaciones del servidor
- `uploadVideo.filename` solo acepta `[A-Z0-9_]+`, sin guiones ni extensión. La sanitización está en `StudioAPIClient._sanitize_video_filename()`. Ej: `'SDS-21644 Foo.mp4'` → `'_SDS_21644_FOO'`.
- El campo `name` del creative SÍ permite minúsculas y espacios (validación distinta del filename).
- `AdTemplateInputType` requeridos: `name: String!`, `size: JSON!` (string `"WxH"`), `productFamily: String!` (`"ctv"` minúsculas), `shortCode: String!` (`"CSV-CTV"`), `manifest: JSON!`, `creativeTree: JSON!`. Toda la forma se construye con `build_csv_ctv_ad_template()`.

### Endpoints útiles y rotos
- **Funciona:** `getVideoById`, `uploadVideo`, `createCovCreative`, `updateCreative`, `getCreativeById`, `getUser`, `getCreativeDimensions`
- **❌ Roto para el bot:** `getVideosByQuery` devuelve `"Something broke!"` (INTERNAL_SERVER_ERROR) independientemente de los parámetros. **No podemos buscar vídeos por nombre**. Como workaround, persistimos el `video_id` en un sidecar `tmp/<TICKET>/.studio_video_id` para idempotencia (si el script muere a mitad, al reintentar salta el upload).

### URLs finales
- **VAST URL** (para Trade Desk / DSP): `https://creatives.seedtag.com/vasts/{video_id}.xml`
- **Preview URL** (lo que se pega en Jira): `https://preview.seedtag.com/creative/{creative_id}`

### Schema completo
24 operaciones extraídas del bundle JS de Studio. El cliente actual solo usa las 7 necesarias para el flujo CSV-CTV. El resto del schema (incluyendo `getVideosByQuery`, `getVideoTemplates`, `createCreative`, `publishCreative`, `uploadResource`, etc.) no está en el cliente pero está documentado en commits anteriores si se necesita.

⛔ **NUNCA incluidos en el cliente:** `removeVideo` ni `removeCreative` — por la regla absoluta de no-borrado.

## Pendientes (en orden de prioridad)

### 1. ✅ Migrar `jira_client.py` (cerrado 2026-05-22)
- `GET /rest/api/3/search` reemplazado por `POST /rest/api/3/search/jql` (enriquecimiento batched: 1 sola llamada para N tickets, ya no N+1).
- Apuntando a cola **1597** vía Service Desk + JQL `issuekey in (...)`.
- Añadidos `get_transitions()` y `transition()` que faltaban en `JiraClient` (los llamaba `main.py` y no existían — `AttributeError` garantizado al primer ticket confirmado).
- `FIELDS` alineado con lo que `main.py` realmente lee: añadidos `15867` (Formato adicional qty) y `15831` (Industry); quitados `14196`/`14197` (no se leían). Antes el qty venía siempre 0 y la category de Studio quedaba vacía silenciosamente.

### 2. Verificar end-to-end con el nuevo wiring de Studio — ÚNICO BLOQUEANTE
- `main.py` ya está enchufado a `StudioAPIClient` y maneja `StudioVideoNotReadyError` + `StudioJWTExpiredError`.
- Falta probar el flujo completo con un ticket real (SDS-21631 es candidato — ya tiene el video raw y convertido en `tmp/SDS-21631/`).
- Confirmar que el pipeline CTV produce formatos 1080p (lo intentamos 22 mayo pero el vídeo nunca llegó a COMPLETED).
- **Prerequisito:** extraer un `STUDIO_JWT_COOKIE` fresco (DevTools → Application → Cookies → `seedtag_jwt` en studio.seedtag.com con el bot `design_automations@seedtag.com` logueado).

### 3. ✅ Persistencia del JWT del bot (cerrado 2026-05-22)
- Layer 1 — cookie jar en memoria: `requests.Session()` en `StudioAPIClient`.
- Layer 2 — sidecar `.studio_jwt` (gitignored): lee al arrancar, escribe tras cada call si cambió, chmod 600. Cableado en `main.py` y `test_real_ticket.py` vía `sidecar_path=Path(TMP_DIR)/".studio_jwt"`.
- Layer 3 — heartbeat 24h: `studio.heartbeat()` en el loop de `main.py`, dispara en arranque (fail-fast de auth) y cada 24h.
- Layer 4 — `StudioJWTExpiredError` (HTTP 401/403) capturado en `process_ticket` y en el heartbeat, postea a `#csv-tickets` con instrucciones de refresh.

### 4. Renovación de la cookie de Filestage
- Actualmente el `_refresh_cookie()` usa Playwright con el login del bot. Funcional pero frágil.
- Investigar si Filestage tiene OAuth o API key con scope suficiente.

### 5. Despliegue a GCP
- Compute Engine + systemd para 24/7
- No tiene sentido hasta que #2 esté verde.

### Mejoras futuras (no bloqueantes)
- Comando `status` en Slack — requiere threading (loop Jira + listener Slack)
- Soporte para Standard Display (JPG/PNG vía Pillow)
- Manejo de Smart Links de Drive (Jira los convierte y no son accesibles vía API)
- Limpieza automática de `tmp/` tras procesado exitoso
- Service Desk queue endpoint hardcodea `limit=50` y no pagina. OK con volumen actual; flag si la cola crece.

## Pre-flight para correr `test_real_ticket.py` (Lunes 2026-05-25)
1. **Extraer JWT fresco del bot:**
   - Loguearse en `https://studio.seedtag.com` como `design_automations@seedtag.com`.
   - DevTools → Application → Cookies → `https://studio.seedtag.com` → copiar el valor de `seedtag_jwt`.
2. **Setear en shell** (o actualizar `.env`):
   ```bash
   export STUDIO_JWT_COOKIE='eyJ...'
   ```
3. **Verificar que `tmp/SDS-21631/` no tiene `.studio_video_id`** (si lo tiene, el upload se salta y se reintenta sólo la espera + creative). Los `.mp4` raw y convertido ya están desde la prueba del 22 mayo, así que el script saltará la descarga y la conversión.
4. **Arrancar el test en foreground** (NO background — perdemos stdout):
   ```bash
   cd /Users/sebastianpacheco/csv-automation
   source venv/bin/activate
   python3 src/test_real_ticket.py
   ```
5. **Esperado:** el script intenta subir, espera 60s + 30s. Es esperable que lance `StudioVideoNotReadyError` (el pipeline CTV tarda 15-20+ min). El `video_id` queda persistido en `tmp/SDS-21631/.studio_video_id`. Re-ejecutar más tarde salta el upload y sólo hace la espera + creative.
6. **NUNCA borrar nada de Studio ni de Jira** durante o después del test, ni siquiera artefactos que el bot haya creado.

## Cómo arrancar
```bash
cd csv-automation
source venv/bin/activate
python3 src/main.py
```

## Cómo detener
```bash
pkill -f "main.py"
```

## Logs
```bash
tail -f ./logs/automation.log
```

## Test aislado del flujo de Studio
Para probar el flujo nuevo de Studio sin tocar Jira ni Filestage:
```bash
export STUDIO_JWT_COOKIE='eyJ...'
python3 src/test_real_ticket.py
```
Descarga el adjunto de SDS-21631, lo convierte, lo sube a Studio bajo el bot, y crea el creative. Idempotente vía `tmp/SDS-21631/.studio_video_id` (re-ejecuciones saltan el upload si ya existe).
