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
# CONFIGURAÇÕES v47.1 - RECONHECIMENTO PASSIVO
# ==========================================
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
WIX_URL = "https://www.ictusfisioterapia.com.br/_functions/conectifisioWebhook"

# --- MOTOR DE HUMANIZAÇÃO ---

def simular_digitacao(to):
    """
    Simula o status 'digitando...' para criar uma experiência humana.
    O atraso é randômico para não parecer um padrão robótico.
    """
    atraso = random.uniform(2.8, 4.5)
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
    btns = [{"type": "reply", "reply": {"id": f"btn_{i}", "title": b}} for i, b in enumerate(lista_botoes)]
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
# WEBHOOK PRINCIPAL (ESTRITO AO MAPA)
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
        
        # Captura de entrada inteligente
        msg_recebida = ""
        if msg_type == "text":
            msg_recebida = message["text"]["body"].strip()
        elif msg_type == "interactive":
            inter = message["interactive"]
            msg_recebida = inter.get("button_reply", {}).get("title", inter.get("list_reply", {}).get("title", ""))
        elif msg_type == "image":
            msg_recebida = "[ARQUIVO_ENVIADO]"

        unit = "Ipiranga" if "23629360" in value.get("metadata", {}).get("display_phone_number", "") else "SCS"

        # 1. CONSULTA AO WIX (RECONHECIMENTO AUTOMÁTICO PELO CELULAR)
        res_wix = requests.post(WIX_URL, json={"from": phone, "text": msg_recebida, "unit": unit}, timeout=15)
        info = res_wix.json()
        
        status = info.get("currentStatus", "triagem")
        p_name = info.get("patientName", "")
        is_veteran = info.get("isVeteran", False)
        p_modalidade = info.get("modalidade", "").lower()

        # --- LÓGICA DE ATENDIMENTO v47.1 (BRASIL) ---

        # PASSO 1: ACOLHIMENTO (IDENTIFICAÇÃO AUTOMÁTICA)
        if status == "triagem":
            if is_veteran:
                # VETERANO RECONHECIDO: Saudação direta pelo nome salvo no Wix (title)
                enviar_botoes(phone, 
                    f"Olá, {p_name}! ✨ Que bom ter você de volta conosco na Conectifisio unidade {unit}.\n\nComo posso te ajudar hoje?",
                    ["Retomar tratamento", "Novo pacote", "Outro assunto"]
                )
                requests.post(WIX_URL, json={"from": phone, "status": "menu_veterano"})
            else:
                # NOVO PACIENTE: Pergunta o nome para gerar Rapport
                enviar_texto(phone, f"Olá! ✨ Seja muito bem-vindo à Conectifisio unidade {unit}.\n\nPara começarmos seu atendimento, com quem eu falo hoje?")
                requests.post(WIX_URL, json={"from": phone, "status": "aguardando_nome_novo"})

        # PASSO 2: FILTRO DE NOVO PACIENTE (VALOR PRIMEIRO)
        elif status == "aguardando_nome_novo":
            nome_informado = msg_recebida.title()
            secoes = [{"title": "Nossos Serviços", "rows": [
                {"id": "s1", "title": "Fisio Ortopédica"}, {"id": "s2", "title": "Fisio Neurológica"},
                {"id": "s3", "title": "Fisio Pélvica"}, {"id": "s4", "title": "Pilates Studio"},
                {"id": "s5", "title": "Recovery"}, {"id": "s6", "title": "Liberação Miofascial"}
            ]}]
            enviar_lista(phone, f"Prazer em conhecer, {nome_informado}! 😊 Qual desses serviços você procura hoje?", "Ver Opções", secoes)
            requests.post(WIX_URL, json={"from": phone, "name": nome_informado, "status": "escolha_especialidade"})

        elif status == "menu_veterano":
            enviar_botoes(phone, "Entendido! Em qual período você prefere agendar o seu retorno?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando", "servico": msg_recebida})

        # PASSO 3: ESPECIALIDADE E QUALIFICAÇÃO
        elif status == "escolha_especialidade":
            servico = msg_recebida
            if "Neurológica" in servico:
                texto_neuro = ("Excelente escolha. 😊 Para agendarmos corretamente, como está a mobilidade do paciente?\n\n🔹 *Independente*\n🤝 *Semidependente*\n👨‍🦽 *Dependente*")
                enviar_botoes(phone, texto_neuro, ["Independente", "Semidependente", "Dependente"])
                requests.post(WIX_URL, json={"from": phone, "status": "triagem_neuro", "servico": "Neurologia"})
            elif servico in ["Recovery", "Liberação Miofascial"]:
                # Serviços focados em performance (Sempre particulares)
                enviar_texto(phone, f"Ótima escolha! O serviço de {servico} é focado em bem-estar e performance. ✨")
                enviar_texto(phone, "Vamos realizar seu cadastro rápido para o agendamento.\n\nPor favor, digite seu **NOME COMPLETO** (conforme documento):")
                requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": "particular", "servico": servico})
            else:
                enviar_botoes(phone, f"Perfeito! ✅ Você deseja atendimento de {servico} pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade", "servico": servico})

        elif status == "triagem_neuro":
            if "Dependente" in msg_recebida:
                enviar_texto(phone, "Nosso fisioterapeuta responsável assumirá este contato agora para te dar atenção total e orientar os próximos passos. 👨‍⚕️")
                requests.post(WIX_URL, json={"from": phone, "status": "atendimento_humano"})
            else:
                enviar_botoes(phone, "Certo! ✅ Você deseja realizar o atendimento pelo seu CONVÊNIO ou de forma PARTICULAR?", ["Convênio", "Particular"])
                requests.post(WIX_URL, json={"from": phone, "status": "escolha_modalidade"})

        # PASSO 4: CADASTRO (A ESCADA DE COMPROMISSO)
        elif status == "escolha_modalidade":
            mod_limpa = "convenio" if "Convênio" in msg_recebida else "particular"
            enviar_texto(phone, "Entendido! Vamos realizar seu cadastro rápido para o agendamento.\n\nPor favor, digite agora o seu **NOME COMPLETO** (conforme documento):")
            requests.post(WIX_URL, json={"from": phone, "status": "cadastrando_nome_completo", "modalidade": mod_limpa})

        elif status == "cadastrando_nome_completo":
            enviar_texto(phone, "Qual a sua DATA DE NASCIMENTO? (Ex: 15/05/1980)")
            requests.post(WIX_URL, json={"from": phone, "name": msg_recebida.title(), "status": "cadastrando_data"})

        elif status == "cadastrando_data":
            enviar_texto(phone, "Qual o seu melhor E-MAIL para enviarmos os lembretes de sessões?")
            requests.post(WIX_URL, json={"from": phone, "birthDate": msg_recebida, "status": "cadastrando_email"})

        elif status == "cadastrando_email":
            enviar_texto(phone, "O que te trouxe à nossa clínica hoje? (Conte-nos sua dor ou queixa principal)")
            requests.post(WIX_URL, json={"from": phone, "email": msg_recebida, "status": "cadastrando_queixa"})

        elif status == "cadastrando_queixa":
            enviar_texto(phone, "Obrigado por compartilhar! 😊 Agora, digite seu CPF (apenas números) para registro.")
            requests.post(WIX_URL, json={"from": phone, "queixa": msg_recebida, "status": "cadastrando_cpf"})

        # PASSO 5: BUROCRACIA (APENAS SE CONVÊNIO)
        elif status == "cadastrando_cpf":
            if p_modalidade == "convenio":
                enviar_texto(phone, "CPF recebido! Qual o nome do seu CONVÊNIO?")
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "cadastrando_convenio"})
            else:
                enviar_botoes(phone, "Cadastro concluído! Qual período você prefere para o agendamento?", ["Manhã", "Tarde"])
                requests.post(WIX_URL, json={"from": phone, "cpf": msg_recebida, "status": "agendando"})

        elif status == "cadastrando_convenio":
            enviar_texto(phone, "Para agilizarmos sua autorização, digite o NÚMERO DA SUA CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "convenio": msg_recebida, "status": "cadastrando_num_carteirinha"})

        elif status == "cadastrando_num_carteirinha":
            enviar_texto(phone, "Anotado! Agora, por favor, envie uma FOTO da sua CARTEIRINHA.")
            requests.post(WIX_URL, json={"from": phone, "numCarteirinha": msg_recebida, "status": "aguardando_carteirinha"})

        elif status == "aguardando_carteirinha":
            enviar_texto(phone, "Recebido! Agora, envie uma FOTO do seu PEDIDO MÉDICO.")
            requests.post(WIX_URL, json={"from": phone, "status": "aguardando_pedido"})

        elif status == "aguardando_pedido":
            enviar_botoes(phone, "Documentos recebidos! 🎉 Qual período você prefere?", ["Manhã", "Tarde"])
            requests.post(WIX_URL, json={"from": phone, "status": "agendando"})

        elif status == "agendando":
            enviar_texto(phone, "Tudo pronto! 🎉 Nossa equipe já recebeu seus dados e entrará em contato em instantes para confirmar o horário exato. Até já!")
            requests.post(WIX_URL, json={"from": phone, "status": "finalizado"})

        return jsonify({"status": "success"}), 200
    except Exception as e:
        return jsonify({"status": "error"}), 200

@app.route("/api/whatsapp", methods=["GET"])
def verify():
    if request.args.get("hub.verify_token") == "conectifisio_2024_seguro":
        return request.args.get("hub.challenge"), 200
    return "Erro", 403
