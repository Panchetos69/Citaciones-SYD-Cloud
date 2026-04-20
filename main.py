from flask import Flask, request, jsonify
import citaciones
import os
import requests
from google.cloud import firestore

app = Flask(__name__)

TG_TOKEN = os.environ.get("TG_TOKEN", "8526676401:AAESmMiVjf7fKUi9bzcq0mMz2CJ0nzIIxxY")
GCP_PROJECT = "crack-map-317501"

def _tg_responder(chat_id: str, texto: str):
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": texto, "parse_mode": "HTML"},
        timeout=10
    )

def _tg_responder_pdf(chat_id: str, ruta: str, caption: str = ""):
    with open(ruta, "rb") as f:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument",
            data={"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
            files={"document": f},
            timeout=30
        )

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": True})

    msg = data.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    texto   = msg.get("text", "").strip()
    nombre  = msg.get("from", {}).get("first_name", "")

    if not chat_id or not texto:
        return jsonify({"ok": True})

    db = firestore.Client(project=GCP_PROJECT)

    if texto == "/start":
        # Registrar usuario
        db.collection("bot_usuarios").document(chat_id).set({
            "chat_id":  chat_id,
            "nombre":   nombre,
            "activo":   True,
            "registro": citaciones.datetime.now(citaciones.timezone.utc),
        })
        _tg_responder(chat_id,
            f"Bienvenido/a <b>{nombre}</b> al Calendario Legislativo KOM.\n\n"
            f"Desde ahora recibirás:\n"
            f"📅 Agenda semanal cada lunes\n"
            f"⚠️ Alertas de cambios en tiempo real\n\n"
            f"Comandos disponibles:\n"
            f"/reporte — PDF de la agenda actual\n"
            f"/agenda  — Resumen de la semana\n"
            f"/stop    — Dejar de recibir notificaciones"
        )

    elif texto == "/stop":
        db.collection("bot_usuarios").document(chat_id).update({"activo": False})
        _tg_responder(chat_id, "Has sido dado de baja. Escribe /start para volver a activarte.")

    elif texto == "/reporte":
        from datetime import datetime, timedelta
        cits    = [d.to_dict() for d in db.collection("citaciones").stream()]
        hoy     = datetime.now()
        fin     = hoy + timedelta(days=(4 - hoy.weekday() % 7))
        num_sem = hoy.isocalendar()[1]
        ruta    = citaciones.generar_pdf(cits, num_sem,
                                         hoy.strftime("%d/%m"), fin.strftime("%d/%m/%Y"))
        if ruta:
            total_s   = sum(1 for c in cits if c.get("fuente") == "senado" and not c.get("suspendida"))
            total_c   = sum(1 for c in cits if c.get("fuente") == "camara" and not c.get("suspendida"))
            priorit   = sum(1 for c in cits if c.get("prioridad"))
            caption = (
                f"📅 <b>Agenda Legislativa — Semana {num_sem}</b>\n"
                f"🏛 Senado: {total_s}  🏦 Camara: {total_c}  ⭐ Priorizadas: {priorit}"
            )
            _tg_responder_pdf(chat_id, ruta, caption)
        else:
            _tg_responder(chat_id, "Error generando el reporte.")

    elif texto == "/agenda":
        cits = [d.to_dict() for d in db.collection("citaciones").stream()
                if not d.to_dict().get("suspendida")]
        if not cits:
            _tg_responder(chat_id, "No hay citaciones disponibles.")
        else:
            from datetime import datetime
            dias_es = {
                "Monday":"Lunes","Tuesday":"Martes","Wednesday":"Miercoles",
                "Thursday":"Jueves","Friday":"Viernes"
            }
            por_fecha = {}
            for c in cits:
                f = c.get("fecha","")
                if f not in por_fecha:
                    por_fecha[f] = []
                por_fecha[f].append(c)

            lineas = [f"📅 <b>Agenda Semana {datetime.now().isocalendar()[1]}</b>\n"]
            for fecha in sorted(por_fecha.keys()):
                try:
                    dt  = datetime.strptime(fecha, "%Y-%m-%d")
                    dia = dias_es.get(dt.strftime("%A"), "")
                    lineas.append(f"\n<b>{dia} {dt.strftime('%d/%m')}</b>")
                except Exception:
                    continue
                for c in sorted(por_fecha[fecha], key=lambda x: x.get("horario","")):
                    emoji = "🏛" if c.get("fuente") == "senado" else "🏦"
                    lineas.append(f"  {emoji} {c.get('comision','')} — {c.get('horario','')}")

            mensaje = "\n".join(lineas)
            if len(mensaje) > 4000:
                _tg_responder(chat_id, mensaje[:4000])
            else:
                _tg_responder(chat_id, mensaje)

    else:
        _tg_responder(chat_id,
            "Comandos disponibles:\n"
            "/start   — Activar notificaciones\n"
            "/reporte — PDF agenda actual\n"
            "/agenda  — Resumen de la semana\n"
            "/stop    — Desactivar notificaciones"
        )

    return jsonify({"ok": True})


@app.route("/monitor", methods=["POST", "GET"])
def monitor():
    try:
        citaciones.main(modo="monitor")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500

@app.route("/sheets", methods=["POST", "GET"])
def sheets():
    try:
        citaciones.main(modo="sheets")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500

@app.route("/cerrar", methods=["POST", "GET"])
def cerrar():
    try:
        citaciones.main(modo="cerrar")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500

@app.route("/reporte", methods=["POST", "GET"])
def reporte():
    try:
        citaciones.main(modo="reporte")
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "detalle": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)