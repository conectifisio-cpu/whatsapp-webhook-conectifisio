import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v33.1
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = os.environ.get("WIX_WEBHOOK_URL")

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
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except:
        pass

def extract_cpf(text):
    nums = re.sub(r'\D', '', text)
    return nums if len(nums) == 11 else None

# ==========================================
# WEBHOOK PRINCIPAL (ROBUSTO)
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    
    # Validação de Segurança contra Erro 500
    if not data or "entry" not in data:
        return jsonify({"status": "no_data"}), 200

    try:
        entry = data["entry"][0]
        changes = entry.get("changes", [{}])[0]
        value = changes.get("value", {})
        
        # Ignora se não for uma mensagem (ex: confirmação de leitura/entrega)
        if "messages" not in value:
            return jsonify({"status": "not_a_message"}), 200

        message = value["messages"][0]
        phone = message["from"]
        
        # Ignora se a mensagem não tiver texto (ex: se for uma imagem sem legenda no início)
        text = message.get("text", {}).get("body", "").strip()
        if not text and "image" not in message:
            return jsonify({"status": "empty_text"}), 200

        # Identificação da Unidade
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unit = "Ipiranga" if "23629360" in display_phone else "SCS"

        # 1. COMUNICAÇÃO COM O WIX
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        p_modalidade = info.get("modalidade", "particular")

        # Se houver intervenção humana no Dashboard, o robô silencia
        if status == "atendimento_humano":
            return jsonify({"status": "human_mode"}), 200

        # --- LÓGICA DE ATENDIMENTO ---
        reply = ""

        if status == "triagem":
            if p_name and p_name != "Paciente Novo":
                reply = f"Olá, {p_name}! Que bom falar consigo novamente na Conectifisio unidade {unit}! 😊\n\nJá está em tratamento connosco ou deseja iniciar um novo Plano?"
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                reply = f"Olá! ✨ Seja bem-vindo à Conectifisio unidade {unit}. Já é nosso paciente?"
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_identificacao"})

        elif status == "aguardando_identificacao":
            reply = "Seja bem-vindo! ✨ Para iniciarmos, como gostaria de ser chamado(a)?"
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})

        elif status == "cadastrando_nome" or (status == "menu_veterano" and "novo" in text.lower()):
            nome = text.title() if status == "cadastrando_nome" else p_name
            reply = f"Prazer, {nome}! 😊 O que o(a) trouxe à clínica hoje? (Qual a sua dor ou queixa principal?)"
            requests.post(WIX_URL, json={"from": phone, "name": nome, "status": "cadastrando_queixa"})

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
                reply = "Deseja atendimento pelo CONVÉNIO ou de forma PARTICULAR?"
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})
            else:
                reply = "O nosso fisioterapeuta responsável assumirá o contacto agora para lhe dar atenção total. 👨‍⚕️"
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})

        elif status == "escolha_modalidade":
            if "particular" in text.lower():
                reply = "Entendido. No atendimento particular focamos na sua evolução total. Digite o seu CPF (apenas números)."
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_cpf", "modalidade": "particular"})
            else:
                reply = "Qual o nome do seu CONVÉNIO?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        elif status == "cadastrando_convenio":
            reply = f"Anotado! Agora, digite o seu CPF (apenas números)."
            requests.post(WIX_URL, json={"from": phone, "convenio": text, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            cpf = extract_cpf(text)
            if cpf:
                if p_modalidade == "convenio":
                    reply = "CPF anotado! Para validarmos, envie uma foto da sua CARTEIRINHA."
                    requests.post(WIX_URL, json={"from": phone, "cpf": cpf, "status": "aguardando_carteirinha"})
                else:
                    reply = "CPF anotado! Qual o período da sua preferência: Manhã ou Tarde? 🕒"
                    requests.post(WIX_URL, json={"from": phone, "cpf": cpf, "status": "agendando"})
            else:
                reply = "CPF inválido. Digite os 11 números novamente."

        elif status == "aguardando_carteirinha":
            reply = "Obrigado! Agora envie a foto do seu PEDIDO MÉDICO."
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            reply = "Recebido! Qual a sua preferência: Manhã ou Tarde? 🕒"
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

        elif status == "agendando":
            reply = "Agendamento pré-confirmado! 🎉 Entraremos em contacto em instantes."
            send_whatsapp(phone, reply)
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        if reply: send_whatsapp(phone, reply)
        return jsonify({"status": "success"}), 200

    except Exception as e:
        # Em caso de erro, não crashar (evitar Erro 500 no log)
        print(f"ERRO: {str(e)}")
        return jsonify({"status": "error", "msg": str(e)}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
