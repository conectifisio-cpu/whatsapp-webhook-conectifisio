import os
import json
import requests
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v33.1 - ANTI-ERRO 500
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = os.environ.get("WIX_WEBHOOK_URL")

# --- MENUS E TEXTOS ---
SERVICOS_MENU = (
    "1. Fisioterapia Ortopédica\n"
    "2. Fisioterapia Neurológica\n"
    "3. Fisioterapia Pélvica\n"
    "4. Acupuntura\n"
    "5. Pilates Studio\n"
    "6. Recovery / Liberação Miofascial"
)

MSG_VALOR_PARTICULAR = (
    "Entendi perfeitamente a sua queixa; vamos avaliar a melhor forma de o(a) ajudar. 😊\n\n"
    "O nosso foco é que volte a movimentar-se sem dor, com segurança e qualidade de vida. "
    "Nos atendimentos particulares conseguimos um plano individualizado, com atenção total à sua evolução. "
    "Trabalhamos com especialistas e tecnologia moderna.\n\n"
    "Trabalhamos com sessões avulsas e pacotes flexíveis. Quer que lhe mostre como funciona na prática?"
)

# --- FUNÇÕES DE APOIO ---
def send_whatsapp(to, text):
    """Envia mensagem via API do WhatsApp Cloud (Meta)"""
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar WhatsApp: {e}")

def extract_cpf(text):
    """Limpa o texto e valida se tem 11 dígitos"""
    nums = re.sub(r'\D', '', text)
    return nums if len(nums) == 11 else None

# ==========================================
# WEBHOOK PRINCIPAL (ESTADOS v33.1)
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    
    # Prevenção de Erro 500 (Ignora notificações que não sejam mensagens)
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
        
        # Identificação da Unidade pelo número de destino
        display_phone = value.get("metadata", {}).get("display_phone_number", "")
        unit = "Ipiranga" if "23629360" in display_phone else "SCS"

        # 1. COMUNICAÇÃO COM O WIX (Identifica o estado do paciente)
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": text, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        p_modalidade = info.get("modalidade", "particular")

        # Se houver intervenção humana no Dashboard, o robô silencia
        if status == "atendimento_humano":
            return jsonify({"status": "human_mode_active"}), 200

        # --- MÁQUINA DE ESTADOS ---
        reply = ""

        # ESTADO: TRIAGEM INICIAL
        if status == "triagem":
            if p_name and p_name != "Paciente Novo":
                reply = f"Olá, {p_name}! Que bom falar consigo novamente na Conectifisio unidade {unit}! 😊\n\nJá está em tratamento connosco no momento ou deseja iniciar um novo Plano de Tratamento?"
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                reply = f"Olá! ✨ Seja bem-vindo à Conectifisio unidade {unit}. Para iniciarmos o seu atendimento, já é paciente da nossa clínica?"
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_identificacao"})

        # ESTADO: IDENTIFICAÇÃO
        elif status == "aguardando_identificacao":
            if "sim" in text.lower() or "já" in text.lower():
                reply = "Que bom tê-lo(a) de volta! 😊 Para localizarmos o seu registo, como gostaria de ser chamado(a)?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})
            else:
                reply = "Seja bem-vindo pela primeira vez! ✨ Para darmos início, como gostaria de ser chamado(a)?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})

        # ESTADO: ESCUTA ATIVA (QUEIXA)
        elif status == "cadastrando_nome" or (status == "menu_veterano" and "novo" in text.lower()):
            nome_final = text.title() if status == "cadastrando_nome" else p_name
            reply = f"Prazer, {nome_final}! 😊 Conte-me um pouco: o que o(a) trouxe à Conectifisio hoje? (Qual a sua dor ou queixa principal?)"
            requests.post(WIX_URL, json={"from": phone, "name": nome_final, "status": "cadastrando_queixa"})

        # ESTADO: ESCOLHA DE SERVIÇO
        elif status == "cadastrando_queixa":
            reply = f"Entendi. Para o(a) ajudarmos da melhor forma, qual o serviço que procura na unidade {unit}?\n\n{SERVICOS_MENU}"
            requests.post(WIX_URL, json={"from": phone, "queixa": text, "status": "escolha_especialidade"})

        # ESTADO: TRIAGEM NEURO E PAGAMENTO
        elif status == "escolha_especialidade":
            if "2" in text or "neuro" in text.lower():
                reply = "Como está a mobilidade do paciente? (Independente, Semidependente ou Dependente)"
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            else:
                reply = "Entendido! ✅ Deseja realizar o atendimento pelo seu CONVÉNIO ou de forma PARTICULAR?"
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": text})

        elif status == "triagem_neuro":
            if "independente" in text.lower():
                reply = "Perfeito! ✅ Deseja atendimento pelo seu CONVÉNIO ou de forma PARTICULAR?"
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})
            else:
                reply = "Para casos que exigem suporte especializado, o nosso fisioterapeuta responsável assumirá o contacto agora. 👨‍⚕️"
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})

        # ESTADO: CONVÉNIO OU PARTICULAR
        elif status == "escolha_modalidade":
            modalidade = "particular" if "particular" in text.lower() else "convenio"
            if modalidade == "particular":
                reply = MSG_VALOR_PARTICULAR
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_cpf", "modalidade": "particular"})
            else:
                reply = "Combinado! Qual o nome do seu CONVÉNIO?"
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        elif status == "cadastrando_convenio":
            reply = f"Anotado! Agora, por favor, digite o seu CPF (apenas números)."
            requests.post(WIX_URL, json={"from": phone, "convenio": text, "status": "cadastrando_cpf"})

        # ESTADO: DOCUMENTAÇÃO
        elif status == "cadastrando_cpf":
            cpf = extract_cpf(text)
            if cpf:
                if p_modalidade == "convenio":
                    reply = "CPF anotado! Para validarmos a cobertura, envie primeiro uma foto da sua CARTEIRINHA."
                    requests.post(WIX_URL, json={"from": phone, "cpf": cpf, "status": "aguardando_carteirinha"})
                else:
                    reply = "CPF anotado! Qual o período da sua preferência: Manhã ou Tarde? 🕒"
                    requests.post(WIX_URL, json={"from": phone, "cpf": cpf, "status": "agendando"})
            else:
                reply = "CPF inválido. Por favor, envie os 11 números novamente."

        elif status == "aguardando_carteirinha":
            reply = "Obrigado! Agora, envie também uma foto do seu PEDIDO MÉDICO (emitido há até 60 dias)."
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            reply = "Documentos recebidos! Qual a sua preferência de horário: Manhã ou Tarde? 🕒"
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

        # ESTADO: FINALIZAÇÃO
        elif status == "agendando":
            reply = "Agendamento pré-confirmado! 🎉 A nossa equipa irá contactá-lo(a) em instantes para confirmar o horário exato. Até já!"
            send_whatsapp(phone, reply)
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        if reply: send_whatsapp(phone, reply)
        return jsonify({"status": "success"}), 200

    except Exception as e:
        # Prevenção de Erro 500 nos logs
        print(f"ERRO CRÍTICO: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 200

# Endpoint de Verificação da Meta
@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro de Token", 403
