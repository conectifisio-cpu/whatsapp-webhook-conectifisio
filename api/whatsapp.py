import os
import json
import requests
import re
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES (LIDAS DA VERCEL)
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_WEBHOOK_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

# ==========================================
# FUNÇÕES DE VALIDAÇÃO E RECONHECIMENTO
# ==========================================

def is_valid_cpf(cpf):
    """ Validação matemática real do CPF """
    cpf = re.sub(r'\D', '', cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11:
        return False
    for i in range(9, 11):
        value = sum((int(cpf[num]) * ((i + 1) - num) for num in range(0, i)))
        digit = ((value * 10) % 11) % 10
        if digit != int(cpf[i]):
            return False
    return True

def extract_date(text):
    """ Tenta encontrar e validar uma data de nascimento no texto """
    pattern = r'(\d{2})/?(\d{2})/?(\d{4})'
    match = re.search(pattern, text)
    if match:
        day, month, year = match.groups()
        try:
            # Valida se a data existe no calendário
            datetime(int(year), int(month), int(day))
            return f"{day}/{month}/{year}"
        except ValueError:
            return None
    return None

def is_valid_email(text):
    """ Validação básica de formato de e-mail """
    return re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text.lower())

def send_reply(to, text):
    """ Envia a resposta via WhatsApp Cloud API """
    if not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
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
        return res.status_code == 200
    except:
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

            payload = {"from": phone, "text": text, "unit": unit, "status": "triagem"}
            
            # --- LÓGICA DE DECISÃO DO ROBÔ ---
            only_numbers = re.sub(r'\D', '', text)
            found_date = extract_date(text)
            
            # 1. Se for CPF
            if len(only_numbers) == 11:
                if is_valid_cpf(only_numbers):
                    payload["cpf"] = only_numbers
                    payload["status"] = "cadastro"
                    reply_msg = f"✅ CPF validado! Agora, por favor, envie sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                else:
                    reply_msg = "⚠️ Esse CPF não parece válido. Por favor, digite novamente apenas os 11 números."
                    send_reply(phone, reply_msg)
                    return jsonify({"status": "invalid_cpf"}), 200

            # 2. Se for Data de Nascimento
            elif found_date:
                payload["birthDate"] = found_date
                reply_msg = f"📅 Data de nascimento {found_date} registrada! Se desejar, envie também seu E-MAIL."

            # 3. Se for E-mail
            elif is_valid_email(text):
                payload["email"] = text.lower()
                reply_msg = "📧 E-mail registrado com sucesso! Um atendente entrará em contato em breve."

            # 4. Mensagem Inicial
            else:
                reply_msg = f"Olá! Recebemos sua mensagem na unidade {unit}. Para agilizar seu cadastro no Feegow, por favor envie seu CPF."

            # Sincroniza com Wix e Responde
            requests.post(WIX_WEBHOOK_URL, json=payload, timeout=10)
            send_reply(phone, reply_msg)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro: {e}")
        return jsonify({"status": "error"}), 500

app.debug = False
