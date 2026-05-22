"""
Filestage Uploader — API REST con multipart upload + auto-refresh de cookie.

(El antiguo StudioUploader basado en Playwright fue eliminado en mayo 2026.
 La integración con Studio Seedtag vive ahora en src/studio_api.py vía API
 GraphQL — ver StudioAPIClient.)
"""
import os
import logging
import requests
from pathlib import Path

log = logging.getLogger(__name__)


class FilestageUploader:
    def __init__(self, session_cookie: str = None, api_key: str = None,
                 email: str = None, password: str = None):
        self.session_cookie = session_cookie
        self.api_key = api_key
        self.email = email
        self.password = password
        self.team_id = "e16f96c4de9a0c1b11bbebab1ac09104"
        self.user_id = "b1cd742149aa51b33b01fec0e3b93663"
        self.ctv_folder_id = "ea234d4b3fcd0eb17588e4fd9b852102"

    def _cookies(self):
        name = os.getenv("FILESTAGE_COOKIE_NAME", "registeredSessionId")
        return {name: self.session_cookie} if self.session_cookie else {}

    def _refresh_cookie(self):
        """Renueva la cookie de sesion via Playwright cuando expira."""
        if not self.email or not self.password:
            log.warning("Sin credenciales Filestage — no se puede renovar cookie")
            return False
        log.info("Renovando cookie de sesion de Filestage via Playwright...")
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()
                page.goto("https://app.filestage.io", wait_until="networkidle")
                page.wait_for_selector("input[type=email], input[name=email]", timeout=10000)
                page.locator("input[type=email], input[name=email]").first.fill(self.email)
                btn = page.locator("button:has-text('Continue'), button:has-text('Next')").first
                if btn.count() > 0:
                    btn.click()
                    page.wait_for_timeout(2000)
                try:
                    pwd = page.locator("input[type=password]").first
                    pwd.wait_for(timeout=8000)
                    pwd.fill(self.password)
                    page.locator("button[type=submit]").first.click()
                except Exception:
                    pass
                page.wait_for_load_state("networkidle", timeout=20000)
                for cookie in context.cookies():
                    if cookie["name"] == "registeredSessionId":
                        self.session_cookie = cookie["value"]
                        log.info("Cookie de Filestage renovada correctamente")
                        browser.close()
                        return True
                browser.close()
                log.warning("No se encontro cookie registeredSessionId tras login")
                return False
        except Exception as e:
            log.error(f"Error renovando cookie Filestage: {e}")
            return False

    def _headers(self, extra=None):
        h = {"Accept": "*/*", "Origin": "https://app.filestage.io",
             "Referer": "https://app.filestage.io/", "Content-Type": "application/json"}
        if extra:
            h.update(extra)
        return h

    def upload(self, file_path, ticket_title: str, operator_entity: str = ""):
        if not self.session_cookie:
            log.warning("Sin FILESTAGE_SESSION_COOKIE — intentando renovar...")
            if not self._refresh_cookie():
                log.warning("No se pudo obtener cookie — saltando Filestage")
                return None

        # Intentar upload — si da 401, renovar cookie y reintentar una vez
        for attempt in range(2):
            try:
                project_id = self._get_or_create_project(ticket_title)
                step_id = self._get_or_create_step(project_id)
                section_id = self._get_section_id(project_id)
                self._multipart_upload(file_path, project_id, step_id, section_id)
                share_url = self._get_share_url(project_id)
                log.info(f"✅ Filestage: {file_path.name} | {share_url}")
                return share_url
            except requests.HTTPError as e:
                if e.response.status_code == 401 and attempt == 0:
                    log.warning("Cookie Filestage expirada — renovando automaticamente...")
                    if self._refresh_cookie():
                        log.info("Cookie renovada, reintentando upload...")
                        continue
                raise
        return None

    def _get_or_create_project(self, title: str) -> str:
        r = requests.get("https://api.filestage.io/projects",
            headers=self._headers(), cookies=self._cookies(),
            params={"team_id": self.team_id, "viewArchived": "false"}, timeout=30)
        r.raise_for_status()
        for folder in r.json():
            if folder.get("id") == self.ctv_folder_id:
                for p in folder.get("projects", []):
                    if p.get("name", "").strip() == title.strip():
                        return p["id"]
        r2 = requests.post("https://api.filestage.io/projects",
            headers=self._headers(), cookies=self._cookies(), timeout=30,
            json={"name": title, "folderId": self.ctv_folder_id, "teamId": self.team_id})
        r2.raise_for_status()
        pid = r2.json()["id"]
        log.info(f"Proyecto Filestage creado: {title} → {pid}")
        return pid

    def _get_or_create_step(self, project_id: str) -> str:
        r = requests.get(f"https://api.filestage.io/projects/{project_id}/steps",
            headers=self._headers(), cookies=self._cookies(), timeout=30)
        r.raise_for_status()
        steps = r.json()
        if steps:
            return steps[0]["id"]
        r2 = requests.post(f"https://api.filestage.io/projects/{project_id}/steps",
            headers=self._headers(), cookies=self._cookies(), timeout=30,
            json={"name": "Video Review"})
        r2.raise_for_status()
        return r2.json()["id"]

    def _get_section_id(self, project_id: str) -> str:
        r = requests.get(f"https://api.filestage.io/projects/{project_id}/sections",
            headers=self._headers(), cookies=self._cookies(), timeout=30)
        if r.status_code == 200 and r.json():
            return r.json()[0]["id"]
        return ""

    def _multipart_upload(self, file_path, project_id, step_id, section_id) -> str:
        import uuid, math, json as _json
        file_size = file_path.stat().st_size
        PART_SIZE = 5 * 1024 * 1024
        num_parts = math.ceil(file_size / PART_SIZE)
        file_id = uuid.uuid4().hex
        browser_tab_id = uuid.uuid4().hex
        file_name = file_path.name

        log.info(f"Filestage upload: {file_name} | {file_size/1024/1024:.1f} MB | {num_parts} partes")

        # s3-create
        r = requests.post("https://api.filestage.io/file-storage/s3-create",
            headers=self._headers(), cookies=self._cookies(), timeout=30,
            json={"id": file_id, "template": "FILE_VERSION", "mimeType": "video/mp4",
                "browserTabId": browser_tab_id, "contentType": "video/mp4",
                "fileExtension": "mp4", "fileName": file_name, "projectId": project_id,
                "sectionId": section_id, "shouldUseMultipart": True, "size": file_size,
                "stepIds": _json.dumps([step_id]),
                "teamId": self.team_id, "userId": self.user_id})
        r.raise_for_status()
        upload_id, key = r.json()["uploadId"], r.json()["key"]

        # subir partes
        with open(file_path, "rb") as f:
            for part_num in range(1, num_parts + 1):
                chunk = f.read(PART_SIZE)
                r2 = requests.post("https://api.filestage.io/file-storage/s3-multipart-create-signedurl",
                    headers=self._headers(), cookies=self._cookies(), timeout=30,
                    json={"key": key, "partNumber": part_num, "teamId": self.team_id,
                          "template": "FILE_VERSION", "uploadId": upload_id, "useCDN": False})
                signed_url = list(r2.json().values())[0]
                requests.put(signed_url, data=chunk, timeout=300)
                log.info(f"  Parte {part_num}/{num_parts} ✅")

        # s3-complete
        requests.post("https://api.filestage.io/file-storage/s3-complete",
            headers=self._headers(), cookies=self._cookies(), timeout=60,
            json={"key": key, "meta": {
                "browserTabId": browser_tab_id, "fileName": file_name,
                "id": file_id, "key": key, "mimeType": "video/mp4",
                "projectId": project_id, "sectionId": section_id,
                "shouldUseMultipart": True, "size": file_size,
                "startReviewBeforeTranscode": "true",
                "stepIds": _json.dumps([step_id]),
                "teamId": self.team_id, "template": "FILE_VERSION", "userId": self.user_id
            }}).raise_for_status()
        return key

    def _get_share_url(self, project_id: str) -> str:
        return f"https://app.filestage.io/reviews/{project_id}"
