import os
import json
import requests
import re
from flask import Flask, request, jsonify

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES DE SEGURANÇA (MÉTODO SEGURO)
# ==========================================

# Agora o código lê as chaves das "Variáveis de Ambiente" da Vercel.
# Não precisas de escrever os tokens aqui!
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_WEBHOOK_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

# ==========================================
# FUNÇÕES DE APOIO
# ==========================================

def send_reply(to, text):
    """ Envia uma resposta automática via WhatsApp Cloud API """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        print("Erro: WHATSAPP_TOKEN ou PHONE_NUMBER_ID não configurados na Vercel.")
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
        print(f"Erro ao disparar resposta WhatsApp: {e}")
        return False

def sync_to_wix(data):
    """ Envia os dados capturados para o CMS do Wix """
    try:
        response = requests.post(WIX_WEBHOOK_URL, json=data, timeout=15)
        print(f"Log Wix Sinc: Status {response.status_code}")
        return response.status_code == 200
    except Exception as e:
        print(f"Erro de ligação com o servidor Wix: {e}")
        return False

# ==========================================
# ROTAS DO WEBHOOK
# ==========================================

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
                reply_msg = f"Confirmado! O CPF {payload['cpf']} foi registado. O seu atendimento foi movido para 'Cadastro' na unidade {unit}."
            else:
                reply_msg = f"Olá! Recebemos o seu contacto na unidade {unit}. Já criámos o seu card no nosso Kanban e será atendido em breve."

            wix_success = sync_to_wix(payload)
            if wix_success:
                send_reply(phone, reply_msg)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    app.run(port=5000)
