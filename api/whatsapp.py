import os
import json
import requests
import time
import random
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

# ==========================================
# CONFIGURAÇÕES v56.0 - SISTEMA CONSOLIDADO
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- MOTOR DE HUMANIZAÇÃO ---

def simular_digitacao(to):
    """Simula o estado 'a escrever...' para humanizar o atendimento"""
    atraso = random.uniform(2.5, 4.5)
    time.sleep(atraso)

# --- FUNÇÕES DE ENVIO (API META) ---

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
    # Limite de 3 botões por mensagem na API do WhatsApp
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

# ==========================================
# WEBHOOK PRINCIPAL (LÓGICA BLINDADA v56.0)
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
        
        # Captura de input do paciente
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # 1. CONSULTA AO WIX (RECONHECIMENTO BLINDADO)
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        is_veteran = info.get("isVeteran", False)
        servico_atual = info.get("servico", "atendimento")
        p_convenio = info.get("convenio", "")
        p_modalidade = info.get("modalidade", "").lower()

        # --- LÓGICA DE CONTINUIDADE INTELIGENTE ---
        if msg_recebida.lower() in ["oi", "olá", "ola", "bom dia"] and status not in ["triagem", "finalizado", "menu_veterano"]:
            enviar_botoes(phone, 
                f"Olá! ✨ Notei que estávamos a meio do seu pedido de {servico_atual}. Podemos continuar de onde parámos?",
                ["Sim, continuar", "Recomeçar Atendimento"]
            )
            return jsonify({"status": "success"}), 200

        # --- NAVEGAÇÃO GLOBAL (RESET / VOLTAR) ---
        if "Recomeçar" in msg_recebida or "Menu Inicial" in msg_recebida or "⬅️ Voltar" in msg_recebida:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
            enviar_texto(phone, "Entendido! Vamos recomeçar o seu atendimento. 😊")
            status = "triagem"

        # --- FLUXO PRINCIPAL v56.0 ---

        if status == "triagem":
            if is_veteran:
                # VETERANO: Saudação Direta (Blindada)
                txt = f"Olá, {p_name}! ✨ Que bom ter você de volta na Conectifisio unidade {unit}.\n\nComo posso facilitar o seu dia hoje?"
                secoes = [{"title": "Escolha uma opção", "rows": [
                    {"id": "v1", "title": "🗓️ Reagendar Sessão"},
                    {"id": "v2", "title": "🔄 Continuar Tratamento"},
                    {"id": "v3", "title": "➕ Novo Serviço"},
                    {"id": "v4", "title": "📁 Outras Solicitações"}
                ]}]
                enviar_lista(phone, txt, "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                # NOVO: Inicia Acolhimento Humano
                enviar_texto(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara começarmos o seu atendimento, como gostaria de ser chamado(a)?")
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_nome_novo"})

        # --- LÓGICA VETERANO (MAPA v1.8) ---
        elif status == "menu_veterano":
            if "Reagendar" in msg_recebida:
                # Cenário Direto (Scenario 1) ou Suporte (Scenario 2)
                enviar_texto(phone, "Não encontrei nenhum agendamento recente para o seu perfil. Mas não se preocupe, vou resolver isso para você agora mesmo! 😊")
                enviar_botoes(phone, "Para agilizarmos, qual o melhor período para você?", ["Manhã", "Tarde", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_reagendando_periodo"})
            
            elif "Continuar Tratamento" in msg_recebida:
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÉNIO ou de forma PARTICULAR?", ["💳 Convénio", "💎 Particular", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_escolha_modalidade"})

            elif "Novo Serviço" in msg_recebida:
                secoes = [{"title": "Serviços", "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"},
                    {"id": "s0", "title": "⬅️ Voltar"}
                ]}]
                enviar_lista(phone, "Qual destes novos serviços procura hoje?", "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_especialidade"})

            elif "Outras Solicitações" in msg_recebida:
                enviar_lista(phone, "Como podemos ajudar?", "Ver Opções", [{"title": "Solicitações", "rows": [
                    {"id": "o1", "title": "📄 Atestado Pendente"},
                    {"id": "o2", "title": "📝 Relatório Pendente"},
                    {"id": "o3", "title": "👤 Falar com Recepção"},
                    {"id": "o4", "title": "⬅️ Voltar"}
                ]}])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_outros"})

        elif status == "veterano_escolha_modalidade":
            if "Particular" in msg_recebida:
                enviar_botoes(phone, "Excelente! Vamos seguir para o agendamento. Qual período prefere?", ["Manhã", "Tarde", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "agendando", "modalidade": "particular"})
            else:
                plano = p_convenio if p_convenio else "registado"
                enviar_botoes(phone, f"Você continua a utilizar o convénio {plano} ou houve mudança no seu plano de saúde?", ["✅ Mesmo Convénio", "🔄 Troquei de Plano", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_valida_convenio"})

        elif status == "veterano_valida_convenio":
            if "Mesmo" in msg_recebida:
                enviar_botoes(phone, "Já está com o novo Pedido Médico em mãos?", ["✅ Sim, já tenho", "❌ Ainda não", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})
            elif "Troquei" in msg_recebida:
                enviar_texto(phone, "Entendido! Vamos atualizar os seus dados cadastrais.\n\nQual o nome do seu **NOVO CONVÉNIO**?")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        # --- FLUXO DE ESPECIALIDADES & NEURO DIDÁTICA ---
        elif status == "escolha_especialidade":
            servico = msg_recebida
            if "Neurológica" in servico:
                explicacao = (
                    "Para agendarmos com o especialista ideal, como está a mobilidade do paciente?\n\n"
                    "🔹 *Independente:* Realiza tarefas sozinho e com segurança.\n\n"
                    "🤝 *Semidependente:* Precisa de ajuda parcial ou dispositivos de apoio (andador/bengala).\n\n"
                    "👨‍🦽 *Dependente:* Precisa de auxílio constante para se movimentar."
                )
                enviar_botoes(phone, explicacao, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            elif servico in ["Recovery", "Liberação Miofascial"]:
                enviar_texto(phone, f"Ótima escolha! O serviço de {servico} é focado em bem-estar e performance. ✨")
                enviar_texto(phone, "Por favor, digite o seu **NOME COMPLETO** (conforme documento) para iniciarmos o cadastro:")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": "particular", "servico": servico})
            else:
                enviar_botoes(phone, f"Deseja atendimento de {servico} pelo seu CONVÉNIO ou PARTICULAR?", ["Convénio", "Particular", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": servico})

        # --- CADASTRO (ORDEM DE VALOR) ---
        elif status == "aguardando_nome_novo":
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "escolha_especialidade"})
            # Gatilho de auto-chamada para carregar a lista de serviços imediatamente
            return webhook() 

        elif status == "escolha_modalidade":
            mod_limpa = "convenio" if "Convénio" in msg_recebida else "particular"
            enviar_texto(phone, "Entendido! Vamos realizar seu cadastro rápido para o agendamento.\n\nPor favor, digite agora o seu **NOME COMPLETO** (conforme documento):")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": mod_limpa})

        elif status == "cadastrando_nome_completo":
            enviar_texto(phone, "Qual a sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "cadastrando_data"})

        elif status == "cadastrando_data":
            enviar_texto(phone, "Qual o seu melhor E-MAIL para enviarmos lembretes?")
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "cadastrando_email"})

        elif status == "cadastrando_email":
            enviar_texto(phone, "O que o trouxe à nossa clínica hoje? (Sua dor ou queixa principal?)")
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            enviar_texto(phone, "Obrigado por partilhar! 😊 Agora, digite o seu CPF (apenas números).")
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            if p_modalidade == "convenio":
                enviar_texto(phone, "CPF recebido! Qual o nome do seu CONVÉNIO?")
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "cadastrando_convenio"})
            else:
                enviar_botoes(phone, "Cadastro concluído! Qual período você prefere para o agendamento?", ["Manhã", "Tarde", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "cadastrando_convenio":
            enviar_texto(phone, "Digite agora o NÚMERO DA SUA CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_num_carteirinha"})

        elif status == "cadastrando_num_carteirinha":
            enviar_texto(phone, "Anotado! Agora, envie uma FOTO da sua CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "numCarteirinha": msg_recebida, "status": "aguardando_carteirinha"})

        elif status == "aguardando_carteirinha":
            enviar_texto(phone, "Recebido! Agora, por favor, envie uma FOTO do seu PEDIDO MÉDICO.")
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            enviar_botoes(phone, "Documentos recebidos! 🎉 Qual período você prefere para o agendamento?", ["Manhã", "Tarde", "⬅️ Voltar"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

        elif status == "agendando":
            enviar_texto(phone, "Tudo pronto! 🎉 Nossa equipa já recebeu os seus dados e entrará em contacto em instantes para confirmar o horário exato. Até já!")
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
