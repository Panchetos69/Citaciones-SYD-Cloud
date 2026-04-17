"""
Monitor de Citaciones — Senado + Cámara de Diputados
=====================================================
- Scraping cada 10 minutos
- Detecta nuevas citaciones, cambios de horario y suspensiones
- Notifica por Telegram
- Reporte semanal los lunes a las 09:00

Fuentes:
  Senado : https://web-back.senado.cl/api/commissions_citations?limit=100
  Cámara : https://camara.cl/legislacion/comisiones/citaciones_semana.aspx?prmSemana=YYYY-WW
"""

import hashlib
import html as html_lib
import json
import logging
import os
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from google.cloud import firestore

# ── Configuración ──────────────────────────────────────────────────────────────
GCP_PROJECT         = "crack-map-317501"
FIRESTORE_COLECCION = "citaciones"
TG_TOKEN            = os.environ.get("TG_TOKEN", "8559495172:AAFv_ZDz1c2CIn2MgB5SwUjSU1zrKzkFx6M")
TG_CHAT_ID          = os.environ.get("TG_CHAT_ID", "6396081535")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Modelo ─────────────────────────────────────────────────────────────────────
@dataclass
class Citacion:
    fuente:    str        # "senado" | "camara"
    comision:  str
    fecha:     str        # "2026-04-21"
    horario:   str        # "10:00 a 12:30"
    sala:      str
    materia:   str
    suspendida: bool = False
    id:        str = ""

    def __post_init__(self):
        # ID estable basado en fuente + comisión + fecha
        clave = f"{self.fuente}_{self.comision}_{self.fecha}"
        self.id = hashlib.sha256(clave.encode()).hexdigest()[:16]

    def hash_contenido(self) -> str:
        """Hash del contenido mutable — detecta cambios."""
        contenido = f"{self.horario}_{self.sala}_{self.suspendida}"
        return hashlib.md5(contenido.encode()).hexdigest()


# ── Telegram ───────────────────────────────────────────────────────────────────
def _telegram(mensaje: str):
    try:
        url  = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id":    TG_CHAT_ID,
            "text":       mensaje,
            "parse_mode": "HTML"
        }, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram error: {resp.text}")
        else:
            log.info(f"[Telegram] Mensaje enviado")
    except Exception as e:
        log.error(f"Error Telegram: {e}")


# ── Helpers de fecha ───────────────────────────────────────────────────────────
def parsear_fecha_senado(texto: str) -> str:
    """'20/04/2026' → '2026-04-20'"""
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", texto)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return texto


def parsear_fecha_camara(texto: str) -> str:
    """'LUNES, 21 DE ABRIL DE 2026' → '2026-04-21'"""
    meses = {
        "ENERO": "01", "FEBRERO": "02", "MARZO": "03", "ABRIL": "04",
        "MAYO": "05", "JUNIO": "06", "JULIO": "07", "AGOSTO": "08",
        "SEPTIEMBRE": "09", "OCTUBRE": "10", "NOVIEMBRE": "11", "DICIEMBRE": "12"
    }
    m = re.search(r"(\d{1,2})\s+DE\s+(\w+)\s+DE\s+(\d{4})", texto.upper())
    if m:
        mes = meses.get(m.group(2), "01")
        return f"{m.group(3)}-{mes}-{m.group(1).zfill(2)}"
    return texto


def semana_siguiente() -> str:
    """Devuelve el número de semana ISO de la semana siguiente."""
    hoy        = datetime.now()
    prox_lunes = hoy + timedelta(days=(7 - hoy.weekday()))
    return prox_lunes.strftime("%Y-%W")


def semana_actual() -> str:
    return datetime.now().strftime("%Y-%W")


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPER SENADO
# ══════════════════════════════════════════════════════════════════════════════
URL_SENADO_API = "https://web-back.senado.cl/api/commissions_citations?limit=100"


def scrape_senado() -> list[Citacion]:
    """
    Llama a la API JSON del Senado.
    SIN_EFECTO=1 significa suspendida.
    """
    citaciones = []
    try:
        r    = requests.get(URL_SENADO_API, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()

        for dia in data.get("data", []):
            for c in dia.get("CITACIONES", []):
                fecha = parsear_fecha_senado(c.get("FECHA", ""))
                citaciones.append(Citacion(
                    fuente     = "senado",
                    comision   = c.get("COMINOMBRE", "").strip(),
                    fecha      = fecha,
                    horario    = c.get("HORARIO", "").strip(),
                    sala       = c.get("LUGAR", "").strip(),
                    materia    = c.get("MATERIA", "").strip()[:500],
                    suspendida = bool(c.get("SIN_EFECTO", 0)),
                ))

    except Exception as e:
        log.error(f"Error scrapeando Senado: {e}")

    log.info(f"Senado: {len(citaciones)} citaciones")
    return citaciones


# ══════════════════════════════════════════════════════════════════════════════
# SCRAPER CÁMARA
# ══════════════════════════════════════════════════════════════════════════════
BASE_CAMARA = "https://camara.cl/legislacion/comisiones/citaciones_semana.aspx"


def scrape_camara(semana: str = None) -> list[Citacion]:
    """
    Scraping de la tabla HTML de la Cámara.
    semana: '2026-17' — si None usa la semana actual.
    """
    if not semana:
        semana = semana_actual()

    url       = f"{BASE_CAMARA}?prmSemana={semana}"
    citaciones = []

    try:
        r    = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(html_lib.unescape(r.text), "html.parser")

        for article in soup.select("article.grid-12.citaciones"):
            fecha_tag = article.select_one("p.fecha")
            if not fecha_tag:
                continue
            fecha = parsear_fecha_camara(fecha_tag.get_text(strip=True))

            for tr in article.select("table.tabla tbody tr"):
                celdas = tr.select("td")
                if len(celdas) < 3:
                    continue

                comision_raw = celdas[0].get_text(strip=True)
                suspendida   = "suspendida" in comision_raw.lower()
                comision     = re.sub(r"(?i)suspendida", "", comision_raw).strip()

                horario  = celdas[1].get_text(strip=True) if len(celdas) > 1 else ""
                sala     = celdas[2].get_text(strip=True) if len(celdas) > 2 else ""
                materia  = celdas[3].get_text(strip=True)[:500] if len(celdas) > 3 else ""

                if not comision or not fecha:
                    continue

                citaciones.append(Citacion(
                    fuente     = "camara",
                    comision   = comision,
                    fecha      = fecha,
                    horario    = horario,
                    sala       = sala,
                    materia    = materia,
                    suspendida = suspendida,
                ))

    except Exception as e:
        log.error(f"Error scrapeando Cámara semana {semana}: {e}")

    # Eliminar duplicados
    vistos, unicos = set(), []
    for c in citaciones:
        if c.id not in vistos:
            vistos.add(c.id)
            unicos.append(c)

    log.info(f"Cámara ({semana}): {len(unicos)} citaciones")
    return unicos


# ══════════════════════════════════════════════════════════════════════════════
# FIRESTORE
# ══════════════════════════════════════════════════════════════════════════════
def guardar_citacion(db: firestore.Client, cit: Citacion) -> str:
    """
    Guarda o actualiza la citación.
    Devuelve: 'nueva' | 'modificada' | 'suspendida' | 'sin_cambios'
    """
    ref = db.collection(FIRESTORE_COLECCION).document(cit.id)
    doc = ref.get()

    nuevo_hash = cit.hash_contenido()

    if not doc.exists:
        ref.set({
            "fuente":         cit.fuente,
            "comision":       cit.comision,
            "fecha":          cit.fecha,
            "horario":        cit.horario,
            "sala":           cit.sala,
            "materia":        cit.materia,
            "suspendida":     cit.suspendida,
            "hash_contenido": nuevo_hash,
            "creada_en":      datetime.now(timezone.utc),
            "actualizada_en": datetime.now(timezone.utc),
        })
        return "nueva"

    datos_prev = doc.to_dict()
    hash_prev  = datos_prev.get("hash_contenido", "")

    if nuevo_hash == hash_prev:
        return "sin_cambios"

    # Detectar qué cambió
    if cit.suspendida and not datos_prev.get("suspendida", False):
        tipo_cambio = "suspendida"
    else:
        tipo_cambio = "modificada"

    ref.update({
        "horario":        cit.horario,
        "sala":           cit.sala,
        "materia":        cit.materia,
        "suspendida":     cit.suspendida,
        "hash_contenido": nuevo_hash,
        "actualizada_en": datetime.now(timezone.utc),
        "horario_anterior": datos_prev.get("horario", ""),
        "sala_anterior":    datos_prev.get("sala", ""),
    })
    return tipo_cambio


# ══════════════════════════════════════════════════════════════════════════════
# NOTIFICACIONES
# ══════════════════════════════════════════════════════════════════════════════
EMOJI = {
    "senado": "🏛",
    "camara": "🏦",
}

def _formato_citacion(cit: Citacion) -> str:
    estado = "❌ SUSPENDIDA" if cit.suspendida else f"🕐 {cit.horario}"
    return (
        f"{EMOJI.get(cit.fuente, '📋')} <b>{cit.comision}</b>\n"
        f"📅 {cit.fecha}  {estado}\n"
        f"📍 {cit.sala}\n"
        f"📝 {cit.materia[:200]}{'...' if len(cit.materia) > 200 else ''}"
    )


def notificar_nueva(cit: Citacion):
    msg = (
        f"🆕 <b>Nueva citación detectada</b>\n"
        f"{'─' * 30}\n"
        f"{_formato_citacion(cit)}"
    )
    _telegram(msg)


def notificar_cambio(cit: Citacion, tipo: str, horario_anterior: str = ""):
    if tipo == "suspendida":
        msg = (
            f"❌ <b>Sesión SUSPENDIDA</b>\n"
            f"{'─' * 30}\n"
            f"{EMOJI.get(cit.fuente, '📋')} <b>{cit.comision}</b>\n"
            f"📅 {cit.fecha}\n"
            f"📍 {cit.sala}"
        )
    else:
        cambio_horario = (
            f"\n⏰ Horario: <s>{horario_anterior}</s> → <b>{cit.horario}</b>"
            if horario_anterior and horario_anterior != cit.horario
            else ""
        )
        msg = (
            f"⚠️ <b>Citación modificada</b>\n"
            f"{'─' * 30}\n"
            f"{EMOJI.get(cit.fuente, '📋')} <b>{cit.comision}</b>\n"
            f"📅 {cit.fecha}{cambio_horario}\n"
            f"📍 {cit.sala}"
        )
    _telegram(msg)


def enviar_reporte_semanal(db: firestore.Client):
    """
    Genera y envía el resumen semanal por Telegram.
    Se llama los lunes a las 09:00.
    """
    hoy       = datetime.now()
    fin_semana = hoy + timedelta(days=(4 - hoy.weekday()))  # viernes

    # Obtener citaciones de la semana desde Firestore
    docs = db.collection(FIRESTORE_COLECCION)\
             .where("fecha", ">=", hoy.strftime("%Y-%m-%d"))\
             .where("fecha", "<=", fin_semana.strftime("%Y-%m-%d"))\
             .order_by("fecha")\
             .stream()

    citaciones = [d.to_dict() for d in docs]

    if not citaciones:
        _telegram("📅 <b>Agenda semanal</b>\n\nNo hay citaciones registradas para esta semana.")
        return

    # Agrupar por fecha
    por_fecha = {}
    for c in citaciones:
        if c.get("suspendida"):
            continue
        fecha = c.get("fecha", "")
        if fecha not in por_fecha:
            por_fecha[fecha] = []
        por_fecha[fecha].append(c)

    # Construir mensaje
    lineas = [f"📅 <b>Agenda Legislativa — Semana del {hoy.strftime('%d/%m')} al {fin_semana.strftime('%d/%m/%Y')}</b>\n"]

    dias_es = {
        "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
        "Thursday": "Jueves", "Friday": "Viernes"
    }

    for fecha in sorted(por_fecha.keys()):
        dt   = datetime.strptime(fecha, "%Y-%m-%d")
        dia  = dias_es.get(dt.strftime("%A"), dt.strftime("%A"))
        lineas.append(f"\n<b>── {dia} {dt.strftime('%d/%m')} ──</b>")

        for c in sorted(por_fecha[fecha], key=lambda x: x.get("horario", "")):
            emoji  = EMOJI.get(c.get("fuente", ""), "📋")
            lineas.append(
                f"{emoji} {c.get('comision', '')} — {c.get('horario', '')} — {c.get('sala', '')[:40]}"
            )

    # Telegram tiene límite de 4096 chars — dividir si es necesario
    mensaje_completo = "\n".join(lineas)
    if len(mensaje_completo) <= 4096:
        _telegram(mensaje_completo)
    else:
        # Enviar en partes
        partes = [lineas[:len(lineas)//2], lineas[len(lineas)//2:]]
        for parte in partes:
            _telegram("\n".join(parte))

    log.info(f"Reporte semanal enviado: {len(citaciones)} citaciones")


# ══════════════════════════════════════════════════════════════════════════════
# FLUJO PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════
def main(modo: str = "monitor"):
    """
    modo='monitor' → revisa cambios y notifica (cada 10 min)
    modo='reporte' → envía resumen semanal (lunes 09:00)
    """
    db = firestore.Client(project=GCP_PROJECT)

    if modo == "reporte":
        log.info("Generando reporte semanal...")
        enviar_reporte_semanal(db)
        return

    log.info("Iniciando monitor de citaciones...")

    # Recolectar semana actual y siguiente
    semanas = [semana_actual(), semana_siguiente()]
    todas   = scrape_senado()

    for sem in semanas:
        todas += scrape_camara(semana=sem)

    log.info(f"Total citaciones encontradas: {len(todas)}")

    nuevas     = 0
    modificadas = 0
    suspendidas = 0

    for cit in todas:
        try:
            resultado = guardar_citacion(db, cit)

            if resultado == "nueva":
                nuevas += 1
                log.info(f"[NUEVA] {cit.fuente.upper()} — {cit.comision} — {cit.fecha}")
                notificar_nueva(cit)

            elif resultado == "modificada":
                modificadas += 1
                log.info(f"[MODIFICADA] {cit.fuente.upper()} — {cit.comision} — {cit.fecha}")
                doc = db.collection(FIRESTORE_COLECCION).document(cit.id).get().to_dict()
                notificar_cambio(cit, "modificada", doc.get("horario_anterior", ""))

            elif resultado == "suspendida":
                suspendidas += 1
                log.info(f"[SUSPENDIDA] {cit.fuente.upper()} — {cit.comision} — {cit.fecha}")
                notificar_cambio(cit, "suspendida")

        except Exception as e:
            log.error(f"Error procesando {cit.comision}: {e}")

    log.info(f"\nResumen: {nuevas} nuevas | {modificadas} modificadas | {suspendidas} suspendidas")


if __name__ == "__main__":
    import sys
    modo = sys.argv[1] if len(sys.argv) > 1 else "monitor"
    main(modo=modo)
