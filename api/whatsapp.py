import os 
import json
import requests
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES (LIDAS DA VERCEL SETTINGS)
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_WEBHOOK_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

def send_reply(to, text):
    """ Tenta responder no WhatsApp e loga o resultado """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("!!! ERRO: Variáveis WHATSAPP_TOKEN ou PHONE_NUMBER_ID não encontradas !!!")
        return False

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
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"Log WhatsApp: Status {res.status_code} - Resposta: {res.text}")
        return res.status_code == 200
    except Exception as e:
        print(f"Erro ao responder: {e}")
        return False

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    """ Validação para o Painel da Meta """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("✅ Webhook Validado!")
        return challenge, 200
    return "Token Inválido", 403

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    """ Recebe mensagens e envia para o Wix """
    data = request.get_json()
    
    # Log para ver o que a Meta está enviando
    print("--- Mensagem Recebida ---")
    print(json.dumps(data, indent=2))

    if not data or "entry" not in data:
        return jsonify({"status": "no_data"}), 200

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            
            # Identifica Unidade
            display_num = value["metadata"]["display_phone_number"]
            unit = "Ipiranga" if "23629360" in display_num else "SCS"

            print(f"Processando: {phone} na unidade {unit}")

            # Envia para o Wix
            payload_wix = {"from": phone, "text": text, "unit": unit}
            res_wix = requests.post(WIX_WEBHOOK_URL, json=payload_wix, timeout=10)
            print(f"Wix Status: {res_wix.status_code}")

            # Responde no WhatsApp
            reply = f"Olá! Recebemos sua mensagem na unidade {unit}. Um atendente falará com você em breve."
            send_reply(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro Crítico: {e}")
        return jsonify({"status": "error"}), 500

app.debug = False
