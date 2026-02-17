import os
import json
import requests
import re
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ==========================================
# CONFIGURAÇÕES (VARIÁVEIS DE AMBIENTE)
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
# URL do seu Wix (Configurada na Vercel como WIX_WEBHOOK_URL)
WIX_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

# ==========================================
# FUNÇÕES DE VALIDAÇÃO E RECONHECIMENTO
# ==========================================

def is_valid_cpf(cpf):
    """ Validação matemática do CPF """
    cpf = re.sub(r'\D', '', cpf)
    if len(cpf) != 11 or cpf == cpf[0] * 11: return False
    for i in range(9, 11):
        value = sum((int(cpf[num]) * ((i + 1) - num) for num in range(0, i)))
        digit = ((value * 10) % 11) % 10
        if digit != int(cpf[i]): return False
    return True

def extract_date(text):
    """ Tenta encontrar uma data de nascimento válida """
    pattern = r'(\d{2})/?(\d{2})/?(\d{4})'
    match = re.search(pattern, text)
    if match:
        day, month, year = match.groups()
        try:
            datetime(int(year), int(month), int(day))
            return f"{day}/{month}/{year}"
        except ValueError: return None
    return None

def is_valid_email(text):
    """ Validação básica de formato de e-mail """
    return re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text.lower())

def send_reply(to, text):
    """ Envia a mensagem de volta para o WhatsApp do paciente """
    if not WHATSAPP_TOKEN: return False
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text}
    }
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
        return True
    except: return False

# ==========================================
# ROTAS DO WEBHOOK (VERCEL)
# ==========================================

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    """ Verificação do Webhook pela Meta """
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Erro de Token", 403

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    """ Processamento de mensagens recebidas """
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            
            # Identifica unidade pelo telefone de destino
            display_num = value["metadata"]["display_phone_number"]
            unit = "Ipiranga" if "23629360" in display_num else "SCS"

            # 1. CONSULTA O WIX PARA SABER O STATUS DO PACIENTE
            info = {}
            try:
                res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
                if res_wix.status_code == 200:
                    info = res_wix.json()
            except: pass
            
            p_name = info.get("patientName", "Paciente Novo")
            has_cpf = info.get("hasCpf", False)
            has_date = info.get("hasBirthDate", False)
            has_email = info.get("hasEmail", False)
            has_feegow = info.get("feegowId") is not None
            
            # --- LÓGICA DE DIÁLOGO INTELIGENTE ---
            only_numbers = re.sub(r'\D', '', text)
            found_date = extract_date(text)

            # A. RECONHECIMENTO DE DADOS NA MENSAGEM ATUAL
            
            # 1. É um CPF?
            if len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                reply = "✅ CPF validado! Agora, por favor, envie a sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                requests.post(WIX_URL, json={"from": phone, "cpf": only_numbers})
            
            # 2. É uma Data?
            elif found_date:
                reply = f"📅 Data {found_date} registada! Para finalizarmos o seu acesso, qual o seu MELHOR E-MAIL?"
                requests.post(WIX_URL, json={"from": phone, "birthDate": found_date})
            
            # 3. É um E-mail?
            elif is_valid_email(text):
                reply = "📧 E-mail registado! Obrigado. Estamos a preparar o seu agendamento no Feegow. Um atendente entrará em contato em breve."
                requests.post(WIX_URL, json={"from": phone, "email": text.lower()})

            # B. DECISÃO BASEADA NO HISTÓRICO (WIX)

            # 4. Já é Paciente do Feegow (Veterano)
            elif has_feegow and p_name not in ["Paciente Novo", ""]:
                reply = f"Olá, {p_name}! Que bom ter-te de volta à Conectifisio {unit}. Como posso ajudar hoje?\n\n1. Novo Agendamento\n2. Alterar Horário\n3. Falar com Atendente"
            
            # 5. Já deu o nome, mas faltam dados técnicos
            elif p_name not in ["Paciente Novo", ""]:
                if not has_cpf:
                    reply = f"Prazer em falar consigo, {p_name}! Para iniciarmos o seu cadastro na unidade {unit}, por favor envie o seu CPF (apenas números)."
                elif not has_date:
                    reply = "Obrigado! Agora, por favor, envie a sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                elif not has_email:
                    reply = "Quase lá! Qual o seu e-mail para receber as confirmações?"
                else:
                    reply = f"Olá {p_name}, como posso ajudar hoje?"

            # 6. Primeiro Contato (Captura de Nome)
            else:
                # Se o texto for longo e tiver espaço, assumimos que é o nome
                if " " in text and len(text) > 5 and len(only_numbers) < 10:
                    reply = f"Prazer, {text.title()}! Para iniciarmos o seu cadastro na unidade {unit}, por favor envie o seu CPF (apenas números)."
                    requests.post(WIX_URL, json={"from": phone, "name": text.title()})
                else:
                    reply = f"Olá! Bem-vindo à Conectifisio {unit}. Qual o seu NOME COMPLETO para iniciarmos?"

            send_reply(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

app.debug = False
