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
WIX_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

# --- UTILITÁRIOS ---
def is_valid_cpf(cpf):
    cpf = re.sub(r'\D', '', cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11: return False
    for i in range(9, 11):
        value = sum((int(cpf[num]) * ((i + 1) - num) for num in range(0, i)))
        digit = ((value * 10) % 11) % 10
        if digit != int(cpf[i]): return False
    return True

def extract_date(text):
    pattern = r'(\d{2})/?(\d{2})/?(\d{4})'
    match = re.search(pattern, text)
    if match:
        day, month, year = match.groups()
        try:
            datetime(int(year), int(month), int(day))
            return f"{day}/{month}/{year}"
        except ValueError: return None
    return None

def send_reply(to, text):
    if not WHATSAPP_TOKEN: return False
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
        return True
    except: return False

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

            # 1. CONSULTA O WIX
            info = {}
            try:
                res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
                if res_wix.status_code == 200:
                    info = res_wix.json()
            except: pass
            
            p_name = info.get("patientName", "")
            has_feegow = info.get("feegowId") is not None
            
            # --- LÓGICA DE DECISÃO ---
            only_numbers = re.sub(r'\D', '', text)
            found_date = extract_date(text)

            # A. SE JÁ É PACIENTE VETERANO
            if has_feegow and p_name not in ["Paciente Novo", "Aguardando Nome...", ""]:
                reply = f"Olá, {p_name}! Como posso ajudar na unidade {unit} hoje?\n\n1. Novo Agendamento\n2. Alterar Horário\n3. Falar com Atendente"

            # B. SE RECEBEU CPF
            elif len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                reply = "✅ CPF validado! Agora, por favor, envie a sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                requests.post(WIX_URL, json={"from": phone, "cpf": only_numbers})

            # C. SE RECEBEU DATA
            elif found_date:
                reply = f"📅 Data {found_date} registrada! Estamos a processar o seu cadastro no Feegow..."
                requests.post(WIX_URL, json={"from": phone, "birthDate": found_date})

            # D. SE RECEBEU NOME (Texto longo com espaço, que não é CPF nem Data)
            elif " " in text and len(text) > 5 and not found_date and len(only_numbers) < 10:
                reply = f"Prazer, {text.title()}! Agora, para completarmos o seu cadastro na unidade {unit}, envie o seu CPF (apenas números)."
                requests.post(WIX_URL, json={"from": phone, "name": text.title()})

            # E. SAUDAÇÃO INICIAL
            else:
                reply = f"Olá! Bem-vindo à Conectifisio {unit}. Qual o seu NOME COMPLETO para iniciarmos?"

            send_reply(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
