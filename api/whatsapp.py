import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES - VERIFIQUE ESTAS CHAVES NA VERCEL
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = os.environ.get("WIX_WEBHOOK_URL")

def send_whatsapp(to, text):
    """Envia mensagem e regista o resultado nos logs da Vercel"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    
    try:
        response = requests.post(url, json=payload, headers=headers, timeout=10)
        # Este print aparecerá nos logs da Vercel para sabermos se a Meta aceitou o token
        print(f"DEBUG META: Status {response.status_code} - Resposta: {response.text}")
    except Exception as e:
        print(f"DEBUG META ERRO: {str(e)}")

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    
    # Validação inicial para evitar Erro 500
    if not data or "entry" not in data:
        return jsonify({"status": "no_data"}), 200

    try:
        entry = data["entry"][0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        
        # Ignora se não for uma mensagem (ex: confirmação de leitura)
        if "messages" not in value:
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        text = message.get("text", {}).get("body", "").strip()
        
        # Identificação da Unidade
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unit = "Ipiranga" if "23629360" in display_phone else "SCS"

        # 1. COMUNICAÇÃO COM O WIX (COM LOG)
        print(f"DEBUG WIX: Enviando para {WIX_URL}...")
        try:
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=15)
            print(f"DEBUG WIX RESPOSTA: {res_wix.status_code} - {res_wix.text}")
            info = res_wix.json()
        except Exception as e:
            print(f"DEBUG WIX ERRO: {str(e)}")
            return jsonify({"status": "wix_connection_error"}), 200

        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")

        # --- LÓGICA DE RESPOSTA (ESTADO: TRIAGEM) ---
        if status == "triagem":
            reply = f"Olá! ✨ Recebemos o seu 'Olá' na unidade {unit}. O sistema está online! Como gostaria de ser chamado(a)?"
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})
            send_whatsapp(phone, reply)

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"DEBUG ERRO GERAL: {str(e)}")
        return jsonify({"status": "error_handled"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
