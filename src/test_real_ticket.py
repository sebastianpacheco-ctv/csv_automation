"""
Test end-to-end con un ticket REAL de la cola 1597 (SDS-21631).

Flujo:
  1. Descarga el attachment .mp4 desde Jira (si no está en tmp/)
  2. Convierte con VideoConverter (si no está convertido en tmp/)
  3. Upload a Studio bajo el bot — si tmp/<ticket>/.studio_video_id existe,
     SE SALTA el upload (idempotencia, porque getVideosByQuery no nos sirve
     para buscar por nombre y un re-upload daría "name already exists")
  4. Espera procesamiento con el patrón: 60s → check → 30s → check → alerta
  5. Crea creative CSV-CTV
  6. Imprime URLs

NO toca Jira (sin comentarios, sin transiciones, sin attachments).
NO toca Filestage.
Solo lectura de Jira + escritura en Studio bajo el bot.

Uso:
    export STUDIO_JWT_COOKIE='eyJ...'   # JWT del bot
    cd csv-automation && source venv/bin/activate
    python3 src/test_real_ticket.py
"""
import os, sys, logging, requests
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from studio_api import StudioAPIClient, StudioVideoNotReadyError
from converter import VideoConverter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Carga .env ──────────────────────────────────────────────────────────────
env_file = Path(__file__).parent.parent / ".env"
for line in env_file.read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, _, v = line.partition("=")
    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

# ── Constantes del ticket SDS-21631 ─────────────────────────────────────────
TICKET_KEY = "SDS-21631"
TICKET_TITLE = "_SDS-21631_CAMP_Standar video Adidas"
ATTACHMENT_ID = "343451"
ATTACHMENT_FILENAME = "adidas Adizero EVO SL.mp4"
OPERATOR_ENTITY = "CA"   # → 'canada' en Studio

# Pipeline CTV — id del template ctv-base (hipótesis verificada empíricamente:
# el upload arrancó con este pipeline y el procesado tarda mucho más que con legacy)
CTV_PIPELINE_ID = "68d10800680fb2e148f30961"

# ── Auth ────────────────────────────────────────────────────────────────────
jira_email = os.environ["JIRA_EMAIL"]
jira_token = os.environ["JIRA_API_TOKEN"]
jira_base = os.environ.get("JIRA_BASE_URL", "https://seedtag.atlassian.net")
jwt = os.environ.get("STUDIO_JWT_COOKIE")
if not jwt:
    print("ERROR: falta STUDIO_JWT_COOKIE (JWT del bot)")
    sys.exit(1)

# ── 1. Descarga del attachment ─────────────────────────────────────────────
tmp_dir = Path(__file__).parent.parent / "tmp" / TICKET_KEY
tmp_dir.mkdir(parents=True, exist_ok=True)
raw_path = tmp_dir / ATTACHMENT_FILENAME

if raw_path.exists():
    log.info(f"Ya descargado: {raw_path.name} ({raw_path.stat().st_size/1024/1024:.1f} MB)")
else:
    download_url = f"{jira_base}/rest/api/3/attachment/content/{ATTACHMENT_ID}"
    log.info(f"Descargando attachment desde Jira: {download_url}")
    r = requests.get(download_url, auth=(jira_email, jira_token), stream=True, timeout=60)
    r.raise_for_status()
    with open(raw_path, "wb") as f:
        for chunk in r.iter_content(8192):
            f.write(chunk)
    log.info(f"✅ Descargado: {raw_path.name} ({raw_path.stat().st_size/1024/1024:.1f} MB)")

# ── 2. Conversión FFmpeg ────────────────────────────────────────────────────
converted_path = tmp_dir / (raw_path.stem.replace("_STANDARD_VIDEO_CONVERTED", "")
                            + "_STANDARD_VIDEO_CONVERTED.mp4")
if converted_path.exists():
    log.info(f"Ya convertido: {converted_path.name} ({converted_path.stat().st_size/1024/1024:.1f} MB)")
else:
    converter = VideoConverter(tmp_dir=str(tmp_dir))
    result = converter.convert(raw_path)
    if not result:
        print("ERROR en conversión FFmpeg")
        sys.exit(1)
    converted_path = result

# ── 3. Auth Studio + verificación de identidad ─────────────────────────────
client = StudioAPIClient(
    jwt_cookie=jwt,
    sidecar_path=Path(os.getenv("TMP_DIR", "./tmp")) / ".studio_jwt",
)
user = client.ping()
if user["email"] != "design_automations@seedtag.com":
    print(f"⚠️  ABORTAR: identidad del JWT es {user['email']!r}, "
          f"esperaba design_automations@seedtag.com")
    sys.exit(1)
log.info(f"✅ Auth Studio como bot: {user['email']}")

# ── 4. Upload (idempotente) ────────────────────────────────────────────────
video_id_file = tmp_dir / ".studio_video_id"
if video_id_file.exists():
    video_id = video_id_file.read_text().strip()
    log.info(f"♻️  Saltando upload — ya existe video_id={video_id} en {video_id_file.name}")
else:
    log.info(f"Pipeline CTV: {CTV_PIPELINE_ID}")
    video = client.upload_video(converted_path, video_pipeline_id=CTV_PIPELINE_ID)
    video_id = video["id"]
    # Persistir INMEDIATAMENTE para sobrevivir a un crash/timeout del cliente HTTP
    video_id_file.write_text(video_id + "\n")
    log.info(f"✅ Video subido id={video_id} (guardado en {video_id_file.name})")

# ── 5. Espera procesamiento (patrón del usuario) ───────────────────────────
ts = datetime.now().strftime("%Y%m%d_%H%M%S")
creative_name = f"_TEST_{ts}_{TICKET_KEY}_CAMP_Standard_video_Adidas"
country = client.map_country(OPERATOR_ENTITY)
log.info(f"Creative a crear: name={creative_name}, country={country}")

try:
    video_ready = client.wait_video_ready(
        video_id, initial_wait=60, retry_wait=30, max_retries=1,
    )
except StudioVideoNotReadyError as e:
    print(f"\n⚠️  VIDEO NO LISTO TRAS {e.elapsed_seconds}s — último estado: {e.last_state}")
    print(f"Video ID: {e.video_id} (guardado en {video_id_file})")
    print(f"En producción, el bot debería avisar en #csv-tickets de Slack:")
    print(f"  '⚠️ Procesado de Studio para {TICKET_KEY} está tardando — "
          f"video_id={e.video_id} sigue en {e.last_state} tras {e.elapsed_seconds}s.'")
    print(f"Re-ejecuta este script más tarde — saltará el upload y solo esperará.")
    sys.exit(2)

formats = video_ready.get("formats") or []
print(f"\n=== Vídeo COMPLETED. Formatos generados ({len(formats)}) ===")
for f in formats:
    print(f"  {f['width']}x{f['height']} {f['type']} @ {f['bitrate']}kbps")

# Diagnóstico
max_h = max((f["height"] for f in formats), default=0)
if max_h >= 1080:
    print(f"\n✅ Pipeline CTV correcto — formatos hasta {max_h}p")
else:
    print(f"\n⚠️  Pipeline NO devolvió CTV — máximo {max_h}p (debería ser 1080p)")

# ── 6. Creative ─────────────────────────────────────────────────────────────
ad_template = client.build_csv_ctv_ad_template(
    video_id=video_id, name=creative_name, formats=formats, country=country,
)
creative_id = client.create_cov_creative(ad_template)

print("\n" + "=" * 70)
print(f"video_id    : {video_id}")
print(f"creative_id : {creative_id}")
print(f"vast_url    : https://creatives.seedtag.com/vasts/{video_id}.xml")
print(f"preview_url : https://preview.seedtag.com/creative/{creative_id}")
print("=" * 70)
print("\nABRE el preview_url para confirmar visualmente.")
print("Cuando termines, borra MANUALMENTE el creative + video desde Studio.")
