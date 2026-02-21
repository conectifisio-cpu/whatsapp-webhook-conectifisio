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
# CONFIGURAÇÕES v48.5 - MAPA ESTRATÉGICO FINAL
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- MOTOR DE HUMANIZAÇÃO ---

def simular_digitacao(to):
    """
    Simula o status 'digitando...' no WhatsApp.
    O atraso entre 2.5s e 4.5s gera uma percepção de atendimento humano.
    """
    atraso = random.uniform(2.5, 4.5)
    time.sleep(atraso)

# --- FUNÇÕES DE ENVIO (API META CLOUD) ---

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
    # A API da Meta limita a 3 botões por mensagem interativa de botões
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
# WEBHOOK PRINCIPAL (LÓGICA UNIFICADA)
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
        
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # 1. CONSULTA AO WIX (RECONHECIMENTO BLINDADO PELO CELULAR)
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        is_veteran = info.get("isVeteran", False)
        p_convenio = info.get("convenio", "") 

        # --- LÓGICA DE NAVEGAÇÃO GLOBAL (BOTÃO VOLTAR) ---
        if "Menu Inicial" in msg_recebida or "Voltar" in msg_recebida:
            requests.post(WIX_URL, json={"from": phone, "status": "triagem"})
            enviar_texto(phone, "Voltando ao menu principal... 🔄")
            # Forçamos a reinicialização enviando o status de triagem no próximo loop
            status = "triagem"

        # --- FLUXO v48.5 (VETERANO VS NOVO) ---

        if status == "triagem":
            if is_veteran:
                # VETERANO RECONHECIDO: Saudação Direta
                txt = f"Olá, {p_name}! ✨ Que bom ter você de volta conosco na Conectifisio unidade {unit}.\n\nComo posso facilitar seu dia hoje?"
                secoes = [{"title": "Escolha uma opção", "rows": [
                    {"id": "v1", "title": "🗓️ Reagendar Sessão"},
                    {"id": "v2", "title": "🔄 Continuar Tratamento"},
                    {"id": "v3", "title": "➕ Novo Serviço"},
                    {"id": "v4", "title": "📁 Outras Solicitações"}
                ]}]
                enviar_lista(phone, txt, "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                # NOVO PACIENTE: Acolhimento em 2 etapas
                enviar_texto(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara começarmos seu atendimento, como gostaria de ser chamado(a)?")
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_nome_novo"})

        # --- FLUXO VETERANO (MAPA v1.7) ---
        elif status == "menu_veterano":
            if "Reagendar" in msg_recebida:
                # CENÁRIO DIRETO: Busca sessões agendadas
                # No futuro, aqui faremos a chamada à API do Feegow. Por enquanto, simulamos o Scenario 2 (Suporte).
                enviar_texto(phone, "Não encontrei nenhum agendamento recente para o seu perfil por aqui. Mas não se preocupe, vou resolver isso para você agora mesmo! 😊")
                enviar_botoes(phone, "Para agilizarmos, qual o melhor período para você?", ["Manhã", "Tarde", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_reagendando_periodo"})
            
            elif "Continuar Tratamento" in msg_recebida:
                enviar_botoes(phone, "Ótimo que vai dar continuidade! 🚀 As novas sessões serão pelo seu CONVÊNIO ou de forma PARTICULAR?", ["💳 Convênio", "💎 Particular", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_escolha_modalidade"})

            elif "Novo Serviço" in msg_recebida:
                secoes = [{"title": "Nossos Serviços", "rows": [
                    {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                    {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"},
                    {"id": "s0", "title": "⬅️ Menu Inicial"}
                ]}]
                enviar_lista(phone, "Qual desses novos serviços você procura hoje?", "Ver Opções", secoes)
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_especialidade"})

            elif "Outras Solicitações" in msg_recebida:
                enviar_lista(phone, "Como podemos ajudar hoje?", "Ver Solicitações", [
                    {"title": "Administrativo", "rows": [
                        {"id": "o1", "title": "📄 Atestado Pendente"},
                        {"id": "o2", "title": "📝 Relatório Pendente"},
                        {"id": "o3", "title": "👤 Falar com Recepção"},
                        {"id": "o4", "title": "⬅️ Voltar"}
                    ]}
                ])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_outros"})

        elif status == "veterano_escolha_modalidade":
            if "Particular" in msg_recebida:
                enviar_botoes(phone, "Excelente! Vamos seguir para o agendamento. Qual período você prefere?", ["Manhã", "Tarde", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "agendando", "modalidade": "particular"})
            else:
                # Validação Proativa de Convênio
                plano_atual = p_convenio if p_convenio else "registrado"
                enviar_botoes(phone, f"Você continua utilizando o convênio {plano_atual} ou houve alguma mudança no seu plano de saúde?", ["✅ Mesmo Convênio", "🔄 Troquei de Plano", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "veterano_valida_convenio"})

        elif status == "veterano_valida_convenio":
            if "Mesmo" in msg_recebida:
                enviar_botoes(phone, "Você já está com o novo Pedido Médico em mãos?", ["✅ Sim, já tenho", "❌ Ainda não", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})
            elif "Troquei" in msg_recebida:
                enviar_texto(phone, "Entendido! Vamos atualizar seus dados para o faturamento.\n\nQual o nome do seu novo CONVÊNIO?")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_convenio"})

        # --- FLUXO NOVO PACIENTE ---
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
                texto_neuro = ("Excelente. 😊 Para agendarmos corretamente, como está a mobilidade do paciente?\n\n🔹 *Independente*\n🤝 *Semidependente*\n👨‍🦽 *Dependente*")
                enviar_botoes(phone, texto_neuro, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            elif servico in ["Recovery", "Liberação Miofascial"]:
                # Pula burocracia de convênio para serviços particulares
                enviar_texto(phone, f"Ótima escolha! O serviço de {servico} é focado em bem-estar e performance. ✨\n\nPor favor, digite seu **NOME COMPLETO** (conforme documento):")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": "particular", "servico": servico})
            else:
                enviar_botoes(phone, f"Perfeito! ✅ Deseja atendimento de {servico} pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": servico})

        elif status == "escolha_modalidade":
            mod_limpa = "convenio" if "Convênio" in msg_recebida else "particular"
            enviar_texto(phone, "Entendido! Vamos realizar seu cadastro rápido para o agendamento.\n\nPor favor, digite agora o seu **NOME COMPLETO** (conforme documento):")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": mod_limpa})

        elif status == "cadastrando_nome_completo":
            enviar_texto(phone, "Qual sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "cadastrando_data"})

        elif status == "cadastrando_data":
            enviar_texto(phone, "Qual o seu melhor E-MAIL para enviarmos os lembretes?")
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "cadastrando_email"})

        elif status == "cadastrando_email":
            enviar_texto(phone, "O que te trouxe à clínica hoje? (Sua dor ou queixa principal?)")
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            enviar_texto(phone, "Obrigado! 😊 Agora, digite seu CPF (apenas números).")
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "cadastrando_cpf"})

        elif status == "cadastrando_cpf":
            if p_modalidade == "convenio":
                enviar_texto(phone, "CPF recebido! Qual o nome do seu CONVÊNIO?")
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "cadastrando_convenio"})
            else:
                enviar_botoes(phone, "Cadastro concluído! Qual período você prefere?", ["Manhã", "Tarde", "⬅️ Voltar"])
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "cadastrando_convenio":
            enviar_texto(phone, "Digite o NÚMERO DA SUA CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_num_carteirinha"})

        elif status == "cadastrando_num_carteirinha":
            enviar_texto(phone, "Envie agora uma FOTO da sua CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "numCarteirinha": msg_recebida, "status": "aguardando_carteirinha"})

        elif status == "aguardando_carteirinha":
            enviar_texto(phone, "Recebido! Agora, envie uma FOTO do seu PEDIDO MÉDICO.")
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            enviar_botoes(phone, "Documentos recebidos! 🎉 Qual período você prefere?", ["Manhã", "Tarde", "⬅️ Voltar"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

        elif status == "agendando":
            enviar_texto(phone, "Tudo pronto! 🎉 Nossa equipe já recebeu seus dados e entrará em contato em instantes para confirmar o horário. Até já!")
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
