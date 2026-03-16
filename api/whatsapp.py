import os
import requests
import traceback
import re
import json
import base64
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES DE AMBIENTE
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
API_KEY = os.environ.get("GEMINI_API_KEY", "")
FEEGOW_TOKEN = os.environ.get("FEEGOW_TOKEN", "")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")

# ==========================================
# INICIALIZAÇÃO DO FIREBASE
# ==========================================
firebase_creds_json = os.environ.get("FIREBASE_CREDENTIALS")
if firebase_creds_json and not firebase_admin._apps:
    try:
        cred_dict = json.loads(firebase_creds_json, strict=False)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
    except: pass

db = firestore.client() if firebase_admin._apps else None

def update_paciente(phone, data):
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    return requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_botoes(to, texto, botoes):
    payload = {"type": "interactive", "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}}}
    return enviar_whatsapp(to, payload)

@app.route("/api/whatsapp", methods=["GET", "POST"])
def handle_whatsapp():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        
        if request.args.get("subscribe_app") == "sim":
            waba_id = request.args.get("waba_id")
            url_sub = f"https://graph.facebook.com/v19.0/{waba_id}/subscribed_apps"
            res = requests.post(url_sub, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
            return jsonify(res.json()), 200

        if request.args.get("action") == "get_patients":
            docs = db.collection("PatientsKanban").stream()
            return jsonify({"items": [doc.to_dict() for doc in docs]}), 200

        return "Acesso Negado", 403

    # --- LÓGICA POST (RECEBER MENSAGEM) ---
    data = request.get_json()
    try:
        # 1. Extrair os dados da mensagem
        val = data["entry"][0]["changes"][0]["value"]
        if "messages" in val:
            message = val["messages"][0]
            phone = message["from"]
            
            # QUALQUER mensagem agora vai disparar o robô para teste
            update_paciente(phone, {"status": "escolhendo_unidade", "cellphone": phone})
            
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Olá! ✨ Robô da Conectifisio ativo.\n\nEm qual unidade deseja ser atendido?", botoes)
            
            print(f"✅ Mensagem respondida para {phone}")
        
        return jsonify({"status": "success"}), 200
    except:
        return jsonify({"status": "ok"}), 200

if __name__ == "__main__":
    app.run(port=5000)
