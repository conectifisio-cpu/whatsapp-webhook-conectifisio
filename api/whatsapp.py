import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS
from datetime import datetime

app = Flask(__name__)
# O CORS é vital para que o seu Dashboard no Wix consiga enviar mensagens manuais
CORS(app)

# ==========================================
# CONFIGURAÇÕES (VARIÁVEIS DE AMBIENTE)
# ==========================================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "conectifisio_2024_seguro")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = os.environ.get("WIX_WEBHOOK_URL", "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook")

# --- UTILITÁRIOS DE VALIDAÇÃO ---
def is_valid_cpf(cpf):
    """ Validação básica de 11 dígitos """
    cpf = re.sub(r'\D', '', cpf)
    return len(cpf) == 11 and not cpf == cpf[0] * 11

def extract_date(text):
    """ Tenta encontrar data no formato DD/MM/AAAA """
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
    """ Envia mensagem real via Meta API """
    if not WHATSAPP_TOKEN: return False
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        return res.status_code == 200
    except: return False

# ==========================================
# ROTA DE ENVIO MANUAL (INTERVENÇÃO HUMANA)
# ==========================================

@app.route("/api/send", methods=["POST"])
def send_manual():
    """ Rota chamada pelo Dashboard para falar como humano """
    data = request.get_json()
    to = data.get("to")
    message = data.get("message")
    
    if send_whatsapp(to, message):
        # Notifica o Wix para pausar o robô e mudar status
        try:
            requests.post(WIX_URL, json={
                "from": to, 
                "text": f"[HUMANO]: {message}", 
                "status": "atendimento_humano"
            }, timeout=5)
        except: pass
        return jsonify({"status": "success"}), 200
    return jsonify({"status": "error"}), 500

# ==========================================
# WEBHOOK PRINCIPAL (WHATSAPP -> VERCEL)
# ==========================================

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    """ Verificação da Meta """
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
            
            # Identifica unidade
            display_num = value.get("metadata", {}).get("display_phone_number", "")
            unit = "Ipiranga" if "23629360" in display_num else "SCS"

            # 1. Consulta o Wix (Histórico e Status)
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=10)
            info = res_wix.json() if res_wix.status_code == 200 else {}
            
            # --- TRAVA DE ATENDIMENTO HUMANO ---
            if info.get("currentStatus") == "atendimento_humano":
                return jsonify({"status": "human_mode"}), 200

            # --- DETECÇÃO DE DADOS (PRIORIDADE) ---
            only_numbers = re.sub(r'\D', '', text)
            found_date = extract_date(text)
            p_name = info.get("patientName", "")
            has_feegow = info.get("feegowId") is not None

            # A. Se enviou um CPF válido
            if len(only_numbers) == 11 and is_valid_cpf(only_numbers):
                reply = "✅ CPF validado! Agora, por favor, envie sua DATA DE NASCIMENTO (Ex: 15/05/1980)."
                requests.post(WIX_URL, json={"from": phone, "cpf": only_numbers})

            # B. Se enviou uma Data válida
            elif found_date:
                reply = f"📅 Data {found_date} registrada! Estamos a processar o seu cadastro no Feegow..."
                requests.post(WIX_URL, json={"from": phone, "birthDate": found_date})

            # C. Se já é veterano no sistema
            elif has_feegow and p_name not in ["Paciente Novo", "Aguardando Nome...", ""]:
                reply = f"Olá, {p_name}! Que bom falar consigo novamente na Conectifisio {unit}. Como posso ajudar hoje?\n\n1. Agendamento\n2. Atendimento Humano"

            # D. Captura de Nome (Loop Killer)
            elif p_name in ["Paciente Novo", "Aguardando Nome...", ""] or not p_name:
                if len(text) > 2 and len(only_numbers) < 10:
                    reply = f"Prazer, {text.title()}! Para iniciarmos seu cadastro na unidade {unit}, por favor envie seu CPF (apenas números)."
                    requests.post(WIX_URL, json={"from": phone, "name": text.title()})
                else:
                    reply = f"Olá! Bem-vindo à Conectifisio {unit}. Qual o seu NOME COMPLETO para iniciarmos?"
            
            # E. Fallback Inteligente
            else:
                reply = f"Olá {p_name}, como posso ajudar na unidade {unit} hoje?"

            send_whatsapp(phone, reply)

        return jsonify({"status": "success"}), 200
    except Exception as e:
        print(f"ERRO: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

if __name__ == "__main__":
    app.run(port=5000)
