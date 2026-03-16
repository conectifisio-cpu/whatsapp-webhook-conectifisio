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
# Lendo a senha que você configurou na Vercel
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
        print("✅ Firebase Inicializado com Sucesso!")
    except Exception as e:
        print(f"❌ Erro Firebase: {e}")

db = firestore.client() if firebase_admin._apps else None

# --- FUNÇÕES AUXILIARES ---
def get_paciente(phone):
    if not db: return {}
    doc = db.collection("PatientsKanban").document(phone).get()
    return doc.to_dict() if doc.exists else {}

def update_paciente(phone, data):
    if not db: return
    data["lastInteraction"] = firestore.SERVER_TIMESTAMP
    db.collection("PatientsKanban").document(phone).set(data, merge=True)

def enviar_whatsapp(to, payload_msg):
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, **payload_msg}
    return requests.post(url, json=payload, headers=headers, timeout=10)

def responder_texto(to, texto):
    return enviar_whatsapp(to, {"type": "text", "text": {"body": texto}})

def enviar_botoes(to, texto, botoes):
    payload = {"type": "interactive", "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": [{"type": "reply", "reply": {"id": b["id"], "title": b["title"][:20]}} for b in botoes]}}}
    return enviar_whatsapp(to, payload)

def enviar_lista(to, texto, titulo_botao, secoes):
    payload = {"type": "interactive", "interactive": {"type": "list", "body": {"text": texto}, "action": {"button": titulo_botao[:20], "sections": secoes}}}
    return enviar_whatsapp(to, payload)

# ==========================================
# ROTA ÚNICA: WHATSAPP (GET E POST)
# ==========================================
@app.route("/api/whatsapp", methods=["GET", "POST"])
def handle_whatsapp():
    # --- LÓGICA GET (Verificação e Comandos) ---
    if request.method == "GET":
        # 1. Aperto de mão com a Meta
        if request.args.get("hub.verify_token") == VERIFY_TOKEN:
            return request.args.get("hub.challenge"), 200
        
        # 2. Comando Especial: Inscrever o App (A Chave Mestra)
        if request.args.get("subscribe_app") == "sim":
            waba_id = request.args.get("waba_id")
            url_sub = f"https://graph.facebook.com/v19.0/{waba_id}/subscribed_apps"
            res = requests.post(url_sub, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"})
            return jsonify({"mensagem": "Inscrição concluída!", "status": res.status_code, "meta": res.json()}), 200

        # 3. Comando Especial: Registrar PIN
        if request.args.get("registrar") == "sim":
            url_reg = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/register"
            res = requests.post(url_reg, headers={"Authorization": f"Bearer {WHATSAPP_TOKEN}"}, json={"messaging_product": "whatsapp", "pin": "123456"})
            return jsonify({"mensagem": "Registro PIN enviado", "meta": res.json()}), 200

        # 4. Rota do Dashboard (Listar pacientes)
        if request.args.get("action") == "get_patients":
            docs = db.collection("PatientsKanban").stream()
            patients = []
            for doc in docs:
                d = doc.to_dict()
                d["id"] = doc.id
                patients.append(d)
            return jsonify({"items": patients}), 200

        return "Acesso Negado", 403

    # --- LÓGICA POST (Receber Mensagens) ---
    data = request.get_json()
    try:
        if not data or "entry" not in data: return jsonify({"status": "ok"}), 200
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "ok"}), 200

        message = value["messages"][0]
        phone = message["from"]
        msg_recebida = message.get("text", {}).get("body", "").strip().lower()

        # Reset simples para teste
        if msg_recebida in ["oi", "reset", "olá"]:
            update_paciente(phone, {"status": "escolhendo_unidade"})
            botoes = [{"id": "u1", "title": "SCS"}, {"id": "u2", "title": "Ipiranga"}]
            enviar_botoes(phone, "Olá! ✨ Seja bem-vindo à Conectifisio. Em qual unidade deseja ser atendido?", botoes)
        
        return jsonify({"status": "success"}), 200

    except:
        print(traceback.format_exc())
        return jsonify({"status": "error"}), 200

if __name__ == "__main__":
    app.run(port=5000)
