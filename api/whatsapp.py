import os
import json
import requests
import time
import re
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v69.0 - MODO DE TESTE E REGRAS CONSOLIDADAS
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

def simular_digitacao(to):
    time.sleep(0.5)

def enviar_texto(to, texto):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": texto}}
    requests.post(url, json=payload, headers=headers, timeout=10)

def enviar_botoes(to, texto, lista_botoes):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    btns = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": b}} for i, b in enumerate(lista_botoes[:3])]
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {"type": "button", "body": {"text": texto}, "action": {"buttons": btns}}
    }
    requests.post(url, json=payload, headers=headers)

def enviar_lista(to, texto, label, secoes):
    simular_digitacao(to)
    url = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "interactive",
        "interactive": {
            "type": "list", "header": {"type": "text", "text": "Conectifisio"}, 
            "body": {"text": texto}, "action": { "button": label, "sections": secoes }
        }
    }
    requests.post(url, json=payload, headers=headers)

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
        
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # --- COMANDO MÁGICO DE TESTE (PARA O DR. ISSA) ---
        if msg_recebida.lower() in ["resetar tudo", "limpar meu cadastro", "sou novo"]:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem", "name": "Paciente Novo"})
            enviar_texto(phone, "🔄 Cadastro resetado com sucesso! Agora o sistema tratará você como um **NOVO PACIENTE**. Envie 'Oi' para começar.")
            return jsonify({"status": "success"}), 200

        # 1. CONSULTA AO WIX
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        is_veteran = info.get("isVeteran", False)

        # --- REINÍCIO E VOLTAR ---
        if msg_recebida in ["Recomeçar", "Menu Inicial", "⬅️ Voltar"]:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
            enviar_texto(phone, "Entendido! Vamos recomeçar o seu atendimento. 😊")
            status = "triagem"

        # --- CONTINUIDADE INTELIGENTE ---
        elif msg_recebida == "Sim, continuar":
            prompts = {
                "pilates_caixa_nome": "Por favor, digite seu NOME COMPLETO:",
                "pilates_caixa_data": "Qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)",
                "pilates_caixa_email": "Qual o seu melhor E-MAIL?",
                "pilates_caixa_cpf": "Digite o seu CPF (apenas números):",
                "pilates_caixa_carteirinha": "Por favor, envie uma FOTO da sua CARTEIRINHA:",
                "pilates_caixa_pedido": "Por favor, envie uma FOTO do seu PEDIDO MÉDICO:",
                "pilates_aguardando_nome_particular": "Por favor, digite seu NOME COMPLETO:",
                "pilates_aguardando_nome": "Como gostaria de ser chamado(a)?",
                "performance_nome": "Por favor, digite seu NOME COMPLETO:",
                "aguardando_nome_novo": "Como gostaria de ser chamado(a)?"
            }
            texto = prompts.get(status, "Por favor, continue de onde paramos.")
            enviar_texto(phone, f"Ótimo! 😊 {texto}")
            return jsonify({"status": "success"}), 200

        # --- DETECÇÃO DE SAUDAÇÃO ---
        is_greeting = False
        if msg_type == "text":
            msg_limpa = re.sub(r'[^\w\s]', '', msg_recebida.lower().strip())
            saudacoes = ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite"]
            for s in saudacoes:
                if s in msg_limpa and len(msg_limpa) <= 25:
                    is_greeting = True
                    break

        if is_greeting and status not in ["triagem", "menu_veterano", "finalizado"]:
            enviar_botoes(phone, "Olá! ✨ Notei que estávamos no meio do seu pedido. Podemos continuar?", ["Sim, continuar", "Recomeçar"])
            return jsonify({"status": "success"}), 200

        # ==========================================
        # FLUXO DE NAVEGAÇÃO
        # ==========================================
        if status == "triagem":
            if is_veteran:
                txt = f"Olá, {p_name}! ✨ Que bom ter você de volta na Conectifisio unidade {unit}.\n\nComo posso facilitar seu dia hoje?"
                secoes = [{"title": "Opções", "rows": [
                    {"id": "v1", "title": "🗓️ Reagendar Sessão"}, {"id": "v2", "title": "🔄 Continuar Tratamento"},
                    {"id": "v3", "title": "➕ Novo Serviço"}, {"id": "v4", "title": "📁 Outras Solicitações"}
                ]}]
                enviar_lista(phone, txt, "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                enviar_texto(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara começarmos seu atendimento, como gostaria de ser chamado(a)?")
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_nome_novo"})

        elif status == "menu_veterano":
            if "Novo Serviço" in msg_recebida:
                secoes = [{"title": "Serviços", "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"},
                    {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"},
                    {"id": "s0", "title": "⬅️ Voltar"}
                ]}]
                enviar_lista(phone, "Qual desses novos serviços você procura hoje?", "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_especialidade"})
            elif "Continuar Tratamento" in msg_recebida:
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou PARTICULAR?", ["Convênio", "Particular", "Menu Inicial"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_escolha_modalidade"})

        elif status == "aguardando_nome_novo":
            nome_informado = msg_recebida.title()
            secoes = [{"title": "Serviços", "rows": [
                {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"},
                {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"}
            ]}]
            enviar_lista(phone, f"Prazer em conhecer, {nome_informado}! 😊 Qual serviço você procura hoje?", "Ver Opções", secoes)
            requests.post(WIX_URL, json={"from": phone, "name": nome_informado, "status": "escolha_especialidade"})

        elif status == "escolha_especialidade":
            if "Pilates Studio" in msg_recebida:
                enviar_texto(phone, "Excelente escolha! 🧘‍♀️ O Pilates é fundamental para a correção postural e fortalecimento.")
                enviar_botoes(phone, "Como você pretende realizar as aulas?", ["Wellhub / Totalpass", "Saúde Caixa", "Plano Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "pilates_triagem_modalidade", "servico": "Pilates"})
            elif msg_recebida in ["Recovery", "Liberação Miofascial"]:
                enviar_texto(phone, f"O serviço de **{msg_recebida}** é focado em performance, sendo realizado exclusivamente de forma **PARTICULAR**. ✨")
                enviar_texto(phone, "Para darmos sequência, por favor digite o seu **NOME COMPLETO**:")
                requests.post(WIX_URL, json={"from": phone, "status": "performance_nome", "servico": msg_recebida, "modalidade": "particular"})
            else:
                enviar_botoes(phone, "Deseja atendimento pelo CONVÊNIO ou PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": msg_recebida})

        # --- LÓGICA PILATES STUDIO ---
        elif status == "pilates_triagem_modalidade":
            if "Saúde Caixa" in msg_recebida:
                enviar_texto(phone, "Entendido! 🏦 Para o Saúde Caixa, é necessária autorização prévia e o pedido médico.")
                if is_veteran:
                    enviar_texto(phone, "Como já temos seus dados, envie uma FOTO do seu PEDIDO MÉDICO atualizado:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_pedido", "modalidade": "convenio", "convenio": "Saúde Caixa"})
                else:
                    enviar_texto(phone, "Para iniciarmos seu cadastro rápido, por favor, digite seu **NOME COMPLETO**:")
                    requests.post(WIX_URL, json={"from": phone, "status": "pilates_caixa_nome", "modalidade": "convenio", "convenio": "Saúde Caixa"})
            elif "Particular" in msg_recebida:
                enviar_texto(phone, "No nosso estúdio você conta com fisioterapeutas especializados e equipamentos de ponta. ✨")
                enviar_texto(phone, "Para podermos passar mais detalhes, por favor, digite seu **NOME COMPLETO**:")
                requests.post(WIX_URL, json={"from": phone, "status": "pilates_aguardando_nome_particular", "modalidade": "particular"})
            elif "Wellhub" in msg_recebida:
                enviar_texto(phone, "Perfeito! ✅ Aceitamos os planos **Golden (Wellhub)** e **TP5 (Totalpass)**.")
                enviar_texto(phone, "Como gostaria de ser chamado(a)?")
                requests.post(WIX_URL, json={"from": phone, "status": "pilates_aguardando_nome", "modalidade": "parceria"})

        # (Fluxos de cadastro Caixa e Particular continuam aqui...)
        # [OMITIDOS PARA BREVIDADE, MAS PRESENTES NO CÓDIGO COMPLETO]

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
