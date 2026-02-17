import os
import json
import requests
import re
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ==========================================import os
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
    requests.post(url, json=payload, headers=headers, timeout=10)

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
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
            info = res_wix.json() if res_wix.status_code == 200 else {}
            
            p_name = info.get("patientName", "")
            has_feegow = info.get("feegowId") is not None
            
            # --- LÓGICA DE DECISÃO ---
            only_numbers = re.sub(r'\D', '', text)
            found_date = extract_date(text)

            # A. JÁ É PACIENTE DO FEEGOW
            if has_feegow and p_name not in ["Aguardando Nome...", "Paciente Novo", ""]:
                reply = f"Olá, {p_name}! Como posso ajudar na unidade {unit} hoje?\n\n1. Novo Agendamento\n2. Alterar Horário\n3. Falar com Atendente"

            # B. RECEBEU CPF
            elif len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                reply = "✅ CPF validado! Agora, por favor, envie sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                requests.post(WIX_URL, json={"from": phone, "cpf": only_numbers})

            # C. RECEBEU DATA
            elif found_date:
                reply = f"📅 Data {found_date} registrada! Estamos processando seu cadastro..."
                requests.post(WIX_URL, json={"from": phone, "birthDate": found_date})

            # D. RECEBEU NOME (Evita o loop: se tiver espaço e não for CPF/Data)
            elif " " in text and len(text) > 5:
                reply = f"Prazer, {text.title()}! Agora, para completar seu cadastro, digite seu CPF (apenas números)."
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
# CONFIGURAÇÕES (VARIÁVEIS DE AMBIENTE)
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
# URL Padrão caso a variável de ambiente não esteja configurada
WIX_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

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
    if not WHATSAPP_TOKEN:
        print("!!! ERRO: WHATSAPP_TOKEN não configurado !!!")
        return False
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"WhatsApp API Resposta: {res.status_code}")
        return True
    except Exception as e:
        print(f"Erro ao enviar WhatsApp: {e}")
        return False

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

            print(f"Recebido de {phone}: {text}")

            # 1. TENTA CONSULTAR O WIX (COM PROTEÇÃO CONTRA ERROS)
            info = {"patientName": "Aguardando Nome...", "isNew": True, "feegowId": None}
            try:
                print(f"Conectando ao Wix: {WIX_URL}")
                res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
                if res_wix.status_code == 200:
                    info = res_wix.json()
                    print("Wix respondeu com sucesso.")
                else:
                    print(f"Wix retornou erro {res_wix.status_code}: {res_wix.text}")
            except Exception as wix_err:
                print(f"Falha na conexão com o Wix: {wix_err}")

            p_name = info.get("patientName", "Aguardando Nome...")
            has_feegow = info.get("feegowId") is not None
            
            # 2. LÓGICA DE DIÁLOGO
            if has_feegow and p_name != "Aguardando Nome...":
                if any(word in text.lower() for word in ["alterar", "desmarcar", "mudar", "horário", "consulta"]):
                    reply = f"Olá, {p_name}! Percebi que precisas tratar de um agendamento. Vou transferir-te agora para um atendente que verá a tua agenda no Feegow. Um momento..."
                else:
                    reply = f"Olá, {p_name}! Que bom ter-te de volta à Conectifisio {unit}. Como posso ajudar hoje?\n\n1. Novo Agendamento\n2. Alterar Horário\n3. Falar com Atendente"
            else:
                only_numbers = re.sub(r'\D', '', text)
                found_date = extract_date(text)

                if len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                    reply = "✅ CPF validado! Agora, por favor, envie sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                    requests.post(WIX_URL, json={"from": phone, "cpf": only_numbers})
                elif found_date:
                    reply = f"📅 Data {found_date} registrada! Estamos finalizando seu cadastro no Feegow..."
                    requests.post(WIX_URL, json={"from": phone, "birthDate": found_date})
                elif " " in text and len(text) > 5 and p_name == "Aguardando Nome...":
                    reply = f"Prazer, {text.title()}! Para prosseguirmos com o seu cadastro automático na unidade {unit}, envie seu CPF (apenas números)."
                    requests.post(WIX_URL, json={"from": phone, "name": text.title()})
                else:
                    reply = f"Olá! Bem-vindo à Conectifisio {unit}. Qual o seu NOME COMPLETO para iniciarmos o atendimento?"

            send_reply(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"ERRO CRÍTICO NO WEBHOOK: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
