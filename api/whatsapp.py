import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v33.5 - ULTRA ESTÁVEL
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")

# URL Direta para garantir comunicação imediata com o Wix
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

SERVICOS_MENU = (
    "1. Fisioterapia Ortopédica\n"
    "2. Fisioterapia Neurológica\n"
    "3. Fisioterapia Pélvica\n"
    "4. Acupuntura\n"
    "5. Pilates Studio\n"
    "6. Recovery / Liberação Miofascial"
)

# --- FUNÇÕES DE APOIO ---
def send_whatsapp(to, text):
    """Envia mensagem via API do WhatsApp Cloud"""
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
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"DEBUG META: {r.status_code} - {r.text}")
    except Exception as e:
        print(f"ERRO ENVIO META: {e}")

def extract_cpf(text):
    """Limpa e valida CPF (11 dígitos)"""
    nums = re.sub(r'\D', '', text)
    return nums if len(nums) == 11 else None

# ==========================================
# WEBHOOK PRINCIPAL (ESTADOS E LÓGICA)
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    
    # Proteção contra dados vazios ou notificações de sistema
    if not data or "entry" not in data:
        return jsonify({"status": "no_data"}), 200

    try:
        entry = data["entry"][0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        
        if "messages" not in value:
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        text = message.get("text", {}).get("body", "").strip()
        
        # Identificação automática da Unidade pelo número de destino
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unit = "Ipiranga" if "23629360" in display_phone else "SCS"

        # 1. SINCRONIZAÇÃO COM O WIX (Pede o estado atual do paciente)
        print(f"DEBUG WIX: Sincronizando {phone}...")
        try:
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=15)
            info = res_wix.json()
        except Exception as e:
            print(f"DEBUG WIX ERRO: {e}")
            return jsonify({"status": "wix_offline"}), 200

        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        p_modalidade = info.get("modalidade", "particular")

        # Se houver intervenção humana no Command Center, o robô para
        if status == "atendimento_humano":
            return jsonify({"status": "human_intervention"}), 200

        # --- MÁQUINA DE ATENDIMENTO ---
        reply = ""

        if status == "triagem":
            if p_name and p_name != "Paciente Novo":
                reply = f"Olá, {p_name}! Que bom falar consigo novamente na Conectifisio {unit}! 😊\n\nDeseja iniciar um novo Plano de Tratamento hoje?"
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                reply = f"Olá! ✨ Seja bem-vindo à Conectifisio unidade {unit}. Para iniciarmos, como gostaria de ser chamado(a)?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})

        elif status == "cadastrando_nome":
            nome_limpo = text.title()
            reply = f"Prazer, {nome_limpo}! 😊 O que o(a) trouxe à clínica hoje? (Qual a sua dor ou queixa principal?)"
            requests.post(WIX_URL, json={"from": phone, "name": nome_limpo, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            reply = f"Entendi. Qual serviço procura hoje?\n\n{SERVICOS_MENU}"
            requests.post(WIX_URL, json={"from": phone, "queixa": text, "status": "escolha_especialidade"})

        elif status == "escolha_especialidade":
            if "2" in text or "neuro" in text.lower():
                reply = "Como está a mobilidade do paciente? (Independente, Semidependente ou Dependente)"
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            else:
                reply = "Deseja atendimento pelo CONVÉNIO ou de forma PARTICULAR?"
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": text})

        elif status == "triagem_neuro":
            if "independente" in text.lower():
                reply = "Certo! ✅ Deseja atendimento pelo CONVÉNIO ou de forma PARTICULAR?"
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})
            else:
                reply = "O nosso fisioterapeuta responsável assumirá o contacto agora para lhe dar atenção total. 👨‍⚕️"
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})

        elif status == "escolha_modalidade":
            modalidade = "particular" if "particular" in text.lower() else "convenio"
            if modalidade == "particular":
                reply = "No atendimento particular focamos na sua evolução total. Digite o seu CPF (apenas números)."
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_cpf", "modalidade": "particular"})
            else:
                reply = "Qual o nome do seu CONVÉNIO?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        elif status == "cadastrando_convenio":
            reply = "Anotado! Agora, digite o seu CPF (apenas números)."
            requests.post(WIX_URL, json={"from": phone, "convenio": text, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            cpf = extract_cpf(text)
            if list(cpf): # Validação simples se existe algo
                reply = "CPF anotado! Qual o período da sua preferência: Manhã ou Tarde? 🕒"
                requests.post(WIX_URL, json={"from": phone, "cpf": cpf, "status": "agendando"})
            else:
                reply = "CPF inválido. Por favor, digite os 11 números novamente."

        elif status == "agendando":
            reply = "Agendamento pré-confirmado! 🎉 Entraremos em contacto em instantes."
            send_whatsapp(phone, reply)
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        if reply: send_whatsapp(phone, reply)
        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"ERRO GLOBAL: {e}")
        return jsonify({"status": "error_handled"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
