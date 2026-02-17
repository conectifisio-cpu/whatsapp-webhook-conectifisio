import os
import json
import requests
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES (LIDAS DA VERCEL)
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_WEBHOOK_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

def send_reply(to, text):
    """ Envia a resposta para o WhatsApp do paciente """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        return False
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res.status_code == 200
    except:
        return False

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Falha", 403

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

            # --- LÓGICA DE RECONHECIMENTO DE CPF ---
            # Remove tudo que não é número
            only_numbers = re.sub(r'\D', '', text)
            
            payload = {
                "from": phone,
                "text": text,
                "unit": unit,
                "status": "triagem"
            }

            # Se tiver exatamente 11 dígitos, tratamos como CPF
            if len(only_numbers) == 11:
                cpf_formatado = f"{only_numbers[:3]}.{only_numbers[3:6]}.{only_numbers[6:9]}-{only_numbers[9:]}"
                payload["cpf"] = only_numbers # Enviamos o número limpo para o Wix
                payload["status"] = "cadastro"
                reply_msg = f"Recebido! Identificamos o CPF {cpf_formatado}. O seu card no Kanban da unidade {unit} foi atualizado para a etapa de Cadastro!"
            else:
                reply_msg = f"Olá! Recebemos sua mensagem na unidade {unit}. Um atendente falará com você em breve."

            # Envia para o Wix e depois responde no Whats
            requests.post(WIX_WEBHOOK_URL, json=payload, timeout=10)
            send_reply(phone, reply_msg)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro: {e}")
        return jsonify({"status": "error"}), 500
