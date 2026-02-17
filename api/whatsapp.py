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
WIX_WEBHOOK_URL = os.environ.get("WIX_WEBHOOK_URL")

# ==========================================
# FUNÇÕES DE VALIDAÇÃO
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
    """ Tenta encontrar data de nascimento no texto """
    pattern = r'(\d{2})/?(\d{2})/?(\d{4})'
    match = re.search(pattern, text)
    if match:
        day, month, year = match.groups()
        try:
            datetime(int(year), int(month), int(day))
            return f"{day}/{month}/{year}"
        except ValueError:
            return None
    return None

def send_reply(to, text):
    """ Envia resposta para o WhatsApp """
    if not WHATSAPP_TOKEN: return False
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
        return True
    except:
        return False

# ==========================================
# WEBHOOK PRINCIPAL
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data:
        return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            
            # Identifica Unidade pelo número de telefone da clínica
            display_num = value["metadata"]["display_phone_number"]
            unit = "Ipiranga" if "23629360" in display_num else "SCS"

            # 1. CONSULTA O WIX PARA SABER SE O PACIENTE JÁ EXISTE
            # O Wix agora devolve: isNew, patientName e feegowId
            res_wix = requests.post(WIX_WEBHOOK_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
            info = res_wix.json()
            
            p_name = info.get("patientName", "Aguardando Nome...")
            has_feegow = info.get("feegowId") is not None
            
            # 2. LÓGICA DE ATENDIMENTO
            
            # CASO A: Paciente já tem cadastro no Feegow (Veterano)
            if has_feegow and p_name != "Aguardando Nome...":
                if any(word in text.lower() for word in ["alterar", "desmarcar", "mudar", "horário"]):
                    reply = f"Olá, {p_name}! Percebi que você quer tratar de um agendamento. Vou transferir você agora para um atendente humano que tem acesso à sua agenda no Feegow. Um momento..."
                else:
                    reply = f"Olá, {p_name}! Que bom ter você de volta à Conectifisio {unit}. Como posso ajudar hoje?\n\n1. Novo Agendamento\n2. Alterar Horário\n3. Falar com Atendente"
            
            # CASO B: Paciente Novo ou em fase de cadastro
            else:
                only_numbers = re.sub(r'\D', '', text)
                found_date = extract_date(text)

                # Se enviou CPF
                if len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                    reply = "✅ CPF validado com sucesso! Agora, por favor, envie sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                    # Envia o CPF para o Wix gravar
                    requests.post(WIX_WEBHOOK_URL, json={"from": phone, "cpf": only_numbers})
                
                # Se enviou Data
                elif found_date:
                    reply = f"📅 Data {found_date} registada! Estamos a finalizar o seu registo no Feegow..."
                    requests.post(WIX_WEBHOOK_URL, json={"from": phone, "birthDate": found_date})
                
                # Se enviou o Nome (mais de 5 letras e com espaço)
                elif " " in text and len(text) > 5 and p_name == "Aguardando Nome...":
                    reply = f"Prazer, {text.title()}! Para prosseguirmos com o seu cadastro na unidade {unit}, por favor envie o seu CPF (apenas números)."
                    requests.post(WIX_WEBHOOK_URL, json={"from": phone, "name": text.title()})
                
                # Saudação para quem ainda não deu o nome
                else:
                    reply = f"Olá! Bem-vindo à Conectifisio {unit}. Qual o seu NOME COMPLETO para iniciarmos o seu atendimento automático?"

            send_reply(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"Erro: {e}")
        return jsonify({"status": "error"}), 500

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Erro de Token", 403

app.debug = False
