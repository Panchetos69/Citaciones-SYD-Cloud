"""
Monitor de Citaciones — Senado + Camara de Diputados
=====================================================
Modos:
  monitor  -> scraping cada 10 min, detecta cambios, notifica Telegram
  sheets   -> llena Google Sheets semanal + email (domingo 20:00)
  prueba   -> genera PDF de prueba por Telegram (domingo 22:30)
  cerrar   -> lee prioridades del Sheets, genera PDF final (lunes 01:00)
  reporte  -> envia PDF final por Telegram (lunes 09:00)
"""

import hashlib
import html as html_lib
import logging
import os
import re
import smtplib
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from google.cloud import firestore
from google.oauth2 import service_account
from googleapiclient.discovery import build
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable, Image, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle
)

# ── Configuracion ──────────────────────────────────────────────────────────────
GCP_PROJECT         = "green-diagram-494113-u4"
FIRESTORE_COLECCION = "citaciones"
FIRESTORE_CONFIG    = "config"

TG_TOKEN   = os.environ.get("TG_TOKEN",   "8526676401:AAESmMiVjf7fKUi9bzcq0mMz2CJ0nzIIxxY")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "6396081535")

SMTP_HOST  = "smtp.gmail.com"
SMTP_PORT  = 587
EMAIL_FROM = os.environ.get("EMAIL_FROM", "fjossio@gmail.com")
SMTP_PASS  = os.environ.get("SMTP_PASS",  "yzgnacsbxjchjknp")
EMAILS_KOM = ["sergio@kom.cl", "francisco@kom.cl"]

CREDS_PATH      = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "credenciales.json")
SHEETS_ID_FIJO  = "18hQKwJzBseudqFrRAzO6dfu0q7JpsE6K_566R-R4p9A"
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "10c5912a5e8fddda31eeb47bfbba055e")

SCOPES_SHEETS = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

HEADERS_SENADO = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
}

DIAS_ES = {
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miercoles",
    "Thursday": "Jueves", "Friday": "Viernes",
    "Saturday": "Sabado", "Sunday": "Domingo"
}

MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio",
         "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


# ── Modelo ─────────────────────────────────────────────────────────────────────
@dataclass
class Citacion:
    fuente:     str
    comision:   str
    fecha:      str
    horario:    str
    sala:       str
    materia:    str
    suspendida: bool = False
    id:         str  = ""

    def __post_init__(self):
        clave   = f"{self.fuente}_{self.comision}_{self.fecha}_{self.horario}"
        self.id = hashlib.sha256(clave.encode()).hexdigest()[:16]

    def hash_contenido(self):
        return hashlib.md5(
            f"{self.horario}_{self.sala}_{self.suspendida}".encode()
        ).hexdigest()


# ── HTTP ───────────────────────────────────────────────────────────────────────
def get_html_camara(url):
    r = requests.get(
        "https://api.scraperapi.com/",
        params={"api_key": SCRAPER_API_KEY, "url": url},
        timeout=60
    )
    r.raise_for_status()
    return r.text


# ── Google Sheets ──────────────────────────────────────────────────────────────
def _sheets_svc():
    import os
    if os.path.exists(CREDS_PATH):
        creds = service_account.Credentials.from_service_account_file(
            CREDS_PATH, scopes=SCOPES_SHEETS
        )
    else:
        import google.auth
        creds, _ = google.auth.default(scopes=SCOPES_SHEETS)
    return build("sheets", "v4", credentials=creds)


# ── Telegram ───────────────────────────────────────────────────────────────────
def _get_chat_ids(db):
    try:
        usuarios = db.collection("bot_usuarios").where("activo", "==", True).stream()
        ids = [d.to_dict().get("chat_id") for d in usuarios]
        return ids if ids else [TG_CHAT_ID]
    except Exception:
        return [TG_CHAT_ID]


def _tg_texto(msg):
    try:
        db = firestore.Client(project=GCP_PROJECT)
        chat_ids = _get_chat_ids(db)
        for chat_id in chat_ids:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "HTML"},
                timeout=10
            )
        log.info(f"[Telegram] Texto enviado a {len(chat_ids)} usuarios")
    except Exception as e:
        log.error(f"Error Telegram: {e}")


def _tg_pdf(ruta, caption=""):
    try:
        db = firestore.Client(project=GCP_PROJECT)
        chat_ids = _get_chat_ids(db)
        for chat_id in chat_ids:
            with open(ruta, "rb") as f:
                requests.post(
                    f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
                    data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                    files={"document": f},
                    timeout=30
                )
        log.info(f"[Telegram] PDF enviado a {len(chat_ids)} usuarios")
    except Exception as e:
        log.error(f"Error Telegram PDF: {e}")


# ── Email ──────────────────────────────────────────────────────────────────────
def _enviar_email(destinatarios, asunto, cuerpo_html):
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = asunto
        msg["From"]    = EMAIL_FROM
        msg["To"]      = ", ".join(destinatarios)
        msg.attach(MIMEText(cuerpo_html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(EMAIL_FROM, SMTP_PASS)
            s.sendmail(EMAIL_FROM, destinatarios, msg.as_string())
        log.info(f"[Email] Enviado a {destinatarios}")
    except Exception as e:
        log.error(f"Error email: {e}")


# ── Fechas ─────────────────────────────────────────────────────────────────────
def parsear_fecha_senado(txt):
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", txt)
    return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}" if m else ""


def parsear_fecha_camara(txt):
    meses = {
        "ENERO":"01","FEBRERO":"02","MARZO":"03","ABRIL":"04",
        "MAYO":"05","JUNIO":"06","JULIO":"07","AGOSTO":"08",
        "SEPTIEMBRE":"09","OCTUBRE":"10","NOVIEMBRE":"11","DICIEMBRE":"12"
    }
    m = re.search(r"(\d{1,2})\s+DE\s+(\w+)\s+DE\s+(\d{4})", txt.upper())
    if m:
        return f"{m.group(3)}-{meses.get(m.group(2),'01')}-{m.group(1).zfill(2)}"
    return ""


def semana_actual():
    hoy = datetime.now()
    ref = hoy - timedelta(days=hoy.weekday()) if hoy.weekday() < 4 else hoy + timedelta(days=(7 - hoy.weekday()))
    y, w, _ = ref.isocalendar()
    return f"{y}-{w:02d}"


def semana_siguiente():
    hoy = datetime.now()
    ref = hoy - timedelta(days=hoy.weekday()) if hoy.weekday() < 4 else hoy + timedelta(days=(7 - hoy.weekday()))
    sig = ref + timedelta(days=7)
    y, w, _ = sig.isocalendar()
    return f"{y}-{w:02d}"


def extraer_tema(materia):
    for linea in materia.split("\n"):
        linea = linea.strip()
        if not linea or linea.startswith("A este") or linea.startswith("A esta"):
            continue
        if "Bol.N" in linea or "Bol. N" in linea:
            partes = linea.split(" ", 2)
            return partes[2][:800] if len(partes) > 2 else linea[:800]
        return linea[:800]
    return ""


# ── Scraper Senado ─────────────────────────────────────────────────────────────
def scrape_senado():
    citaciones = []
    try:
        r = requests.get(
            "https://web-back.senado.cl/api/commissions_citations?limit=100",
            headers=HEADERS_SENADO, timeout=20
        )
        r.raise_for_status()
        for dia in r.json().get("data", []):
            for c in dia.get("CITACIONES", []):
                fecha = parsear_fecha_senado(c.get("FECHA", ""))
                if not fecha:
                    continue
                citaciones.append(Citacion(
                    fuente     = "senado",
                    comision   = c.get("COMINOMBRE", "").strip(),
                    fecha      = fecha,
                    horario    = c.get("HORARIO", "").strip(),
                    sala       = c.get("LUGAR", "").strip(),
                    materia    = c.get("MATERIA", "").strip()[:2000],
                    suspendida = bool(c.get("SIN_EFECTO", 0)),
                ))
    except Exception as e:
        log.error(f"Error Senado: {e}")
    log.info(f"Senado: {len(citaciones)} citaciones")
    return citaciones


# ── Scraper Camara ─────────────────────────────────────────────────────────────
def scrape_camara(semana=None):
    if not semana:
        semana = semana_actual()
    citaciones = []
    try:
        url  = f"https://camara.cl/legislacion/comisiones/citaciones_semana.aspx?prmSemana={semana}"
        html = get_html_camara(url)
        soup = BeautifulSoup(html_lib.unescape(html), "html.parser")

        for article in soup.select("article.citaciones"):
            fecha = ""
            txt   = article.get_text()
            m     = re.search(r"(\d{1,2})\s+DE\s+(\w+)\s+DE\s+(\d{4})", txt.upper())
            if m:
                fecha = parsear_fecha_camara(m.group(0))
            if not fecha:
                continue

            for tr in article.select("table.tabla tbody tr"):
                celdas = tr.find_all("td", recursive=False)
                if len(celdas) < 3:
                    continue

                comision_raw = celdas[0].get_text(strip=True)
                p_rojo       = celdas[0].find("p", style=lambda s: s and "color:red" in s)
                suspendida   = bool(p_rojo and p_rojo.get_text(strip=True)) or \
                               "suspendida" in comision_raw.lower()
                comision     = re.sub(r"(?i)suspendida", "", comision_raw).strip()
                comision     = comision.split("\n")[0].strip()

                if not comision:
                    continue

                horario = celdas[1].get_text(strip=True) if len(celdas) > 1 else ""
                sala    = celdas[2].get_text(strip=True) if len(celdas) > 2 else ""
                materia = ""

                if len(celdas) > 3:
                    tabla_inner = celdas[3].find("table")
                    if tabla_inner:
                        cit_txt = []
                        inv_txt = []
                        for fila_inner in tabla_inner.find_all("tr"):
                            tds = fila_inner.find_all("td")
                            if len(tds) >= 1:
                                t = tds[0].get_text(separator=" ", strip=True)
                                if t:
                                    cit_txt.append(t)
                            if len(tds) >= 2:
                                t = tds[1].get_text(separator=" ", strip=True)
                                if t:
                                    inv_txt.append(t)
                        materia = "\n".join(cit_txt)
                        if inv_txt:
                            materia += "\n\nInvitados:\n" + "\n".join(inv_txt)

                citaciones.append(Citacion(
                    fuente     = "camara",
                    comision   = comision,
                    fecha      = fecha,
                    horario    = horario,
                    sala       = sala,
                    materia    = materia.strip()[:2000],
                    suspendida = suspendida,
                ))

    except Exception as e:
        log.error(f"Error Camara ({semana}): {e}")

    vistos, unicos = set(), []
    for c in citaciones:
        if c.id not in vistos:
            vistos.add(c.id)
            unicos.append(c)
    log.info(f"Camara ({semana}): {len(unicos)} citaciones")
    return unicos


# ── Firestore ──────────────────────────────────────────────────────────────────
def guardar_citacion(db, cit):
    ref        = db.collection(FIRESTORE_COLECCION).document(cit.id)
    doc        = ref.get()
    nuevo_hash = cit.hash_contenido()
    from zoneinfo import ZoneInfo
    hoy        = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")

    if not doc.exists:
        ref.set({
            "fuente": cit.fuente, "comision": cit.comision,
            "fecha": cit.fecha, "horario": cit.horario,
            "sala": cit.sala, "materia": cit.materia,
            "suspendida": cit.suspendida, "hash_contenido": nuevo_hash,
            "prioridad": False, "inicio_notificado": False,
            "en_resumen": False,
            "horario_anterior": cit.horario,
            "creada_en": datetime.now(timezone.utc),
            "actualizada_en": datetime.now(timezone.utc),
        })
        return "nueva"

    datos = doc.to_dict()
    if nuevo_hash == datos.get("hash_contenido", ""):
        return "sin_cambios"

    horario_cambio   = cit.horario != datos.get("horario", "")
    sala_cambio      = cit.sala    != datos.get("sala", "")
    suspendida_nueva = cit.suspendida and not datos.get("suspendida")

    if suspendida_nueva:
        tipo = "suspendida"
    elif horario_cambio or sala_cambio:
        tipo = "modificada"
    else:
        tipo = "sin_cambios"

    # No notificar cambios de sesiones pasadas
    if tipo in ("suspendida", "modificada") and cit.fecha < hoy:
        tipo = "sin_cambios"

    ref.update({
        "horario": cit.horario, "sala": cit.sala, "materia": cit.materia,
        "suspendida": cit.suspendida, "hash_contenido": nuevo_hash,
        "actualizada_en": datetime.now(timezone.utc),
        "horario_anterior": datos.get("horario", ""),
        "inicio_notificado": False if horario_cambio else datos.get("inicio_notificado", False),
    })
    return tipo


# ── Notificaciones ─────────────────────────────────────────────────────────────
def notificar_nueva(cit, en_resumen=False):
    """Solo notifica citaciones nuevas que NO estaban en el resumen semanal."""
    from zoneinfo import ZoneInfo
    hoy = datetime.now(ZoneInfo("America/Santiago")).strftime("%Y-%m-%d")
    if cit.fecha < hoy:
        return
    if en_resumen:
        return  # Ya fue incluida en el resumen del domingo, no spamear
    emoji = "🏛" if cit.fuente == "senado" else "🏦"
    inst  = "Senado" if cit.fuente == "senado" else "Camara"
    tema  = extraer_tema(cit.materia)
    msg   = (
        f"🆕 <b>Nueva citacion — {inst}</b>\n{'─'*28}\n"
        f"{emoji} <b>{cit.comision}</b>\n"
        f"📅 {cit.fecha}  🕐 {cit.horario}\n"
        f"📍 {cit.sala[:60]}\n"
    )
    if tema:
        msg += f"📌 {tema[:150]}"
    _tg_texto(msg)


def notificar_cambio(cit, tipo, horario_anterior=""):
    emoji = "🏛" if cit.fuente == "senado" else "🏦"
    inst  = "Senado" if cit.fuente == "senado" else "Camara"
    if tipo == "suspendida":
        msg = (
            f"❌ <b>Sesion SUSPENDIDA — {inst}</b>\n{'─'*28}\n"
            f"{emoji} <b>{cit.comision}</b>\n"
            f"📅 {cit.fecha}  🕐 {cit.horario}\n"
            f"📍 {cit.sala[:50]}"
        )
    else:
        cambio = ""
        if horario_anterior and horario_anterior != cit.horario:
            cambio = f"\n⏰ Horario: <s>{horario_anterior}</s> → <b>{cit.horario}</b>"
        msg = (
            f"⚠️ <b>Citacion modificada — {inst}</b>\n{'─'*28}\n"
            f"{emoji} <b>{cit.comision}</b>\n"
            f"📅 {cit.fecha}{cambio}\n"
            f"📍 {cit.sala[:50]}"
        )
    _tg_texto(msg)


def notificar_inicios(db):
    from zoneinfo import ZoneInfo
    ahora = datetime.now(ZoneInfo("America/Santiago"))
    hoy   = ahora.strftime("%Y-%m-%d")
    desde = ahora.hour * 60 + ahora.minute
    hasta = desde + 11

    try:
        docs = db.collection(FIRESTORE_COLECCION)\
                 .where("fecha", "==", hoy)\
                 .where("prioridad", "==", True)\
                 .stream()
    except Exception as e:
        log.error(f"Error Firestore inicios: {e}")
        return

    for doc in docs:
        c = doc.to_dict()
        if c.get("suspendida") or c.get("inicio_notificado"):
            continue
        horario = c.get("horario", "")
        try:
            h, m    = map(int, horario.split(" ")[0].split(":"))
            minutos = h * 60 + m
        except Exception:
            continue
        if desde <= minutos <= hasta:
            emoji     = "🏛" if c.get("fuente") == "senado" else "🏦"
            inst      = "Senado" if c.get("fuente") == "senado" else "Camara"
            mins_rest = minutos - desde
            tema      = extraer_tema(c.get("materia", ""))
            enc = f"🟢 <b>Sesion iniciando ahora — {inst}</b>" if mins_rest <= 1 \
                  else f"⏰ <b>En {mins_rest} min — {inst}</b>"
            msg = f"{enc}\n{'─'*28}\n{emoji} <b>{c.get('comision','')}</b>\n🕐 {horario}\n📍 {c.get('sala','')[:60]}\n"
            if tema:
                msg += f"📌 {tema[:200]}"
            _tg_texto(msg)
            doc.reference.update({"inicio_notificado": True})
            log.info(f"[INICIO] {c.get('comision','')} — {horario}")


# ── Google Sheets semanal ──────────────────────────────────────────────────────
def llenar_sheets_semanal(db):
    hoy        = datetime.now()
    num_semana = hoy.isocalendar()[1]
    docs = db.collection(FIRESTORE_COLECCION).stream()
    cits = [d.to_dict() for d in docs if not d.to_dict().get("suspendida")]
    if not cits:
        log.warning("No hay citaciones")
        return
    cits.sort(key=lambda x: (x.get("fecha",""), x.get("horario","")))
    svc = _sheets_svc()
    svc.spreadsheets().values().clear(spreadsheetId=SHEETS_ID_FIJO, range="A1:Z2000").execute()
    cabecera = [["PRIORIDAD (SI/NO)", "Institucion", "Comision", "Fecha", "Horario", "Sala", "Tema"]]
    filas = []
    for c in cits:
        inst  = "Senado" if c.get("fuente") == "senado" else "Camara de Diputados"
        fecha = c.get("fecha", "")
        try:
            dt    = datetime.strptime(fecha, "%Y-%m-%d")
            dia   = DIAS_ES.get(dt.strftime("%A"), dt.strftime("%A"))
            fecha = f"{dia} {dt.strftime('%d/%m/%Y')}"
        except Exception:
            pass
        filas.append(["NO", inst, c.get("comision",""), fecha,
                      c.get("horario",""), c.get("sala","")[:60], c.get("materia","")[:800]])
    svc.spreadsheets().values().update(
        spreadsheetId=SHEETS_ID_FIJO, range="A1",
        valueInputOption="RAW", body={"values": cabecera + filas}
    ).execute()
    log.info(f"Sheets llenado: {len(filas)} citaciones")
    n = len(filas) + 1
    svc.spreadsheets().batchUpdate(spreadsheetId=SHEETS_ID_FIJO, body={"requests": [
        {"repeatCell": {"range": {"sheetId":0,"startRowIndex":0,"endRowIndex":1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red":0.176,"green":0.169,"blue":0.42},
                "textFormat": {"foregroundColor":{"red":1,"green":1,"blue":1},"bold":True,"fontSize":11},
                "horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId":0,"startRowIndex":1,"endRowIndex":n,"startColumnIndex":0,"endColumnIndex":1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red":1,"green":0.98,"blue":0.8},
                "horizontalAlignment": "CENTER","textFormat":{"bold":True}}},
            "fields": "userEnteredFormat"}},
        {"updateDimensionProperties": {"range":{"sheetId":0,"dimension":"COLUMNS","startIndex":0,"endIndex":1},
            "properties":{"pixelSize":150},"fields":"pixelSize"}},
        {"updateDimensionProperties": {"range":{"sheetId":0,"dimension":"COLUMNS","startIndex":2,"endIndex":3},
            "properties":{"pixelSize":200},"fields":"pixelSize"}},
        {"updateDimensionProperties": {"range":{"sheetId":0,"dimension":"COLUMNS","startIndex":6,"endIndex":7},
            "properties":{"pixelSize":350},"fields":"pixelSize"}},
        {"updateSheetProperties": {"properties":{"sheetId":0,"gridProperties":{"frozenRowCount":1}},
            "fields":"gridProperties.frozenRowCount"}},
    ]}).execute()
    link = f"https://docs.google.com/spreadsheets/d/{SHEETS_ID_FIJO}/edit"
    db.collection(FIRESTORE_CONFIG).document("sheets_semana").set({
        "sheets_id": SHEETS_ID_FIJO, "semana": num_semana,
        "link": link, "creado_en": datetime.now(timezone.utc),
    })

    # ── CLAVE: marcar todas las citaciones como ya incluidas en el resumen ──
    for doc in db.collection(FIRESTORE_COLECCION).stream():
        doc.reference.update({"en_resumen": True})
    log.info("Todas las citaciones marcadas como en_resumen=True")

    asunto = f"Agenda Legislativa Semana {num_semana} — Marcar prioridades antes del lunes"
    cuerpo = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:0 auto;">
      <div style="background:#2D2B6B;padding:20px;border-radius:8px 8px 0 0;">
        <h1 style="color:white;margin:0;font-size:20px;">Agenda Legislativa — Semana {num_semana}</h1>
        <p style="color:#00F5A0;margin:5px 0 0 0;">{hoy.strftime('%d de %B de %Y')}</p>
      </div>
      <div style="background:#f9f9f9;padding:24px;border:1px solid #e0e0e0;">
        <p>Hola equipo, se han recopilado <b>{len(filas)} citaciones</b> para la semana.</p>
        <p>Cambien <b>NO a SI</b> en la columna PRIORIDAD para marcar sesiones.</p>
        <div style="text-align:center;margin:30px 0;">
          <a href="{link}" style="background:#2D2B6B;color:white;padding:14px 28px;
             border-radius:6px;text-decoration:none;font-weight:bold;font-size:16px;">
            Abrir Agenda en Google Sheets
          </a>
        </div>
        <p style="color:#666;font-size:13px;"><b>Plazo:</b> Cierre automatico el lunes a la 01:00 AM.</p>
        <hr style="border:none;border-top:1px solid #e0e0e0;margin:20px 0;">
        <p style="color:#999;font-size:11px;text-align:center;">KOM — Sistema de Monitoreo Legislativo</p>
      </div>
    </div>"""
    _enviar_email(EMAILS_KOM, asunto, cuerpo)
    log.info(f"Email enviado: {link}")
    return link


# ── Cerrar semana ──────────────────────────────────────────────────────────────
def cerrar_semana(db):
    svc = _sheets_svc()
    try:
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEETS_ID_FIJO, range="A2:G2000"
        ).execute()
        filas = result.get("values", [])
    except Exception as e:
        log.error(f"Error leyendo Sheets: {e}")
        return None

    docs      = db.collection(FIRESTORE_COLECCION).stream()
    cits_dict = {d.id: d.to_dict() for d in docs}

    for doc_id in cits_dict:
        db.collection(FIRESTORE_COLECCION).document(doc_id).update({
            "prioridad": False, "inicio_notificado": False,
        })

    priorizadas = 0
    for fila in filas:
        if not fila:
            continue
        val = fila[0].strip().upper()
        if val not in ("SI", "SÍ", "YES", "S", "1"):
            continue
        inst      = fila[1].strip() if len(fila) > 1 else ""
        comision  = fila[2].strip() if len(fila) > 2 else ""
        fecha_raw = fila[3].strip() if len(fila) > 3 else ""
        fuente    = "senado" if "Senado" in inst else "camara"
        fecha_iso = ""
        try:
            partes    = fecha_raw.split(" ")
            fecha_str = partes[-1] if len(partes) > 1 else fecha_raw
            dt        = datetime.strptime(fecha_str, "%d/%m/%Y")
            fecha_iso = dt.strftime("%Y-%m-%d")
        except Exception:
            pass
        for doc_id, c in cits_dict.items():
            if (c.get("fuente") == fuente and c.get("comision") == comision and
                    (not fecha_iso or c.get("fecha") == fecha_iso)):
                db.collection(FIRESTORE_COLECCION).document(doc_id).update({
                    "prioridad": True, "inicio_notificado": False,
                })
                priorizadas += 1
                break

    log.info(f"Prioridades actualizadas: {priorizadas} sesiones")
    docs_upd = db.collection(FIRESTORE_COLECCION).stream()
    cits     = [d.to_dict() for d in docs_upd]
    hoy      = datetime.now()
    fin      = hoy + timedelta(days=(4 - hoy.weekday() % 7))
    num_sem  = hoy.isocalendar()[1]
    ruta_pdf = generar_pdf(cits, num_sem, hoy.strftime("%d/%m"), fin.strftime("%d/%m/%Y"))
    if ruta_pdf:
        db.collection(FIRESTORE_CONFIG).document("pdf_semana").set({
            "ruta_local": ruta_pdf, "semana": num_sem,
            "creado_en": datetime.now(timezone.utc),
        })
    return ruta_pdf


# ── PDF ejecutivo ──────────────────────────────────────────────────────────────
def generar_pdf(cits, num_semana, fecha_inicio, fecha_fin):
    KOM_MORADO = colors.HexColor("#2D2B6B")
    KOM_VERDE  = colors.HexColor("#00F5A0")
    SENADO_BG  = colors.HexColor("#F8F7FF")
    SENADO_ACC = colors.HexColor("#2D2B6B")
    CAMARA_BG  = colors.HexColor("#F0FBF7")
    CAMARA_ACC = colors.HexColor("#00A878")
    GRIS_CLARO = colors.HexColor("#F5F5F5")
    GRIS_BORDE = colors.HexColor("#E8E8E8")
    GRIS_TEXTO = colors.HexColor("#666666")
    GRIS_MUTED = colors.HexColor("#AAAAAA")
    BADGE_S_BG = colors.HexColor("#EEEDFE")
    BADGE_S_TX = colors.HexColor("#3C3489")
    BADGE_C_BG = colors.HexColor("#E1F5EE")
    BADGE_C_TX = colors.HexColor("#085041")
    ROJO       = colors.HexColor("#C0392B")

    tmp      = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    ruta_pdf = tmp.name
    tmp.close()

    W       = A4[0]
    INNER_W = W - 3 * cm

    doc     = SimpleDocTemplate(ruta_pdf, pagesize=A4,
                                rightMargin=0, leftMargin=0,
                                topMargin=0, bottomMargin=1.5*cm)
    estilos = getSampleStyleSheet()

    def E(name, **kw):
        return ParagraphStyle(name, parent=estilos["Normal"], **kw)

    e_ht    = E("ht",    fontSize=16, fontName="Helvetica-Bold", textColor=colors.white, leading=20)
    e_hs    = E("hs",    fontSize=10, textColor=KOM_VERDE, leading=14)
    e_sn    = E("sn",    fontSize=20, fontName="Helvetica-Bold", textColor=KOM_MORADO, alignment=TA_CENTER, leading=24)
    e_sg    = E("sg",    fontSize=20, fontName="Helvetica-Bold", textColor=CAMARA_ACC, alignment=TA_CENTER, leading=24)
    e_sr    = E("sr",    fontSize=20, fontName="Helvetica-Bold", textColor=ROJO, alignment=TA_CENTER, leading=24)
    e_sl    = E("sl",    fontSize=8,  textColor=GRIS_MUTED, alignment=TA_CENTER, leading=10)
    e_sec   = E("sec",   fontSize=8,  fontName="Helvetica-Bold", textColor=KOM_MORADO, leading=10)
    e_day   = E("day",   fontSize=9,  fontName="Helvetica-Bold", textColor=GRIS_MUTED, leading=12)
    e_bs    = E("bs",    fontSize=7,  fontName="Helvetica-Bold", textColor=BADGE_S_TX, leading=9)
    e_bc    = E("bc",    fontSize=7,  fontName="Helvetica-Bold", textColor=BADGE_C_TX, leading=9)
    e_sname = E("sname", fontSize=10, fontName="Helvetica-Bold", textColor=colors.HexColor("#1a1a2e"), leading=13)
    e_stema = E("stema", fontSize=7.5, textColor=GRIS_TEXTO, leading=11)
    e_sts   = E("sts",   fontSize=9,  fontName="Helvetica-Bold", textColor=KOM_MORADO, leading=11)
    e_stc   = E("stc",   fontSize=9,  fontName="Helvetica-Bold", textColor=CAMARA_ACC, leading=11)
    e_rl    = E("rl",    fontSize=8,  fontName="Helvetica-Bold", textColor=GRIS_MUTED, leading=10)
    e_rn    = E("rn",    fontSize=9,  textColor=colors.HexColor("#333333"), leading=11)
    e_rh    = E("rh",    fontSize=8.5, textColor=GRIS_MUTED, alignment=TA_RIGHT, leading=11)
    e_ft    = E("ft",    fontSize=7.5, textColor=GRIS_MUTED, leading=10)

    elementos = []

    LOGO      = "LOGO_KOM_BLANCO_Sin_fondo.png"
    logo_cell = Image(LOGO, width=3.2*cm, height=1.4*cm) if os.path.exists(LOGO) else \
                Paragraph("KOM", E("lt", fontSize=18, fontName="Helvetica-Bold", textColor=colors.white))

    cab = Table([[
        [Paragraph("Agenda Legislativa", e_ht),
         Paragraph(f"Semana {num_semana}  ·  {fecha_inicio} al {fecha_fin}", e_hs)],
        logo_cell
    ]], colWidths=[W-4.5*cm, 4.5*cm])
    cab.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), KOM_MORADO),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("LEFTPADDING",   (0,0),(0,0),   24),
        ("RIGHTPADDING",  (1,0),(1,0),   20),
        ("TOPPADDING",    (0,0),(-1,-1), 18),
        ("BOTTOMPADDING", (0,0),(-1,-1), 18),
        ("ALIGN",         (1,0),(1,0),   "RIGHT"),
    ]))
    elementos.append(cab)

    total_s   = sum(1 for c in cits if c.get("fuente")=="senado" and not c.get("suspendida"))
    total_c   = sum(1 for c in cits if c.get("fuente")=="camara" and not c.get("suspendida"))
    priorit   = sum(1 for c in cits if c.get("prioridad"))
    suspendid = sum(1 for c in cits if c.get("suspendida"))

    stats = Table([[
        [Paragraph(str(total_s),   e_sn), Paragraph("SENADO",      e_sl)],
        [Paragraph(str(total_c),   e_sn), Paragraph("CAMARA",      e_sl)],
        [Paragraph(str(priorit),   e_sg), Paragraph("PRIORIZADAS", e_sl)],
        [Paragraph(str(suspendid), e_sr), Paragraph("SUSPENDIDAS", e_sl)],
    ]], colWidths=[W/4]*4)
    stats.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(-1,-1), colors.white),
        ("TOPPADDING",    (0,0),(-1,-1), 10),
        ("BOTTOMPADDING", (0,0),(-1,-1), 10),
        ("LINEAFTER",     (0,0),(2,0),   0.5, GRIS_BORDE),
        ("LINEBELOW",     (0,0),(-1,-1), 0.5, GRIS_BORDE),
    ]))
    elementos.append(stats)
    elementos.append(Spacer(1, 14))

    priorizadas = [c for c in cits if c.get("prioridad") and not c.get("suspendida")]
    if priorizadas:
        elementos.append(Table([[Paragraph("SESIONES PRIORIZADAS", e_sec)]],
            colWidths=[INNER_W],
            style=[("LEFTPADDING",(0,0),(-1,-1),1.5*cm),("BOTTOMPADDING",(0,0),(-1,-1),2)]))
        elementos.append(Table([[HRFlowable(width=INNER_W, thickness=0.5, color=KOM_MORADO)]],
            colWidths=[INNER_W],
            style=[("LEFTPADDING",(0,0),(-1,-1),1.5*cm),("BOTTOMPADDING",(0,0),(-1,-1),8)]))

        por_fecha = {}
        for c in priorizadas:
            f = c.get("fecha","")
            if f not in por_fecha:
                por_fecha[f] = []
            por_fecha[f].append(c)

        for fecha in sorted(por_fecha.keys()):
            try:
                dt    = datetime.strptime(fecha, "%Y-%m-%d")
                label = f"{DIAS_ES.get(dt.strftime('%A'),'')} {dt.day} de {MESES[dt.month]}".upper()
            except Exception:
                label = fecha

            elementos.append(Table([[Paragraph(label, e_day)]],
                colWidths=[INNER_W],
                style=[("LEFTPADDING",(0,0),(-1,-1),1.5*cm),("BOTTOMPADDING",(0,0),(-1,-1),4)]))

            for c in sorted(por_fecha[fecha], key=lambda x: x.get("horario","")):
                fuente   = c.get("fuente","senado")
                es_s     = fuente == "senado"
                comision = c.get("comision","").strip()
                horario  = c.get("horario","")
                tema     = extraer_tema(c.get("materia",""))

                badge = Table([[Paragraph("SENADO" if es_s else "CAMARA", e_bs if es_s else e_bc)]],
                    colWidths=[1.5*cm],
                    style=[("BACKGROUND",(0,0),(-1,-1),BADGE_S_BG if es_s else BADGE_C_BG),
                           ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1),
                           ("LEFTPADDING",(0,0),(-1,-1),4),("RIGHTPADDING",(0,0),(-1,-1),4)])

                info = [badge, Paragraph(comision, e_sname)]
                if tema:
                    info.append(Paragraph(tema[:800], e_stema))

                card = Table([[Paragraph(horario, e_sts if es_s else e_stc), info]],
                    colWidths=[2.2*cm, INNER_W-2.8*cm])
                card.setStyle(TableStyle([
                    ("VALIGN",(0,0),(-1,-1),"TOP"),
                    ("LEFTPADDING",(0,0),(0,0),10),("RIGHTPADDING",(1,0),(1,0),10),
                    ("TOPPADDING",(0,0),(-1,-1),8),("BOTTOMPADDING",(0,0),(-1,-1),8),
                    ("BACKGROUND",(0,0),(-1,-1),SENADO_BG if es_s else CAMARA_BG),
                    ("LINEBEFORE",(0,0),(0,-1),3,SENADO_ACC if es_s else CAMARA_ACC),
                ]))
                elementos.append(Table([[card]], colWidths=[INNER_W],
                    style=[("LEFTPADDING",(0,0),(-1,-1),1.5*cm),
                           ("RIGHTPADDING",(0,0),(-1,-1),1.5*cm),
                           ("BOTTOMPADDING",(0,0),(-1,-1),5),
                           ("TOPPADDING",(0,0),(-1,-1),0)]))
        elementos.append(Spacer(1, 14))

    resto = [c for c in cits if not c.get("prioridad") and not c.get("suspendida")]
    if resto:
        elementos.append(Table([[Paragraph(f"RESTO DE LA AGENDA  ({len(resto)} sesiones)", e_rl)]],
            colWidths=[INNER_W],
            style=[("LEFTPADDING",(0,0),(-1,-1),1.5*cm),("BOTTOMPADDING",(0,0),(-1,-1),6)]))

        filas_r = []
        for c in sorted(resto, key=lambda x: (x.get("fecha",""), x.get("horario",""))):
            fuente = c.get("fuente","")
            fecha  = c.get("fecha","")
            try:
                dt        = datetime.strptime(fecha, "%Y-%m-%d")
                fecha_fmt = f"{DIAS_ES.get(dt.strftime('%A'),'')[:3]} {dt.strftime('%d/%m')}"
            except Exception:
                fecha_fmt = fecha

            inst_lbl = "S" if fuente=="senado" else "C"
            inst_bg  = BADGE_S_BG if fuente=="senado" else BADGE_C_BG
            inst_tx  = BADGE_S_TX if fuente=="senado" else BADGE_C_TX

            badge_r = Table([[Paragraph(inst_lbl, ParagraphStyle("rb", parent=estilos["Normal"],
                             fontSize=7, fontName="Helvetica-Bold", textColor=inst_tx, alignment=TA_CENTER))]],
                colWidths=[0.4*cm],
                style=[("BACKGROUND",(0,0),(-1,-1),inst_bg),
                       ("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1),
                       ("LEFTPADDING",(0,0),(-1,-1),2),("RIGHTPADDING",(0,0),(-1,-1),2)])
            filas_r.append([badge_r,
                            Paragraph(c.get("comision","").strip(), e_rn),
                            Paragraph(f"{fecha_fmt}  {c.get('horario','')}", e_rh)])

        if filas_r:
            COL1 = 0.6*cm; COL3 = 3.5*cm; COL2 = INNER_W - COL1 - COL3
            t = Table(filas_r, colWidths=[COL1, COL2, COL3], splitByRow=True)
            t.setStyle(TableStyle([
                ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
                ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
                ("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),
                ("LINEBELOW",(0,0),(-1,-2),0.5,GRIS_BORDE),
                ("ROWBACKGROUNDS",(0,0),(-1,-1),[colors.white, GRIS_CLARO]),
            ]))
            elementos.append(t)

    elementos.append(Spacer(1, 16))
    pie = Table([[
        Paragraph("Generado automaticamente · KOM Sistema Legislativo", e_ft),
        Paragraph(f"{datetime.now().strftime('%d/%m/%Y')}  ·  senado.cl / camara.cl", e_ft)
    ]], colWidths=[INNER_W/2, INNER_W/2])
    pie.setStyle(TableStyle([
        ("ALIGN",(1,0),(1,0),"RIGHT"),
        ("LINEABOVE",(0,0),(-1,-1),0.5,GRIS_BORDE),
        ("TOPPADDING",(0,0),(-1,-1),8),
        ("LEFTPADDING",(0,0),(0,0),1.5*cm),
        ("RIGHTPADDING",(1,0),(1,0),1.5*cm),
    ]))
    elementos.append(pie)

    doc.build(elementos)
    log.info(f"PDF generado: {ruta_pdf}")
    return ruta_pdf


# ── Reporte ────────────────────────────────────────────────────────────────────
def enviar_reporte_final(db, es_prueba=False):
    hoy       = datetime.now()
    cits      = [d.to_dict() for d in db.collection(FIRESTORE_COLECCION).stream()]
    total_s   = sum(1 for c in cits if c.get("fuente")=="senado" and not c.get("suspendida"))
    total_c   = sum(1 for c in cits if c.get("fuente")=="camara" and not c.get("suspendida"))
    priorit   = sum(1 for c in cits if c.get("prioridad"))
    suspendid = sum(1 for c in cits if c.get("suspendida"))
    num_sem   = hoy.isocalendar()[1]
    fin       = hoy + timedelta(days=(4 - hoy.weekday() % 7))
    ruta_pdf  = generar_pdf(cits, num_sem, hoy.strftime("%d/%m"), fin.strftime("%d/%m/%Y"))

    prefijo = "🔍 <b>PRUEBA</b> — " if es_prueba else ""
    caption = (
        f"{prefijo}📅 <b>Agenda Legislativa — Semana {num_sem}</b>\n"
        f"<i>{hoy.strftime('%d/%m/%Y')}</i>\n\n"
        f"🏛 Senado: {total_s} sesiones\n"
        f"🏦 Camara: {total_c} sesiones\n"
        f"⭐ Priorizadas: {priorit}\n"
        f"❌ Suspendidas: {suspendid}"
    )
    if es_prueba:
        caption += "\n\n<i>Reporte de prueba. El definitivo se envia el lunes 09:00.</i>"

    if ruta_pdf:
        _tg_pdf(ruta_pdf, caption)
        if not es_prueba:
            db.collection(FIRESTORE_CONFIG).document("pdf_semana").set({
                "ruta_local": ruta_pdf, "semana": num_sem,
                "creado_en": datetime.now(timezone.utc),
            })
        try:
            os.remove(ruta_pdf)
        except Exception:
            pass
    log.info(f"Reporte {'prueba' if es_prueba else 'final'} enviado")


# ── Main ───────────────────────────────────────────────────────────────────────
def main(modo="monitor"):
    db = firestore.Client(project=GCP_PROJECT)

    if modo == "sheets":
        log.info("Llenando Sheets...")
        llenar_sheets_semanal(db)
        return

    if modo == "prueba":
        log.info("Enviando reporte de prueba...")
        enviar_reporte_final(db, es_prueba=True)
        return

    if modo == "cerrar":
        log.info("Cerrando semana...")
        cerrar_semana(db)
        return

    if modo == "reporte":
        log.info("Enviando reporte final...")
        enviar_reporte_final(db, es_prueba=False)
        return

    # monitor
    log.info("Iniciando monitor...")
    todas = scrape_senado()
    for sem in [semana_actual(), semana_siguiente()]:
        todas += scrape_camara(semana=sem)

    log.info(f"Total scraped: {len(todas)}")
    nuevas = modificadas = suspendidas = 0

    for cit in todas:
        try:
            res = guardar_citacion(db, cit)
            if res == "nueva":
                nuevas += 1
                # Solo notificar si NO estaba en el resumen semanal
                d = db.collection(FIRESTORE_COLECCION).document(cit.id).get().to_dict()
                notificar_nueva(cit, en_resumen=d.get("en_resumen", False))
            elif res == "modificada":
                modificadas += 1
                d = db.collection(FIRESTORE_COLECCION).document(cit.id).get().to_dict()
                notificar_cambio(cit, "modificada", d.get("horario_anterior",""))
            elif res == "suspendida":
                suspendidas += 1
                notificar_cambio(cit, "suspendida")
        except Exception as e:
            log.error(f"Error {cit.comision}: {e}")

    notificar_inicios(db)
    log.info(f"Resumen: {nuevas} nuevas | {modificadas} mod | {suspendidas} susp")


if __name__ == "__main__":
    main(modo=sys.argv[1] if len(sys.argv) > 1 else "monitor")