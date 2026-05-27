"""
GCS Uploader — sube el .mp4 convertido a un bucket de Google Cloud Storage
y devuelve un link.

⚠️ INERTE por defecto. Solo se usa si la env var `GCS_BUCKET` esta seteada.
Durante el burn-in local no se toca (GCS_BUCKET vacia → main.py no instancia
esta clase → ni siquiera se importa google-cloud-storage).

Pensado para correr en la VM de GCP con la SERVICE ACCOUNT de la instancia
(Application Default Credentials, sin key file). En local, se puede pasar
GOOGLE_APPLICATION_CREDENTIALS apuntando a una key JSON.

Tipo de link:
- **Publico** (default, GCS_PUBLIC=true): hace el objeto public-read y devuelve
  https://storage.googleapis.com/<bucket>/<obj>. NUNCA caduca. Cualquiera con
  la URL accede (aceptable para creatives que igual van a DSPs).
- **Signed URL** (GCS_PUBLIC=false): URL firmada V4. Caduca en max 7 dias
  (limite de GCS). Requiere que la service account pueda firmar (signBlob).
"""
import os
import logging
from pathlib import Path
from datetime import timedelta

log = logging.getLogger(__name__)


class GCSUploader:
    def __init__(self, bucket_name: str, prefix: str = "ctv",
                 public: bool = True, signed_days: int = 7):
        self.bucket_name = bucket_name
        self.prefix = prefix.strip("/")
        self.public = public
        self.signed_days = min(signed_days, 7)  # GCS V4 cap: 7 dias
        self._client = None
        self._bucket = None

    def _ensure_client(self):
        if self._client is None:
            # Import perezoso: solo si realmente se usa GCS
            from google.cloud import storage
            self._client = storage.Client()
            self._bucket = self._client.bucket(self.bucket_name)

    def upload(self, file_path: Path, ticket_key: str) -> str | None:
        """Sube file_path a gs://<bucket>/<prefix>/<ticket_key>/<filename> y
        devuelve la URL (publica o firmada). None si falla.
        """
        if not file_path.exists():
            log.warning(f"GCS: archivo no existe {file_path}")
            return None
        try:
            self._ensure_client()
            obj_name = f"{self.prefix}/{ticket_key}/{file_path.name}"
            blob = self._bucket.blob(obj_name)
            blob.upload_from_filename(str(file_path), content_type="video/mp4")
            log.info(f"GCS: subido gs://{self.bucket_name}/{obj_name}")

            if self.public:
                blob.make_public()
                return blob.public_url  # https://storage.googleapis.com/<bucket>/<obj>
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(days=self.signed_days),
                method="GET",
            )
        except Exception as e:
            log.warning(f"GCS: error subiendo {file_path.name}: {e}")
            return None


def from_env() -> "GCSUploader | None":
    """Construye un GCSUploader desde env vars, o None si GCS_BUCKET no esta
    seteada (modo inerte). El caller (main.py) usa esto y solo sube a GCS si
    devuelve un uploader.

    Env vars:
      GCS_BUCKET     — nombre del bucket. Si vacia → None (GCS desactivado).
      GCS_PREFIX     — prefijo dentro del bucket (default 'ctv').
      GCS_PUBLIC     — 'true' (default) hace objetos publicos; 'false' usa
                       signed URLs de 7 dias.
    """
    bucket = os.getenv("GCS_BUCKET", "").strip()
    if not bucket:
        return None
    prefix = os.getenv("GCS_PREFIX", "ctv").strip()
    public = os.getenv("GCS_PUBLIC", "true").strip().lower() != "false"
    return GCSUploader(bucket_name=bucket, prefix=prefix, public=public)
