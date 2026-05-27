"""
Jira Client — lectura de tickets Omniscreen Video, descarga, upload y comentarios.

Campos reales descubiertos via API (proyecto SDS):
  customfield_14324 → Operator Entity (US, CA, MX, BR, ROLA, ES, FR, DE, IT, UK, BNL, AND, MENA, EMEA, EU)
  customfield_11531 → Ticket Type (CAMP / PROP)
  customfield_15827 → CSV quantity total
  customfield_15865 → Standard Video (CTV) qty ← clave
  customfield_15866 → Standard Display (Open Web) qty
  customfield_15867 → Formato adicional qty
  customfield_15831 → Industry (→ Studio category)
  customfield_11300 → Deadline
  customfield_10800 → Request Type del formulario (id 1916 = CSV/COV)
"""

import re
import json
import requests
import logging
from pathlib import Path
from urllib.parse import urlparse

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mxf"}

# Links directos a archivos de video (.mp4, .mov, etc.)
URL_PATTERN = re.compile(
    r"https?://[^\s\]>\"')]+\.(?:mp4|mov|avi|mkv|webm|mxf)", re.IGNORECASE
)

# Links de servicios de transferencia (WeTransfer, Drive, Dropbox, etc.)
TRANSFER_DOMAINS = re.compile(
    r"https?://(?:we\.tl|wetransfer\.com|drive\.google\.com"
    r"|drive\.usercontent\.google\.com|dropbox\.com"
    r"|frame\.io|app\.frame\.io|vimeo\.com|sharepoint\.com|1drv\.ms"
    r"|mediafire\.com|transfer\.sh|hightail\.com|smash\.pm"
    r"|sendgb\.com|send\.tresorit\.com)/[^\s\]>\"']+",
    re.IGNORECASE
)

# Cualquier link http(s) (para el fallback generico de servicios desconocidos)
ANY_URL = re.compile(r"https?://[^\s\]>\"')]+", re.IGNORECASE)

# Dominios que NUNCA son el video (specs, hojas de calculo, figma, jira mismo).
# Se excluyen del fallback generico para no bajar el archivo equivocado.
NON_VIDEO_DOMAINS = re.compile(
    r"(?:design\.seedtag\.com|docs\.google\.com|sheets\.google\.com"
    r"|figma\.com|atlassian\.net|seedtag\.atlassian|confluence"
    r"|notion\.so|slack\.com|loom\.com/share/folder)",
    re.IGNORECASE
)


FIELDS = (
    "summary,status,attachment,comment,description,"
    "customfield_14324,"  # Operator Entity
    "customfield_11531,"  # Ticket Type (CAMP/PROP)
    "customfield_15827,"  # CSV quantity total
    "customfield_15865,"  # Standard Video (CTV) qty
    "customfield_15866,"  # Standard Display (Open Web) qty
    "customfield_15867,"  # Formato adicional qty
    "customfield_15831,"  # Industry (→ Studio category)
    "customfield_11300,"  # Deadline
    "customfield_13309,"  # Link or QR ("Advertiser's website for QR")
    "customfield_10800"   # Request Type (para detectar form 1916)
    # Nota: Creative URLs (links de Drive/WeTransfer) vienen en description
)


JIRA_CLOUD_ID = "f27c696c-ab8c-4c73-896e-079ad4bb1763"
FORMS_API_BASE = f"https://api.atlassian.com/jira/forms/cloud/{JIRA_CLOUD_ID}"


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, project_key: str):
        self.base_url = base_url.rstrip("/")
        self.auth = (email, api_token)
        self.headers = {"Accept": "application/json"}
        self.project_key = project_key

    def _get(self, path: str, **kwargs) -> dict:
        r = requests.get(f"{self.base_url}{path}",
                         auth=self.auth, headers=self.headers, **kwargs)
        r.raise_for_status()
        return r.json()

    def get_omniscreen_video_issues(self) -> list:
        """
        Obtiene tickets directamente de la cola 1597 (Video Operations)
        via Service Desk API — solo tickets del equipo CTV.
        """
        r = requests.get(
            f"{self.base_url}/rest/servicedeskapi/servicedesk/10/queue/1597/issue",
            auth=self.auth,
            headers={**self.headers, "X-ExperimentalApi": "opt-in"},
            params={"start": 0, "limit": 50}
        )
        r.raise_for_status()
        basic_issues = r.json().get("values", [])

        keys = [i.get("key") for i in basic_issues if i.get("key")]
        if not keys:
            log.info("Jira: 0 tickets en cola Video Operations")
            return []

        # Una sola llamada JQL para enriquecer los N tickets — evita N+1.
        keys_csv = ", ".join(keys)
        r2 = requests.post(
            f"{self.base_url}/rest/api/3/search/jql",
            auth=self.auth,
            headers={**self.headers, "Content-Type": "application/json"},
            json={
                "jql": f"issuekey in ({keys_csv})",
                "maxResults": len(keys),
                "fields": FIELDS.split(","),
            }
        )

        if r2.status_code == 200:
            enriched_by_key = {i["key"]: i for i in r2.json().get("issues", [])}
            # Preservar el orden de la cola (Service Desk lo ordena por SLA/prioridad).
            issues = [enriched_by_key.get(i["key"], i) for i in basic_issues if i.get("key")]
        else:
            log.warning(f"JQL enrichment falló ({r2.status_code}); uso datos básicos del SD.")
            issues = basic_issues

        log.info(f"Jira: {len(issues)} tickets en cola Video Operations")
        return issues

    @staticmethod
    def _is_bot_attachment(filename: str) -> bool:
        """True si el adjunto fue subido por el bot (convertido), no por el
        cliente. El bot sube con sufijo _STANDARD_VIDEO_CONVERTED (legacy) o
        con el nombre canonico que termina en _CTV_CSV[_Vn]. No hay que
        re-procesar esos.
        """
        stem = Path(filename).stem.upper()
        if "_STANDARD_VIDEO_CONVERTED" in stem:
            return True
        # Nombre canonico del bot: termina en _CTV_CSV o _CTV_CSV_V<n>
        import re as _re
        if _re.search(r"_CTV_CSV(_V\d+)?$", stem):
            return True
        return False

    def download_all_videos(self, issue: dict, dest_dir: Path) -> list[Path]:
        """Descarga TODOS los videos originales del cliente adjuntos al ticket.
        Ignora los adjuntos que el propio bot subio (convertidos). Si no hay
        adjuntos, intenta links directos / de servicio (devuelve lista de 0-1).

        Devuelve lista de Paths descargados (puede ser vacia).
        """
        fields = issue.get("fields", {})
        downloaded: list[Path] = []

        # ── Adjuntos de video del cliente ─────────────────────────────────
        for att in fields.get("attachment", []):
            filename = att.get("filename", "")
            ext = Path(filename).suffix.lower()
            if self._is_bot_attachment(filename):
                log.info(f"Ignorando adjunto del bot: {filename}")
                continue
            if ext in VIDEO_EXTENSIONS:
                log.info(f"Adjunto de vídeo del cliente: {filename}")
                try:
                    p = self._download_url(att["content"], dest_dir / filename)
                    downloaded.append(p)
                except Exception as e:
                    log.warning(f"No se pudo bajar {filename}: {e}")

        if downloaded:
            return downloaded

        # ── Sin adjuntos: ¿el ticket apunta a una CARPETA de Google Drive?
        # (varios archivos → varios creatives). gdown lista y baja la carpeta.
        folder_url = self._find_drive_folder_url(issue)
        if folder_url:
            vids = self._download_gdrive_folder(folder_url, dest_dir)
            if vids:
                return vids
            # Si la carpeta falló, seguimos al fallback de link único por si
            # hubiera además un link directo a un archivo en el ticket.

        # ── Sin adjuntos: buscar links (devuelve 0-1) ─────────────────────
        single = self.download_video(issue, dest_dir)
        return [single] if single else []

    def download_video(self, issue: dict, dest_dir: Path) -> Path | None:
        """
        Obtiene UN vídeo del ticket (el primero que encuentre):
          1. Adjuntos con extensión de vídeo (.mp4, .mov…)
          2. Links directos a .mp4/.mov en descripción o comentarios
          3. Links de servicios (WeTransfer, Google Drive, Dropbox, etc.)
        Se mantiene por compatibilidad y para el fallback de links.
        """
        fields = issue.get("fields", {})

        # ── Adjuntos ──────────────────────────────────────────────────────
        for att in fields.get("attachment", []):
            filename = att.get("filename", "")
            ext = Path(filename).suffix.lower()
            if self._is_bot_attachment(filename):
                log.info(f"Ignorando adjunto del bot: {filename}")
                continue
            if ext in VIDEO_EXTENSIONS:
                log.info(f"Adjunto de vídeo encontrado: {filename}")
                return self._download_url(att["content"], dest_dir / filename)

        # ── Buscar links en TODOS los campos de texto del ticket ──────────
        texts = []

        # Descripción / Creative URLs
        texts.append(self._extract_text(fields.get("description") or {}))

        # Todos los campos customfield que puedan contener texto
        for key, val in fields.items():
            if key.startswith("customfield_") and isinstance(val, str) and val:
                texts.append(val)
            elif key.startswith("customfield_") and isinstance(val, dict):
                # Campos ADF o texto anidado
                texts.append(self._extract_text(val))

        # Comentarios
        for comment in reversed(fields.get("comment", {}).get("comments", [])):
            texts.append(self._extract_text(comment.get("body", {})))

        full_text = " ".join(texts)

        # 1. Link directo a archivo de video (.mp4/.mov/...)
        match = URL_PATTERN.search(full_text)
        if match:
            url = match.group(0)
            name = Path(urlparse(url).path).name or "video.mp4"
            log.info(f"Link directo encontrado: {url}")
            return self._download_url(url, dest_dir / name)

        # 2. Link de servicio conocido (WeTransfer, Drive, Dropbox, etc.)
        match = TRANSFER_DOMAINS.search(full_text)
        if match:
            url = match.group(0)
            log.info(f"Link de servicio conocido: {url}")
            return self._handle_transfer_link(url, dest_dir)

        # 3. Fallback generico: cualquier otro link http(s) que NO sea un
        # dominio conocido de no-video (specs, sheets, figma, jira). Best-effort:
        # intenta descarga directa y, si es HTML, scrapea la pagina buscando
        # URLs de video.
        for m in ANY_URL.finditer(full_text):
            url = m.group(0).rstrip(".,);]")
            if NON_VIDEO_DOMAINS.search(url):
                continue
            log.info(f"Link desconocido — fallback generico: {url}")
            result = self._handle_generic_link(url, dest_dir)
            if result:
                return result

        log.warning("No se encontró vídeo adjunto ni link descargable en el ticket")
        return None

    def find_transfer_link(self, issue: dict) -> str | None:
        """Devuelve el primer link descargable encontrado en el ticket
        (directo, servicio conocido, o cualquier link no-excluido). Sirve
        para el aviso en Slack cuando el bot NO pudo bajar: asi el equipo ve
        de donde sacar el video aunque sea un servicio desconocido.
        """
        fields = issue.get("fields", {})
        texts = [self._extract_text(fields.get("description") or {})]
        for key, val in fields.items():
            if key.startswith("customfield_") and isinstance(val, str) and val:
                texts.append(val)
            elif key.startswith("customfield_") and isinstance(val, dict):
                texts.append(self._extract_text(val))
        for comment in fields.get("comment", {}).get("comments", []):
            texts.append(self._extract_text(comment.get("body", {})))
        full = " ".join(texts)
        # Prioridad: link directo > servicio conocido > cualquier link no-excluido
        for rx in (URL_PATTERN, TRANSFER_DOMAINS):
            m = rx.search(full)
            if m:
                return m.group(0).rstrip(".,);]")
        for m in ANY_URL.finditer(full):
            url = m.group(0).rstrip(".,);]")
            if not NON_VIDEO_DOMAINS.search(url):
                return url
        return None

    def _handle_transfer_link(self, url: str, dest_dir: Path) -> Path | None:
        """Intenta descargar desde servicios de transferencia.
        Soporta Google Drive y WeTransfer (los mas comunes segun el equipo),
        Dropbox, y descarga directa para el resto. Devuelve None si no se
        pudo (entonces el orquestador avisa para descarga manual).
        """
        log.info(f"Intentando descarga desde servicio externo: {url}")
        try:
            if "drive.google.com" in url or "drive.usercontent.google.com" in url:
                return self._download_gdrive(url, dest_dir)
            if "we.tl" in url or "wetransfer.com" in url:
                return self._download_wetransfer(url, dest_dir)
            if "dropbox.com" in url:
                # Forzar descarga directa: dl=1
                dl = url.replace("dl=0", "dl=1")
                if "dl=1" not in dl:
                    dl += ("&" if "?" in dl else "?") + "dl=1"
                return self._stream_to_file(dl, dest_dir)
            # Resto: intento directo
            return self._stream_to_file(url, dest_dir)
        except Exception as e:
            log.warning(f"No se pudo descargar desde {url}: {e}")
            return None

    def _handle_generic_link(self, url: str, dest_dir: Path) -> Path | None:
        """Fallback para servicios desconocidos. Best-effort:
        1. Intenta descarga directa (si Content-Type es video/binario).
        2. Si la respuesta es HTML, scrapea la pagina buscando URLs de video
           (og:video meta, tags <video>/<source>, links .mp4/.mov directos).
        Devuelve None si no encuentra nada descargable.
        """
        import re
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36",
        })
        try:
            r = session.get(url, stream=True, timeout=60, allow_redirects=True)
        except Exception as e:
            log.warning(f"Generic link: GET fallo {url[:100]}: {e}")
            return None

        ct = r.headers.get("Content-Type", "")
        # Caso 1: la URL ya es el archivo
        if "video" in ct or "octet-stream" in ct:
            return self._stream_to_file(r.url, dest_dir, session=session)

        # Caso 2: HTML — scrapear por URLs de video
        if "text/html" not in ct:
            return None
        try:
            html = r.text
        except Exception:
            return None

        candidates = []
        # og:video / og:video:url / og:video:secure_url
        for m in re.finditer(r'<meta[^>]+property=["\']og:video(?::secure_url|:url)?["\'][^>]+content=["\']([^"\']+)["\']', html, re.IGNORECASE):
            candidates.append(m.group(1))
        # <video src> y <source src>
        for m in re.finditer(r'<(?:video|source)[^>]+src=["\']([^"\']+)["\']', html, re.IGNORECASE):
            candidates.append(m.group(1))
        # Links directos .mp4/.mov/etc en el HTML
        for m in URL_PATTERN.finditer(html):
            candidates.append(m.group(0))

        # Probar cada candidato (resolviendo URLs relativas)
        from urllib.parse import urljoin
        seen = set()
        for c in candidates:
            full = urljoin(r.url, c)
            if full in seen:
                continue
            seen.add(full)
            log.info(f"Generic link: candidato de video en pagina: {full[:120]}")
            got = self._stream_to_file(full, dest_dir, session=session)
            if got:
                return got
        log.warning(f"Generic link: no encontre video descargable en {url[:100]}")
        return None

    def _stream_to_file(self, url: str, dest_dir: Path, session=None,
                         fallback_name: str = "video_from_link.mp4") -> Path | None:
        """Descarga una URL a disco si el Content-Type es video/binario."""
        session = session or requests.Session()
        r = session.get(url, stream=True, timeout=120, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        if not ("video" in ct or "octet-stream" in ct or "application/" in ct):
            log.warning(f"Link no descargable directamente ({ct}): {url[:120]}")
            return None
        cd = r.headers.get("Content-Disposition", "")
        name = fallback_name
        if "filename=" in cd:
            name = cd.split("filename=")[-1].strip('"').strip("'").strip()
            # filename*=UTF-8'' style
            if name.lower().startswith("utf-8''"):
                from urllib.parse import unquote
                name = unquote(name[7:])
        dest = dest_dir / name
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        log.info(f"Descarga OK: {dest.name} ({dest.stat().st_size/1024/1024:.1f} MB)")
        return dest

    def _download_gdrive(self, url: str, dest_dir: Path) -> Path | None:
        """Google Drive: extrae el file_id de cualquier formato de URL y baja
        via drive.usercontent.google.com, manejando el token de confirmacion
        de archivos grandes. Requiere que el archivo este compartido
        'cualquiera con el link'; si es restringido, devuelve None.
        """
        import re
        m = (re.search(r"/d/([a-zA-Z0-9_-]+)", url)
             or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url))
        if not m:
            log.warning(f"Google Drive: no pude extraer file_id de {url}")
            return None
        file_id = m.group(1)
        session = requests.Session()
        dl_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        r = session.get(dl_url, stream=True, timeout=120, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        # Si Drive devuelve HTML, es la pagina de confirmacion o de error (acceso)
        if "text/html" in ct:
            body = r.text
            # Buscar el form de confirmacion (archivos grandes)
            token = re.search(r'name="confirm"\s+value="([^"]+)"', body)
            uuid = re.search(r'name="uuid"\s+value="([^"]+)"', body)
            if token:
                params = {"id": file_id, "export": "download", "confirm": token.group(1)}
                if uuid:
                    params["uuid"] = uuid.group(1)
                r = session.get("https://drive.usercontent.google.com/download",
                                params=params, stream=True, timeout=120)
                ct = r.headers.get("Content-Type", "")
            if "text/html" in ct:
                log.warning(f"Google Drive: el archivo {file_id} no es accesible "
                            f"(restringido o requiere login). Descarga manual.")
                return None
        cd = r.headers.get("Content-Disposition", "")
        name = "gdrive_video.mp4"
        if "filename=" in cd:
            name = cd.split("filename=")[-1].strip('"').strip("'").strip()
            if name.lower().startswith("utf-8''"):
                from urllib.parse import unquote
                name = unquote(name[7:])
        dest = dest_dir / name
        with open(dest, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        log.info(f"Google Drive: descargado {dest.name} ({dest.stat().st_size/1024/1024:.1f} MB)")
        return dest

    def _find_drive_folder_url(self, issue: dict) -> str | None:
        """Busca un link a una CARPETA de Google Drive (/drive/folders/{id}) en
        el texto del ticket (descripcion, customfields, comentarios). Una carpeta
        puede traer varios videos → varios creatives, por eso se trata aparte
        del link a archivo unico."""
        fields = issue.get("fields", {})
        texts = [self._extract_text(fields.get("description") or {})]
        for key, val in fields.items():
            if key.startswith("customfield_") and isinstance(val, str) and val:
                texts.append(val)
            elif key.startswith("customfield_") and isinstance(val, dict):
                texts.append(self._extract_text(val))
        for comment in fields.get("comment", {}).get("comments", []):
            texts.append(self._extract_text(comment.get("body", {})))
        full = " ".join(texts)
        m = re.search(
            r"https?://drive\.google\.com/drive/(?:u/\d+/)?folders/"
            r"[a-zA-Z0-9_-]+[^\s\)\]\"'>]*",
            full,
        )
        return m.group(0).rstrip(".,);]") if m else None

    def _download_gdrive_folder(self, url: str, dest_dir: Path) -> list[Path]:
        """Descarga TODOS los archivos de una carpeta PUBLICA de Google Drive
        (link /drive/folders/{id}) y devuelve los que sean videos.

        Usa gdown, que lista y baja carpetas publicas sin credenciales (hasta
        ~50 archivos). Requiere que la carpeta este compartida 'cualquiera con
        el link'. Devuelve [] si gdown no esta instalado, la carpeta es privada,
        o no tiene videos.
        """
        try:
            import gdown
        except ImportError:
            log.warning("gdown no instalado — no puedo bajar carpetas de Drive "
                        "(pip install gdown). Aviso para descarga manual.")
            return []
        dest_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"Google Drive: bajando carpeta {url}")
        try:
            gdown.download_folder(url=url, output=str(dest_dir),
                                  quiet=True, use_cookies=False)
        except Exception as e:
            log.warning(f"Google Drive: fallo al bajar la carpeta {url}: {e}")
        # Recolectar los videos que hayan quedado. rglob por si la carpeta de
        # Drive tenia subcarpetas. Se ignoran adjuntos del propio bot.
        videos = sorted(
            p for p in dest_dir.rglob("*")
            if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
            and not self._is_bot_attachment(p.name)
        )
        if videos:
            log.info(f"Google Drive: {len(videos)} video(s) bajados de la carpeta")
        else:
            log.warning(f"Google Drive: carpeta sin videos descargables o "
                        f"privada: {url}")
        return videos

    def _download_wetransfer(self, url: str, dest_dir: Path) -> Path | None:
        """WeTransfer: resuelve la URL de descarga directa via su API interna.
        we.tl/... redirige a wetransfer.com/downloads/<transfer_id>/<security_hash>.
        Hay que pedir un CSRF token y POST a /api/v4/transfers/.../download.
        Fragil (depende de la API no documentada); si cambia, devuelve None.
        """
        import re
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36",
            "x-requested-with": "XMLHttpRequest",
        })
        # Resolver short link we.tl → URL completa
        r0 = session.get(url, allow_redirects=True, timeout=30)
        final = r0.url
        m = re.search(r"wetransfer\.com/downloads/([a-zA-Z0-9]+)/([a-zA-Z0-9]+)", final)
        if not m:
            log.warning(f"WeTransfer: formato de URL inesperado: {final}")
            return None
        transfer_id, security_hash = m.group(1), m.group(2)
        # CSRF token del meta tag
        csrf = re.search(r'name="csrf-token"\s+content="([^"]+)"', r0.text)
        if csrf:
            session.headers["x-csrf-token"] = csrf.group(1)
        api = f"https://wetransfer.com/api/v4/transfers/{transfer_id}/download"
        resp = session.post(api, json={"security_hash": security_hash,
                                       "intent": "entire_transfer"}, timeout=30)
        if resp.status_code != 200:
            log.warning(f"WeTransfer: API download fallo HTTP {resp.status_code}")
            return None
        direct = resp.json().get("direct_link")
        if not direct:
            log.warning("WeTransfer: la API no devolvio direct_link")
            return None
        # El direct_link suele ser un .zip si hay varios archivos, o el archivo directo
        return self._stream_to_file(direct, dest_dir, session=session,
                                    fallback_name="wetransfer_video.mp4")

    def _download_url(self, url: str, dest: Path) -> Path:
        is_jira = self.base_url in url
        with requests.get(url, auth=self.auth if is_jira else None,
                          stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(8192):
                    f.write(chunk)
        log.info(f"Descargado: {dest} ({dest.stat().st_size/1024/1024:.1f} MB)")
        return dest

    def _extract_text(self, node) -> str:
        """Extrae texto de un nodo ADF, incluyendo URLs de Smart Links.
        Cuando pegas un link de Drive/WeTransfer en Jira, se convierte en
        Smart Link: la URL queda en attrs.url (inlineCard/embedCard/blockCard)
        o en un link mark (marks[].attrs.href), NO en un nodo de texto. Hay
        que recolectar esas URLs ademas del texto plano.
        """
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            ntype = node.get("type")
            parts = []
            if ntype == "text":
                parts.append(node.get("text", ""))
                # URL dentro de un link mark
                for mark in node.get("marks", []) or []:
                    if mark.get("type") == "link":
                        href = (mark.get("attrs") or {}).get("href", "")
                        if href:
                            parts.append(href)
            elif ntype in ("inlineCard", "blockCard", "embedCard"):
                # Smart Link: la URL real esta en attrs.url
                attrs = node.get("attrs") or {}
                url = attrs.get("url") or ""
                if url:
                    parts.append(url)
            # Recursion sobre hijos
            for c in node.get("content", []) or []:
                parts.append(self._extract_text(c))
            return " ".join(p for p in parts if p)
        return ""

    def attach_file(self, ticket_key: str, file_path: Path):
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}/attachments"
        with open(file_path, "rb") as f:
            r = requests.post(url, auth=self.auth,
                              headers={"X-Atlassian-Token": "no-check"},
                              files={"file": (file_path.name, f, "video/mp4")})
        r.raise_for_status()
        log.info(f"Adjunto subido a {ticket_key}")

    def add_comment(self, ticket_key: str, text: str):
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}/comment"
        body = {
            "body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph",
                             "content": [{"type": "text", "text": text}]}]
            }
        }
        r = requests.post(url, auth=self.auth,
                          headers={**self.headers, "Content-Type": "application/json"},
                          json=body)
        r.raise_for_status()
        log.info(f"Comentario añadido a {ticket_key}")

    def add_comment_adf(self, ticket_key: str, doc_body: dict):
        """Variante de add_comment que acepta el doc ADF (Atlassian Document Format)
        ya construido. Necesaria para comentarios con links clickables, formato,
        codigos inline, etc.

        doc_body debe ser un dict tipo {"type": "doc", "version": 1, "content": [...]}.
        """
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}/comment"
        r = requests.post(url, auth=self.auth,
                          headers={**self.headers, "Content-Type": "application/json"},
                          json={"body": doc_body})
        r.raise_for_status()
        log.info(f"Comentario (ADF) añadido a {ticket_key}")

    def get_transitions(self, ticket_key: str) -> dict:
        """Devuelve {nombre_transicion: id} para las transiciones disponibles."""
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}/transitions"
        r = requests.get(url, auth=self.auth, headers=self.headers)
        r.raise_for_status()
        return {t["name"]: t["id"] for t in r.json().get("transitions", [])}

    def get_attachment_limit_mb(self, default_mb: int = 100) -> int:
        """Límite REAL de tamaño de adjunto de la instancia Jira, en MB.
        Lo consulta en /rest/api/3/attachment/meta (campo uploadLimit, bytes).
        Sirve para no intentar adjuntar archivos que Jira va a rechazar (el bot
        usa esto para decidir cuándo recomprimir). Fallback a default_mb si falla.
        """
        try:
            r = requests.get(f"{self.base_url}/rest/api/3/attachment/meta",
                             auth=self.auth, headers=self.headers, timeout=15)
            r.raise_for_status()
            limit = r.json().get("uploadLimit")
            if limit:
                return int(limit / (1024 * 1024))
        except Exception as e:
            log.warning(f"No se pudo leer el límite de adjuntos de Jira: {e}")
        return default_mb

    def get_form_answers(self, ticket_key: str) -> dict:
        """Lee las forms de Atlassian Forms adjuntas al ticket y devuelve
        un dict {question_label: answer_text}. Algunos fields (como
        'Advertiser's website for QR') no son customfields estandar de
        Jira, viven dentro de Atlassian Forms y solo se acceden por la API
        forms.cloud externa.

        Devuelve dict vacio si no hay forms o no se puede leer.
        """
        try:
            r = requests.get(
                f"{FORMS_API_BASE}/issue/{ticket_key}/form",
                auth=self.auth, headers=self.headers, timeout=15,
            )
            if r.status_code != 200:
                return {}
            forms = r.json() or []
        except Exception as e:
            log.warning(f"{ticket_key}: no se pudieron listar forms: {e}")
            return {}

        merged: dict[str, str] = {}
        for f in forms:
            form_id = f.get("id")
            if not form_id:
                continue
            try:
                rr = requests.get(
                    f"{FORMS_API_BASE}/issue/{ticket_key}/form/{form_id}",
                    auth=self.auth, headers=self.headers, timeout=15,
                )
                if rr.status_code != 200:
                    continue
                data = rr.json()
            except Exception as e:
                log.warning(f"{ticket_key}: no se pudo leer form {form_id}: {e}")
                continue
            questions = (data.get("design") or {}).get("questions") or {}
            answers = (data.get("state") or {}).get("answers") or {}
            for qid, q in questions.items():
                label = q.get("label", "").strip()
                if not label:
                    continue
                ans = answers.get(qid, {})
                if isinstance(ans, dict):
                    text = (ans.get("text") or "").strip()
                    if text:
                        merged[label] = text
        return merged

    def set_fields(self, ticket_key: str, fields: dict):
        """PUT al issue para setear campos directamente. Usar antes de una
        transicion si esta exige fields que el body de /transitions no acepta
        (caso: workflow con campos que no estan en el screen de la transicion).
        """
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}"
        r = requests.put(url, auth=self.auth,
                         headers={**self.headers, "Content-Type": "application/json"},
                         json={"fields": fields})
        if r.status_code >= 400:
            log.warning(f"{ticket_key}: set_fields fallo HTTP {r.status_code} — {r.text[:300]}")
            r.raise_for_status()
        log.info(f"{ticket_key}: fields actualizados ({list(fields.keys())})")

    @staticmethod
    def _collect_hrefs(nodes) -> list:
        """Recoge todos los href de marks tipo 'link' dentro de una lista de
        nodos ADF (recursivo). Sirve para idempotencia: no duplicar links que
        ya estan en la descripcion."""
        hrefs: list[str] = []

        def walk(n):
            if isinstance(n, dict):
                for m in (n.get("marks") or []):
                    if m.get("type") == "link":
                        href = (m.get("attrs") or {}).get("href")
                        if href:
                            hrefs.append(href)
                for c in (n.get("content") or []):
                    walk(c)
            elif isinstance(n, list):
                for c in n:
                    walk(c)

        walk(nodes)
        return hrefs

    def append_to_description(self, ticket_key: str, new_nodes: list) -> bool:
        """Agrega `new_nodes` (lista de nodos ADF, p.ej. paragraphs) al FINAL de
        la descripcion actual del ticket, PRESERVANDO el contenido existente
        (brief del cliente, etc). Hace GET de la descripcion, concatena y PUT.

        Idempotente: si todos los hrefs de los nodos nuevos ya aparecen en la
        descripcion actual, no escribe nada (evita duplicar en re-procesos del
        mismo ticket).

        Devuelve True si escribio, False si no habia nada que agregar o si los
        links ya estaban presentes.
        """
        if not new_nodes:
            return False
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}"
        r = requests.get(url, auth=self.auth, headers=self.headers,
                         params={"fields": "description"})
        r.raise_for_status()
        current = (r.json().get("fields") or {}).get("description")
        if not isinstance(current, dict):
            current = {"type": "doc", "version": 1, "content": []}
        existing = current.get("content") or []

        # Idempotencia por hrefs: si todos los links nuevos ya estan en la
        # descripcion actual, no duplicar.
        new_hrefs = self._collect_hrefs(new_nodes)
        if new_hrefs:
            existing_str = json.dumps(existing, ensure_ascii=False)
            if all(h in existing_str for h in new_hrefs):
                log.info(f"{ticket_key}: la descripcion ya tiene los links de Studio, no se duplica")
                return False

        merged = {"type": "doc", "version": 1, "content": existing + new_nodes}
        pr = requests.put(url, auth=self.auth,
                          headers={**self.headers, "Content-Type": "application/json"},
                          json={"fields": {"description": merged}})
        if pr.status_code >= 400:
            log.warning(f"{ticket_key}: append_to_description fallo HTTP {pr.status_code} — {pr.text[:300]}")
            pr.raise_for_status()
        log.info(f"{ticket_key}: links de Studio agregados a la descripcion")
        return True

    def transition(self, ticket_key: str, transition_id: str, fields: dict | None = None):
        url = f"{self.base_url}/rest/api/3/issue/{ticket_key}/transitions"
        body: dict = {"transition": {"id": transition_id}}
        if fields:
            body["fields"] = fields
        r = requests.post(url, auth=self.auth,
                          headers={**self.headers, "Content-Type": "application/json"},
                          json=body)
        if r.status_code >= 400:
            # Loguear el body real de Jira facilita diagnosticar campos
            # requeridos que el endpoint /transitions?expand=fields a veces
            # no reporta como required.
            log.warning(f"{ticket_key}: transicion {transition_id} fallo "
                        f"HTTP {r.status_code} — {r.text[:300]}")
            r.raise_for_status()
        log.info(f"{ticket_key}: transicion {transition_id} ejecutada")
