import os
import json
import requests
import re
import time
import random
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v35.2 - BRASIL (PT-BR)
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- FUNÇÕES DE HUMANIZAÇÃO ---

def simular_digitacao(to, segundos=None):
    """
    Cria um atraso proposital antes de enviar a resposta.
    Isso faz o paciente ver o status 'digitando...' no WhatsApp.
    """
    if segundos is None:
        segundos = random.uniform(2.5, 4.5)
    time.sleep(segundos)

# --- FUNÇÕES DE ENVIO (API META INTERATIVA) ---

def enviar_texto(to, texto):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_botoes(to, texto, lista_botoes):
    """Envia botões de resposta rápida (Máximo 3)"""
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    botoes_payload = []
    for i, nome_botao in enumerate(lista_botoes):
        botoes_payload.append({
            "type": "reply",
            "reply": {"id": f"btn_{i}", "title": nome_botao}
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": texto},
            "action": {"buttons": botoes_payload}
        }
    }
    requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_lista(to, texto, etiqueta_botao, secoes):
    """Envia um menu suspenso (Lista) com até 10 opções"""
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "header": {"type": "text", "text": "Conectifisio"},
            "body": {"text": texto},
            "footer": {"text": "Toque no botão para escolher"},
            "action": {
                "button": etiqueta_botao,
                "sections": secoes
            }
        }
    }
    requests.post(url, json=payload, headers=headers, timeout=10)

# ==========================================
# WEBHOOK PRINCIPAL (LÓGICA DE ATENDIMENTO)
# ==========================================

@app.route("/api/whatsapp", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "entry" not in data: return jsonify({"status": "ok"}), 200

    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "messages" not in value: return jsonify({"status": "not_msg"}), 200

        message = value["messages"][0]
        phone = message["from"]
        
        # Identifica se a entrada veio por texto livre ou por clique em botão/lista
        msg_recebida = ""
        if message["type"] == "text":
            msg_recebida = message["text"]["body"].strip()
        elif message["type"] == "interactive":
            inter = message["interactive"]
            if inter["type"] == "button_reply":
                msg_recebida = inter["button_reply"]["title"]
            elif inter["type"] == "list_reply":
                msg_recebida = inter["list_reply"]["title"]

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # 1. SINCRONIZAÇÃO COM O WIX CMS
        try:
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
            info = res_wix.json()
        except:
            info = {"currentStatus": "triagem"}

        status = info.get("currentStatus", "triagem")

        # --- FLUXO DE CONVERSA HUMANIZADO (BRASIL) ---

        if status == "triagem":
            enviar_botoes(phone, 
                f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara iniciarmos seu atendimento com toda atenção, você já é nosso paciente?",
                ["Sim, já sou", "Não, primeira vez"]
            )
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_identificacao"})

        elif status == "aguardando_identificacao":
            if "Sim" in msg_recebida:
                enviar_texto(phone, "Que bom ter você de volta! 😊 Para localizarmos sua ficha rapidamente, como gostaria de ser chamado(a)?")
            else:
                enviar_texto(phone, "Seja bem-vindo! ✨ Para darmos início ao seu cadastro e agendamento, como gostaria de ser chamado(a)?")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome"})

        elif status == "cadastrando_nome":
            nome = msg_recebida.title()
            enviar_texto(phone, f"Prazer em te conhecer, {nome}! 😊\n\nConte-me um pouco: o que te trouxe à nossa clínica hoje? Qual sua principal queixa ou dor?")
            requests.post(WIX_URL, json={"from": phone, "name": nome, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            secoes = [{
                "title": "Nossas Especialidades",
                "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"},
                    {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"},
                    {"id": "s4", "title": "Acupuntura"},
                    {"id": "s5", "title": "Pilates Studio"},
                    {"id": "s6", "title": "Outros / Recovery"}
                ]
            }]
            enviar_lista(phone, 
                "Entendido. Vamos cuidar disso! Qual dessas especialidades você procura hoje?", 
                "Ver Especialidades", 
                secoes
            )
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "escolha_especialidade"})

        elif status == "escolha_especialidade":
            if "Neurológica" in msg_recebida:
                texto_neuro = (
                    "Olá! Tudo bem? 😊 Para darmos sequência ao seu agendamento de fisioterapia, "
                    "precisamos entender melhor seu grau de independência nas atividades do dia a dia.\n\n"
                    "Em qual dessas opções você se enquadra?\n\n"
                    "🔹 *Independente:* Realizo as atividades de forma autônoma e com segurança.\n\n"
                    "🤝 *Semidependente:* Consigo fazer algumas atividades sozinho(a), mas preciso de ajuda parcial ou de dispositivos auxiliares (bengala, andador).\n\n"
                    "👨‍🦽 *Dependente:* Preciso de ajuda total para me locomover e realizar atividades diárias."
                )
                enviar_botoes(phone, texto_neuro, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            else:
                enviar_botoes(phone,
                    "Perfeito! ✅ Como você deseja realizar o seu atendimento?",
                    ["Convênio", "Particular"]
                )
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": msg_recebida})

        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida:
                enviar_texto(phone, "Entendido. Como seu caso exige uma atenção especial, nosso fisioterapeuta responsável assumirá este contato agora para te dar suporte total. 👨‍⚕️")
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            else:
                enviar_botoes(phone, "Certo! ✅ Você deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            modalidade = "particular" if "Particular" in msg_recebida else "convenio"
            if modalidade == "particular":
                enviar_texto(phone, "No atendimento particular focamos na sua evolução total, com tempo e especialistas dedicados. 😊\n\nPor favor, digite seu CPF (apenas números) para seu registro.")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_cpf", "modalidade": "particular"})
            else:
                enviar_texto(phone, "Ótimo! Qual o nome do seu CONVÊNIO?")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        elif status == "cadastrando_convenio":
            enviar_texto(phone, "Anotado! Agora, por favor, digite o seu CPF (apenas números).")
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            enviar_botoes(phone, "CPF recebido! Qual o período da sua preferência para o agendamento?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "agendando":
            enviar_texto(phone, "Recebido! 🎉 Nossa equipe já recebeu seus dados e entrará em contato em instantes para confirmar o horário exato. Até já!")
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        return jsonify({"status": "success"}), 200

    except Exception as e:
        print(f"ERRO: {e}")
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403

if __name__ == "__main__":
    app.run(port=5000)
