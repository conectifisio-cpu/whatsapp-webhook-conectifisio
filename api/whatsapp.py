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
# CONFIGURAÇÕES v63.0 - FLUXO SOLIDIFICADO & MENUS COMPLETOS
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

def simular_digitacao(to):
    """
    O tempo foi reduzido para 0.5s para evitar o Timeout da Vercel (10 segundos).
    Assim garantimos que o robô nunca morre a meio do processo.
    """
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

        # 1. CONSULTA AO WIX
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        is_veteran = info.get("isVeteran", False)
        servico_atual = info.get("servico", "atendimento")
        p_convenio = info.get("convenio", "")
        p_modalidade = info.get("modalidade", "").lower()

        # --- REINÍCIO MANUAL SEGURO ---
        if msg_recebida in ["Recomeçar", "Menu Inicial", "⬅️ Voltar"]:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
            enviar_texto(phone, "Entendido! Vamos recomeçar o seu atendimento. 😊")
            status = "triagem"

        # --- INTERCEPTA O BOTÃO DE CONTINUAR ---
        elif msg_recebida == "Sim, continuar":
            prompts = {
                "aguardando_nome_novo": "Como gostaria de ser chamado(a)?",
                "escolha_especialidade": "Qual desses serviços você procura hoje? (Ortopedia, Neuro, etc)",
                "escolha_modalidade": "Deseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?",
                "cadastrando_nome_completo": "Por favor, digite o seu NOME COMPLETO (conforme documento):",
                "cadastrando_data": "Qual a sua DATA DE NASCIMENTO? (Ex: 15/05/1980)",
                "cadastrando_email": "Qual o seu melhor E-MAIL para enviarmos os lembretes?",
                "cadastrando_queixa": "O que te trouxe à clínica hoje? (Dor ou queixa principal?)",
                "cadastrando_cpf": "Digite o seu CPF (apenas números).",
                "cadastrando_convenio": "Qual o nome do seu CONVÊNIO?",
                "cadastrando_num_carteirinha": "Digite o NÚMERO DA SUA CARTEIRINHA.",
                "aguardando_carteirinha": "Envie agora uma FOTO da sua CARTEIRINHA.",
                "aguardando_pedido": "Envie agora uma FOTO do seu PEDIDO MÉDICO."
            }
            texto = prompts.get(status, "Por favor, continue de onde paramos.")
            enviar_texto(phone, f"Ótimo! 😊 {texto}")
            return jsonify({"status": "success"}), 200

        # --- DETECÇÃO AVANÇADA DE SAUDAÇÃO (CONTINUIDADE) ---
        is_greeting = False
        if msg_type == "text":
            # Remove pontuação para entender intenção de saudação ("Oi, boa tarde!")
            msg_limpa = re.sub(r'[^\w\s]', '', msg_recebida.lower().strip())
            saudacoes = ["oi", "ola", "olá", "bom dia", "boa tarde", "boa noite", "oii", "oie"]
            
            for s in saudacoes:
                if s in msg_limpa and len(msg_limpa) <= 25:
                    is_greeting = True
                    break

        if is_greeting:
            if status == "finalizado":
                requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
                status = "triagem"
            elif status not in ["triagem", "menu_veterano"]:
                enviar_botoes(phone, 
                    f"Olá! ✨ Notei que estávamos no meio do seu pedido de atendimento. Podemos continuar de onde paramos?",
                    ["Sim, continuar", "Recomeçar"]
                )
                return jsonify({"status": "success"}), 200

        # ==========================================
        # FLUXO EXATO VALIDADO (VETERANO VS NOVO)
        # ==========================================

        if status == "triagem":
            if is_veteran:
                txt = f"Olá, {p_name}! ✨ Que bom ter você de volta conosco na Conectifisio unidade {unit}.\n\nComo posso facilitar seu dia hoje?"
                secoes = [{"title": "Opções", "rows": [
                    {"id": "v1", "title": "🗓️ Reagendar Sessão"},
                    {"id": "v2", "title": "🔄 Continuar Tratamento"},
                    {"id": "v3", "title": "➕ Novo Serviço"},
                    {"id": "v4", "title": "📁 Outras Solicitações"}
                ]}]
                enviar_lista(phone, txt, "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                enviar_texto(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara começarmos seu atendimento, como gostaria de ser chamado(a)?")
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_nome_novo"})

        # --- FLUXO DE VETERANO ---
        elif status == "menu_veterano":
            if "Reagendar" in msg_recebida:
                enviar_texto(phone, "Não encontrei nenhum agendamento recente para o seu perfil. Mas não se preocupe, vou resolver isso para você agora mesmo! 😊")
                enviar_botoes(phone, "Para agilizarmos, qual o melhor período para você?", ["Manhã", "Tarde", "Menu Inicial"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_reagendando_periodo"})
            
            elif "Continuar Tratamento" in msg_recebida:
                enviar_botoes(phone, "As novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular", "Menu Inicial"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_escolha_modalidade"})

            elif "Novo Serviço" in msg_recebida:
                secoes = [{"title": "Serviços", "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"}, 
                    {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s3", "title": "Fisio Pélvica"}, 
                    {"id": "s4", "title": "Pilates Studio"},
                    {"id": "s5", "title": "Recovery"}, 
                    {"id": "s6", "title": "Liberação Miofascial"},
                    {"id": "s0", "title": "⬅️ Voltar"}
                ]}]
                enviar_lista(phone, "Qual desses novos serviços você procura hoje?", "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_especialidade"})

            elif "Outras Solicitações" in msg_recebida:
                enviar_lista(phone, "Como podemos ajudar?", "Ver Solicitações", [{"title": "Solicitações", "rows": [
                    {"id": "o1", "title": "📄 Atestado Pendente"},
                    {"id": "o2", "title": "📝 Relatório Pendente"},
                    {"id": "o3", "title": "👤 Falar com Recepção"},
                    {"id": "o0", "title": "⬅️ Voltar"}
                ]}])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_outros"})

        elif status == "veterano_escolha_modalidade":
            if "Particular" in msg_recebida:
                enviar_botoes(phone, "Excelente! Vamos seguir para o agendamento. Qual período você prefere?", ["Manhã", "Tarde", "Menu Inicial"])
                requests.post(WIX_URL, json={"from": phone, "status": "agendando", "modalidade": "particular"})
            else:
                plano = p_convenio if p_convenio else "registrado"
                enviar_botoes(phone, f"Você continua utilizando o convênio {plano} ou houve alguma mudança no seu plano?", ["✅ Mesmo Convênio", "🔄 Troquei de Plano"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_valida_convenio"})

        elif status == "veterano_valida_convenio":
            if "Mesmo" in msg_recebida:
                enviar_botoes(phone, "Você já está com o novo Pedido Médico em mãos?", ["✅ Sim, já tenho", "❌ Ainda não"])
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})
            elif "Troquei" in msg_recebida:
                enviar_texto(phone, "Entendido! Vamos atualizar seus dados.\n\nQual o nome do seu **NOVO CONVÊNIO**?")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio", "modalidade": "convenio"})

        # --- FLUXO NOVO PACIENTE CONSOLIDADO ---
        elif status == "aguardando_nome_novo":
            nome_informado = msg_recebida.title()
            secoes = [{"title": "Serviços", "rows": [
                {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"},
                {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"}
            ]}]
            enviar_lista(phone, f"Prazer em conhecer, {nome_informado}! 😊 Qual desses serviços você procura hoje?", "Ver Opções", secoes)
            requests.post(WIX_URL, json={"from": phone, "name": nome_informado, "status": "escolha_especialidade"})

        elif status == "escolha_especialidade":
            servico = msg_recebida
            if "Neurológica" in servico:
                explicacao = (
                    "Para agendarmos com o especialista ideal, como está a mobilidade do paciente?\n\n"
                    "🔹 *Independente:* Realiza tarefas sozinho e com segurança.\n\n"
                    "🤝 *Semidependente:* Precisa de ajuda parcial ou dispositivos de apoio.\n\n"
                    "👨‍🦽 *Dependente:* Precisa de auxílio constante para se movimentar."
                )
                enviar_botoes(phone, explicacao, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            elif servico in ["Recovery", "Liberação Miofascial"]:
                enviar_texto(phone, f"Ótima escolha! O serviço de {servico} é focado em bem-estar e performance. ✨")
                enviar_texto(phone, "Para darmos sequência, por favor digite o seu **NOME COMPLETO** (conforme documento):")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": "particular", "servico": servico})
            else:
                enviar_botoes(phone, f"Perfeito! Deseja atendimento de {servico} pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": servico})

        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida:
                enviar_texto(phone, "Devido à complexidade, nosso fisioterapeuta responsável entrará em contato agora para te dar atenção total. 👨⚕️")
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            else:
                enviar_botoes(phone, "Certo! ✅ Deseja atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})

        elif status == "escolha_modalidade":
            mod_limpa = "convenio" if "Convênio" in msg_recebida or "Convénio" in msg_recebida else "particular"
            enviar_texto(phone, "Entendido! Vamos realizar seu cadastro rápido para o agendamento.\n\nPor favor, digite agora o seu **NOME COMPLETO** (conforme documento):")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": mod_limpa})

        elif status == "cadastrando_nome_completo":
            enviar_texto(phone, "Qual a sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "cadastrando_data"})

        elif status == "cadastrando_data":
            enviar_texto(phone, "Qual o seu melhor E-MAIL para enviarmos os lembretes?")
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "cadastrando_email"})

        elif status == "cadastrando_email":
            enviar_texto(phone, "O que te trouxe à clínica hoje? (Sua dor ou queixa principal?)")
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            enviar_texto(phone, "Obrigado por compartilhar! 😊 Agora, digite o seu CPF (apenas números).")
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            if p_modalidade == "convenio":
                enviar_texto(phone, "CPF recebido! Qual o nome do seu CONVÊNIO?")
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "cadastrando_convenio"})
            else:
                enviar_botoes(phone, "Cadastro concluído! Qual período você prefere para o agendamento?", ["Manhã", "Tarde"])
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "cadastrando_convenio":
            enviar_texto(phone, "Anotado! Agora, digite o NÚMERO DA SUA CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_num_carteirinha"})

        elif status == "cadastrando_num_carteirinha":
            enviar_texto(phone, "Envie agora uma FOTO da sua CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "numCarteirinha": msg_recebida, "status": "aguardando_carteirinha"})

        elif status == "aguardando_carteirinha":
            enviar_texto(phone, "Recebido! Agora, por favor, envie uma FOTO do seu PEDIDO MÉDICO.")
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            enviar_botoes(phone, "Documentos recebidos! 🎉 Qual período você prefere para o agendamento?", ["Manhã", "Tarde"])
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
