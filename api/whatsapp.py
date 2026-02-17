import os
import json
import requests
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES DE SEGURANÇA (VARIÁVEIS DE AMBIENTE)
# ==========================================
# Certifique-se de que adicionou estas chaves na aba "Settings > Environment Variables" da Vercel
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_WEBHOOK_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

def send_reply(to, text):
    """ Envia resposta automática via WhatsApp Cloud API """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("ERRO: WHATSAPP_TOKEN ou PHONE_NUMBER_ID não configurados.")
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
        print(f"Log Meta API: Status {res.status_code}")
        return res.status_code == 200
    except Exception as e:
        print(f"Erro WhatsApp: {e}")
        return False

def sync_to_wix(data):
    """ Envia dados para o Wix CMS """
    try:
        response = requests.post(WIX_WEBHOOK_URL, json=data, timeout=15)
        print(f"Log Wix Sinc: Status {response.status_code}")
        return response.status_code == 200
    except Exception as e:
        print(f"Erro Wix: {e}")
        return False

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Falha na verificação", 403

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data:
        return jsonify({"status": "no_payload"}), 200

    try:
        entry = data["entry"][0]
        changes = entry["changes"][0]
        value = changes["value"]
        
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            
            display_num = value["metadata"]["display_phone_number"]
            unit = "Ipiranga" if "23629360" in display_num else "SCS"

            # Lógica de Captura (CPF)
            status = "triagem"
            cpf_match = re.search(r'\d{11}', re.sub(r'\D', '', text))
            
            payload = {
                "from": phone,
                "text": text,
                "unit": unit,
                "status": status,
                "name": "Paciente Novo"
            }

            if cpf_match:
                payload["cpf"] = cpf_match.group()
                payload["status"] = "cadastro"
                reply_msg = f"Recebido! O CPF {payload['cpf']} foi registado na unidade {unit}."
            else:
                reply_msg = f"Olá! Recebemos a sua mensagem na unidade {unit}. Um atendente falará consigo brevemente."

            if sync_to_wix(payload):
                send_reply(phone, reply_msg)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro: {e}")
        return jsonify({"status": "error"}), 500

# Exportação necessária para a Vercel
app.debug = False
