import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
# Permite que o Dashboard hospedado no Wix chame esta API sem bloqueios
CORS(app)

# ==========================================
# CONFIGURAÇÕES (VARIÁVEIS DE AMBIENTE VERCEL)
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

# ==========================================
# FUNÇÕES DE APOIO
# ==========================================

def is_valid_cpf(cpf):
    """ Validação básica de 11 dígitos para CPF """
    cpf = re.sub(r'\D', '', cpf)
    return len(cpf) == 11 and not cpf == cpf[0] * 11

def extract_date(text):
    """ Tenta encontrar e validar uma data DD/MM/AAAA """
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
    """ Dispara a mensagem via Meta Cloud API """
    if not WHATSAPP_TOKEN: return False
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
    except: return False

# ==========================================
# ROTA: ENVIO MANUAL (INTERVENÇÃO DO DASHBOARD)
# ==========================================

@app.route("/api/send", methods=["POST"])
def send_manual():
    """ 
    Recebe ordens do Command Center no Wix.
    Envia a mensagem e avisa o Wix para pausar o robô automático.
    """
    data = request.get_json()
    to = data.get("to")
    message = data.get("message")
    
    if not to or not message:
        return jsonify({"error": "Dados incompletos"}), 400
        
    if send_whatsapp(to, message):
        # Sincroniza com o Wix para mudar o status para 'atendimento_humano'
        try:
            requests.post(WIX_URL, json={
                "from": to,
                "text": f"[INTERVENÇÃO HUMANA]: {message}",
                "status": "atendimento_humano"
            }, timeout=5)
        except: pass
        return jsonify({"status": "success"}), 200
    
    return jsonify({"status": "error", "message": "Falha na Meta API"}), 500

# ==========================================
# ROTA: WEBHOOK WHATSAPP (RECEBIMENTO)
# ==========================================

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    """ Verificação exigida pela Meta para ativar o Webhook """
    if request.args.get("hub.verify_token") == VERIFY_TOKEN:
        return request.args.get("hub.challenge"), 200
    return "Token Inválido", 403

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    """ Processa as mensagens enviadas pelos pacientes """
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        entry = data["entry"][0]; changes = entry["changes"][0]; value = changes["value"]
        if "messages" in value:
            message = value["messages"][0]
            phone = message["from"]
            text = message.get("text", {}).get("body", "").strip()
            
            display_num = value.get("metadata", {}).get("display_phone_number", "")
            unit = "Ipiranga" if "23629360" in display_num else "SCS"

            # 1. Consulta o Wix para saber quem é e se o robô deve responder
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
            info = res_wix.json()
            
            # --- TRAVA DE ATENDIMENTO HUMANO ---
            # Se o status for 'atendimento_humano', o robô fica em silêncio.
            if info.get("currentStatus") == "atendimento_humano":
                print(f"Robô pausado para {phone} - Humano no controle.")
                return jsonify({"status": "human_intervention"}), 200

            # --- LÓGICA DO ROBÔ ---
            only_numbers = re.sub(r'\D', '', text)
            found_date = extract_date(text)
            p_name = info.get("patientName", "")
            has_feegow = info.get("feegowId") is not None

            # Caso A: Paciente já tem prontuário no Feegow
            if has_feegow and p_name not in ["Paciente Novo", "Aguardando Nome...", ""]:
                reply = f"Olá, {p_name}! Bem-vindo à Conectifisio {unit}. Como podemos ajudar hoje?\n\n1. Agendamento\n2. Falar com Atendente"
            
            # Caso B: Recebeu um CPF válido
            elif len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                reply = "✅ CPF validado! Agora, por favor, envie sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                requests.post(WIX_URL, json={"from": phone, "cpf": only_numbers})
            
            # Caso C: Recebeu uma Data de Nascimento
            elif found_date:
                reply = f"📅 Data {found_date} registada! Estamos a preparar o seu acesso ao prontuário..."
                requests.post(WIX_URL, json={"from": phone, "birthDate": found_date})

            # Caso D: Primeiro contacto (Captura de Nome)
            elif " " in text and len(text) > 5 and not found_date:
                reply = f"Prazer, {text.title()}! Para iniciarmos o seu cadastro na unidade {unit}, envie o seu CPF (apenas números)."
                requests.post(WIX_URL, json={"from": phone, "name": text.title()})

            # Caso E: Saudação Padrão
            else:
                reply = f"Olá! Bem-vindo à Conectifisio {unit}. Qual o seu NOME COMPLETO para iniciarmos?"

            send_whatsapp(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(port=5000)
