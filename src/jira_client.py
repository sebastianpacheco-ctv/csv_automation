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
    r"https?://(?:we\.tl|wetransfer\.com|drive\.google\.com|dropbox\.com"
    r"|frame\.io|app\.frame\.io|vimeo\.com|sharepoint\.com|1drv\.ms"
    r"|mediafire\.com|transfer\.sh|hightail\.com|smash\.pm"
    r"|sendgb\.com|send\.tresorit\.com)/[^\s\]>\"']+",
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
    "customfield_10800"   # Request Type (para detectar form 1916)
    # Nota: Creative URLs (links de Drive/WeTransfer) vienen en description
)


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

    def download_video(self, issue: dict, dest_dir: Path) -> Path | None:
        """
        Obtiene el vídeo del ticket:
          1. Adjuntos con extensión de vídeo (.mp4, .mov…)
          2. Links directos a .mp4/.mov en descripción o comentarios
          3. Links de servicios (WeTransfer, Google Drive, Dropbox, etc.)
        """
        fields = issue.get("fields", {})

        # ── Adjuntos ──────────────────────────────────────────────────────
        for att in fields.get("attachment", []):
            filename = att.get("filename", "")
            ext = Path(filename).suffix.lower()
            # Ignorar archivos ya convertidos por el bot
            if "_STANDARD_VIDEO_CONVERTED" in filename:
                log.info(f"Ignorando adjunto ya convertido: {filename}")
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

        for text in texts:
            # 1. Link directo a archivo de video
            match = URL_PATTERN.search(text)
            if match:
                url = match.group(0)
                name = Path(urlparse(url).path).name or "video.mp4"
                log.info(f"Link directo encontrado: {url}")
                return self._download_url(url, dest_dir / name)

            # 2. Link de servicio de transferencia (WeTransfer, Drive, etc.)
            match = TRANSFER_DOMAINS.search(text)
            if match:
                url = match.group(0)
                log.info(f"Link de servicio encontrado: {url}")
                # Para servicios externos notificamos en Slack pero no podemos
                # descargar automáticamente (requieren autenticación/click humano)
                return self._handle_transfer_link(url, dest_dir)

        log.warning("No se encontró vídeo adjunto ni link en el ticket")
        return None

    def find_transfer_link(self, issue: dict) -> str | None:
        """Devuelve el primer link de servicio de transferencia encontrado en el ticket."""
        fields = issue.get("fields", {})
        texts = [self._extract_text(fields.get("description") or {})]
        for comment in fields.get("comment", {}).get("comments", []):
            texts.append(self._extract_text(comment.get("body", {})))
        for text in texts:
            match = TRANSFER_DOMAINS.search(text)
            if match:
                return match.group(0)
            match = URL_PATTERN.search(text)
            if match:
                return match.group(0)
        return None

    def _handle_transfer_link(self, url: str, dest_dir: Path) -> Path | None:
        """
        Intenta descargar desde servicios de transferencia.
        - Google Drive: convierte link de vista a link de descarga directa
        - WeTransfer: intenta descarga directa
        - Otros: intenta descarga directa
        """
        log.info(f"Intentando descarga desde servicio externo: {url}")

        # Google Drive — convertir a link de descarga directa
        if "drive.google.com" in url:
            import re
            match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
            if match:
                file_id = match.group(1)
                url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm=t"
                log.info(f"Google Drive → descarga directa: {url}")

        try:
            session = requests.Session()
            r = session.get(url, stream=True, timeout=60, allow_redirects=True)

            # Verificar que es un video por Content-Type
            content_type = r.headers.get("Content-Type", "")
            if "video" in content_type or "octet-stream" in content_type:
                cd = r.headers.get("Content-Disposition", "")
                if "filename=" in cd:
                    name = cd.split("filename=")[-1].strip('"').strip("'").strip()
                else:
                    name = "video_from_link.mp4"
                dest = dest_dir / name
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                log.info(f"Descarga OK: {dest} ({dest.stat().st_size/1024/1024:.1f} MB)")
                return dest
            else:
                log.warning(
                    f"Link no descargable directamente ({content_type}). "
                    f"Requiere accion manual: {url}"
                )
                return None
        except Exception as e:
            log.warning(f"No se pudo descargar desde {url}: {e}")
            return None

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
        if isinstance(node, str):
            return node
        if isinstance(node, dict):
            if node.get("type") == "text":
                return node.get("text", "")
            return " ".join(self._extract_text(c) for c in node.get("content", []))
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
