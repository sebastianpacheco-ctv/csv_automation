"""
Studio Seedtag GraphQL API client.

Reemplaza la automatización por Playwright. Todas las operaciones de Studio
(login, subir vídeo, crear creative CTV/COV, actualizar country/category,
obtener link de preview) se hacen contra el endpoint GraphQL único:

    POST https://studio.seedtag.com/g

Auth: cookie `seedtag_jwt` (JWT con expiry de ~30 días). El cookie se obtiene
manualmente la primera vez desde el navegador y se guarda en .env como
STUDIO_JWT_COOKIE. Cuando expira, hay que renovarlo manualmente (o vía SSO
si lo implementamos más adelante).

Schema descubierto desde el bundle JS de Studio. Las 24 operaciones GraphQL
están definidas como constantes al final del módulo.
"""
import logging
import json
import os
import stat
import requests
from pathlib import Path

log = logging.getLogger(__name__)

STUDIO_GRAPHQL_URL = "https://studio.seedtag.com/g"
STUDIO_BASE = "https://studio.seedtag.com"

# Pipeline CTV: produce variantes 1080p (avc1, 29.97fps, etc.). Si se usa el
# default "legacy" Studio genera variantes open-web <=960x540 que NO sirven
# para CTV.
#
# IMPORTANTE: usar el SELECTOR_NAME ("ctv-base"), NO el ID hex. Aunque la API
# acepta el ID hex 68d10800680fb2e148f30961, los videos subidos con ese ID
# quedan PROGRESSING forever (verificado experimentalmente 26-may-2026). Con
# selector_name el upload procesa en ~30s.
VIDEO_PIPELINE_CTV_BASE = "ctv-base"
PREVIEW_BASE = "https://preview.seedtag.com/creative"


# ─────────────────────────────────────────────────────────────────────────────
# Mapeos Jira → Studio (mantienen los del uploader.py anterior)
# ─────────────────────────────────────────────────────────────────────────────

COUNTRY_MAP = {
    "us":    "usa",
    "usa":   "usa",
    "ca":    "canada",
    "mx":    "mexico",
    "br":    "brazil",
    "rola":  "international",
    "and":   "international",
    "es":    "spain",
    "fr":    "france",
    "de":    "germany",
    "it":    "italy",
    "uk":    "uk",
    "bnl":   "netherlands",
    "mena":  "mena",
    "emea":  "international",
    "eu":    "international",
}

# Mapeo Jira Industry (customfield_15831) → Studio category.
# Auditado 2026-05-28 contra:
#   - allowedValues del campo Industry en Jira (13 opciones).
#   - getCreativeDimensions().categories en Studio (24 categorías válidas).
# Bug fixeado: "technology"/"tech" mapeaban a "industry" — incorrecto, son
# categorías distintas en Studio. Ahora "technology"→"technology".
# Las claves se comparan en lower()/strip(). Opciones de Jira "combinadas"
# (ej. "Business, Industry, And Logistics") mapean a la categoría Studio más
# englobante. Las entradas extra son aliases por compatibilidad futura.
CATEGORY_MAP = {
    # ── 13 opciones reales del field Industry en Jira (mayúsculas en Jira) ──
    "automotive":                        "automotive",
    "beauty":                            "beauty",
    "business, industry, and logistics": "industry",
    "entertainment and culture":         "entertainment",
    "fashion":                           "fashion",
    "food and drinks":                   "food-and-drinks",
    "health":                            "health",
    "home":                              "home",
    "non-profits and public services":   "charity",
    "pets":                              "pets",
    "retail":                            "retail",
    "technology":                        "technology",
    "travel and transportation":         "travel",
    # ── Aliases / posibles valores históricos o variantes ──
    "food & drinks":                     "food-and-drinks",
    "tech":                              "technology",
    "entertainment":                     "entertainment",
    "industry":                          "industry",
    "charity":                           "charity",
    "travel":                            "travel",
    # ── Categorías Studio sin opción Jira directa (compat forward) ──
    "appliances":                        "appliances",
    "betting":                           "betting",
    "education":                         "education",
    "energy":                            "energy",
    "financial":                         "financial",
    "governmental":                      "governmental",
    "insurance":                         "insurance",
    "shipping":                          "shipping",
    "spirits":                           "spirits",
    "sports":                            "sports",
    "tobacco and vaping":                "tobacco-and-vaping",
    "tobacco-and-vaping":                "tobacco-and-vaping",
}


# ─────────────────────────────────────────────────────────────────────────────
# Cliente
# ─────────────────────────────────────────────────────────────────────────────

class StudioAPIError(Exception):
    """Error devuelto por la API GraphQL de Studio."""


class StudioVideoNotReadyError(StudioAPIError):
    """
    El vídeo subido NO terminó de procesarse en el tiempo previsto.
    El orquestador debería capturar esto, dejar el ticket donde estaba,
    y notificar en Slack para que un humano lo revise (no reintentar
    el upload — el vídeo está en Studio, solo está PROGRESSING).
    """
    def __init__(self, video_id: str, last_state: str, elapsed_seconds: int):
        self.video_id = video_id
        self.last_state = last_state
        self.elapsed_seconds = elapsed_seconds
        super().__init__(
            f"Video {video_id} no estuvo COMPLETED tras {elapsed_seconds}s "
            f"(último estado: {last_state})"
        )


class StudioJWTExpiredError(StudioAPIError):
    """
    El JWT del bot fue rechazado por Studio (HTTP 401/403). El caller
    (main.py / test_real_ticket.py) debe capturar esto y postear en
    #csv-tickets pidiendo a un humano renovar el cookie:

      1. Abrir https://studio.seedtag.com en Chrome bajo la cuenta del
         bot (design_automations@seedtag.com)
      2. DevTools → Application → Cookies → studio.seedtag.com
      3. Copiar el valor de `seedtag_jwt` y actualizarlo en .env
         (STUDIO_JWT_COOKIE) o en el sidecar `.studio_jwt`
      4. Reiniciar el proceso
    """
    def __init__(self, status_code: int, body: str = ""):
        self.status_code = status_code
        self.body = body
        super().__init__(
            f"Studio rechazó el JWT (HTTP {status_code}). Renovar cookie "
            f"seedtag_jwt desde DevTools → Application → Cookies en "
            f"studio.seedtag.com bajo la cuenta design_automations@seedtag.com."
        )


class StudioAPIClient:
    """
    Cliente GraphQL para Studio Seedtag.

    Uso:
        client = StudioAPIClient(jwt_cookie="eyJ...")
        client.ping()                              # verifica auth
        video = client.upload_video(path)          # sube el .mp4
        client.wait_video_ready(video["id"])       # espera procesamiento
        creative_id = client.create_cov_creative(video["id"], name)
        client.set_creative_dimensions(creative_id, "usa", "automotive", "animation")
        link = client.get_preview_link(creative_id)
    """

    def __init__(self, jwt_cookie: str = None, timeout: int = 120,
                 sidecar_path: Path = None):
        """
        jwt_cookie: JWT extraído del navegador. Opcional si `sidecar_path`
            existe en disco con un JWT válido.
        sidecar_path: ruta a un archivo donde persistir el último JWT visto
            (layer 2 de la persistencia). Si el archivo existe, su contenido
            tiene preferencia sobre `jwt_cookie` (es más reciente). Tras cada
            call a Studio el cookie rolado se escribe de vuelta al archivo.
            chmod 600 — es una credencial.
        """
        self.sidecar_path = Path(sidecar_path) if sidecar_path else None

        sidecar_jwt = self._read_sidecar()
        if sidecar_jwt:
            jwt_cookie = sidecar_jwt
        if not jwt_cookie:
            raise ValueError(
                "jwt_cookie es obligatorio — extraer de Chrome o proveer "
                "sidecar_path con un JWT previo"
            )

        self.jwt_cookie = jwt_cookie
        self.timeout = timeout
        self.session = requests.Session()
        self.session.cookies.set("seedtag_jwt", jwt_cookie, domain=".seedtag.com")
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            "Origin": STUDIO_BASE,
            "Referer": STUDIO_BASE + "/",
            "Accept": "*/*",
        })

    # ── Persistencia del JWT (layer 2) ─────────────────────────────────────

    def _read_sidecar(self) -> str:
        if not self.sidecar_path:
            return ""
        try:
            if self.sidecar_path.exists():
                v = self.sidecar_path.read_text().strip()
                if v:
                    log.info(f"Studio API: JWT cargado desde sidecar {self.sidecar_path}")
                return v
        except OSError as e:
            log.warning(f"Studio API: no se pudo leer sidecar {self.sidecar_path}: {e}")
        return ""

    def _persist_jwt(self) -> None:
        """
        Lee el cookie actual del session jar y lo escribe al sidecar si
        difiere del contenido en disco. No-op si no hay `sidecar_path`.
        Permisos 0600 (es una credencial).
        """
        if not self.sidecar_path:
            return
        current = self.session.cookies.get("seedtag_jwt", domain=".seedtag.com")
        if not current:
            return
        try:
            existing = self.sidecar_path.read_text().strip() if self.sidecar_path.exists() else ""
            if existing == current:
                return
            self.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            self.sidecar_path.write_text(current)
            os.chmod(self.sidecar_path, stat.S_IRUSR | stat.S_IWUSR)
            log.debug(f"Studio API: JWT actualizado en sidecar {self.sidecar_path}")
        except OSError as e:
            log.warning(f"Studio API: no se pudo escribir sidecar {self.sidecar_path}: {e}")

    # ── Low-level GraphQL ──────────────────────────────────────────────────

    def _graphql(self, query: str, variables: dict = None,
                 operation_name: str = None, max_retries: int = 3) -> dict:
        """Ejecuta una query/mutation GraphQL sin upload de archivos.

        Reintenta hasta `max_retries` veces ante errores 5xx transitorios.
        """
        import time
        payload = {"query": query, "variables": variables or {}}
        if operation_name:
            payload["operationName"] = operation_name
        last_err = None
        for attempt in range(max_retries):
            r = self.session.post(
                STUDIO_GRAPHQL_URL,
                json=payload,
                timeout=self.timeout,
            )
            # Retry on 5xx, no retry on 4xx
            if 500 <= r.status_code < 600:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
                log.warning(f"Studio API: {last_err} — reintento {attempt+1}/{max_retries}")
                time.sleep(2 ** attempt)
                continue
            data = self._check_response(r)
            self._persist_jwt()
            return data
        raise StudioAPIError(f"Studio API: {max_retries} reintentos fallidos. Último: {last_err}")

    def _graphql_upload(self, query: str, variables: dict,
                        file_path: Path, file_var_path: str,
                        operation_name: str = None,
                        multipart_filename: str = None,
                        timeout: int = 600) -> dict:
        """
        Ejecuta una mutation con upload de archivo siguiendo el spec
        graphql-multipart-request-spec (apollo-upload-client).

        file_var_path: ruta al campo File en variables (e.g. "variables.file").
        multipart_filename: filename a usar en el Content-Disposition. Si no se
            pasa, usa file_path.name (que puede contener espacios/lowercase).
        """
        operations = {"query": query, "variables": variables}
        if operation_name:
            operations["operationName"] = operation_name

        # apollo-upload-client manda variables.<x> = null en operations
        # y luego mapea el campo file en el multipart con 'map'
        # path en variables se da con notación dot (e.g. "variables.file")
        map_payload = {"0": [file_var_path]}

        mp_filename = multipart_filename or file_path.name

        with open(file_path, "rb") as f:
            files = {
                "operations": (None, json.dumps(operations), "application/json"),
                "map": (None, json.dumps(map_payload), "application/json"),
                "0": (mp_filename, f, "video/mp4"),
            }
            r = self.session.post(
                STUDIO_GRAPHQL_URL,
                files=files,
                timeout=timeout,
            )
        data = self._check_response(r)
        self._persist_jwt()
        return data

    def _check_response(self, r: requests.Response) -> dict:
        if r.status_code in (401, 403):
            raise StudioJWTExpiredError(r.status_code, r.text[:500])
        if r.status_code != 200:
            raise StudioAPIError(f"HTTP {r.status_code}: {r.text[:500]}")
        try:
            data = r.json()
        except Exception as e:
            raise StudioAPIError(f"Respuesta no es JSON: {r.text[:500]}") from e
        if "errors" in data and data["errors"]:
            raise StudioAPIError(f"GraphQL errors: {data['errors']}")
        return data.get("data", {})

    # ── Operaciones de alto nivel ──────────────────────────────────────────

    def ping(self) -> dict:
        """Verifica que el JWT es válido pidiendo el user."""
        data = self._graphql(Q_USER)
        log.info(f"Studio API auth OK — user={data['user']['email']}")
        return data["user"]

    def heartbeat(self) -> dict:
        """
        Layer 3 de la persistencia del JWT: una llamada real a Studio que
        fuerza el set-cookie con un JWT rolado nuevo. Pensado para llamarse
        periódicamente (e.g. cada 24h) desde el loop de main.py cuando no
        hay tickets que procesar — así la cookie nunca expira por inactividad.

        El loop scheduling vive en main.py; aquí solo exponemos el método.
        Si el JWT ya está expirado, esto propagará StudioJWTExpiredError.
        """
        return self.ping()

    # ── CSV-CTV: factory + orquestación ────────────────────────────────────

    @staticmethod
    def build_csv_ctv_ad_template(video_id: str, name: str, formats: list,
                                  country: str = None, category: str = None,
                                  configuration: str = "none",
                                  metatags: list = None) -> dict:
        """
        Construye el AdTemplateInputType para un creative CSV-CTV.

        Estructura derivada de un creative real (referencia
        guardada en tmp/ctv_template_reference_*.json).

        Args:
            video_id: ID del vídeo (devuelto por uploadVideo)
            name: nombre del creative (típicamente el título del ticket)
            formats: lista de formatos del vídeo procesado — viene de
                     getVideoById(id).formats. Cada dict: {width, height,
                     type, bitrate, url}.
            country: valor de getCreativeDimensions().countries (e.g. 'usa')
            category: valor de getCreativeDimensions().categories (e.g. 'automotive')
            configuration: por defecto 'none' (correcto para CSV-CTV; la
                           referencia traía 'animation' pero era incorrecto)
            metatags: por defecto ['ctv-express']
        """
        if metatags is None:
            metatags = ["ctv-express"]
        vast_url = f"https://creatives.seedtag.com/vasts/{video_id}.xml"
        video_html = (
            f"<video-player-ctv src='{vast_url}'>"
            f"</video-player-ctv></video-player-ctv>"
        )
        compiled_html = (
            "\n\n      \n      <template id=\"additional\">\n        \n"
            "        <template-component name=\"additional\" "
            "component-script=\"data:application/script,base64;\">\n          \n"
            "        </template-component>\n      </template>\n    \n"
            "<scene-group>\n\n      <scene name=\"full\" initial=\"true\">\n"
            f"        {video_html}\n        \n        \n      </scene>\n"
            "    \n</scene-group>\n"
        )

        # MediaFile children — uno por cada formato del vídeo procesado
        media_files = []
        for f in formats or []:
            media_files.append({
                "type": "MediaFile",
                "props": {
                    "url": f["url"],
                    "width": f["width"],
                    "height": f["height"],
                    "bitrate": f["bitrate"],
                    "type_": f["type"],
                },
                "children": [],
            })

        return {
            "id": "new-id",
            "name": name,
            "size": "600x600",
            "productFamily": "ctv",
            "shortCode": "CSV-CTV",
            "manifest": {
                "messages": [
                    "click", "close", "error", "impression", "bounce",
                    "view_0s50", "view_1s50", "view_2s50", "view_3s50",
                    "hover", "intervention",
                    "video_loaded", "video_play",
                    "video_percentile_25", "video_percentile_50",
                    "video_percentile_75", "video_complete", "video_replay",
                ],
                "arguments": [
                    {"name": "augmentedClickArea", "type": "boolean", "defaultValue": False},
                    {"name": "fixed",              "type": "boolean", "defaultValue": False},
                    {"name": "hideMuteButton",     "type": "boolean", "defaultValue": False},
                    {"name": "closeable",          "type": "boolean", "defaultValue": True},
                    {"name": "expandable",         "type": "boolean", "defaultValue": True},
                    {"name": "clickUrl",           "type": "url",     "defaultValue": ""},
                ],
                "contexts": {},
            },
            "creativeTree": {
                "type": "Creative",
                "props": {
                    "version": "v2",
                    "editor": {
                        "name": name,
                        "assets": [],
                        "components": [],
                        "scenes": [["full", {
                            "initial": True,
                            "name": "full",
                            "html": video_html,
                            "css": "",
                            "js": "",
                        }]],
                        "isVerified": False,
                        "additional": {"html": "", "css": "", "js": ""},
                        "externalPreviewContent": {"contextDataOverride": []},
                        "libraries": [],
                        "template": {"id": "CSV-CTV", "version": 1},
                        "contexts": [],
                        "metatags": metatags,
                        "isPreset": False,
                        "isReadonly": False,
                        "version": "v7",
                        "configuration": configuration,
                        "country": country,
                        "category": category,
                        "agency": None,
                        "dataSources": [],
                    },
                },
                "children": [
                    {"type": "HtmlSnippets", "props": {"compiled": compiled_html}, "children": []},
                    {"type": "Assets", "props": {}, "children": []},
                    {"type": "Libraries", "props": {"libraries": []}, "children": []},
                    {"type": "EngagementMetrics", "props": {"metrics": {}, "elements": {}}, "children": []},
                    {"type": "Contexts", "props": {}, "children": []},
                    {"type": "VideoOutlet", "props": {"vast": vast_url}, "children": []},
                    {"type": "Vasts", "props": {}, "children": [{
                        "type": "Vast",
                        "props": {"url": vast_url},
                        "children": media_files,
                    }]},
                ],
            },
        }

    def process_video_to_creative(self, file_path: Path, ticket_title: str,
                                  video_filename: str = None,
                                  country: str = None, category: str = None,
                                  initial_wait: int = 10, retry_wait: int = 10,
                                  max_retries: int = 15) -> dict:
        """
        Flujo end-to-end:
          1. Sube el vídeo
          2. Espera procesamiento (patrón del usuario: 60s, 30s, alerta)
          3. Crea el creative CSV-CTV referenciando el vídeo
          4. Devuelve {video_id, creative_id, vast_url, preview_url}

        Si el vídeo no está COMPLETED tras initial_wait + max_retries*retry_wait,
        propaga StudioVideoNotReadyError. El llamante debe capturarla y avisar
        en Slack — el vídeo queda subido en Studio, no se reintenta.

        country y category deben venir mapeados con map_country/map_category.
        """
        # 1. Upload — filename del video en Studio. Si el caller pasa
        # `video_filename` explicito, ese gana. Si no, usamos el basename
        # del archivo en disco (que ya viene canonico tras el rename en
        # main.py, con el sufijo _CTV_CSV correcto). Antes usabamos
        # `ticket_title` aqui, pero eso ignoraba el rename canonico y
        # tiraba a Studio el summary sin _CTV_CSV.
        upload_filename = video_filename or file_path.name
        video = self.upload_video(file_path, filename=upload_filename)
        video_id = video["id"]

        # 2. Wait for processing — propaga StudioVideoNotReadyError si no llega
        video_ready = self.wait_video_ready(
            video_id,
            initial_wait=initial_wait,
            retry_wait=retry_wait,
            max_retries=max_retries,
        )
        formats = video_ready.get("formats") or []
        log.info(f"Studio API: vídeo {video_id} ready — {len(formats)} formatos")

        # 3. Build adTemplate + create creative
        ad_template = self.build_csv_ctv_ad_template(
            video_id=video_id,
            name=ticket_title,
            formats=formats,
            country=country,
            category=category,
        )
        creative_id = self.create_cov_creative(ad_template)
        log.info(f"Studio API: creative CSV-CTV creado id={creative_id}")

        # 3b. Setear country/category/configuration a nivel creative para que
        # aparezcan en la LISTA de Studio Manager (createCovCreative solo los
        # deja dentro del creativeTree → se ven al editar pero no en la lista).
        # No fatal: el creative ya existe; si esto falla, solo faltan los tags
        # de la columna.
        try:
            self.set_creative_dimensions(
                creative_id, country=country, category=category,
                configuration="none",
            )
            log.info(f"Studio API: dimensions seteadas en {creative_id} "
                     f"(country={country}, category={category}, config=none)")
        except Exception as e:
            log.warning(f"Studio API: no se pudieron setear dimensions en "
                        f"{creative_id}: {e}")

        # 4. Return all useful URLs
        return {
            "video_id": video_id,
            "creative_id": creative_id,
            "vast_url": f"https://creatives.seedtag.com/vasts/{video_id}.xml",
            "preview_url": self.get_preview_link(creative_id),
        }

    def upload_video(self, file_path: Path,
                     video_pipeline_id: str = VIDEO_PIPELINE_CTV_BASE,
                     filename: str = None) -> dict:
        """Sube un .mp4 y devuelve {id, name, status, ...}. Devuelve el dict del vídeo.

        Studio valida el `filename` con la regla: mayúsculas, dígitos, guiones
        y underscores ("Name must be upper-case only with - or _"). SIEMPRE
        sanitizamos antes de mandar — daba igual que el caller pasara un
        filename ya formateado, si tenia minusculas el servidor rechazaba.

        Si Studio responde "The name already exists" (re-proceso de un ticket
        cuyo video ya esta subido), reintentamos UNA vez con un sufijo de
        fecha-hora (_RYYYYMMDDHHMM) para crear un nombre unico. Asi el
        re-proceso no choca con el video viejo (que no podemos borrar).
        """
        if not file_path.exists():
            raise FileNotFoundError(file_path)
        size_mb = file_path.stat().st_size / 1024 / 1024
        base_name = self._sanitize_video_filename(filename or file_path.name)

        for attempt in range(2):
            send_name = base_name
            if attempt == 1:
                # Reintento: sufijo de fecha-hora para nombre unico
                from datetime import datetime as _dt
                suffix = _dt.now().strftime("_R%Y%m%d%H%M")
                send_name = self._sanitize_video_filename(base_name + suffix)
            log.info(f"Studio API: subiendo {file_path.name} como '{send_name}' "
                     f"({size_mb:.1f} MB) con pipeline='{video_pipeline_id}'")
            variables = {
                "filename": send_name,
                "file": None,  # apollo-upload-client placeholder
                "videoPipelineId": video_pipeline_id,
            }
            try:
                data = self._graphql_upload(
                    query=M_UPLOAD_VIDEO,
                    variables=variables,
                    file_path=file_path,
                    file_var_path="variables.file",
                    multipart_filename=send_name,
                )
            except StudioAPIError as e:
                if "name already exists" in str(e).lower() and attempt == 0:
                    log.warning(f"Studio API: '{send_name}' ya existe — reintento con sufijo de fecha")
                    continue
                raise
            video = data["uploadVideo"]
            log.info(f"Studio API: vídeo subido id={video['id']} (name='{send_name}')")
            return video

    @staticmethod
    def _sanitize_video_filename(name: str) -> str:
        """
        Convierte un nombre al formato que acepta Studio.
        Visto en vídeos reales: solo `[A-Z0-9_]+`, sin guiones, sin extensión.
        Ej: 'SDS-21644 Foo.mp4' → '_SDS_21644_FOO'
        """
        import re
        # Quitar extensión si existe
        if "." in name:
            stem = name.rpartition(".")[0]
        else:
            stem = name
        # Reemplazar cualquier no-alfanumérico (incluyendo guiones) por _
        stem = re.sub(r'[^A-Za-z0-9]', '_', stem).upper()
        # Colapsar underscores múltiples
        stem = re.sub(r'_+', '_', stem)
        # Si empieza por número, prefijar con _
        if stem and stem[0].isdigit():
            stem = "_" + stem
        return stem

    def get_video(self, video_id: str) -> dict:
        return self._graphql(Q_GET_VIDEO_BY_ID, {"id": video_id})["getVideoById"]

    def wait_video_ready(self, video_id: str,
                         initial_wait: int = 10,
                         retry_wait: int = 10,
                         max_retries: int = 15) -> dict:
        """
        Patrón de espera definido por el usuario:
          1. Sube el vídeo (esto se hace antes de llamar esta función)
          2. Espera `initial_wait` segundos (default 60s)
          3. Comprueba estado vía getVideoById ("refresh" la página)
          4. Si COMPLETED → continúa (devuelve el dict del vídeo)
          5. Si PROGRESSING → espera `retry_wait` segundos (default 30s) y comprueba otra vez
          6. Si tras `max_retries` reintentos sigue PROGRESSING → lanza
             StudioVideoNotReadyError (el orquestador notifica a Slack)
          7. Si ERROR/FAILED en cualquier momento → lanza StudioAPIError

        El patrón es deliberadamente acotado: si el procesado tarda más de
        ~90s no es normal y conviene que un humano lo mire. El vídeo se
        queda en Studio (no se borra) y un humano puede continuar
        manualmente o reintentar desde una ejecución posterior del bot.
        """
        import time
        log.info(f"Studio API: esperando {initial_wait}s antes del primer check de {video_id}")
        time.sleep(initial_wait)

        for attempt in range(max_retries + 1):
            v = self.get_video(video_id)
            state = (v.get("status") or {}).get("state")
            n_fmts = len(v.get("formats") or [])
            log.info(f"Studio API: check #{attempt + 1} de {video_id} — state={state}, formatos={n_fmts}")
            if state == "COMPLETED":
                return v
            if state in ("ERROR", "FAILED"):
                raise StudioAPIError(f"Video {video_id} procesamiento error: {v}")
            if attempt < max_retries:
                log.info(f"Studio API: aún no listo, esperando {retry_wait}s antes del siguiente check")
                time.sleep(retry_wait)

        # Agotados los reintentos sin COMPLETED → excepción específica
        elapsed = initial_wait + (max_retries * retry_wait)
        raise StudioVideoNotReadyError(
            video_id=video_id, last_state=state, elapsed_seconds=elapsed
        )

    def create_cov_creative(self, ad_template: dict) -> str:
        """
        Crea un creative COV (Connected Video / CTV). Devuelve el ID del creative.

        ad_template: dict con la forma de AdTemplateInputType. Lo más cómodo es
        construirlo con `build_csv_ctv_ad_template(video_id, name, formats, ...)`.
        El frontend asigna id="new-id" antes de mutar — lo replicamos.
        """
        ad_template = dict(ad_template)
        ad_template.setdefault("id", "new-id")
        data = self._graphql(M_CREATE_COV_CREATIVE, {"adTemplate": ad_template})
        return data["createCovCreative"]["id"]

    def get_creative(self, creative_id: str) -> dict:
        return self._graphql(Q_GET_CREATIVE_BY_ID, {"id": creative_id})["getCreativeById"]

    def update_creative(self, creative_id: str, creative_input: dict) -> str:
        """
        Actualiza un creative con un CreativeModelInputType.
        Campos conocidos: id, name, size, productFamily, templateShortCode,
        loader, manifest, creativeTree, metatags, isPreset, isReadonly,
        country, category.
        """
        variables = {"id": creative_id, "creative": creative_input}
        return self._graphql(M_UPDATE_CREATIVE, variables)["updateCreative"]["id"]

    def set_creative_dimensions(self, creative_id: str,
                                country: str = None,
                                category: str = None,
                                configuration: str = None) -> str:
        """Setea country / category / configuration a NIVEL creative (top-level).

        IMPORTANTE: estos son los campos que muestra la LISTA de Studio Manager
        (columnas Country / Category / Config). El bot ya los pone dentro del
        `creativeTree.props` al crear el creative — eso es lo que se ve al EDITAR
        (panel "Dimensions") — pero la lista lee los campos top-level del modelo
        del creative, que `createCovCreative` NO rellena. Hay que setearlos
        aparte con `updateCreative`, que es lo que hace este método.

        Internamente obtiene el creative actual (getCreativeById) y lo re-envía
        con los campos cambiados, porque updateCreative espera el modelo completo.
        """
        current = self.get_creative(creative_id)
        creative_input = {
            "id": current["id"],
            "name": current["name"],
            "size": current.get("size"),
            "productFamily": current.get("productFamily"),
            "templateShortCode": current.get("templateShortCode"),
            "loader": current.get("loader"),
            "manifest": current.get("manifest"),
            "creativeTree": current.get("creativeTree"),
            "metatags": current.get("metatags") or [],
            "isPreset": False,
            "isReadonly": False,
        }
        if country is not None:
            creative_input["country"] = country
        if category is not None:
            creative_input["category"] = category
        if configuration is not None:
            creative_input["configuration"] = configuration
        return self.update_creative(creative_id, creative_input)

    def get_preview_link(self, creative_id: str) -> str:
        """Devuelve el link público de preview del creative."""
        return f"{PREVIEW_BASE}/{creative_id}"

    # ── Mapeos ────────────────────────────────────────────────────────────

    @staticmethod
    def map_country(jira_operator_entity: str) -> str:
        return COUNTRY_MAP.get((jira_operator_entity or "").lower().strip(),
                               "international")

    @staticmethod
    def map_category(jira_industry: str):
        """Mapea el valor del field Industry de Jira a una categoría válida de
        Studio (CATEGORY_MAP). Devuelve None si no hay mapeo — así
        set_creative_dimensions no setea categoría inválida (mejor vacío en la
        lista de Studio que un valor incorrecto). Loguea WARNING cuando llegó
        un valor no vacío sin mapeo, para detectar gaps del map."""
        key = (jira_industry or "").lower().strip()
        if not key:
            return None
        mapped = CATEGORY_MAP.get(key)
        if mapped is None:
            log.warning(f"Industry {jira_industry!r} sin mapeo en CATEGORY_MAP — "
                        f"la categoría NO se setea en el creative")
        return mapped


# ─────────────────────────────────────────────────────────────────────────────
# Operaciones GraphQL (extraídas del bundle de studio.seedtag.com)
# ─────────────────────────────────────────────────────────────────────────────

Q_USER = """
query user {
  user {
    name
    permissions
    username
    surname
    email
    status
    country
    additionalInfo { country team }
  }
}
"""

Q_GET_CREATIVE_DIMENSIONS = """
query getCreativeDimensions {
  getCreativeDimensions {
    configurations
    countries
    categories
    agencies
  }
}
"""

Q_GET_VIDEO_BY_ID = """
query getVideoById($id: String!) {
  getVideoById(id: $id) {
    name
    id
    createdAt
    vastUrl
    status { state }
    duration
    formats { width height type bitrate url }
  }
}
"""

Q_GET_CREATIVE_BY_ID = """
query getCreativeById($id: ID!) {
  getCreativeById(id: $id) {
    id name size productFamily loader
    manifest {
      messages
      arguments { name type defaultValue }
      contexts
    }
    creativeTree
    templateShortCode
    status
  }
}
"""

M_UPLOAD_VIDEO = """
mutation uploadVideo($filename: String!, $file: Upload!, $videoPipelineId: String) {
  uploadVideo(filename: $filename, file: $file, videoPipelineId: $videoPipelineId) {
    id
  }
}
"""

M_CREATE_COV_CREATIVE = """
mutation createCovCreative($adTemplate: AdTemplateInputType!) {
  createCovCreative(adTemplate: $adTemplate) { id }
}
"""

M_UPDATE_CREATIVE = """
mutation updateCreative($id: ID!, $creative: CreativeModelInputType!) {
  updateCreative(id: $id, creative: $creative) { id }
}
"""

# Notas del schema completo (24 operaciones) en CLAUDE.md.
#
# ⛔ DELIBERADAMENTE NO INCLUIDOS:
#   - M_REMOVE_VIDEO / M_REMOVE_CREATIVE: regla del proyecto, Claude no borra.
#   - Q_GET_VIDEOS_BY_QUERY: roto para el bot (devuelve "Something broke!").
#     Para buscar un vídeo por ID usar getVideoById; para idempotencia tras
#     upload usar el sidecar `.studio_video_id` en tmp/<ticket>/.
#   - M_CREATE_CREATIVE / M_PUBLISH_CREATIVE / M_UPLOAD_RESOURCE: no se usan
#     en el flujo CSV-CTV actual. La publicación la hace un humano tras
#     revisión; los recursos se referencian vía URL del vídeo.
