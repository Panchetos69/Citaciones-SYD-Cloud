"""
Servidor HTTP para Cloud Run.
GET/POST /monitor → ejecuta el monitor de citaciones
GET/POST /reporte → envía el reporte semanal
GET      /health  → health check
"""
from flask import Flask, jsonify
import citaciones

app = Flask(__name__)

@app.route("/monitor", methods=["POST", "GET"])
def monitor():
    try:
        citaciones.main(modo="monitor")
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
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
