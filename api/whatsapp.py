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
# CONFIGURAÇÕES v38.2 - ESTRITO AO MAPA BR
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
# URL fixa para garantir estabilidade na comunicação com o Wix
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- FUNÇÕES DE HUMANIZAÇÃO ---

def simular_digitacao(to, segundos=None):
    """Cria um atraso proposital para parecer que uma pessoa está a digitar"""
    if segundos is None:
        segundos = random.uniform(2.5, 4.0)
    time.sleep(segundos)

# --- FUNÇÕES DE ENVIO INTERATIVO (API META) ---

def enviar_texto(to, texto):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    try:
        requests.post(url, json=payload, headers=headers, timeout=10)
    except: pass

def enviar_botoes(to, texto, lista_botoes):
    """Envia botões azuis de resposta rápida (Máximo 3)"""
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    btns = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": b}} for i, b in enumerate(lista_botoes)]
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": btns}}
    }
    requests.post(url, json=payload, headers=headers)

def enviar_lista(to, texto, etiqueta_botao, secoes):
    """Envia o menu suspenso de especialidades"""
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list", "header": {"type": "text", "text": "Conectifisio"}, 
            "body": {"text": texto}, "action": { "button": etiqueta_botao, "sections": secoes }
        }
    }
    requests.post(url, json=payload, headers=headers)

# ==========================================
# WEBHOOK PRINCIPAL (ALINHADO À ESTRATÉGIA)
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
        msg_type = message.get("type")
        
        # Detecta se a entrada foi texto, clique em botão, lista ou imagem
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type == "image":
            msg_recebida = "[ARQUIVO_ENVIADO]"

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # 1. SINCRONIZAÇÃO COM O WIX CMS (Busca dados e estado atual)
        try:
            res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
            info = res_wix.json()
        except:
            info = {"currentStatus": "triagem"}

        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        p_modalidade = info.get("modalidade", "particular")

        # --- FLUXO v38.2 (QUEBRA DE RESISTÊNCIA À ENTREGA DE DADOS) ---

        if status == "triagem":
            enviar_botoes(phone, 
                f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara iniciarmos seu atendimento, você já é nosso paciente?",
                ["Sim, já sou", "Não, primeira vez"]
            )
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_identificacao"})

        elif status == "aguardando_identificacao":
            if "Sim" in msg_recebida:
                # VETERANO: Saudação direta e Menu simplificado
                saudacao = f"Que bom ter você de volta, {p_name}! 😊" if p_name else "Que bom ter você de volta! 😊"
                enviar_botoes(phone, 
                    f"{saudacao} Como nossos atendimentos funcionam em blocos de 10 sessões, como posso te ajudar hoje?",
                    ["Retomar tratamento", "Novo pacote", "Outro assunto"]
                )
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                # NOVO PACIENTE: Especialidade PRIMEIRO (Gera valor)
                secoes = [{"title": "Especialidades", "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"}
                ]}]
                enviar_lista(phone, "Seja muito bem-vindo! ✨ Qual serviço você procura hoje?", "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_especialidade"})

        elif status == "menu_veterano":
            enviar_botoes(phone, "Certo! E em qual período você tem preferência para agendar seu retorno?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando", "servico": msg_recebida})

        elif status == "escolha_especialidade":
            if "Neurológica" in msg_recebida:
                # Triagem de Mobilidade (Escala profissional)
                texto_neuro = ("Excelente. 😊 Para agendarmos corretamente, como está a mobilidade do paciente?\n\n🔹 *Independente*\n🤝 *Semidependente*\n👨‍🦽 *Dependente*")
                enviar_botoes(phone, texto_neuro, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            else:
                enviar_botoes(phone, "Perfeito! ✅ Você deseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": msg_recebida})

        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida:
                enviar_texto(phone, "Como seu caso exige atenção especial, nosso fisioterapeuta responsável assumirá este contato agora. 👨‍⚕️")
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            else:
                enviar_botoes(phone, "Certo! ✅ Você deseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            modalidade = "particular" if "Particular" in msg_recebida else "convenio"
            enviar_texto(phone, "Entendido! Vamos agora realizar seu cadastro rápido para o agendamento.\n\nQual o seu NOME COMPLETO?")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome", "modalidade": modalidade})

        elif status == "cadastrando_nome":
            enviar_texto(phone, "Qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "cadastrando_data"})

        elif status == "cadastrando_data":
            enviar_texto(phone, "E qual o seu melhor E-MAIL para enviarmos os lembretes de sessões?")
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "cadastrando_email"})

        elif status == "cadastrando_email":
            enviar_texto(phone, "O que te trouxe à clínica hoje? (Qual sua dor ou queixa principal?)")
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            enviar_texto(phone, "Obrigado por compartilhar! 😊 Agora, digite seu CPF (apenas números) para registro.")
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            if p_modalidade == "convenio":
                enviar_texto(phone, "CPF recebido! Para validarmos sua cobertura, envie agora uma FOTO da sua CARTEIRINHA do convênio.")
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "aguardando_carteirinha"})
            else:
                enviar_botoes(phone, "Cadastro concluído! Qual período você prefere para o agendamento?", ["Manhã", "Tarde"])
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "aguardando_carteirinha":
            enviar_texto(phone, "Recebido! Agora, por favor, envie uma FOTO do seu PEDIDO MÉDICO.")
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            enviar_botoes(phone, "Documentos recebidos! 🎉 Qual período você prefere?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

        elif status == "agendando":
            enviar_texto(phone, "Tudo pronto! 🎉 Nossa equipe entrará em contato em instantes para confirmar o horário exato. Até já!")
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
