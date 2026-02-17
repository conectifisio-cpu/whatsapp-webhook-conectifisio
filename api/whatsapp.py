import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

# --- UTILITÁRIOS ---
def is_valid_cpf(cpf):
    cpf = re.sub(r'\D', '', cpf)
    return len(cpf) == 11 and not cpf == cpf[0] * 11

def extract_date(text):
    pattern = r'(\d{2})/?(\d{2})/?(\d{4})'
    match = re.search(pattern, text)
    if match:
        day, month, year = match.groups()
        try:
            datetime(int(year), int(month), int(day))
            return f"{day}/{month}/{year}"
        except: return None
    return None

def send_whatsapp(to, text):
    if not WHATSAPP_TOKEN: return False
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
        return True
    except: return False

# ==========================================
# ROTAS
# ==========================================

@app.route("/api/send", methods=["POST"])
def send_manual():
    data = request.get_json()
    to = data.get("to")
    message = data.get("message")
    if send_whatsapp(to, message):
        try:
            requests.post(WIX_URL, json={"from": to, "text": f"[HUMANO]: {message}", "status": "atendimento_humano"}, timeout=5)
        except: pass
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "error"}), 500

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Erro", 403

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            unit = "Ipiranga" if "23629360" in value["metadata"]["display_phone_number"] else "SCS"

            # 1. Consulta o Wix
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
            info = res_wix.json()
            
            if info.get("currentStatus") == "atendimento_humano":
                return jsonify({"status": "human"}), 200

            p_name = info.get("patientName", "")
            has_feegow = info.get("feegowId") is not None
            only_numbers = re.sub(r'\D', '', text)
            found_date = extract_date(text)

            # --- LÓGICA DE DECISÃO ---
            
            # A. Veterano
            if has_feegow and p_name not in ["Paciente Novo", "Aguardando Nome...", ""]:
                reply = f"Olá, {p_name}! Que bom falar contigo novamente na Conectifisio {unit}. Como posso ajudar?\n\n1. Agendamento\n2. Falar com Atendente"
            
            # B. CPF
            elif len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                reply = "✅ CPF validado! Agora, por favor, envie sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                requests.post(WIX_URL, json={"from": phone, "cpf": only_numbers})
            
            # C. Data
            elif found_date:
                reply = f"📅 Data {found_date} registrada! Estamos processando seu cadastro..."
                requests.post(WIX_URL, json={"from": phone, "birthDate": found_date})

            # D. Nome (Se for a primeira interação ou o Wix ainda não tiver o nome)
            elif p_name in ["Paciente Novo", "Aguardando Nome...", ""]:
                # Qualquer texto que não seja CPF ou Data agora é aceito como nome
                reply = f"Prazer, {text.title()}! Para iniciarmos seu cadastro na unidade {unit}, por favor envie seu CPF (apenas números)."
                requests.post(WIX_URL, json={"from": phone, "name": text.title()})

            # E. Saudação de Segurança
            else:
                reply = f"Olá! Bem-vindo à Conectifisio {unit}. Qual o seu NOME COMPLETO para iniciarmos?"

            send_whatsapp(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
